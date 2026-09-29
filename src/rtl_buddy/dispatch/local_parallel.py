# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend that runs jobs as capped subprocesses on this host.

It follows the same plan, build job and gated sim fan-out as Slurm, but every
job is a ``Popen`` of ``rb _build-job`` / ``rb _test-job``, throttled by one
pool of ``jobs`` slots. A sim job's gate is the build process exiting 0.

For simulation jobs it does not enforce ``resources:`` (cpus, mem, time),
``max-jobs-per-array`` or ``max-array-size``; ``jobs`` is the only concurrency
control. Elaboration jobs pass ``cpus`` to pyslang's thread setting. It reports
no usage, so reservation right-sizing gives no advice.

Jobs run in their own session so the head owns their lifecycle: an interrupt
reaches the head, which stops the fleet through :meth:`cancel_all` with
``SIGTERM``. A ``SIGKILL``ed head runs no cleanup and its children run to
completion. The pool drives ``Popen`` directly because ``run_managed_process``
owns only one process, and reuses ``process_utils`` signalling and kill timeout
for teardown.
"""

import logging
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ..config.dispatch import JobResources
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..process_utils import DEFAULT_KILL_TIMEOUT, signal_process_group
from .argv import build_job_argv, elab_job_argv, test_job_argv
from .base import (
    BuildJobSpec,
    DispatchBackend,
    ElabJobSpec,
    JobHandle,
    RunnableJobSpec,
    TestJobSpec,
)
from .progress import DispatchProgress, group_job_ids

logger = logging.getLogger(__name__)


def _runnable_job_argv(spec: RunnableJobSpec) -> list[str]:
    if isinstance(spec, ElabJobSpec):
        return elab_job_argv(spec)
    return test_job_argv(spec)


# Not `cfg-dispatch.poll-interval`, which paces `squeue`. A sweep here is cheap
# `Popen.poll()` calls, so it is short to refill freed slots promptly.
_POLL_INTERVAL_SEC = 0.05

# Reservations are advisory here, so the pool size is the only backpressure.
_DEFAULT_MAX_JOBS = 4


def default_jobs() -> int:
    """Pool size when neither ``--jobs`` nor ``cfg-dispatch.jobs`` is set."""
    return max(1, min(_DEFAULT_MAX_JOBS, os.cpu_count() or 1))


@dataclass
class _PoolJob:
    """One submitted job and where it is in its lifecycle.

    Queued is ``proc is None and not skipped``, running is ``proc`` with no
    ``returncode``, and terminal is a ``returncode`` or ``skipped``. ``skipped``
    is the local equivalent of a Slurm job whose ``afterok`` dependency failed.
    """

    job_id: str
    spec: BuildJobSpec | RunnableJobSpec
    argv: list[str] = field(default_factory=list)
    kind: str = "sim"  # "build" | "sim"; builds launch first
    seq: int = 0
    dependency: str | None = None
    proc: subprocess.Popen | None = None
    log_handle: IO | None = None
    returncode: int | None = None
    skipped: bool = False
    # Monotonic instant before which the job must not start (retry backoff). It stays
    # queued and holds no slot.
    not_before: float | None = None
    # Monotonic launch time, for naming the longest-running job.
    started_at: float | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.returncode is None

    @property
    def finished(self) -> bool:
        return self.skipped or self.returncode is not None

    def label(self) -> str:
        if isinstance(self.spec, (TestJobSpec, ElabJobSpec)):
            return f"job for {self.spec.display_name()}"
        return f"build job for {self.spec.suite_dir}"


class LocalProcessBackend(DispatchBackend):
    """Dispatch jobs as capped concurrent subprocesses on this host."""

    name = "local-parallel"
    # No scheduler exists to blame for a missing result.
    scheduled = False

    def __init__(self, dispatch_cfg):
        self.max_jobs = (
            dispatch_cfg.jobs if dispatch_cfg.jobs is not None else default_jobs()
        )
        self.progress_interval = getattr(dispatch_cfg, "progress_interval", 60.0)
        self.max_wait = getattr(dispatch_cfg, "max_wait", None)
        # Every job ever submitted, by id.
        self._jobs: dict[str, _PoolJob] = {}
        # Non-terminal subsets; scanning `_jobs` would make each sweep O(all jobs ever).
        self._queued: list[_PoolJob] = []
        self._running: dict[str, _PoolJob] = {}
        self._seq = 0
        self._warned_reservations = False
        log_event(
            logger,
            logging.INFO,
            "dispatch.pool_configured",
            backend=self.name,
            jobs=self.max_jobs,
            cpus=os.cpu_count(),
        )

    # ---- submission -------------------------------------------------

    def _warn_if_reserved(self, spec) -> None:
        """Warn once when a resolved reservation this backend cannot honour is set.

        A build job's cpus are divided by its ``parallel`` first, since that
        factor buys concurrency the build honours and is not a user reservation.
        """
        if self._warned_reservations:
            return
        resources = getattr(spec, "resources", None)
        if resources is None:
            return
        parallel = max(1, getattr(spec, "parallel", 1) or 1)
        if parallel > 1:
            resources = JobResources(
                cpus=max(1, resources.cpus // parallel),
                mem=resources.mem,
                time=resources.time,
            )
        honors_cpus = isinstance(spec, ElabJobSpec)
        ignored = JobResources(
            cpus=1 if honors_cpus else resources.cpus,
            mem=resources.mem,
            time=resources.time,
        )
        if ignored == JobResources():
            return
        self._warned_reservations = True
        log_event(
            logger,
            logging.WARNING,
            "dispatch.reservations_ignored",
            backend=self.name,
            cpus=None if honors_cpus else resources.cpus,
            mem=resources.mem,
            time=resources.time,
        )

    def _enqueue(self, spec, argv, *, kind, dependency, delay_sec=0.0) -> JobHandle:
        if dependency is not None and dependency not in self._jobs:
            raise FatalRtlBuddyError(
                f"{self.name}: unknown dependency job id {dependency!r} — a job "
                "can only be gated on one this backend submitted"
            )
        self._warn_if_reserved(spec)
        self._seq += 1
        job = _PoolJob(
            job_id=f"lp-{self._seq}",
            spec=spec,
            argv=list(argv),
            kind=kind,
            seq=self._seq,
            dependency=dependency,
            not_before=(
                time.monotonic() + delay_sec if delay_sec and delay_sec > 0 else None
            ),
        )
        self._jobs[job.job_id] = job
        self._queued.append(job)
        return JobHandle(job_id=job.job_id, spec=spec)

    def submit_build(
        self, spec: BuildJobSpec, *, dependency: str | None = None
    ) -> JobHandle:
        # `dependency` is ignored: the head never splits a compile for this backend.
        # A build with `--parallel N` takes one slot and runs N compiles, so `jobs` x
        # `compile.parallel` is the real ceiling. Not clamped; see docs/known-issues.md.
        handle = self._enqueue(
            spec, build_job_argv(spec), kind="build", dependency=None
        )
        log_event(
            logger,
            logging.INFO,
            "dispatch.build_submitted",
            backend=self.name,
            job_id=handle.job_id,
            suite_dir=spec.suite_dir,
            parallel=spec.parallel,
            # No `job_name`: consumers must treat it as optional and Slurm-only.
        )
        # Start now if a slot is free; the head plans further suites meanwhile.
        self._pump()
        return handle

    def submit(
        self,
        spec: RunnableJobSpec,
        *,
        dependency: str | None = None,
        delay_sec: float = 0.0,
    ) -> JobHandle:
        handle = self._enqueue(
            spec,
            _runnable_job_argv(spec),
            kind="sim",
            dependency=dependency,
            delay_sec=delay_sec,
        )
        fields = {
            "backend": self.name,
            "job_id": handle.job_id,
            "dependency": dependency,
            "begin_delay_sec": delay_sec or None,
        }
        if isinstance(spec, TestJobSpec):
            fields.update(test=spec.test_name, run_id=spec.run_id)
        else:
            fields.update(model=spec.model_name, profile=spec.profile_name)
        log_event(logger, logging.INFO, "dispatch.submitted", **fields)
        self._pump()
        return handle

    # ``submit_array`` is inherited: a loop over :meth:`submit`, since the pool is one global cap.

    # ---- the pool ---------------------------------------------------

    def _gate(self, job: _PoolJob) -> str:
        """``open`` / ``closed`` / ``failed`` for ``job``'s dependency."""
        if job.dependency is None:
            return "open"
        dep = self._jobs[job.dependency]
        if not dep.finished:
            return "closed"
        # A skipped dependency has no returncode and counts as failed.
        return "open" if dep.returncode == 0 else "failed"

    def _reap(self) -> None:
        """Collect exited processes, then skip jobs whose gate can never open."""
        for job in list(self._running.values()):
            proc = job.proc
            returncode = proc.poll()
            if returncode is None:
                continue
            job.returncode = returncode
            del self._running[job.job_id]
            self._close_log(job)
            log_event(
                logger,
                logging.INFO if returncode != 0 else logging.DEBUG,
                "dispatch.job_exited",
                backend=self.name,
                job_id=job.job_id,
                kind=job.kind,
                returncode=returncode,
            )

        skipped, still_queued = [], []
        for job in self._queued:
            if self._gate(job) == "failed":
                job.skipped = True
                skipped.append(job.job_id)
            else:
                still_queued.append(job)
        if skipped:
            self._queued = still_queued
            log_event(
                logger,
                logging.WARNING,
                "dispatch.dependency_failed",
                backend=self.name,
                jobs=skipped,
            )

    def _launchable(self) -> list[_PoolJob]:
        """Queued jobs whose gate is open, in start order.

        Builds go first because they unblock a whole suite. A job inside its
        retry backoff is not ready and takes no slot.
        """
        now = time.monotonic()
        ready = [
            job
            for job in self._queued
            if self._gate(job) == "open"
            and (job.not_before is None or now >= job.not_before)
        ]
        ready.sort(key=lambda job: (0 if job.kind == "build" else 1, job.seq))
        return ready

    def _close_log(self, job: _PoolJob) -> None:
        if job.log_handle is not None:
            job.log_handle.close()
            job.log_handle = None

    def _launch(self, job: _PoolJob) -> None:
        log_path = getattr(job.spec, "log_path", None)
        # Jobs run `rb --machine`; inheriting the head's stdout would corrupt its output.
        stream: IO | int = subprocess.DEVNULL
        if log_path is not None:
            log_path = Path(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(log_path, "w")  # closed when the job is reaped
            job.log_handle = stream
        try:
            job.proc = subprocess.Popen(
                job.argv,
                cwd=job.spec.suite_dir,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            self._close_log(job)
            raise FatalRtlBuddyError(
                f"{self.name}: could not start {job.label()}: {e}"
            ) from e
        job.started_at = time.monotonic()
        self._queued.remove(job)
        self._running[job.job_id] = job
        log_event(
            logger,
            logging.INFO,
            "dispatch.job_started",
            backend=self.name,
            job_id=job.job_id,
            pid=job.proc.pid,
            kind=job.kind,
            log=str(log_path) if log_path is not None else None,
        )

    def _pump(self) -> None:
        """One sweep: reap what finished, fill free slots with what is ready."""
        self._reap()
        free = self.max_jobs - len(self._running)
        if free <= 0:
            return
        for job in self._launchable()[:free]:
            self._launch(job)

    def advance(self) -> None:
        """Refill the pool without waiting — see :meth:`DispatchBackend.advance`."""
        self._pump()

    # ---- waiting and teardown ---------------------------------------

    @staticmethod
    def _longest_running(outstanding: list[_PoolJob]) -> tuple[str, float] | None:
        """The running job started earliest, and for how long."""
        started = [job for job in outstanding if job.running and job.started_at]
        if not started:
            return None
        oldest = min(started, key=lambda job: job.started_at)
        name = (
            oldest.spec.display_name()
            if isinstance(oldest.spec, (TestJobSpec, ElabJobSpec))
            else f"build:{os.path.basename(str(oldest.spec.suite_dir).rstrip(os.sep))}"
        )
        return name, time.monotonic() - oldest.started_at

    def _sweep_interval(self, outstanding: list[_PoolJob]) -> float:
        """Seconds to sleep before the next sweep.

        Normally :data:`_POLL_INTERVAL_SEC`. When every outstanding job is in
        its retry backoff, sleep until the earliest is due, capped at 1 s.
        """
        now = time.monotonic()
        held = [
            job.not_before
            for job in outstanding
            if job.proc is None and job.not_before is not None and job.not_before > now
        ]
        if not held or len(held) != len(outstanding):
            return _POLL_INTERVAL_SEC
        return max(_POLL_INTERVAL_SEC, min(1.0, min(held) - now))

    def wait_all(self, handles: list[JobHandle], *, extra_wait: float = 0.0) -> None:
        if not handles:
            return
        watched = [self._jobs[h.job_id] for h in handles if h is not None]
        progress = DispatchProgress(
            handles,
            backend=self.name,
            interval=self.progress_interval,
            # Held retry jobs stay queued, so the deadline includes their delay.
            max_wait=(
                None if self.max_wait is None else self.max_wait + max(0.0, extra_wait)
            ),
            clock=time.monotonic,
        )
        while True:
            self._pump()
            outstanding = [job for job in watched if not job.finished]
            if not outstanding:
                progress.finish()
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(watched),
                )
                return
            states = {
                job.job_id: "running" if job.running else "pending"
                for job in outstanding
            }
            progress.observe(
                states.keys(), states=states, longest=self._longest_running(outstanding)
            )
            time.sleep(self._sweep_interval(outstanding))

    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        """Take the fleet down: signal everything, then reap on one deadline.

        Signalling and waiting are separate phases so the grace period does not
        scale with the fleet and a second Ctrl-C cannot leave jobs unsignalled.
        """
        if not handles:
            return
        # Keyed by job id so a repeated handle is acted on once.
        victims: dict[str, _PoolJob] = {}
        for handle in handles:
            if handle is None:
                continue
            job = self._jobs.get(handle.job_id)
            if job is not None and not job.finished:
                victims[job.job_id] = job

        # Phase 1: signal running jobs; mark queued ones skipped so no sweep starts them.
        signalled = []
        for job in victims.values():
            if job.proc is None:
                job.skipped = True
                self._queued.remove(job)
                continue
            signal_process_group(job.proc, signal.SIGTERM)
            signalled.append(job)

        # Phase 2: one grace period for the fleet, then SIGKILL.
        deadline = time.monotonic() + DEFAULT_KILL_TIMEOUT
        for job in signalled:
            proc = job.proc
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_process_group(proc, signal.SIGKILL)
                proc.wait()
            # Set explicitly: `wait_all` spins on a job whose returncode stays None.
            job.returncode = proc.returncode
            self._running.pop(job.job_id, None)
            self._close_log(job)

        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(victims),
            job_ids=group_job_ids(victims.keys()),
        )

    def build_outcome(self, handle: JobHandle) -> str | None:
        """``COMPLETED`` or ``FAILED`` from the build process's exit code.

        ``None`` for a job that is not this pool's or is still running; a
        skipped job has no returncode and is not ``COMPLETED``.
        """
        job = self._jobs.get(getattr(handle, "job_id", None))
        if job is None or job.returncode is None:
            return None
        return "COMPLETED" if job.returncode == 0 else "FAILED"

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Empty: a bare host has no accounting source."""
        return {}
