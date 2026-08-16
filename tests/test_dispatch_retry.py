"""Retry classification and backoff math (#405).

The rule these tests pin down is deliberately narrow: a job is retried
only when the scheduler killed it for a *resource* reason AND its own
artefacts show it was queueing for a license seat. Everything else — a
hung testbench that hit the same TIMEOUT with no banner, a job that FAILED
on its own merits, a backend with no scheduler state at all — keeps
failing, because a vanished job must never score green.
"""

from __future__ import annotations

import random
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
    monkeypatch.setattr(retry_module, "_CHUNK_BYTES", 64)
    spec = _spec(tmp_path)
    _write_sim_log(spec, "x" * 50 + BANNER + "y" * 200)
    assert classify_missing_result(spec, "TIMEOUT", classifiers=ON) == "license-queue"


def test_scan_stops_at_the_size_cap(tmp_path, monkeypatch):
    """Collection must not read a run's whole output back off a share."""
    monkeypatch.setattr(retry_module, "_CHUNK_BYTES", 64)
    monkeypatch.setattr(retry_module, "_MAX_SCAN_BYTES", 128)
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
