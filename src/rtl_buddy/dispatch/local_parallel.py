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
from .argv import build_job_argv, test_job_argv
from .base import BuildJobSpec, DispatchBackend, JobHandle, TestJobSpec
from .progress import DispatchProgress, group_job_ids

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
    # Monotonic instant before which this job must not start — the retry
    # backoff (#405). There is no scheduler here to hold the job, so the
    # pool holds it itself: it stays queued, occupying no slot, and simply
    # is not launchable until its time comes.
    not_before: float | None = None
    # When the process was launched (monotonic), so progress reporting can
    # name the job that has been running longest.
    started_at: float | None = None

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
        # Same two knobs the Slurm backend honours (#435): the pool is
        # quieter than a queue, but a laptop regression can still run for an
        # hour with nothing on the console between submit and drain.
        self.progress_interval = getattr(dispatch_cfg, "progress_interval", 60.0)
        self.max_wait = getattr(dispatch_cfg, "max_wait", None)
        # Every job ever submitted, by id: the dependency graph and the
        # handles the head collects with both index into this.
        self._jobs: dict[str, _PoolJob] = {}
        # The non-terminal subsets a sweep actually has to look at. Walking
        # `_jobs` instead would make every 50 ms sweep O(all jobs ever) —
        # steady background CPU on the head for no new information once a
        # few thousand jobs have finished.
        self._queued: list[_PoolJob] = []  # submit order; launch order sorts it
        self._running: dict[str, _PoolJob] = {}
        self._seq = 0
        self._warned_reservations = False
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

    # ---- submission -------------------------------------------------

    def _warn_if_reserved(self, spec) -> None:
        """Warn once when a reservation this backend cannot honour is set.

        Checked against the **resolved** ``JobResources`` on each spec rather
        than against ``cfg-dispatch``, so a project that reserves only per
        test or per testbench in tests.yaml — where the heavy tests are — is
        told too. WARNING, not INFO: the console shows INFO only under
        ``-v``, and a reservation silently doing nothing is precisely the
        misreading this exists to prevent.

        A build job's cpus arrive from the head already multiplied by its
        own ``parallel`` (#495), so they are undone again before the
        comparison: that factor buys concurrency the build job *does*
        honour here, and left in it would turn ``compile: {parallel: 2}``
        with no ``resources:`` anywhere into a warning about a reservation
        the project never wrote.
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
        if resources == JobResources():
            return
        self._warned_reservations = True
        log_event(
            logger,
            logging.WARNING,
            "dispatch.reservations_ignored",
            backend=self.name,
            cpus=resources.cpus,
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

    def submit_build(self, spec: BuildJobSpec) -> JobHandle:
        # The pool's own cap counts jobs, not the processes inside them: a
        # build job carrying `--parallel N` (#495) occupies ONE slot and then
        # fans out to N concurrent compiles inside it, so `jobs` x
        # `compile.parallel` is the real ceiling on this host. Deliberately
        # not clamped here: this backend enforces the job count and nothing
        # else, and silently reinterpreting a project's `compile.parallel`
        # against the local pool would be exactly the kind of hidden
        # reservation semantics `dispatch.reservations_ignored` exists to
        # announce. Sizing the two knobs together is a docs obligation
        # instead — see docs/known-issues.md.
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
        )
        # Start it now if a slot is free: the head goes on to plan further
        # suites, and a build running through that is free wall-clock.
        self._pump()
        return handle

    def submit(
        self,
        spec: TestJobSpec,
        *,
        dependency: str | None = None,
        delay_sec: float = 0.0,
    ) -> JobHandle:
        handle = self._enqueue(
            spec,
            test_job_argv(spec),
            kind="sim",
            dependency=dependency,
            delay_sec=delay_sec,
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
            begin_delay_sec=delay_sec or None,
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
        for job in list(self._running.values()):
            # Every entry here was launched, so it has a process; cancel_all
            # takes its victims out of `_running` itself.
            proc = job.proc
            returncode = proc.poll()
            if returncode is None:
                continue
            job.returncode = returncode
            del self._running[job.job_id]
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

        skipped, still_queued = [], []
        for job in self._queued:
            if self._gate(job) == "failed":
                job.skipped = True
                skipped.append(job.job_id)
            else:
                still_queued.append(job)
        if skipped:
            # Rebuild rather than remove() per entry: a failed build can doom
            # its whole fan-out at once, and that should cost one pass.
            self._queued = still_queued
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

        A job still inside its retry backoff is not ready either: it stays
        queued (and therefore outstanding for ``wait_all``) but takes no
        slot, which is the local stand-in for Slurm holding it ``PENDING``
        on ``--begin`` (#405).
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
            if isinstance(oldest.spec, TestJobSpec)
            else f"build:{os.path.basename(str(oldest.spec.suite_dir).rstrip(os.sep))}"
        )
        return name, time.monotonic() - oldest.started_at

    def _sweep_interval(self, outstanding: list[_PoolJob]) -> float:
        """How long to sleep before the next sweep.

        Normally :data:`_POLL_INTERVAL_SEC`, so a freed slot is refilled
        promptly. But a job inside its retry backoff (#405) is outstanding
        for minutes with nothing to reap and nothing launchable, and 20
        full sweeps a second for a 600 s hold is pure head-side CPU for no
        new information. When *every* outstanding job is waiting on a
        ``not_before``, sleep until the earliest one is due instead —
        capped at a second so the wait still reacts promptly to a
        cancellation or a progress heartbeat.
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
        # The reporter replaces the old DEBUG `dispatch.waiting`: identical
        # numbers, but throttled and console-visible, so a long local run
        # proves it is alive (#435). The per-job job_started/job_exited
        # events stay as they are — they are detail, not the liveness signal.
        progress = DispatchProgress(
            handles,
            backend=self.name,
            interval=self.progress_interval,
            # A job the pool is holding for its retry backoff stays queued
            # for the whole delay, so the deadline allows for the hold the
            # head itself asked for (#405).
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

        Two phases on purpose. Signalling and waiting in the same loop would
        make the grace period scale with the fleet (``jobs`` × 5 s, so a head
        that looks hung for 20 s at ``-j 4`` after the user's Ctrl-C), and
        would leave every job past the interruption point unsignalled if an
        impatient second Ctrl-C landed mid-teardown — the orphaned fleet this
        method exists to prevent. Signalling first means the worst a second
        interrupt costs is the escalation to SIGKILL, never the SIGTERM.
        """
        if not handles:
            return
        # Keyed by job id, so a caller that repeats a handle cannot make the
        # teardown act on one job twice and abort partway through on the
        # second pass. Same reasoning as tolerating ``None`` below: this is
        # the last line of defence against an orphaned fleet, so a malformed
        # handle list must not disarm it (#361).
        victims: dict[str, _PoolJob] = {}
        for handle in handles:
            if handle is None:
                continue
            job = self._jobs.get(handle.job_id)
            if job is not None and not job.finished:
                victims[job.job_id] = job

        # Phase 1 — signal every running job; disarm every queued one. A
        # queued job is cancelled by marking it skipped: a later sweep must
        # not start work the head has given up on.
        signalled = []
        for job in victims.values():
            if job.proc is None:
                job.skipped = True
                self._queued.remove(job)
                continue
            signal_process_group(job.proc, signal.SIGTERM)
            signalled.append(job)

        # Phase 2 — one grace period for the whole fleet, then SIGKILL the
        # stragglers.
        deadline = time.monotonic() + DEFAULT_KILL_TIMEOUT
        for job in signalled:
            proc = job.proc
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_process_group(proc, signal.SIGKILL)
                proc.wait()
            # Read the code back rather than assuming the signal set it: a
            # process that had already exited leaves `finished` False
            # otherwise, and `wait_all` would spin on a job that is gone.
            job.returncode = proc.returncode
            self._running.pop(job.job_id, None)
            self._close_log(job)

        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(victims),
            # Named, not just counted: an interrupted run's ids are what a
            # reader needs to check nothing outlived the head (#435).
            job_ids=group_job_ids(victims.keys()),
        )

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """No accounting source on a bare host, so no reserved-vs-used data.

        This restates the ABC default deliberately: an empty mapping here is
        a documented non-goal (nothing reserved anything, so there is nothing
        to compare against), not a gap waiting to be filled. Reservation
        right-sizing degrades to no advice.
        """
        return {}
