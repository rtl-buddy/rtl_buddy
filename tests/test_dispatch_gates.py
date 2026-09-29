"""Tests for the head-to-build-job gates manifest and its plan-index to job-id map."""

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
    assert payload["entries"] == [
        {"index": 0, "test": "alpha", "job_id": "1234_1", "cluster": "hpc"},
        {"index": 1, "test": "beta", "job_id": "1234_2"},
    ]
    # Written via a temp name so a polling reader never sees a partial file.
    assert [p.name for p in tmp_path.iterdir()] == ["gates-9.json"]


def test_a_missing_manifest_is_a_reason_not_an_exception(tmp_path):
    """Loading never raises: a raise would fail the build job and cancel the sims gated
    on it.
    """
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
    """A manifest that is absent at the first release means the head is still
    submitting.
    """
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
    """A head that died mid-submit costs the build job only the wait."""
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        time.sleep(seconds)

    payload, reason = wait_for_gates(
        tmp_path / "never.json", timeout_s=0.05, interval_s=0.01, sleep=_sleep
    )
    assert payload is None
    assert "not written" in reason
    # The last wait is trimmed to the deadline.
    assert slept and all(nap <= 0.01 for nap in slept)
    assert sum(slept) <= 0.05


def test_a_manifest_from_an_earlier_run_is_rejected_immediately(tmp_path):
    """A manifest from another run (token mismatch) is refused, not waited out."""
    path = write_gates(tmp_path / "gates-9.json", run_token="old", entries=_entries())
    slept = []
    payload, reason = wait_for_gates(
        path, run_token="new", timeout_s=30.0, interval_s=1.0, sleep=slept.append
    )
    assert payload is None
    assert "not this run's" in reason
    assert slept == []


def test_one_plan_index_can_hold_several_jobs(tmp_path):
    """A test fanned out over N run_ids releases N job ids under one key."""
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
    """Jobs of one key can live on two clusters, so batches are grouped per cluster."""
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
    assert release_batches(payload, [2]) == [(None, ["9_1"])]


def test_two_clusters_may_hand_out_the_same_job_id(tmp_path):
    """An id is unique within a cluster, not across a federation."""
    payload, _ = load_gates(
        write_gates(
            tmp_path / "g.json",
            run_token="tok",
            entries=[
                (0, "alpha", "77_1", "east"),
                (0, "alpha", "77_1", "west"),
                (0, "alpha", "77_1", "east"),
            ],
        )
    )
    assert release_batches(payload, [0]) == [("east", ["77_1"]), ("west", ["77_1"])]


def test_a_malformed_entry_does_not_cost_the_others_their_release():
    payload = {
        "entries": [
            {"index": 0, "job_id": "7_1"},
            {"index": "one", "job_id": "7_2"},
            {"job_id": "7_3"},
            "not an entry",
            {"index": 1, "job_id": ""},
            # A non-string cluster is treated as the local one.
            {"index": 1, "job_id": "7_4", "cluster": 17},
        ]
    }
    assert release_batches(payload, [0, 1]) == [(None, ["7_1", "7_4"])]
