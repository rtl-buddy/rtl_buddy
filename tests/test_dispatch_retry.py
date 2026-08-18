"""Retry classification and backoff math (#405).

The rule these tests pin down is deliberately narrow: a job is retried
only when the scheduler killed it for a *resource* reason AND its own
fresh output shows it was *still* queueing for a license seat when it
died. Everything else — a hung testbench that hit the same TIMEOUT with no
banner, one that queued, got its seat and then hung, a job that FAILED on
its own merits, one whose build job never opened the gate, a days-old log
left behind by a previous run — keeps failing, because a vanished job must
never score green.
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import pytest

from rtl_buddy.config.dispatch import DispatchConfigFile, RetryConfigFile
from rtl_buddy.dispatch import retry as retry_module
from rtl_buddy.dispatch.base import TestJobSpec
from rtl_buddy.dispatch.retry import (
    backoff_delay,
    classify_missing_result,
    job_output_paths,
    normalise_scheduler_state,
)

BANNER = "Queuing for License... (Licensed number of users already reached)\n"
ON = ["license-queue"]


def _spec(tmp_path: Path, *, run_id=None) -> TestJobSpec:
    dispatch_dir = tmp_path / "artefacts" / "basic" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    return TestJobSpec(
        test_name="basic",
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
        result_json=dispatch_dir / "result-single.json",
        run_id=run_id,
        log_path=dispatch_dir / "slurm-single.log",
    )


def _write_sim_log(spec: TestJobSpec, text: str, *, name="test.log") -> Path:
    artefacts = Path(spec.suite_dir) / "artefacts" / spec.test_name
    if spec.run_id is not None:
        artefacts = artefacts / f"run-{spec.run_id:04d}"
    artefacts.mkdir(parents=True, exist_ok=True)
    path = artefacts / name
    path.write_text(text)
    return path


# ---- what counts as a retryable missing result ---------------------------


def test_timeout_with_the_queue_banner_is_license_queue(tmp_path):
    spec = _spec(tmp_path)
    _write_sim_log(spec, "starting sim\n" + BANNER)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_timeout_without_the_banner_is_not_retried(tmp_path):
    """The hung-testbench case: same kill, no seat contention behind it."""
    spec = _spec(tmp_path)
    _write_sim_log(spec, "starting sim\nrunning...\n")
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None


def test_no_artefacts_at_all_is_not_retried(tmp_path):
    assert classify_missing_result(_spec(tmp_path), "TIMEOUT", classifiers=ON) is None


@pytest.mark.parametrize("state", ["TIMEOUT", "NODE_FAIL", "PREEMPTED"])
def test_every_resource_kill_state_qualifies(tmp_path, state):
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert classify_missing_result(spec, state, classifiers=ON) == "license-queue"


@pytest.mark.parametrize(
    "state", ["FAILED", "COMPLETED", "CANCELLED", "OUT_OF_MEMORY", "", None]
)
def test_states_that_are_the_jobs_own_outcome_are_not_retried(tmp_path, state):
    """Banner or not, a job that decided its own fate is not resubmitted."""
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert classify_missing_result(spec, state, classifiers=ON) is None


def test_cancelled_by_user_is_not_read_as_a_resource_kill(tmp_path):
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert classify_missing_result(spec, "CANCELLED by 4711", classifiers=ON) is None


def test_truncated_state_spelling_still_matches(tmp_path):
    # sacct renders some states with a trailing '+' when the column is cut.
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert classify_missing_result(spec, "timeout+", classifiers=ON) == "license-queue"


def test_classifier_not_enabled_means_no_retry(tmp_path):
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=[]) is None
    assert classify_missing_result(spec, "TIMEOUT", classifiers=None) is None


def test_banner_in_test_err_counts(tmp_path):
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER, name="test.err")
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_banner_in_the_scheduler_log_counts(tmp_path):
    """A sim whose output never reached its artefact files still queued."""
    spec = _spec(tmp_path)
    Path(spec.log_path).write_text(BANNER)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_run_fanout_reads_that_runs_own_artefacts(tmp_path):
    """Seed 2's banner must not make seed 1 look like it queued."""
    queued, hung = _spec(tmp_path, run_id=2), _spec(tmp_path, run_id=1)
    _write_sim_log(queued, BANNER)
    _write_sim_log(hung, "running...\n")
    assert classify_missing_result(queued, "TIMEOUT", classifiers=ON) == "license-queue"
    assert classify_missing_result(hung, "TIMEOUT", classifiers=ON) is None


def test_an_unscheduled_backend_classifies_on_the_banner_alone(tmp_path):
    """local-parallel reports no scheduler state, so requiring one is fatal.

    The pool has no accounting source at all (``collect_telemetry`` is
    empty by design), so demanding a resource kill state there would make
    the rule unsatisfiable and retry dead code on that backend.
    """
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert (
        classify_missing_result(spec, None, classifiers=ON, scheduled=False)
        == "license-queue"
    )
    # ...and the same job under a scheduler that reported nothing is not
    # retried, because there the missing state is missing evidence.
    assert classify_missing_result(spec, None, classifiers=ON, scheduled=True) is None


def test_an_unscheduled_backend_still_needs_the_banner(tmp_path):
    spec = _spec(tmp_path)
    _write_sim_log(spec, "sim started\nrunning...\n")
    assert classify_missing_result(spec, None, classifiers=ON, scheduled=False) is None


# ---- queued when it died, or queued and then running? --------------------


def test_a_sim_that_got_its_seat_and_then_hung_is_not_retried(tmp_path):
    """The common shape, not a corner: most queued sims do get a seat.

    Banner, then real simulator output, then the reservation runs out. The
    seat was granted; whatever went wrong after that is the test's own, and
    a whole-file search for the banner would resubmit it.
    """
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER + "..\nVCS Simulation Report\nrunning...\n")
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None
    assert classify_missing_result(spec, None, classifiers=ON, scheduled=False) is None


def test_the_banner_as_the_last_meaningful_content_is_retried(tmp_path):
    """Everything after the last marker is queue-banner vocabulary."""
    spec = _spec(tmp_path)
    _write_sim_log(
        spec,
        "Chronologic VCS simulator copyright 1991-2024\n"
        + BANNER
        + "..........\n"
        + "HIT CTRL-C to exit\n"
        + BANNER
        + "\n"
        + "....",  # killed mid-poll: no trailing newline
    )
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_output_printed_before_the_banner_is_not_a_granted_seat(tmp_path):
    """simv's startup lines precede the queue wait; they are not sim output."""
    spec = _spec(tmp_path)
    _write_sim_log(spec, "starting sim\nloading design\n" + BANNER)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_a_capture_showing_the_seat_was_granted_outranks_one_that_does_not(tmp_path):
    """The banner and what followed it can land in different files."""
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER + "VCS Simulation Report\n")
    _write_sim_log(spec, BANNER, name="test.err")
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None


def test_a_partial_last_line_does_not_end_the_queue(tmp_path):
    """A killed job's last line is truncated; only a complete line decides.

    Same rule as the live monitor, which enters the queued state on a
    partial marker line and leaves it only on a complete non-banner one.
    """
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER.rstrip("\n"))  # banner with no newline yet
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_sim_output_after_a_long_queue_wait_still_ends_the_queue(tmp_path, monkeypatch):
    """The dots can outlast several read chunks; the line after them decides."""
    monkeypatch.setattr(retry_module, "_CHUNK_CHARS", 64)
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER + "." * 300 + "\nVCS Simulation Report\n")
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None


# ---- evidence has to be this attempt's ------------------------------------


def test_a_stale_artefact_is_not_this_attempts_evidence(tmp_path):
    """`artefacts/<test>/test.log` is never cleaned between runs.

    Without a recency check a banner printed days ago would satisfy the
    rule forever — including for a job that never started at all.
    """
    spec = _spec(tmp_path)
    log = _write_sim_log(spec, BANNER)
    submitted_at = time.time()
    os.utime(log, (submitted_at - 2 * 86400, submitted_at - 2 * 86400))
    assert (
        classify_missing_result(
            spec, "TIMEOUT", classifiers=ON, submitted_at=submitted_at
        )
        is None
    )
    assert (
        classify_missing_result(
            spec, None, classifiers=ON, scheduled=False, submitted_at=submitted_at
        )
        is None
    )


def test_an_artefact_written_after_submission_is_evidence(tmp_path):
    spec = _spec(tmp_path)
    submitted_at = time.time()
    _write_sim_log(spec, BANNER)
    assert (
        classify_missing_result(
            spec, "TIMEOUT", classifiers=ON, submitted_at=submitted_at
        )
        == "license-queue"
    )


def test_a_build_job_that_did_not_succeed_blocks_every_retry(tmp_path):
    """A sim gated on a failed build never started, so it is not retryable.

    Its artefacts cannot be this attempt's evidence, and resubmitting it
    would run — with no gate at all — a job the head deliberately skipped.
    """
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)
    assert (
        classify_missing_result(spec, "TIMEOUT", classifiers=ON, build_succeeded=False)
        is None
    )
    assert (
        classify_missing_result(
            spec, None, classifiers=ON, scheduled=False, build_succeeded=False
        )
        is None
    )


def test_paths_searched_include_both_job_logs(tmp_path):
    spec = _spec(tmp_path)
    names = [p.name for p in job_output_paths(spec)]
    # The sim's own capture first, then the job's rtl_buddy log and the
    # scheduler's stdout log beside the envelope (#437).
    assert names == [
        "test.log",
        "test.err",
        "rtl_buddy-single.log",
        "slurm-single.log",
    ]


def test_marker_straddling_a_chunk_boundary_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(retry_module, "_CHUNK_CHARS", 64)
    spec = _spec(tmp_path)
    _write_sim_log(spec, "x" * 50 + BANNER + "y" * 200)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_scan_stops_at_the_size_cap(tmp_path, monkeypatch):
    """Collection must not read a run's whole output back off a share."""
    monkeypatch.setattr(retry_module, "_CHUNK_CHARS", 64)
    monkeypatch.setattr(retry_module, "_MAX_SCAN_CHARS", 128)
    spec = _spec(tmp_path)
    _write_sim_log(spec, "z" * 4096 + BANNER)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None


def test_unreadable_artefact_is_not_evidence(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    _write_sim_log(spec, BANNER)

    def _boom(*args, **kwargs):
        raise OSError("stale NFS file handle")

    monkeypatch.setattr("builtins.open", _boom)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("TIMEOUT", "TIMEOUT"),
        ("timeout", "TIMEOUT"),
        ("TIMEOUT+", "TIMEOUT"),
        ("CANCELLED by 42", "CANCELLED"),
        (None, ""),
        ("", ""),
    ],
)
def test_state_normalisation(raw, expected):
    assert normalise_scheduler_state(raw) == expected


# ---- the backoff schedule -----------------------------------------------


def _retry(**kwargs):
    # Floats, not ints: the serde dataclass is type-checked at construction
    # (YAML coerces on the way in, a direct constructor call does not).
    kwargs.setdefault("attempts", 3)
    for key in ("backoff_sec", "backoff_max_sec", "jitter"):
        if key in kwargs:
            kwargs[key] = float(kwargs[key])
    return DispatchConfigFile(retry=RetryConfigFile(**kwargs)).initialise().retry


def test_delay_doubles_per_attempt_and_is_capped():
    cfg = _retry(backoff_sec=60, backoff_max_sec=600, jitter=0.0)
    assert [backoff_delay(n, cfg) for n in range(1, 6)] == [60, 120, 240, 480, 600]


def test_jitter_spreads_the_delay_around_the_base():
    cfg = _retry(backoff_sec=100, backoff_max_sec=1000, jitter=0.5)

    class _Rng:
        def __init__(self, value):
            self.value = value
            self.seen = None

        def uniform(self, low, high):
            self.seen = (low, high)
            return self.value

    low_rng, high_rng = _Rng(0.5), _Rng(1.5)
    assert backoff_delay(1, cfg, rng=low_rng) == 50
    assert backoff_delay(1, cfg, rng=high_rng) == 150
    # The window is exactly 1 +/- jitter, so the mean stays the base delay.
    assert low_rng.seen == (0.5, 1.5)


def test_jitter_keeps_every_sample_inside_its_window():
    cfg = _retry(backoff_sec=60, backoff_max_sec=600, jitter=0.25)
    rng = random.Random(20250405)  # seeded: a flaky bound is worse than none
    for attempt in range(1, 5):
        base = min(600, 60 * 2 ** (attempt - 1))
        for _ in range(200):
            delay = backoff_delay(attempt, cfg, rng=rng)
            assert 0.75 * base <= delay <= 1.25 * base


def test_jitter_is_applied_after_the_cap_so_capped_retries_still_spread():
    cfg = _retry(backoff_sec=60, backoff_max_sec=100, jitter=0.5)
    rng = random.Random(7)
    samples = {backoff_delay(9, cfg, rng=rng) for _ in range(50)}
    assert len(samples) > 1  # not every capped retry lands on the same second
    assert all(50 <= s <= 150 for s in samples)


def test_zero_jitter_is_deterministic_and_touches_no_rng():
    cfg = _retry(backoff_sec=30, backoff_max_sec=300, jitter=0.0)

    class _Explode:
        def uniform(self, low, high):  # pragma: no cover - must not be called
            raise AssertionError("jitter 0 must not consult the rng")

    assert backoff_delay(2, cfg, rng=_Explode()) == 60


def test_result_missing_says_retrying_when_a_classifier_fired():
    """Human mode must not call a row a failure while it is being retried.

    The event carries ``attempt`` and ``retry_classifier``, but only
    ``--machine`` renders fields; ``rtl_buddy.log`` and the console read
    the human message, and three byte-identical "counting it as a
    failure" lines would contradict the ``dispatch.retry`` line that
    follows each of the first two (#405 review).
    """
    from rtl_buddy.logging_utils import _human_message

    retried = _human_message(
        "dispatch.result_missing",
        {
            "job_id": "6553_2",
            "test": "seqr_add",
            "scheduler_state": "TIMEOUT",
            "attempt": 1,
            "retry_classifier": "license-queue",
        },
    )
    assert "license-queue" in retried
    assert "retrying" in retried
    assert "counting it as a failure" not in retried

    exhausted = _human_message(
        "dispatch.result_missing",
        {
            "job_id": "6553_2",
            "test": "seqr_add",
            "scheduler_state": "TIMEOUT",
            "attempt": 3,
            "retry_classifier": None,
        },
    )
    assert "attempt 3" in exhausted
    assert "counting it as a failure" in exhausted

    # No retry configured: the message is exactly what it always was.
    plain = _human_message(
        "dispatch.result_missing",
        {"job_id": "1", "test": "t", "scheduler_state": None, "attempt": 1},
    )
    assert plain == "Dispatch job 1 for t produced no result — counting it as a failure"
