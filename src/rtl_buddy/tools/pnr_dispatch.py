"""Staged `rb pnr` runs dispatched to a scheduler: one chained job per stage.

The head renders every stage script into the checkpoint run directory, then submits one job per stage, each held until the previous one succeeds (`afterok`), and blocks until the chain has left the queue. Each job runs `rb _pnr-stage-job`, which runs its stage exactly as a local staged run does (`pnr_openroad.run_stage`): the stage's sessions, its log section appended to `pnr.log`, and its `stage_begin` and `stage_end` events with the resources measured inside the job. The head then reads the run directory as it does after a local chain.

Files under the run directory's `dispatch/`:

    jobs.json                     # each stage's job, written as the scheduler accepts it
    NN_<stage>.slurm.log          # the job's scheduler output
    NN_<stage>.rtl_buddy.log      # the job's rtl_buddy log
    NN_<stage>.result.json        # how the stage ended, written by the job
"""

import json
import logging
import os
import signal
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..dispatch.base import JobHandle, telemetry_key
from ..dispatch.retry import normalise_scheduler_state
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from . import pnr_checkpoints

logger = logging.getLogger(__name__)

DISPATCH_DIRNAME = "dispatch"
JOBS_NAME = "jobs.json"

#: Allocation source recorded in a dispatched stage's thread plan: the job's `--cpus-per-task`.
JOB_CPUS_SOURCE = "SLURM_CPUS_PER_TASK"

#: Accounting can trail a job's exit; a failed stage's state is asked for this many times.
_STATE_ATTEMPTS = 3
_STATE_RETRY_S = 2.0

#: How much of a failed session's stderr a job result keeps.
_STDERR_KEEP = 8000

_BACKEND_LABELS = {"slurm": "Slurm"}


def jobs_dir(run_dir: str) -> str:
    return os.path.join(run_dir, DISPATCH_DIRNAME)


def stage_name(stage: str) -> str:
    return f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}"


def job_file(run_dir: str, stage: str, suffix: str) -> str:
    """`dispatch/NN_<stage>.<suffix>` of a run directory."""
    return os.path.join(jobs_dir(run_dir), f"{stage_name(stage)}.{suffix}")


class ChainCancelled(FatalRtlBuddyError):
    """The chain was cancelled while it was still being submitted."""


_LIVE: set["StageChain"] = set()
_LIVE_LOCK = threading.Lock()


class StageChain:
    """One run's stage jobs, submitted in order, each held until the previous one succeeds.

    Used as a context manager: while open, an interrupt (`cancel_on_interrupt`) cancels it, and leaving it with an exception cancels every job it submitted. It cancels only its own jobs.
    """

    def __init__(self, backend):
        self.backend = backend
        self.handles: list[JobHandle] = []
        self._cancelled = False
        self._lock = threading.Lock()

    def __enter__(self) -> "StageChain":
        with _LIVE_LOCK:
            _LIVE.add(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        with _LIVE_LOCK:
            _LIVE.discard(self)
        if exc_type is not None:
            self.cancel()
        return False

    def submit(self, spec) -> JobHandle:
        """Submit `spec` behind the last job of the chain and return its handle."""
        with self._lock:
            if self._cancelled:
                raise ChainCancelled("the P&R stage chain was cancelled")
            dependency = self.handles[-1].job_id if self.handles else None
        with _signals_held():
            handle = self.backend.submit_pnr_stage(spec, dependency=dependency)
            with self._lock:
                self.handles.append(handle)
                cancelled = self._cancelled
        if cancelled:
            self.backend.cancel_all([handle])
            raise ChainCancelled("the P&R stage chain was cancelled")
        return handle

    def wait(self) -> None:
        """Block until every job of the chain has left the queue."""
        self.backend.wait_all(list(self.handles))

    def cancel(self) -> None:
        """Cancel the chain's jobs once; later calls do nothing."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            handles = list(self.handles)
        if handles:
            self.backend.cancel_all(handles)

    def state(self, handle: JobHandle) -> dict:
        """The scheduler's accounting record of one job, asked again while it has no state yet."""
        record: dict = {}
        for attempt in range(_STATE_ATTEMPTS):
            record = (
                self.backend.collect_telemetry([handle]).get(telemetry_key(handle))
                or {}
            )
            if record.get("state") or attempt == _STATE_ATTEMPTS - 1:
                break
            time.sleep(_STATE_RETRY_S)
        return record


def _cancel_live_chains() -> None:
    with _LIVE_LOCK:
        chains = list(_LIVE)
    for chain in chains:
        chain.cancel()


@contextmanager
def _signals_held():
    """Hold SIGINT and SIGTERM in the main thread while a job is submitted, so an interrupt cancels it instead of losing its id; the submit command inherits the mask."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "pthread_sigmask"
    ):
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def cancel_on_interrupt():
    """While open, SIGINT and SIGTERM cancel every open stage chain of this process, then raise `KeyboardInterrupt` or `SystemExit(128 + signal)`.

    Install it in the main thread around runs that dispatch; chains waiting in worker threads are cancelled from here, which lets those threads finish.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _handler(signum, _frame):
        _cancel_live_chains()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def record_jobs(run_dir: str, chain: StageChain) -> None:
    """Write `dispatch/jobs.json`: the job of each stage submitted so far."""
    document = {
        "backend": chain.backend.name,
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


def read_result(run_dir: str, stage: str) -> dict | None:
    try:
        data = json.loads(Path(job_file(run_dir, stage, "result.json")).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_result(run_dir: str, stage: str, **fields) -> None:
    path = job_file(run_dir, stage, "result.json")
    Path(path + ".tmp").write_text(json.dumps({"stage": stage, **fields}) + "\n")
    os.replace(path + ".tmp", path)


def _refusal(run_dir: str, stage: str) -> str | None:
    """Why a job must not run `stage` of `run_dir`, or None.

    A job runs only for the run its head started: `latest` of its P&R run must still point at it and the run must not have finished. A later `rb pnr` of the same run re-points `latest`, so a chain its head left behind stops at its next stage instead of writing over the new run's `pnr.log` and outputs.
    """
    script = os.path.join(run_dir, f"{stage_name(stage)}.tcl")
    if not os.path.isfile(script):
        return f"stage script {script} is missing"
    latest = os.path.join(os.path.dirname(run_dir), pnr_checkpoints.LATEST_NAME)
    if os.path.realpath(latest) != os.path.realpath(run_dir):
        return (
            f"run {os.path.basename(run_dir)} is no longer the latest of its P&R "
            "run; a later rb pnr of the run has started"
        )
    manifest = pnr_checkpoints.read_manifest(run_dir)
    if manifest is None:
        return f"run {os.path.basename(run_dir)} has no readable manifest"
    if manifest.get("outcome") is not None:
        return f"run {os.path.basename(run_dir)} has already finished"
    return None


def run_stage_job(run_dir: str, stage: str, *, openroad: str, pnr_log: str) -> bool:
    """Run `stage` of the staged run in `run_dir` inside a dispatched job; return whether it ended cleanly.

    Writes `dispatch/NN_<stage>.result.json` for the head. The stage itself runs as in a local staged run, with the job's id and host added to its stage events.
    """
    from .pnr_openroad import run_stage

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
    env = os.environ.copy()
    env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    fields = {"slurm_job": os.environ.get("SLURM_JOB_ID"), "host": socket.gethostname()}
    with open(pnr_log, "ab") as log:
        outcome = run_stage(
            run_dir,
            stage,
            openroad=openroad,
            log=log,
            log_path=pnr_log,
            env=env,
            run_name=str(manifest.get("run") or os.path.basename(run_dir)),
            fields=fields,
        )
    _write_result(
        run_dir,
        stage,
        ok=outcome.ok,
        started=True,
        returncode=outcome.returncode,
        stderr=outcome.stderr[-_STDERR_KEEP:],
        problem=outcome.problem,
        **fields,
    )
    return outcome.ok


@dataclass
class ChainOutcome:
    """How a dispatched chain ended, as `OpenRoadPnr` reads a local chain: the last session's exit code and stderr, and the failed stage with why."""

    returncode: int | None = 0
    stderr: str = ""
    problem: str | None = None
    failed_stage: str | None = None
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
    """Read how a chain that has left the queue ended, from each job's result and, for the first stage that did not end cleanly, the scheduler's accounting."""
    events = pnr_checkpoints.read_progress(run_dir)
    begun = {e.get("stage"): e for e in events if e.get("event") == "stage_begin"}
    ended = {e.get("stage") for e in events if e.get("event") == "stage_end"}
    label = _BACKEND_LABELS.get(chain.backend.name, chain.backend.name)
    returncode, stderr = 0, ""
    for handle in chain.handles:
        stage = handle.spec.stage
        result = read_result(run_dir, stage)
        if result is not None and result.get("ok"):
            returncode = result.get("returncode", 0)
            stderr = result.get("stderr") or ""
            continue
        record = chain.state(handle)
        state = normalise_scheduler_state(record.get("state"))
        reason = f"{label} job {handle.job_id} ended" + (f" {state}" if state else "")
        name = stage_name(stage)
        outcome = ChainOutcome(returncode=None, failed_stage=stage, reason=reason)
        if result is not None and result.get("started"):
            outcome.returncode = result.get("returncode")
            outcome.stderr = result.get("stderr") or ""
            problem = result.get("problem")
            outcome.problem = f"{problem} ({reason})" if problem else None
        elif result is not None:
            outcome.problem = f"stage {name}: {result.get('problem')} ({reason})"
        elif stage in begun and stage not in ended:
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
    return ChainOutcome(returncode=returncode, stderr=stderr)
