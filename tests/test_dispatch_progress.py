"""Dispatch progress reporting (#435).

The bug this covers is a *silence*: a dispatched regression printed
nothing between "submitted" and "drained", so these tests assert on both
channels at once — the structured record (what ``rtl_buddy.log`` and
``--machine`` keep) and the line a default-verbosity console actually
sees, which is the one that was missing.
"""

from __future__ import annotations

import logging

import pytest

from rtl_buddy.dispatch.base import JobHandle, TestJobSpec
from rtl_buddy.dispatch.progress import DispatchProgress, group_job_ids, suite_labels
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import setup_logging


class _Clock:
    """Hand-advanced monotonic clock: no test may depend on wall time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _handle(job_id: str, suite_dir: str = "/proj/verif/tb_a") -> JobHandle:
    spec = TestJobSpec(
        test_name=job_id,
        suite_dir=suite_dir,
        test_config_path=f"{suite_dir}/tests.yaml",
        result_json=f"{suite_dir}/artefacts/{job_id}/result.json",
    )
    return JobHandle(job_id=job_id, spec=spec)


@pytest.fixture
def console(capsys, caplog):
    """Real console handlers plus record capture.

    ``setup_logging`` clears the root handlers, which would take caplog's
    with them — so it is re-attached afterwards. Both channels matter here:
    a record with no console line is exactly the defect.
    """

    def _setup(**kwargs):
        setup_logging(color=False, **kwargs)
        logging.getLogger().addHandler(caplog.handler)
        caplog.set_level(logging.DEBUG)
        return capsys

    return _setup


def _records(caplog, event: str) -> list[dict]:
    return [
        record.__dict__["rtl_fields"]
        for record in caplog.records
        if record.__dict__.get("rtl_event") == event
    ]


def _stderr(capsys) -> str:
    return " ".join(capsys.readouterr().err.split())


# ---- id grouping and suite labels ---------------------------------------


def test_group_job_ids_collapses_array_elements():
    assert group_job_ids(["1235_1", "1235_2", "1235_3", "1236"]) == [
        "1235_[1-3]",
        "1236",
    ]
    # Gaps stay visible: the point is a form squeue/sacct takes back.
    assert group_job_ids(["9_1", "9_3", "9_4"]) == ["9_[1,3-4]"]
    assert group_job_ids(["42"]) == ["42"]
    # A backend with another id shape still round-trips.
    assert group_job_ids(["lp-1", "lp-2"]) == ["lp-1", "lp-2"]


def test_suite_labels_shorten_but_stay_distinguishable():
    assert suite_labels(["/proj/verif/tb_a"]) == {"/proj/verif/tb_a": "tb_a"}
    labels = suite_labels(["/proj/verif/tb_a", "/proj/verif/tb_b"])
    assert labels == {
        "/proj/verif/tb_a": "tb_a",
        "/proj/verif/tb_b": "tb_b",
    }
    # A deeper split keeps enough path to tell two `verif` dirs apart.
    labels = suite_labels(["/proj/blk_a/verif", "/proj/blk_b/verif"])
    assert set(labels.values()) == {"blk_a/verif", "blk_b/verif"}


# ---- when the reporter speaks -------------------------------------------


def test_first_observation_reaches_the_console_immediately(console, caplog):
    """Entering the wait is itself news: the console says N/N straight away."""
    capsys = console()
    clock = _Clock()
    handles = [_handle(f"9_{i}") for i in (1, 2, 3)]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )

    progress.observe(["9_1", "9_2", "9_3"])

    (fields,) = _records(caplog, "dispatch.progress")
    assert (fields["remaining"], fields["total"]) == (3, 3)
    assert fields["heartbeat"] is False
    err = _stderr(capsys)
    assert err.count("3/3 jobs remaining") == 1


def test_change_is_recorded_unchanged_is_not_and_the_console_is_throttled(
    console, caplog
):
    capsys = console()
    clock = _Clock()
    handles = [_handle(f"9_{i}") for i in (1, 2, 3)]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )

    progress.observe(["9_1", "9_2", "9_3"])  # first: emits
    clock.advance(5)
    progress.observe(["9_1", "9_2", "9_3"])  # unchanged, too soon: silent
    clock.advance(5)
    progress.observe(["9_1", "9_2"])  # changed: recorded, console throttled

    counts = [f["remaining"] for f in _records(caplog, "dispatch.progress")]
    assert counts == [3, 2]
    # One console line in the first interval, however often the count moved.
    assert _stderr(capsys).count("jobs remaining") == 1


def test_heartbeat_proves_liveness_when_nothing_moves(console, caplog):
    """A count that has not moved for an hour is the case that looked hung."""
    capsys = console()
    clock = _Clock()
    handles = [_handle("9_1")]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )

    progress.observe(["9_1"])
    clock.advance(30)
    progress.observe(["9_1"])
    clock.advance(31)  # past the interval since the last console line
    progress.observe(["9_1"])

    records = _records(caplog, "dispatch.progress")
    assert [f["heartbeat"] for f in records] == [False, True]
    assert _stderr(capsys).count("1/1 jobs remaining") == 2


def test_interval_zero_keeps_the_terminal_quiet_but_logs_everything(console, caplog):
    """The developer's opt-out silences the console, never the log file."""
    capsys = console()
    clock = _Clock()
    handles = [_handle(f"9_{i}") for i in (1, 2)]
    progress = DispatchProgress(
        handles, backend="slurm", interval=0.0, max_wait=None, clock=clock
    )

    progress.observe(["9_1", "9_2"])
    clock.advance(600)
    progress.observe(["9_1"])
    progress.finish()

    assert [f["remaining"] for f in _records(caplog, "dispatch.progress")] == [2, 1]
    assert _records(caplog, "dispatch.suite_drained")  # still logged...
    assert _stderr(capsys) == ""  # ...and still not printed


def test_progress_line_splits_running_from_pending_and_names_the_longest(
    console, caplog
):
    capsys = console()
    clock = _Clock()
    handles = [_handle(f"9_{i}") for i in (1, 2, 3)]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )
    clock.advance(754)

    progress.observe(
        ["9_1", "9_2", "9_3"],
        states={"9_1": "running", "9_2": "pending", "9_3": "pending"},
        longest=("rb:demo_alu", 482.0),
    )

    err = _stderr(capsys)
    assert "3/3 jobs remaining (1 running, 2 pending)" in err
    assert "12m34s elapsed" in err
    assert "longest running rb:demo_alu 8m02s" in err


# ---- suite completion ----------------------------------------------------


def test_suites_are_reported_as_they_drain_in_order(console, caplog):
    capsys = console()
    clock = _Clock()
    handles = [
        _handle("9_1", "/proj/verif/tb_a"),
        _handle("9_2", "/proj/verif/tb_a"),
        _handle("10_1", "/proj/verif/tb_b"),
    ]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )

    progress.observe(["9_1", "9_2", "10_1"])
    clock.advance(100)
    progress.observe(["10_1"])  # tb_a is done
    clock.advance(100)
    progress.observe(["10_1"])
    progress.finish()  # tb_b's last job left with the queue

    drained = _records(caplog, "dispatch.suite_drained")
    assert [f["suite"] for f in drained] == ["tb_a", "tb_b"]
    assert [f["jobs"] for f in drained] == [2, 1]
    err = _stderr(capsys)
    # "finished", never "passed": no result has been collected yet.
    assert "tb_a — all 2 jobs finished" in err
    assert "passed" not in err


def test_a_drained_suite_is_reported_once(console, caplog):
    console()
    clock = _Clock()
    handles = [_handle("9_1", "/proj/verif/tb_a")]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=None, clock=clock
    )
    progress.observe([])
    progress.observe([])
    progress.finish()
    assert len(_records(caplog, "dispatch.suite_drained")) == 1


# ---- the deadline --------------------------------------------------------


def test_max_wait_fails_with_the_outstanding_ids_and_warns(console, caplog):
    console()
    clock = _Clock()
    handles = [_handle(f"9_{i}") for i in (1, 2, 3)] + [_handle("11")]
    progress = DispatchProgress(
        handles, backend="slurm", interval=60.0, max_wait=120.0, clock=clock
    )

    progress.observe(["9_1", "9_2", "9_3", "11"])
    clock.advance(121)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        progress.observe(["9_1", "9_2", "9_3", "11"])

    message = str(excinfo.value)
    assert "9_[1-3]" in message and "11" in message
    assert "cancelling the fleet" in message.lower()
    (fields,) = _records(caplog, "dispatch.max_wait_exceeded")
    assert fields["jobs"] == ["9_[1-3]", "11"]
    (record,) = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.max_wait_exceeded"
    ]
    assert record.levelno == logging.WARNING


def test_no_deadline_means_the_wait_is_unbounded(console, caplog):
    """`max-wait` unset must keep today's behaviour exactly."""
    console()
    clock = _Clock()
    progress = DispatchProgress(
        [_handle("9_1")], backend="slurm", interval=60.0, max_wait=None, clock=clock
    )
    progress.observe(["9_1"])
    clock.advance(10_000)
    progress.observe(["9_1"])  # no raise


def test_an_empty_queue_past_the_deadline_is_not_a_failure(console, caplog):
    """Everything finished: a late-arriving poll must not fail a done run."""
    console()
    clock = _Clock()
    progress = DispatchProgress(
        [_handle("9_1")], backend="slurm", interval=60.0, max_wait=1.0, clock=clock
    )
    clock.advance(500)
    progress.observe([])
    progress.finish()


def test_none_handles_do_not_break_the_reporter(console, caplog):
    """A caller that lets a None through (#361) must not lose its liveness."""
    console()
    progress = DispatchProgress(
        [None, _handle("9_1")],
        backend="slurm",
        interval=60.0,
        max_wait=None,
        clock=_Clock(),
    )
    progress.observe(["9_1"])
    (fields,) = _records(caplog, "dispatch.progress")
    assert fields["total"] == 1
