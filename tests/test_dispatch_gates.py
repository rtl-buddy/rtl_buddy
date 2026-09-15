"""The head→build-job gates manifest (#548).

The manifest is the only thing that lets the build job turn "plan index 3
is built" into "clear job 1234_2's dependency". It is written by the head
after its fan-out and read on a compute node, so the contract under test
here is the file itself: what it holds, what a reader does with one that
is late, stale or damaged, and how a plan index maps to job ids.
"""

from __future__ import annotations

import json
import threading
import time

from rtl_buddy.dispatch.gates import (
    GATES_SCHEMA_VERSION,
    load_gates,
    release_batches,
    wait_for_gates,
    write_gates,
)


def _entries():
    return [(0, "alpha", "1234_1", None), (1, "beta", "1234_2", None)]


def test_manifest_round_trips_through_the_file(tmp_path):
    path = write_gates(
        tmp_path / "gates-9.json",
        run_token="tok",
        entries=[(0, "alpha", "1234_1", "hpc"), (1, "beta", "1234_2", None)],
    )
    payload, reason = load_gates(path)
    assert reason is None
    assert payload["schema_version"] == GATES_SCHEMA_VERSION
    assert payload["run_token"] == "tok"
    # Cluster per entry, and omitted where it is the local one (#548 review).
    assert payload["entries"] == [
        {"index": 0, "test": "alpha", "job_id": "1234_1", "cluster": "hpc"},
        {"index": 1, "test": "beta", "job_id": "1234_2"},
    ]
    # Written through a temp name, so a polling build job never sees a
    # partial manifest — and the temp name is gone afterwards.
    assert [p.name for p in tmp_path.iterdir()] == ["gates-9.json"]


def test_a_missing_manifest_is_a_reason_not_an_exception(tmp_path):
    """Nothing here may raise: an exception inside the build job exits it
    non-zero, and Slurm cancels every afterok sim job behind that."""
    payload, reason = load_gates(tmp_path / "absent.json")
    assert payload is None
    assert "not written" in reason


def test_a_damaged_or_foreign_manifest_is_declined(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_gates(bad)[0] is None

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": 99, "entries": []}))
    payload, reason = load_gates(wrong)
    assert payload is None
    assert "schema_version" in reason

    shapeless = tmp_path / "shapeless.json"
    shapeless.write_text(json.dumps({"schema_version": GATES_SCHEMA_VERSION}))
    assert load_gates(shapeless)[0] is None


def test_wait_returns_a_manifest_that_arrives_late(tmp_path):
    """The build job is submitted BEFORE the sims it gates, so "absent" at
    its first release means "the head is still submitting"."""
    path = tmp_path / "gates-9.json"

    def _write_later():
        time.sleep(0.15)
        write_gates(path, run_token="tok", entries=_entries())

    writer = threading.Thread(target=_write_later)
    writer.start()
    try:
        payload, reason = wait_for_gates(
            path, run_token="tok", timeout_s=5.0, interval_s=0.02
        )
    finally:
        writer.join()
    assert reason is None
    assert payload["run_token"] == "tok"


def test_wait_gives_up_bounded_when_the_head_never_writes(tmp_path):
    """A head that died between submits costs the build job this wait and
    nothing else — never the compile, never the job's exit status."""
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        time.sleep(seconds)

    payload, reason = wait_for_gates(
        tmp_path / "never.json", timeout_s=0.05, interval_s=0.01, sleep=_sleep
    )
    assert payload is None
    assert "not written" in reason
    # Polled at the interval, and the last wait is trimmed to the deadline
    # rather than overshooting it.
    assert slept and all(nap <= 0.01 for nap in slept)
    assert sum(slept) <= 0.05


def test_a_manifest_from_an_earlier_run_is_rejected_immediately(tmp_path):
    """The path is keyed on the head pid, which the OS reuses. Releasing a
    stale manifest's ids would clear the dependency of somebody else's
    jobs, so a token mismatch is refused rather than waited out."""
    path = write_gates(tmp_path / "gates-9.json", run_token="old", entries=_entries())
    slept = []
    payload, reason = wait_for_gates(
        path, run_token="new", timeout_s=30.0, interval_s=1.0, sleep=slept.append
    )
    assert payload is None
    assert "not this run's" in reason
    assert slept == []


def test_one_plan_index_can_hold_several_jobs(tmp_path):
    """A test fanned out over N run_ids is one config in the plan and N
    rows in the fan-out, so its key releases N job ids."""
    payload, _ = load_gates(
        write_gates(
            tmp_path / "g.json",
            run_token="tok",
            entries=[
                (0, "alpha", "7_1", None),
                (0, "alpha", "7_2", None),
                (1, "beta", "7_3", None),
            ],
        )
    )
    assert release_batches(payload, [0]) == [(None, ["7_1", "7_2"])]
    assert release_batches(payload, [1, 0]) == [(None, ["7_3", "7_1", "7_2"])]
    assert release_batches(payload, [4]) == []


def test_ids_are_batched_by_the_cluster_that_issued_them(tmp_path):
    """`--clusters=a,b` places each array wherever it can start first, so
    one key's jobs can live on two controllers — and a job id means
    nothing against the wrong one (#509)."""
    payload, _ = load_gates(
        write_gates(
            tmp_path / "g.json",
            run_token="tok",
            entries=[
                (0, "alpha", "7_1", "east"),
                (0, "alpha", "8_1", "west"),
                (1, "beta", "7_2", "east"),
                (2, "gamma", "9_1", None),
            ],
        )
    )
    assert release_batches(payload, [0, 1]) == [
        ("east", ["7_1", "7_2"]),
        ("west", ["8_1"]),
    ]
    # An absent cluster is the local one and batches on its own.
    assert release_batches(payload, [2]) == [(None, ["9_1"])]


def test_a_malformed_entry_does_not_cost_the_others_their_release():
    payload = {
        "entries": [
            {"index": 0, "job_id": "7_1"},
            {"index": "one", "job_id": "7_2"},
            {"job_id": "7_3"},
            "not an entry",
            {"index": 1, "job_id": ""},
            # A cluster that is not a name is no cluster: released against
            # the local controller rather than guessed at.
            {"index": 1, "job_id": "7_4", "cluster": 17},
        ]
    }
    assert release_batches(payload, [0, 1]) == [(None, ["7_1", "7_4"])]
