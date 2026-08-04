# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Native-process dispatch backend — parallel regression on one host (#360).

``--dispatch slurm`` needs a cluster, and ``--dispatch local`` runs tests
strictly one at a time in-process. This backend is the middle ground a
laptop can run: the same plan → build job → gated sim fan-out as the Slurm
path, but every job is a plain :class:`subprocess.Popen` of ``rb
_build-job`` / ``rb _test-job`` on *this* machine, throttled by a single
pool of ``jobs`` slots. There is no scheduler to install or configure —
macOS included, where Slurm has no native build.

Everything backend-independent is reused unchanged: the head's single
sweep expansion (the plan manifest), one shared build per suite with the
sims gated on its success — here the gate is "the build process exited 0"
rather than ``--dependency=afterok`` — and per-job ``result.json``
envelopes that the head collects.

Two things it deliberately does **not** do:

* **Enforce reservations.** ``resources:`` cpus/mem/time are ignored, not
  half-honoured: a single host has no portable per-process cap
  (``ulimit``/``nice``/``taskset`` are coarse and platform-specific), so
  ``jobs`` is the only concurrency control. ``max-jobs-per-array`` is a
  Slurm ``%N`` throttle and equally inapplicable — the pool is one global
  cap across every suite and resource group.
* **Report usage.** With no accounting source there is no reserved-vs-used
  telemetry, so reservation right-sizing yields no advice
  (:meth:`LocalProcessBackend.collect_telemetry`).

Jobs run in their own session (``start_new_session``) so the *head* owns
their lifecycle: an interrupt reaches the head, which takes the fleet down
through :meth:`LocalProcessBackend.cancel_all` — the shape ``scancel``
has, and it lets a simulator receive a graceful ``SIGTERM`` instead of a
bare terminal ``SIGINT``. The trade-off is the same one Slurm's
``--kill-on-invalid-dep`` exists to avoid: a ``SIGKILL``ed head runs no
cleanup, and with no scheduler to reap them its children run to
completion.

``run_managed_process`` is not usable here — it owns exactly one process
for the duration of a call, and the whole point of this backend is holding
several at once — so the pool drives ``Popen`` directly and reuses that
module's :func:`~rtl_buddy.process_utils.terminate_process_group` for
teardown, keeping signal escalation identical to every other long-lived
rtl_buddy subprocess.
"""

import logging
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..process_utils import terminate_process_group
from .argv import build_job_argv, test_job_argv
from .base import BuildJobSpec, DispatchBackend, JobHandle, TestJobSpec

logger = logging.getLogger(__name__)

# How long the pool sleeps between reap/launch sweeps. Deliberately not
# `cfg-dispatch.poll-interval`: that paces `squeue` calls against a
# scheduler that rate-limits them, whereas a sweep here is a handful of
# `Popen.poll()` calls in this process, so it can be quick enough that a
# freed slot is refilled promptly rather than seconds later.
_POLL_INTERVAL_SEC = 0.05

# Default cap: enough parallelism to matter on a laptop, low enough that N
# unreserved simulators do not make the machine unusable. Reservations are
# advisory here, so the pool size is the only backpressure there is.
_DEFAULT_MAX_JOBS = 4


def default_jobs() -> int:
    """Pool size when neither ``--jobs`` nor ``cfg-dispatch.jobs`` is set."""
    return max(1, min(_DEFAULT_MAX_JOBS, os.cpu_count() or 1))


@dataclass
class _PoolJob:
    """One submitted job and where it is in its lifecycle.

    ``proc is None and not skipped`` is queued, ``proc`` with no
    ``returncode`` is running, and a ``returncode`` or ``skipped`` is
    terminal. ``skipped`` is the local equivalent of a Slurm job whose
    ``afterok`` dependency failed: never launched, and reported by the
    collector as producing no result — which is exactly what happened.
    """

    job_id: str
    spec: BuildJobSpec | TestJobSpec
    argv: list[str] = field(default_factory=list)
    kind: str = "sim"  # "build" | "sim"; builds launch first
    seq: int = 0
    dependency: str | None = None
    proc: subprocess.Popen | None = None
    log_handle: IO | None = None
    returncode: int | None = None
    skipped: bool = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.returncode is None

    @property
    def finished(self) -> bool:
        return self.skipped or self.returncode is not None

    def label(self) -> str:
        if isinstance(self.spec, TestJobSpec):
            return f"job for {self.spec.display_name()}"
        return f"build job for {self.spec.suite_dir}"


class LocalProcessBackend(DispatchBackend):
    """Dispatch jobs as capped concurrent subprocesses on this host."""

    name = "local-parallel"
    # This process is the scheduler, so a missing result is never a queue
    # decision — diagnostics must not offer one as the explanation.
    scheduled = False

    def __init__(self, dispatch_cfg):
        self.max_jobs = (
            dispatch_cfg.jobs if dispatch_cfg.jobs is not None else default_jobs()
        )
        self._jobs: dict[str, _PoolJob] = {}
        self._seq = 0
        # Records how many slots this run actually has — the one number that
        # explains its wall-clock, and it is often a default nobody chose.
        log_event(
            logger,
            logging.INFO,
            "dispatch.pool_configured",
            backend=self.name,
            jobs=self.max_jobs,
            cpus=os.cpu_count(),
        )
        if dispatch_cfg.resources is not None or dispatch_cfg.compile is not None:
            # A `resources:` block written for a cluster is silently inert
            # here. Say so once rather than let it read as enforced.
            log_event(
                logger,
                logging.INFO,
                "dispatch.reservations_ignored",
                backend=self.name,
            )

    # ---- submission -------------------------------------------------

    def _enqueue(self, spec, argv, *, kind, dependency) -> JobHandle:
        if dependency is not None and dependency not in self._jobs:
            raise FatalRtlBuddyError(
                f"{self.name}: unknown dependency job id {dependency!r} — a job "
                "can only be gated on one this backend submitted"
            )
        self._seq += 1
        job = _PoolJob(
            job_id=f"lp-{self._seq}",
            spec=spec,
            argv=list(argv),
            kind=kind,
            seq=self._seq,
            dependency=dependency,
        )
        self._jobs[job.job_id] = job
        return JobHandle(job_id=job.job_id, spec=spec)

    def submit_build(self, spec: BuildJobSpec) -> JobHandle:
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
        )
        # Start it now if a slot is free: the head goes on to plan further
        # suites, and a build running through that is free wall-clock.
        self._pump()
        return handle

    def submit(self, spec: TestJobSpec, *, dependency: str | None = None) -> JobHandle:
        handle = self._enqueue(
            spec, test_job_argv(spec), kind="sim", dependency=dependency
        )
        log_event(
            logger,
            logging.INFO,
            "dispatch.submitted",
            backend=self.name,
            job_id=handle.job_id,
            test=spec.test_name,
            run_id=spec.run_id,
            dependency=dependency,
        )
        self._pump()
        return handle

    # ``submit_array`` is inherited: an "array" is a Slurm grouping, and its
    # ``%max_parallel`` throttle has no meaning against one global pool, so
    # the base implementation's loop over :meth:`submit` is exactly right.

    # ---- the pool ---------------------------------------------------

    def _gate(self, job: _PoolJob) -> str:
        """``open`` / ``closed`` / ``failed`` for ``job``'s dependency."""
        if job.dependency is None:
            return "open"
        dep = self._jobs[job.dependency]
        if not dep.finished:
            return "closed"
        # A skipped dependency (its own gate failed) has no returncode, and
        # counts as failed — the failure propagates down the chain.
        return "open" if dep.returncode == 0 else "failed"

    def _reap(self) -> None:
        """Collect exited processes, then skip jobs whose gate can never open."""
        for job in self._jobs.values():
            proc = job.proc
            if proc is None or job.returncode is not None:
                continue
            returncode = proc.poll()
            if returncode is None:
                continue
            job.returncode = returncode
            self._close_log(job)
            log_event(
                logger,
                # A clean exit is bookkeeping (the result envelope carries the
                # outcome); a nonzero one is the diagnosis for a missing
                # envelope or a fan-out that never ran, so surface it.
                logging.INFO if returncode != 0 else logging.DEBUG,
                "dispatch.job_exited",
                backend=self.name,
                job_id=job.job_id,
                kind=job.kind,
                returncode=returncode,
            )

        skipped = []
        for job in self._jobs.values():
            if job.proc is None and not job.skipped and self._gate(job) == "failed":
                job.skipped = True
                skipped.append(job.job_id)
        if skipped:
            # The counterpart of Slurm cancelling an afterok dependent: the
            # shared build failed, so its sims have nothing to run against.
            log_event(
                logger,
                logging.WARNING,
                "dispatch.dependency_failed",
                backend=self.name,
                jobs=skipped,
            )

    def _launchable(self) -> list[_PoolJob]:
        """Queued jobs whose gate is open, in the order they should start.

        Build jobs first: a build unblocks a whole suite's fan-out, while a
        sim unblocks nothing, so a second suite's build must not queue
        behind the first suite's sims.
        """
        ready = [
            job
            for job in self._jobs.values()
            if job.proc is None and not job.skipped and self._gate(job) == "open"
        ]
        ready.sort(key=lambda job: (0 if job.kind == "build" else 1, job.seq))
        return ready

    def _close_log(self, job: _PoolJob) -> None:
        if job.log_handle is not None:
            job.log_handle.close()
            job.log_handle = None

    def _launch(self, job: _PoolJob) -> None:
        log_path = getattr(job.spec, "log_path", None)
        # Never let a job inherit the head's stdout: every one runs
        # `rb --machine`, so its envelope would interleave into the head's
        # own machine-mode output and corrupt it. Without a log path the
        # output is dropped rather than mixed in.
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
                # Own session: the head cancels the fleet explicitly, so a
                # terminal SIGINT does not race that, and a simulator gets a
                # graceful SIGTERM (see the module docstring).
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            self._close_log(job)
            raise FatalRtlBuddyError(
                f"{self.name}: could not start {job.label()}: {e}"
            ) from e
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
        running = sum(1 for job in self._jobs.values() if job.running)
        for job in self._launchable():
            if running >= self.max_jobs:
                break
            self._launch(job)
            running += 1

    # ---- waiting and teardown ---------------------------------------

    def wait_all(self, handles: list[JobHandle]) -> None:
        if not handles:
            return
        watched = [self._jobs[h.job_id] for h in handles if h is not None]
        remaining = None
        while True:
            self._pump()
            outstanding = sum(1 for job in watched if not job.finished)
            if not outstanding:
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(watched),
                )
                return
            if outstanding != remaining:
                # Only on change: at this poll interval, logging every sweep
                # would bury the run's log in thousands of identical lines.
                remaining = outstanding
                log_event(
                    logger,
                    logging.DEBUG,
                    "dispatch.waiting",
                    backend=self.name,
                    remaining=outstanding,
                    total=len(watched),
                )
            time.sleep(_POLL_INTERVAL_SEC)

    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        if not handles:
            return
        cancelled = 0
        for handle in handles:
            # Tolerate None: this is the last line of defence against an
            # orphaned fleet and must not be disarmed by one bad entry (#361).
            if handle is None:
                continue
            job = self._jobs.get(handle.job_id)
            if job is None or job.finished:
                continue
            if job.proc is not None:
                terminate_process_group(job.proc)
                job.returncode = job.proc.returncode
            else:
                # Queued: marking it skipped is the cancellation — a later
                # sweep must not start a job the head has given up on.
                job.skipped = True
            self._close_log(job)
            cancelled += 1
        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=cancelled,
        )

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """No accounting source on a bare host, so no reserved-vs-used data.

        This restates the ABC default deliberately: an empty mapping here is
        a documented non-goal (nothing reserved anything, so there is nothing
        to compare against), not a gap waiting to be filled. Reservation
        right-sizing degrades to no advice.
        """
        return {}
