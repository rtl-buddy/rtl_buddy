"""Staged `rb pnr` runs dispatched to a scheduler: one chained job per stage.

The head renders every stage script into the checkpoint run directory, then submits one job per stage, each held until the previous one succeeds (`afterok`), and blocks until the chain has left the queue. Each job runs `rb _pnr-stage-job`, which runs its stage exactly as a local staged run does (`pnr_openroad.run_stage`): the stage's sessions, its log section appended to `pnr.log`, and its `stage_begin` and `stage_end` events with the resources measured inside the job. The head then reads the run directory as it does after a local chain.

Files under the run directory's `dispatch/`:

    jobs.json                     # each stage's job, written as the scheduler accepts it, and whether all have left the queue
    given_up                      # rb gave the run up; written before it cancels the jobs and clears the run's outputs
    NN_<stage>.sbatch             # the scheduler's answer to the stage's submission
    NN_<stage>.sbatch.tmp         # the answer while the scheduler has not given it
    NN_<stage>.sbatch.cancel      # the head gave the submission up
    NN_<stage>.slurm.log          # the job's scheduler output
    NN_<stage>.rtl_buddy.log      # the job's rtl_buddy log
    NN_<stage>.result.json        # how the stage ended, written by the job
    NN_<stage>.tcl                # the stage script as the job ran it, when the job set another thread count
"""

import contextvars
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

from ..config.dispatch import time_to_seconds
from ..config.openroad_threads import detect_allocation, plan_threads
from ..dispatch.base import (
    CANCEL_TIMEOUT_S,
    SUBMIT_TIMEOUT_S,
    JobHandle,
    SubmissionAbandoned,
    telemetry_key,
)
from ..dispatch.retry import normalise_scheduler_state
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_console_event, log_event
from ..process_utils import (
    STOP_SIGNALS,
    signals_blocked,
    terminate_live_managed_processes,
)
from . import pnr_checkpoints

logger = logging.getLogger(__name__)

DISPATCH_DIRNAME = "dispatch"
JOBS_NAME = "jobs.json"
GIVEN_UP_NAME = "given_up"

#: Allocation source of the thread plan the head writes into a dispatched stage's script: the CPUs it asks the job for. The job sets the count again from the CPUs it receives (`job_scripts`).
JOB_CPUS_SOURCE = "requested cpus"

#: `fail_stage` of a stage the scheduler ended, or that never ran; an `xfail:` never excuses it.
DISPATCH_FAIL_STAGE = "dispatch"

#: End states of a job that ended itself, so its failed stage's result is the verdict. In any other state (`TIMEOUT`, `OUT_OF_MEMORY`, `CANCELLED`, `NODE_FAIL`, `PREEMPTED`, ...) the scheduler ended the job, as for a lost `rb test` job.
_SELF_ENDED_STATES = ("COMPLETED", "FAILED")

#: Accounting can trail a job's exit; a failed stage's state is asked for this many times while it has none or still reads as queued or running.
_STATE_ATTEMPTS = 4
_STATE_RETRY_S = 2.0
_UNSETTLED_STATES = ("", "PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED")

#: How much of a failed session's stderr a job result keeps.
_STDERR_KEEP = 8000

_BACKEND_LABELS = {"slurm": "Slurm"}


def failure_desc(error) -> str:
    """The description of a submission or wait failure, prefixed `dispatch:` once."""
    text = str(error)
    return text if text.startswith("dispatch: ") else f"dispatch: {text}"


def jobs_dir(run_dir: str) -> str:
    return os.path.join(run_dir, DISPATCH_DIRNAME)


def stage_name(stage: str) -> str:
    return f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}"


def see_job_writes(run_dir: str, log_path: str) -> None:
    """Open the directories stage jobs write, so this host sees the files jobs on other hosts created there.

    On NFS, a name this host looked up while it was missing, such as an output the run cleared, stays missing here for up to the mount's `acdirmin` after another host creates it. Opening its directory revalidates it.
    """
    for path in (
        run_dir,
        jobs_dir(run_dir),
        os.path.dirname(os.path.abspath(log_path)),
    ):
        try:
            os.listdir(path)
        except OSError:
            pass


def job_file(run_dir: str, stage: str, suffix: str) -> str:
    """`dispatch/NN_<stage>.<suffix>` of a run directory."""
    return os.path.join(jobs_dir(run_dir), f"{stage_name(stage)}.{suffix}")


def mark_given_up(run_dir: str, *, run: str | None = None) -> None:
    """Record in `dispatch/given_up` that rb gave the run up. The head writes it before it cancels the jobs and clears the run's outputs.

    A job then refuses to start its stage (`_refusal`), and a job whose stage was already running removes the run's outputs once the stage ends (`run_stage_job`). The head marks before it clears and the job checks after its stage has ended, so either the job sees the mark or the head clears after the stage wrote its outputs.
    """
    path = os.path.join(jobs_dir(run_dir), GIVEN_UP_NAME)
    try:
        Path(path).touch()
    except OSError as e:
        log_event(
            logger,
            logging.WARNING,
            "pnr.given_up_unrecorded",
            pnr=run,
            path=path,
            error=str(e),
        )


def given_up(run_dir: str) -> bool:
    """Whether rb gave the run in `run_dir` up (`mark_given_up`)."""
    return os.path.exists(os.path.join(jobs_dir(run_dir), GIVEN_UP_NAME))


class ChainCancelled(FatalRtlBuddyError):
    """The chain was cancelled, or `rb pnr` was interrupted, before the chain finished."""


class QueueWaitExceeded(FatalRtlBuddyError):
    """A stage job waited in the queue longer than `cfg-dispatch.max-wait` without starting."""


class NoSchedulerClient(FatalRtlBuddyError):
    """This host has no client for the scheduler an earlier run's stage jobs were submitted to."""


_LIVE: set["StageChain"] = set()
#: Re-entrant, like every lock the interrupt handler takes: the handler runs in the main thread, which may hold it.
_LIVE_LOCK = threading.RLock()

#: Set once an interrupt reached `cancel_on_interrupt`; no stage chain opens or submits after it. Only `_interrupt` sets it, with further signals deferred, since an Event's lock is not re-entrant.
_INTERRUPTED = threading.Event()
#: The signal that set `_INTERRUPTED`, and whether another signal followed it (`hurry`).
_INTERRUPT = {"signum": None, "repeated": False}

#: Main-thread state of `signals_deferred`: how deeply the running code defers signals, and the first signal that arrived meanwhile.
_DEFERRAL = {"depth": 0, "pending": None}

_HANDLED_SIGNALS = STOP_SIGNALS

#: After rb cancels a chain, how long the head waits for its jobs to leave the queue, and how often it looks.
_DRAIN_WAIT_S = 30.0
_DRAIN_POLL_S = 2.0

#: The longest a queue poll of the chain's wait may take; one that takes longer counts as the queue not answering.
_WAIT_POLL_TIMEOUT_S = 30.0


#: The clock the chain's queue deadlines and progress lines read.
_clock = time.monotonic


def _pause(wake: threading.Event | None, seconds: float) -> None:
    """Wait `seconds` between queue polls, or in a worker thread until `wake` is set.

    The main thread sleeps instead, since the interrupt handler that sets `wake` runs there and an Event's lock is not re-entrant; a signal ends its sleep anyway.
    """
    if wake is None or threading.current_thread() is threading.main_thread():
        time.sleep(seconds)
    else:
        wake.wait(seconds)


def interrupted() -> bool:
    """Whether an interrupt has reached `cancel_on_interrupt` in this process."""
    return _INTERRUPTED.is_set()


def _reset_interrupted() -> None:
    """Forget an interrupt. Tests only."""
    _INTERRUPTED.clear()
    _INTERRUPT.update(signum=None, repeated=False)
    _DEFERRAL.update(depth=0, pending=None)


def _signal_name(signum) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _interrupted_desc() -> str:
    signum = _INTERRUPT["signum"]
    return f"interrupted by {_signal_name(signum)}" if signum else "interrupted"


class StageChain:
    """One run's stage jobs, submitted in order, each held until the previous one succeeds.

    Used as a context manager: while open, an interrupt (`cancel_on_interrupt`) cancels it, and leaving it with an exception cancels every job it submitted. It cancels only its own jobs. After an interrupt no chain opens and none submits.
    """

    def __init__(self, backend, *, run: str | None = None):
        self.backend = backend
        #: The P&R run the chain belongs to, named in console lines.
        self.run = run
        self.handles: list[JobHandle] = []
        #: When each job was submitted (monotonic), by `telemetry_key`.
        self._submitted_at: dict[str, float] = {}
        #: Jobs seen to have left the queue, by `telemetry_key`.
        self._left: set[str] = set()
        self._cancel_requested = False
        #: Jobs the scheduler took a cancel for, by `telemetry_key`; a later cancel retries the others.
        self._cancelled: set[str] = set()
        #: The first exception the chain was left with, which settling the run reports (`settle_cancelled_chain`).
        self.failure: BaseException | None = None
        #: Set by a cancel, so a wait in another thread notices it at once.
        self._wake = threading.Event()
        self._lock = threading.RLock()

    def __enter__(self) -> "StageChain":
        with _LIVE_LOCK:
            if _INTERRUPTED.is_set():
                raise ChainCancelled(_interrupted_desc())
            _LIVE.add(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                if self.failure is None:
                    self.failure = exc
                self.cancel()
        finally:
            with _LIVE_LOCK:
                _LIVE.discard(self)
        return False

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested or _INTERRUPTED.is_set()

    def _cancelled_desc(self) -> str:
        return (
            _interrupted_desc()
            if _INTERRUPTED.is_set()
            else "the P&R stage chain was cancelled"
        )

    def submit(self, spec) -> JobHandle:
        """Submit `spec` behind the last job of the chain and return its handle.

        A cancel or an interrupt gives a submission still waiting on the scheduler up at once (`ChainCancelled`); the job it submits is cancelled. An exception between the scheduler's answer and the chain recording the job cancels the job that answer names.
        """
        with self._lock:
            if self.cancel_requested:
                raise ChainCancelled(self._cancelled_desc())
            dependency = self.handles[-1].job_id if self.handles else None
        try:
            handle = self.backend.submit_pnr_stage(
                spec,
                dependency=dependency,
                abandon=lambda: self.cancel_requested,
            )
            with self._lock:
                self.handles.append(handle)
                self._submitted_at[telemetry_key(handle)] = _clock()
                cancelled = self.cancel_requested
        except SubmissionAbandoned:
            raise ChainCancelled(self._cancelled_desc()) from None
        except BaseException:
            self._cancel_answered(spec)
            raise
        log_console_event(
            logger,
            logging.INFO,
            "pnr.stage_job_submitted",
            backend=self.backend.name,
            pnr=self.run,
            stage=stage_name(spec.stage),
            job_id=handle.job_id,
            cluster=handle.cluster,
            dependency=dependency,
        )
        if cancelled:
            self.cancel()
            raise ChainCancelled(self._cancelled_desc())
        return handle

    def _cancel_answered(self, spec) -> None:
        """Add the job a submission's recorded answer names to the chain and cancel it, when an exception left the chain without its handle."""
        path = getattr(spec, "submission_path", None)
        if path is None:
            return
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            return
        job_id, _, cluster = text.strip().partition(";")
        with self._lock:
            if not job_id.strip() or job_id.strip() in {h.job_id for h in self.handles}:
                return
            self.handles.append(
                JobHandle(
                    job_id=job_id.strip(), spec=spec, cluster=cluster.strip() or None
                )
            )
        self.cancel()

    def wait(self) -> None:
        """Block until every job of the chain has left the queue.

        `cfg-dispatch.max-wait` bounds how long each job waits in the queue before it starts: the first job from its submission, a later one from when the job before it left the queue. A job that waits longer raises `QueueWaitExceeded`. How long a job runs is bounded by its own `time`, so a job is not given up while it may be running: past `max-wait` with the queue not answering, accounting says whether the job started, and without its answer either the job is given up only once it is past `max-wait` plus its `time`. Each poll gets at most `_WAIT_POLL_TIMEOUT_S`; one that runs out counts as the queue not answering. A cancel or an interrupt raises `ChainCancelled`.
        """
        max_wait = getattr(self.backend, "max_wait", None)
        interval = getattr(self.backend, "poll_interval", 5.0)
        progress = _ChainProgress(
            self, interval=getattr(self.backend, "progress_interval", 60.0)
        )
        started: set[str] = set()
        queued_since: dict[str, float] = {}
        current = None
        #: When the queue last answered, or the wait began.
        answered = _clock()
        while True:
            if self.cancel_requested:
                raise ChainCancelled(self._cancelled_desc())
            states = self.backend.queue_states(
                self.handles, timeout_s=_WAIT_POLL_TIMEOUT_S
            )
            now = _clock()
            if states is not None:
                answered = now
                current = self._note_queue(states)
                if current is None:
                    progress.drained(now)
                    return
                key = telemetry_key(current)
                if states[key] == "running":
                    started.add(key)
                progress.observe(current, states[key], now)
            elif current is None and self.handles:
                # The queue has not answered yet: the first job counts as waiting.
                current = self.handles[0]
            if current is not None and telemetry_key(current) not in started:
                key = telemetry_key(current)
                since = queued_since.setdefault(
                    key,
                    self._submitted_at.get(key, now)
                    if current is self.handles[0]
                    else now,
                )
                if max_wait is not None and now - since > max_wait:
                    # When the queue did not answer, accounting says whether the job started.
                    state = None if states is not None else self._accounted(current)
                    if state in (None, "PENDING"):
                        raise QueueWaitExceeded(
                            self._waited_desc(current, now - since, max_wait)
                        )
                    if state:
                        started.add(key)
                    elif self._ended_by_now(current, now - since, max_wait):
                        raise QueueWaitExceeded(
                            self._unseen_desc(current, now - answered, max_wait)
                        )
            _pause(self._wake, interval)

    def _waited_desc(self, handle: JobHandle, waited: float, max_wait: float) -> str:
        return (
            f"stage {stage_name(handle.spec.stage)}: "
            f"{_label(self.backend)} job {handle.job_id} waited "
            f"{waited:.0f}s in the queue without starting, longer "
            f"than cfg-dispatch.max-wait ({max_wait:g}s); "
            f"`squeue -j {handle.job_id}` gives the reason it is held"
        )

    def _accounted(self, handle: JobHandle) -> str:
        """The job's state as accounting records it, or "" without one."""
        record = (
            self.backend.collect_telemetry([handle]).get(telemetry_key(handle)) or {}
        )
        return normalise_scheduler_state(record.get("state"))

    @staticmethod
    def _ended_by_now(handle: JobHandle, waited: float, max_wait: float) -> bool:
        """Whether a job queued `waited` seconds ago has ended even if it started just before `max-wait` ran out: it is past `max-wait` plus its own `time`."""
        limit = time_to_seconds(
            getattr(getattr(handle.spec, "resources", None), "time", None)
        )
        return limit is not None and waited > max_wait + limit

    def _unseen_desc(
        self, handle: JobHandle, unanswered: float, max_wait: float
    ) -> str:
        return (
            f"stage {stage_name(handle.spec.stage)}: {_label(self.backend)} job "
            f"{handle.job_id} could not be seen to start; the queue has not answered "
            f"for {unanswered:.0f}s, accounting has no state for the job, and it "
            f"has been queued longer than cfg-dispatch.max-wait ({max_wait:g}s) "
            f"plus its time ({handle.spec.resources.time})"
        )

    def _note_queue(self, states: dict, handles=None) -> JobHandle | None:
        """Record which of `handles` (default: the chain's jobs), polled for `states`, have left the queue; return the first job of the chain still in it."""
        polled = self.handles if handles is None else handles
        with self._lock:
            self._left.update(
                telemetry_key(h) for h in polled if telemetry_key(h) not in states
            )
        return next((h for h in self.handles if telemetry_key(h) in states), None)

    def drain(self, timeout: float = _DRAIN_WAIT_S) -> list[JobHandle]:
        """After a cancel, wait up to `timeout` seconds for the chain's jobs to leave the queue, less when a repeated signal asks rb to hurry (`hurry`); return those still in it."""
        deadline = _clock() + timeout
        while True:
            remaining = deadline - _clock()
            states = self.backend.queue_states(
                self.handles, timeout_s=max(1.0, min(CANCEL_TIMEOUT_S, remaining))
            )
            if states is not None:
                self._note_queue(states)
            live = [h for h in self.handles if telemetry_key(h) not in self._left]
            remaining = deadline - _clock()
            if not live or remaining <= 0 or hurry():
                return live
            _pause(None, min(_DRAIN_POLL_S, remaining))

    def cancel(self, *, poll: bool = True) -> bool:
        """Cancel the chain's jobs still in the queue; return whether the scheduler took the request for every one.

        Each job counts as cancelled only once the scheduler took its cancel, so a later call, such as a repeated signal's, retries a refused one and asks nothing about the rest. With `poll` the queue is read first, so jobs that have left it are not asked about; when it cannot be read, or without `poll` (the interrupt handler, which must not wait on two commands), every job not yet seen to leave is cancelled. Signals are deferred until the call ends.
        """
        with signals_deferred():
            with self._lock:
                self._cancel_requested = True
                self._wake.set()
                handles = [
                    h
                    for h in self.handles
                    if telemetry_key(h) not in self._left
                    and telemetry_key(h) not in self._cancelled
                ]
            if handles and poll:
                states = self.backend.queue_states(handles, timeout_s=CANCEL_TIMEOUT_S)
                if states is not None:
                    self._note_queue(states, handles)
                    handles = [h for h in handles if telemetry_key(h) in states]
            ok = (
                not handles
                or self.backend.cancel_all(handles, timeout_s=CANCEL_TIMEOUT_S)
                is not False
            )
            if ok:
                with self._lock:
                    self._cancelled.update(telemetry_key(h) for h in handles)
            return ok

    def state(self, handle: JobHandle) -> dict:
        """The scheduler's accounting record of a job that has left the queue (see `states`)."""
        return self.states([handle]).get(telemetry_key(handle)) or {}

    def states(self, handles: list[JobHandle]) -> dict[str, dict]:
        """The scheduler's accounting records of jobs that have left the queue, by `telemetry_key`, asked again while accounting has no end state for one of them yet."""
        records: dict = {}
        for attempt in range(_STATE_ATTEMPTS):
            records = self.backend.collect_telemetry(list(handles))
            settled = all(
                _end_state(records, handle) not in _UNSETTLED_STATES
                for handle in handles
            )
            if settled or attempt == _STATE_ATTEMPTS - 1 or hurry():
                break
            time.sleep(_STATE_RETRY_S)
        return records


def _end_state(records: dict, handle: JobHandle) -> str:
    """A job's state in accounting `records`, or "" without one."""
    return normalise_scheduler_state(
        (records.get(telemetry_key(handle)) or {}).get("state")
    )


def _label(backend) -> str:
    return _BACKEND_LABELS.get(backend.name, backend.name)


class _ChainProgress:
    """A chain's console lines while it drains: one when the job it waits on, or that job's state, changes; a heartbeat every `progress-interval` seconds (0 keeps them in the log only); one when the chain has left the queue."""

    def __init__(self, chain: StageChain, *, interval: float):
        self.chain = chain
        self.interval = max(0.0, float(interval or 0.0))
        self.start = _clock()
        self.last: tuple | None = None
        self.last_line: float | None = None

    def _emit(self, event: str, **fields) -> None:
        emit = log_console_event if self.interval > 0 else log_event
        emit(logger, logging.INFO, event, pnr=self.chain.run, **fields)

    def observe(self, handle: JobHandle, state: str, now: float) -> None:
        seen = (telemetry_key(handle), state)
        due = self.last_line is not None and now - self.last_line >= self.interval > 0
        if seen == self.last and not due:
            return
        self._emit(
            "pnr.stage_job_waiting",
            backend=self.chain.backend.name,
            stage=stage_name(handle.spec.stage),
            job_id=handle.job_id,
            state=state,
            stages_left=len(self.chain.handles) - self.chain.handles.index(handle),
            elapsed_s=round(now - self.start, 1),
        )
        self.last, self.last_line = seen, now

    def drained(self, now: float) -> None:
        self._emit(
            "pnr.stage_jobs_drained",
            jobs=len(self.chain.handles),
            elapsed_s=round(now - self.start, 1),
        )


def _cancel_live_chains() -> None:
    with _LIVE_LOCK:
        chains = list(_LIVE)
    for chain in chains:
        chain.cancel(poll=False)


@contextmanager
def signals_deferred(*, block: bool = True):
    """In the main thread, handle an interrupt only once the block ends; elsewhere, nothing.

    With `block` the signals are also blocked, which a child process started in the block inherits, so the terminal's Ctrl-C does not kill it; one that still reaches the handler (delivered to another thread) is recorded. Without it the handler records the signal at once, which `signal_pending` reports, so a long wait in the block can end early.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _DEFERRAL["depth"] += 1
    try:
        if block:
            with signals_blocked(_HANDLED_SIGNALS):
                yield
        else:
            yield
    finally:
        _DEFERRAL["depth"] -= 1
        pending = _DEFERRAL["pending"] if _DEFERRAL["depth"] == 0 else None
        if pending is not None:
            _DEFERRAL["pending"] = None
            _interrupt(pending)


def signal_pending() -> bool:
    """Whether a signal arrived in the main thread during `signals_deferred` and waits for the block to end."""
    return (
        threading.current_thread() is threading.main_thread()
        and _DEFERRAL["pending"] is not None
    )


def hurry() -> bool:
    """Whether a wait for the scheduler should end now: a signal followed an interrupt, in any thread, or a signal waits in this (main) thread's `signals_deferred`."""
    return _INTERRUPT["repeated"] or signal_pending()


def _interrupt(signum: int) -> None:
    """Handle an interrupt: no chain opens or submits from now on, every open chain is cancelled, live tool processes are stopped, and the signal's exception is raised.

    Further signals are deferred meanwhile; they only make waits `hurry`, since this one is raised anyway.
    """
    _DEFERRAL["depth"] += 1
    try:
        if _INTERRUPT["signum"] is None:
            _INTERRUPT["signum"] = signum
        _INTERRUPTED.set()
        with signals_blocked(_HANDLED_SIGNALS):
            _cancel_live_chains()
            terminate_live_managed_processes()
    finally:
        _DEFERRAL["depth"] -= 1
        if _DEFERRAL["depth"] == 0:
            _DEFERRAL["pending"] = None
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def _handler(signum, _frame) -> None:
    if _INTERRUPT["signum"] is not None:
        _INTERRUPT["repeated"] = True
    if _DEFERRAL["depth"]:
        if _DEFERRAL["pending"] is None:
            _DEFERRAL["pending"] = signum
        return
    _interrupt(signum)


@contextmanager
def cancel_on_interrupt():
    """While open, SIGINT, SIGTERM and SIGHUP cancel every open stage chain of this process and stop its live tool processes, then raise `KeyboardInterrupt` or `SystemExit(128 + signal)`.

    Install it in the main thread around runs that dispatch; chains in worker threads (`rb pnr -j`) are cancelled from here and open no new chain, which lets those threads finish. A signal repeated while the scheduler is asked to cancel waits for the request to end. A signal the process ignores stays ignored.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    for signum in _HANDLED_SIGNALS:
        if signal.getsignal(signum) is signal.SIG_IGN:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def settle_cancelled_chain(
    run_dir: str, chain: StageChain, *, log_path: str, error: BaseException
) -> str:
    """Finish a chain rb gave up on (an interrupt, `max-wait`, a failed submission or wait) and return the run's failure description.

    The run is first marked given up (`mark_given_up`), so the caller may clear its outputs once this returns. The chain's remaining jobs are cancelled (again, if the scheduler refused before), the head waits a bounded time for them to leave the queue, and a stage a cancelled job was running gets its `stage_end` from accounting and its log from `pnr.log`. Jobs still queued after the wait are named with the command that cancels them, and `jobs.json` is marked drained only when none is, so the next `rb pnr` looks them up. The description is that of the first exception the chain was left with (`StageChain.failure`), else `error`'s.
    """
    mark_given_up(run_dir, run=chain.run)
    first = chain.failure if chain.failure is not None else error
    if isinstance(first, KeyboardInterrupt | SystemExit):
        text = _interrupted_desc()
    elif isinstance(first, FatalRtlBuddyError):
        text = str(first)
    else:
        text = f"{type(first).__name__}: {first}"
    cancelled = chain.cancel()
    live = chain.drain() if chain.handles else []
    see_job_writes(run_dir, log_path)
    recorded = _recorded_jobs(run_dir) or {}
    unrecorded = set(recorded) - {h.job_id for h in chain.handles}
    # A submission given up before sbatch answered may still have its job accepted.
    unanswered = _unresolved_submissions(run_dir, time.time())
    try:
        record_jobs(
            run_dir, chain, drained=not live and not unrecorded and not unanswered
        )
    except OSError:
        pass
    if live:
        log_console_event(
            logger,
            logging.WARNING,
            "pnr.stage_jobs_survived_cancel",
            backend=chain.backend.name,
            pnr=chain.run,
            run_dir=run_dir,
            job_ids=[h.job_id for h in live],
            cancelled=cancelled,
            command=_scancel_command(live),
        )
    killed = _close_cancelled_stage(run_dir, chain, log_path=log_path, live=live)
    if killed is not None:
        stage, handle = killed
        text = (
            f"{text}; stage {stage_name(stage)}'s {_label(chain.backend)} job "
            f"{handle.job_id} was cancelled during the stage"
        )
    return failure_desc(text)


def _scancel_command(handles) -> str:
    by_cluster: dict = {}
    for handle in handles:
        by_cluster.setdefault(handle.cluster, []).append(handle.job_id)
    return "; ".join(
        "scancel " + (f"-M {cluster} " if cluster else "") + " ".join(ids)
        for cluster, ids in by_cluster.items()
    )


def _close_cancelled_stage(
    run_dir: str, chain: StageChain, *, log_path: str, live: list
) -> tuple[str, JobHandle] | None:
    """Close the stage a job of a cancelled chain was running when it ended (see `_close_killed_stage`); return that stage and job, or None."""
    events = pnr_checkpoints.read_progress(run_dir)
    begun = {e.get("stage"): e for e in events if e.get("event") == "stage_begin"}
    ended = {e.get("stage") for e in events if e.get("event") == "stage_end"}
    for handle in chain.handles:
        stage = handle.spec.stage
        if (
            handle in live
            or stage not in begun
            or stage in ended
            or read_result(run_dir, stage) is not None
        ):
            continue
        record = chain.state(handle)
        state = normalise_scheduler_state(record.get("state"))
        _close_killed_stage(
            run_dir,
            stage,
            begun[stage],
            record,
            log_path=log_path,
            slurm_job=handle.job_id,
            slurm_state=state or None,
        )
        return stage, handle
    return None


def record_jobs(run_dir: str, chain: StageChain, *, drained: bool = False) -> None:
    """Write `dispatch/jobs.json`: the job of each stage submitted so far, and whether every one has left the queue.

    A later `rb pnr` of the same configuration reads it (`live_orphans`) unless `drained` is set.
    """
    document = {
        "backend": chain.backend.name,
        "drained": drained,
        "jobs": [
            {
                "stage": h.spec.stage,
                "job_id": h.job_id,
                "cluster": h.cluster,
                "cpus": h.spec.resources.cpus,
                "mem": h.spec.resources.mem,
                "time": h.spec.resources.time,
                "dependency": chain.handles[i - 1].job_id if i else None,
            }
            for i, h in enumerate(chain.handles)
        ],
    }
    path = os.path.join(jobs_dir(run_dir), JOBS_NAME)
    Path(path + ".tmp").write_text(json.dumps(document, indent=2) + "\n")
    os.replace(path + ".tmp", path)


def _recorded_jobs(run_dir: str) -> dict | None:
    """The jobs a run directory's `dispatch/` records and still counts as queued, by job id, as `(cluster, stage)`; None once `jobs.json` records them drained.

    Besides `jobs.json`, each `NN_<stage>.sbatch` answer counts, so a job accepted after its head died is found too, and so does one the submission guard was stopped before it filed (`NN_<stage>.sbatch.tmp`).
    """
    try:
        document = json.loads(Path(jobs_dir(run_dir), JOBS_NAME).read_text())
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    if document.get("drained"):
        return None
    jobs: dict = {}
    for job in document.get("jobs") or []:
        if isinstance(job, dict) and job.get("job_id"):
            jobs[str(job["job_id"])] = (job.get("cluster"), job.get("stage"))
    answers = Path(jobs_dir(run_dir))
    for answer in sorted([*answers.glob("*.sbatch"), *answers.glob("*.sbatch.tmp")]):
        accepted = _answered_job(answer)
        if accepted is not None:
            job_id, cluster = accepted
            jobs.setdefault(
                job_id, (cluster, answer.name.split(".")[0].partition("_")[2])
            )
    return jobs


def _answered_job(answer: Path) -> tuple[str, str | None] | None:
    """The job id and cluster a submission's answer file names, or None."""
    try:
        lines = answer.read_text(errors="replace").strip().splitlines()
    except OSError:
        return None
    job_id, _, cluster = (lines[0] if lines else "").partition(";")
    if not job_id.strip().isdigit():
        return None
    return job_id.strip(), cluster.strip() or None


#: How long a stage job submission can go without an answer before a later `rb pnr` takes it to have failed: the submission's own limit (`SUBMIT_TIMEOUT_S`), plus a margin for stopping `sbatch` and for the clocks of other hosts.
_UNRESOLVED_AFTER_S = SUBMIT_TIMEOUT_S + 60.0


def _unresolved_submissions(run_dir: str, now: float) -> list[tuple[str, float]]:
    """The stage job submissions of a run directory with neither an answer nor a job id yet, as `(file, seconds since it last changed)`.

    A submission is unresolved while its `NN_<stage>.sbatch.tmp` (`sbatch` still running) or `NN_<stage>.sbatch.cancel` (the head gave it up) has no `NN_<stage>.sbatch` beside it, and the `.tmp` names no job. The file named is the `.tmp` when there is one.
    """
    directory = Path(jobs_dir(run_dir))
    by_answer: dict[Path, list[Path]] = {}
    for path in [*directory.glob("*.sbatch.tmp"), *directory.glob("*.sbatch.cancel")]:
        by_answer.setdefault(path.with_suffix(""), []).append(path)
    unresolved = []
    for answer, paths in sorted(by_answer.items()):
        tmp = answer.with_name(f"{answer.name}.tmp")
        if answer.exists() or _answered_job(tmp) is not None:
            continue
        try:
            changed = max(p.stat().st_mtime for p in paths)
        except OSError:
            continue
        unresolved.append((str(tmp if tmp in paths else paths[0]), now - changed))
    return unresolved


def _mark_drained(run_dir: str) -> None:
    path = Path(jobs_dir(run_dir), JOBS_NAME)
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    document["drained"] = True
    try:
        Path(f"{path}.tmp").write_text(json.dumps(document, indent=2) + "\n")
        os.replace(f"{path}.tmp", path)
    except OSError:
        pass


@dataclass
class Orphans:
    """Stage jobs of earlier dispatched runs still in the queue, or that could not be looked up."""

    run_dirs: list[str]
    job_ids: list[str]
    command: str
    #: Set when the queue could not be read, so the jobs may or may not be queued.
    error: str | None = None
    #: Set when this host has no client for the scheduler.
    no_client: bool = False
    #: Submissions of the runs that have no answer yet (`_unresolved_submissions`), so a job they submit is not known.
    unresolved: list[str] = field(default_factory=list)

    def desc(self) -> str:
        runs = ", ".join(os.path.basename(d) for d in self.run_dirs)
        ids = " ".join(self.job_ids)
        dirs = " ".join(jobs_dir(d) for d in self.run_dirs)
        if self.unresolved:
            return failure_desc(
                f"stage job submission(s) {' '.join(self.unresolved)} of earlier "
                f"dispatched run(s) {runs} have no answer from sbatch yet, and a job "
                "sbatch accepts would write into this run's outputs; run rb pnr "
                "again once sbatch has answered (a submission without an answer for "
                f"{_UNRESOLVED_AFTER_S:g}s is taken to have failed), or once the "
                f"submission is known to have died remove {dirs} to run without it"
            )
        if self.no_client:
            return failure_desc(
                f"stage job(s) {ids} of earlier dispatched run(s) {runs} may still "
                f"be queued or running, and {self.error}; run rb pnr where squeue "
                f"answers, or once they have ended remove {dirs} to run without them"
            )
        if self.error is not None:
            return failure_desc(
                f"cannot tell whether stage job(s) {ids} of earlier dispatched "
                f"run(s) {runs} are still queued: {self.error}; run rb pnr again "
                f"once squeue answers or cancel them with `{self.command}`; once "
                f"they have ended, removing {dirs} lets rb pnr run without them"
            )
        return failure_desc(
            f"stage job(s) {ids} of earlier dispatched run(s) {runs} are still "
            "queued or running and would write into this run's outputs; wait for "
            f"them to end or cancel them with `{self.command}`"
        )


def live_orphans(artefact_dir: str, backend_factory) -> Orphans | None:
    """The stage jobs of earlier dispatched runs in `artefact_dir`'s checkpoints still in the queue, or None.

    Each run directory whose `dispatch/jobs.json` is not marked drained is looked up, among this user's jobs; one whose jobs have all left the queue is marked drained, so it is not looked up again. A submission of such a run that `sbatch` has not answered (`_unresolved_submissions`) counts as a queued job until it is older than `_UNRESOLVED_AFTER_S`; after that it is taken to have failed, with a warning (`pnr.stage_submission_presumed_failed`). `backend_factory()` returns the backend to ask; it is called only when there is something to look up. A backend it cannot make, or a queue that cannot be read, leaves the jobs counted as queued. When it raises `NoSchedulerClient`, a run directory whose jobs could have run their whole chain since it last changed is passed with a warning (`_unchecked`).
    """
    found: dict[str, dict] = {}
    pending: dict[str, list[tuple[str, float]]] = {}
    lapsed: dict[str, list[tuple[str, float]]] = {}
    now = time.time()
    for jobs in sorted(
        Path(artefact_dir, pnr_checkpoints.CHECKPOINTS_DIRNAME).glob(
            f"*/{DISPATCH_DIRNAME}"
        )
    ):
        if jobs.parent.is_symlink():
            continue
        run_dir = str(jobs.parent)
        # Before the answers are read, so a submission answered in between is found by them.
        unresolved = _unresolved_submissions(run_dir, now)
        recorded = _recorded_jobs(run_dir)
        if recorded is None:
            continue
        for path, age in unresolved:
            group = pending if age < _UNRESOLVED_AFTER_S else lapsed
            group.setdefault(run_dir, []).append((path, age))
        if recorded:
            found[run_dir] = recorded
    if pending:
        return _unanswered(artefact_dir, pending)
    for run_dir, submissions in lapsed.items():
        for path, age in submissions:
            log_event(
                logger,
                logging.WARNING,
                "pnr.stage_submission_presumed_failed",
                run_dir=run_dir,
                file=path,
                age_s=round(age),
                after_s=_UNRESOLVED_AFTER_S,
            )
        if run_dir not in found:
            _mark_drained(run_dir)
    if not found:
        return None
    handles = {
        run_dir: [
            JobHandle(job_id=job_id, spec=_OrphanSpec(artefact_dir), cluster=cluster)
            for job_id, (cluster, _stage) in recorded.items()
        ]
        for run_dir, recorded in found.items()
    }
    every = [h for run in handles.values() for h in run]
    states, error = None, None
    try:
        backend = backend_factory()
    except NoSchedulerClient as e:
        return _unchecked(artefact_dir, found, handles, str(e))
    except (FatalRtlBuddyError, OSError, NotImplementedError) as e:
        error = str(e) or type(e).__name__
    else:
        try:
            states = backend.queue_states(
                every, timeout_s=_ORPHAN_POLL_TIMEOUT_S, mine=True
            )
        except (FatalRtlBuddyError, OSError, NotImplementedError) as e:
            error = str(e) or type(e).__name__
        if states is None and error is None:
            error = getattr(backend, "last_poll_error", None) or "squeue did not answer"
    runs, queued = [], []
    for run_dir, run_handles in handles.items():
        live = [h for h in run_handles if states is None or telemetry_key(h) in states]
        if live:
            runs.append(run_dir)
            queued += live
        else:
            _mark_drained(run_dir)
    if not runs:
        return None
    return _orphans(artefact_dir, runs, queued, error=error)


def _unanswered(artefact_dir: str, pending: dict) -> Orphans:
    """The refusal for submissions of earlier runs that have no answer yet, by run directory."""
    runs = list(pending)
    files = [path for submissions in pending.values() for path, _age in submissions]
    log_event(
        logger,
        logging.ERROR,
        "pnr.stage_submission_unanswered",
        artefact_dir=artefact_dir,
        run_dirs=runs,
        files=files,
        after_s=_UNRESOLVED_AFTER_S,
        clear=[jobs_dir(d) for d in runs],
    )
    return Orphans(run_dirs=runs, job_ids=[], command="", unresolved=files)


def _orphans(artefact_dir, runs, queued, *, error=None, no_client=False) -> Orphans:
    orphans = Orphans(
        run_dirs=runs,
        job_ids=[h.job_id for h in queued],
        command=_scancel_command(queued),
        error=error,
        no_client=no_client,
    )
    log_event(
        logger,
        logging.ERROR,
        "pnr.stage_jobs_orphaned",
        artefact_dir=artefact_dir,
        run_dirs=runs,
        job_ids=orphans.job_ids,
        command=orphans.command,
        error=error,
        no_client=no_client,
        clear=[jobs_dir(d) for d in runs],
    )
    return orphans


def _unchecked(
    artefact_dir: str, found: dict, handles: dict, reason: str
) -> Orphans | None:
    """Earlier dispatched runs whose jobs this host cannot look up (`reason`): those whose `dispatch/` changed within the `time` their jobs asked for in all, as `Orphans`, or None.

    The jobs of a run whose record is older than that could have run their whole chain since, so it is passed with a warning (`pnr.stage_jobs_unchecked`) and left as it is, for a host with a scheduler client to look up.
    """
    now = time.time()
    runs, queued = [], []
    for run_dir, run_handles in handles.items():
        quiet = _quiet_s(run_dir, now)
        chain = _chain_time_s(run_dir, found[run_dir])
        if quiet is None or chain is None or quiet < chain:
            runs.append(run_dir)
            queued += run_handles
            continue
        log_console_event(
            logger,
            logging.WARNING,
            "pnr.stage_jobs_unchecked",
            run_dir=run_dir,
            job_ids=[h.job_id for h in run_handles],
            reason=reason,
            quiet_s=round(quiet),
            chain_s=chain,
            clear=jobs_dir(run_dir),
        )
    if not runs:
        return None
    return _orphans(artefact_dir, runs, queued, error=reason, no_client=True)


def _quiet_s(run_dir: str, now: float) -> float | None:
    """How long ago anything in the run directory's `dispatch/` last changed, or None when it cannot be read."""
    try:
        times = [p.stat().st_mtime for p in Path(jobs_dir(run_dir)).iterdir()]
    except OSError:
        return None
    return now - max(times) if times else None


def _chain_time_s(run_dir: str, recorded: dict) -> int | None:
    """The `time` the run's recorded jobs (`_recorded_jobs`) asked for in all, in seconds, from `jobs.json`, else the manifest's `dispatch` entry for the job's stage; None when one of them is not known."""
    try:
        document = json.loads(Path(jobs_dir(run_dir), JOBS_NAME).read_text())
    except (OSError, ValueError):
        document = {}
    jobs = document.get("jobs") if isinstance(document, dict) else None
    asked = {
        str(job.get("job_id")): job.get("time")
        for job in jobs or []
        if isinstance(job, dict)
    }
    planned = (
        (pnr_checkpoints.read_manifest(run_dir) or {}).get("dispatch") or {}
    ).get("stages") or {}
    total = 0
    for job_id, (_cluster, stage) in recorded.items():
        seconds = time_to_seconds(
            asked.get(job_id) or (planned.get(stage) or {}).get("time")
        )
        if seconds is None:
            return None
        total += seconds
    return total


#: How long the orphan lookup waits for each `squeue`.
_ORPHAN_POLL_TIMEOUT_S = 30.0


@dataclass
class _OrphanSpec:
    """The spec of a job rebuilt from a run directory: only where to run the scheduler's commands."""

    suite_dir: str


def read_result(run_dir: str, stage: str) -> dict | None:
    try:
        data = json.loads(Path(job_file(run_dir, stage, "result.json")).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _write_result(run_dir: str, stage: str, **fields) -> None:
    path = job_file(run_dir, stage, "result.json")
    Path(path + ".tmp").write_text(json.dumps({"stage": stage, **fields}) + "\n")
    os.replace(path + ".tmp", path)


def _refusal(run_dir: str, stage: str) -> str | None:
    """Why a job must not run `stage` of `run_dir`, or None.

    A job runs only while its run has not finished and has not been given up (`mark_given_up`), and only with the rtl_buddy version its head ran (the manifest's `generator`), since the head wrote the stage scripts and reads what the job leaves. A later `rb pnr` of the same configuration refuses to start while a job of this run is queued (`live_orphans`), so a job never writes over a newer run.
    """
    script = os.path.join(run_dir, f"{stage_name(stage)}.tcl")
    if not os.path.isfile(script):
        return f"stage script {script} is missing"
    manifest = pnr_checkpoints.read_manifest(run_dir)
    if manifest is None:
        return f"run {os.path.basename(run_dir)} has no readable manifest"
    if manifest.get("outcome") is not None:
        return f"run {os.path.basename(run_dir)} has already finished"
    if given_up(run_dir):
        return f"rb gave run {os.path.basename(run_dir)} up"
    head, job = manifest.get("generator"), generator()
    if head != job:
        return (
            f"the job runs {job}, but the run was started by {head}; run the head "
            "and its jobs from the same rtl_buddy installation"
        )
    return None


def generator() -> str:
    """This rtl_buddy, as a manifest's `generator` names it."""
    return f"rtl-buddy {version('rtl-buddy')}"


_SCRIPT_THREADS_RE = re.compile(r'(\nputs ">>> Threads"\nset_thread_count )\d+(\n)')

#: The scripts a stage job runs in place of the run's recorded ones, by recorded path (see `job_scripts`).
_JOB_SCRIPTS: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "pnr_job_scripts", default={}
)


def session_script(path: str) -> str:
    """The script an OpenROAD session runs for the run's recorded script `path`: inside `job_scripts`, the job's copy, else `path`."""
    return _JOB_SCRIPTS.get().get(os.path.abspath(path), path)


@contextmanager
def job_scripts(run_dir: str, scripts: list[str], rewrite):
    """While the block runs, sessions run a copy of each of `scripts` (recorded scripts of `run_dir`) rewritten by `rewrite(text)`, and the recorded scripts stay as the head wrote them, matching the manifest.

    A copy is written to `dispatch/<script name>` only when `rewrite` changes the script; `session_script` names it. A script that cannot be copied runs as recorded, with a warning.
    """
    copies = {}
    for path in scripts:
        try:
            text = Path(path).read_text()
            updated = rewrite(text)
            if updated == text:
                continue
            copy = os.path.join(jobs_dir(run_dir), os.path.basename(path))
            Path(f"{copy}.tmp").write_text(updated)
            os.replace(f"{copy}.tmp", copy)
        except OSError as e:
            log_event(
                logger,
                logging.WARNING,
                "pnr.stage_job_script_unchanged",
                run_dir=run_dir,
                script=path,
                error=str(e),
            )
            continue
        copies[os.path.abspath(path)] = copy
    token = _JOB_SCRIPTS.set({**_JOB_SCRIPTS.get(), **copies})
    try:
        yield copies
    finally:
        _JOB_SCRIPTS.reset(token)


def _job_thread_plan(stage: str, manifest: dict, *, run_name: str):
    """The thread plan of `stage` for the CPUs this job received, or None when the run sets no thread count.

    The head wrote the count its plan gave for the CPUs it asked for; `sbatch-args` or the site can change what the job gets, so the job resolves `threads:` (recorded in the manifest's `dispatch`) again against its own allocation (`detect_allocation`). Every script the stage runs gets it, each post-route repair session too (`stage_scripts`). The thread count is not part of the stage's identity (`pnr_stages`), so the checkpoint is the same either way.
    """
    entry = ((manifest.get("dispatch") or {}).get("stages") or {}).get(stage) or {}
    requested = entry.get("threads_requested")
    if requested is None:
        return None
    return plan_threads(
        requested,
        flow="pnr",
        run=f"{run_name} {stage}",
        allocation=detect_allocation(),
    )


def _with_threads(count: int):
    """A `job_scripts` rewrite that sets a script's thread count to `count`."""
    return lambda text: _SCRIPT_THREADS_RE.sub(
        lambda m: f"{m.group(1)}{count}{m.group(2)}", text, count=1
    )


def _artefact_dir(run_dir: str) -> str:
    """The artefact directory of a checkpoint run directory (`<artefact dir>/checkpoints/<run id>`), where its sessions run."""
    return os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))


def run_stage_job(run_dir: str, stage: str, *, openroad: str, pnr_log: str) -> bool:
    """Run `stage` of the staged run in `run_dir` inside a dispatched job; return whether it ended cleanly.

    Writes `dispatch/NN_<stage>.result.json` for the head. The stage itself runs as in a local staged run, with the job's id and host added to its stage events, and from a copy of each of its scripts, every post-route repair session's too (`stage_scripts`), with the thread count the job's CPUs give (`job_scripts`). When rb gave the run up while the stage ran, the job removes the stage's outputs from the artefact directory once the stage ends, and the stage fails (`_discard_given_up_stage`).
    """
    from .pnr_openroad import run_stage, stage_scripts

    see_job_writes(run_dir, pnr_log)
    refused = _refusal(run_dir, stage)
    if refused is not None:
        log_event(
            logger,
            logging.ERROR,
            "pnr.stage_job_refused",
            run_dir=run_dir,
            stage=stage,
            reason=refused,
        )
        _write_result(
            run_dir, stage, ok=False, started=False, returncode=None, problem=refused
        )
        return False
    manifest = pnr_checkpoints.read_manifest(run_dir) or {}
    run_name = str(manifest.get("run") or os.path.basename(run_dir))
    env = os.environ.copy()
    env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    fields = {"slurm_job": os.environ.get("SLURM_JOB_ID"), "host": socket.gethostname()}
    plan = _job_thread_plan(stage, manifest, run_name=run_name)
    if plan is not None:
        fields["thread_plan"] = plan.fields()
    scripts = []
    if plan is not None:
        try:
            scripts = stage_scripts(run_dir, stage)
        except (OSError, ValueError, KeyError):
            # The stage reads the same plan and reports why it cannot.
            scripts = [os.path.join(run_dir, f"{stage_name(stage)}.tcl")]
    try:
        with (
            open(pnr_log, "ab") as log,
            job_scripts(
                run_dir,
                scripts,
                _with_threads(plan.count) if plan is not None else None,
            ),
        ):
            outcome = run_stage(
                run_dir,
                stage,
                openroad=openroad,
                log=log,
                log_path=pnr_log,
                cwd=_artefact_dir(run_dir),
                env=env,
                run_name=run_name,
                fields=fields,
            )
    finally:
        discarded = _discard_given_up_stage(
            run_dir, stage, manifest, pnr_log=pnr_log, run_name=run_name
        )
    ok = outcome.ok and discarded is None
    _write_result(
        run_dir,
        stage,
        ok=ok,
        started=True,
        returncode=outcome.returncode,
        stderr=outcome.stderr[-_STDERR_KEEP:],
        stderr_first_line=_first_line(outcome.stderr),
        problem=discarded or outcome.problem,
        fail_stage=DISPATCH_FAIL_STAGE if discarded else outcome.fail_stage,
        errors=outcome.errors,
        rtl_buddy=generator(),
        **fields,
    )
    return ok


def _discard_given_up_stage(
    run_dir: str, stage: str, manifest: dict, *, pnr_log: str, run_name: str
) -> str | None:
    """When rb gave the run up while `stage` ran, remove the run's outputs from the artefact directory and return why the stage failed; else None.

    The head clears the same outputs after it marks the run given up, so they hold nothing of the run but what this stage wrote. The directories are opened first (`see_job_writes`), so a mark written on another host is seen.
    """
    from .pnr_openroad import clear_run_outputs

    see_job_writes(run_dir, pnr_log)
    if not given_up(run_dir):
        return None
    problem = (
        f"rb gave run {os.path.basename(run_dir)} up while stage "
        f"{stage_name(stage)} ran; the job removed the outputs the stage wrote"
    )
    error = None
    try:
        removed = clear_run_outputs(
            _artefact_dir(run_dir), owner=run_name, design=manifest.get("design")
        )
    except (FatalRtlBuddyError, OSError) as e:
        removed, error = [], str(e)
        problem = (
            f"rb gave run {os.path.basename(run_dir)} up while stage "
            f"{stage_name(stage)} ran, and the job could not remove the outputs "
            f"the stage wrote: {error}"
        )
    log_event(
        logger,
        logging.WARNING,
        "pnr.stage_job_given_up",
        run_dir=run_dir,
        stage=stage,
        removed=removed,
        error=error,
    )
    return problem


@dataclass
class ChainOutcome:
    """How a dispatched chain ended, as `OpenRoadPnr` reads a local chain: the last session's exit code and stderr, and the failed stage with why and the `fail_stage` its failure carries."""

    returncode: int | None = 0
    stderr: str = ""
    #: The session's first non-empty stderr line, kept whole however much of `stderr` the job kept.
    stderr_first_line: str = ""
    problem: str | None = None
    failed_stage: str | None = None
    fail_stage: str | None = None
    #: The scheduler's account of the failed stage's job, for the result description.
    reason: str | None = None


def _close_killed_stage(
    run_dir: str, stage: str, begun: dict, record: dict, *, log_path: str, **fields
) -> None:
    """Finish the records of a stage whose job was killed inside it: its log from `pnr.log`, and a `stage_end` with what the scheduler measured."""
    stage_log = Path(run_dir, f"{stage_name(stage)}.log")
    offset = begun.get("log_offset")
    if not stage_log.exists() and isinstance(offset, int):
        try:
            with open(log_path, "rb") as f:
                f.seek(offset)
                stage_log.write_bytes(f.read())
        except OSError:
            pass
    rss = record.get("max_rss_bytes")
    pnr_checkpoints.stage_end(
        run_dir,
        stage,
        returncode=None,
        resources={
            "wall_s": record.get("elapsed_s"),
            "cpu_s": record.get("total_cpu_s"),
            "peak_rss_kb": rss // 1024 if isinstance(rss, int) else None,
            "measured_by": "scheduler",
        },
        status="error",
        **fields,
    )


def chain_outcome(run_dir: str, chain: StageChain, *, log_path: str) -> ChainOutcome:
    """Read how a chain that has left the queue ended, from each job's result and the scheduler's accounting.

    A stage succeeded only when its job wrote a successful result and ended `COMPLETED`; when accounting gives no end state, the result is taken as written, with a warning (`pnr.stage_jobs_unaccounted`). A job that wrote a successful result and ended in any other state fails its stage with `fail_stage: dispatch`. Otherwise the failed stage carries its own `fail_stage` only when its job ran the stage and ended itself (`COMPLETED` or `FAILED`). A job the scheduler ended (any other state), one that refused its stage or never started it, and one that left no result fail with `fail_stage: dispatch`.
    """
    see_job_writes(run_dir, log_path)
    events = pnr_checkpoints.read_progress(run_dir)
    begun = {e.get("stage"): e for e in events if e.get("event") == "stage_begin"}
    ended = {e.get("stage") for e in events if e.get("event") == "stage_end"}
    label = _label(chain.backend)
    records = chain.states(chain.handles)
    returncode, stderr = 0, ""
    unaccounted: list[JobHandle] = []
    for handle in chain.handles:
        stage = handle.spec.stage
        result = read_result(run_dir, stage)
        record = records.get(telemetry_key(handle)) or {}
        state = _end_state(records, handle)
        succeeded = result is not None and bool(result.get("ok"))
        if succeeded and (state == "COMPLETED" or state in _UNSETTLED_STATES):
            if state != "COMPLETED":
                unaccounted.append(handle)
            returncode = result.get("returncode", 0)
            stderr = result.get("stderr") or ""
            continue
        _warn_unaccounted(chain, unaccounted)
        reason = f"{label} job {handle.job_id} ended" + (f" {state}" if state else "")
        name = stage_name(stage)
        outcome = ChainOutcome(
            returncode=None,
            failed_stage=stage,
            fail_stage=DISPATCH_FAIL_STAGE,
            reason=reason,
        )
        if succeeded:
            outcome.problem = (
                f"stage {name}: {reason} after the stage succeeded and wrote its "
                f"result (see {handle.spec.log_path})"
            )
        elif result is not None and result.get("started"):
            outcome.returncode = result.get("returncode")
            outcome.stderr = result.get("stderr") or ""
            outcome.stderr_first_line = result.get(
                "stderr_first_line", _first_line(outcome.stderr)
            )
            if not state or state in _SELF_ENDED_STATES:
                outcome.fail_stage = result.get("fail_stage")
            problem = result.get("problem")
            outcome.problem = f"{problem} ({reason})" if problem else None
        elif result is not None:
            outcome.problem = f"stage {name}: {result.get('problem')} ({reason})"
        elif stage in ended:
            outcome.problem = (
                f"stage {name}: {reason} after the stage ended, without writing "
                f"its result (see {handle.spec.log_path})"
            )
        elif stage in begun:
            _close_killed_stage(
                run_dir,
                stage,
                begun[stage],
                record,
                log_path=log_path,
                slurm_job=handle.job_id,
                slurm_state=state or None,
            )
            outcome.problem = f"stage {name}: {reason} during the stage"
        else:
            outcome.problem = (
                f"stage {name}: {reason} before the stage started "
                f"(see {handle.spec.log_path})"
            )
        log_event(
            logger,
            logging.WARNING,
            "pnr.stage_job_failed",
            stage=stage,
            job_id=handle.job_id,
            state=state or None,
            started=bool(result and result.get("started")),
            log=str(handle.spec.log_path),
        )
        return outcome
    _warn_unaccounted(chain, unaccounted)
    return ChainOutcome(returncode=returncode, stderr=stderr)


def _warn_unaccounted(chain: StageChain, handles: list[JobHandle]) -> None:
    """Warn that the successful results of `handles` were taken as written, since accounting gave no end state for their jobs."""
    if not handles:
        return
    log_event(
        logger,
        logging.WARNING,
        "pnr.stage_jobs_unaccounted",
        backend=chain.backend.name,
        pnr=chain.run,
        stages=[stage_name(h.spec.stage) for h in handles],
        job_ids=[h.job_id for h in handles],
    )
