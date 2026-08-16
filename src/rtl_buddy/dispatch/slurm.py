# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Slurm dispatch backend (#351 P1).

Nothing heavy runs on the submit host (usually an interactive login node).
The head submits one **build job** per suite (``submit_build`` →
``rb _build-job``) that Verilates the shared executable on a compute node,
then one ``sbatch --wrap`` sim job per (test, run_id) (``submit`` →
``rb _test-job``) gated on that build with ``--dependency=afterok`` — a sim
only starts once its shared build succeeded, and its own ``compile()`` then
short-circuits on the shared-build stamp so it effectively runs SIM + POST
only. What each job runs is the backend-independent argv from
:mod:`.argv`: the same Python environment (``sys.executable``, on the
shared filesystem alongside the project), handed the head's dispatch plan
(``--plan``) so the suite's sweep hook is never re-run off the head.

Collection waits for the queue to drain via ``squeue`` polling; loading
the per-job result envelopes is the caller's job (backend-independent).

The Slurm client calls (``sbatch`` / ``squeue`` / ``scancel``) use plain
``subprocess.run`` rather than ``run_managed_process``: they are short,
synchronous probes that submit or poll and return immediately, not
long-lived simulation processes that need signal-forwarding / cleanup.
Each passes an explicit ``cwd`` per the engineering guidelines, since the
head process cwd is re-anchored per suite during a regression.
"""

import logging
import math
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tool_manifest import require as require_tool
from .argv import build_job_argv, test_job_argv
from .base import BuildJobSpec, DispatchBackend, JobHandle, TestJobSpec
from .progress import DispatchProgress, group_job_ids

logger = logging.getLogger(__name__)

# Queue states that mean "still occupying the queue". Anything else
# (COMPLETED/FAILED/TIMEOUT/CANCELLED...) has finished as far as the
# collector is concerned — the result envelope decides pass/fail.
_ACTIVE_STATES = "PD,R,S,CG,CF"

# squeue's reason for a job whose `afterok` dependency has already failed.
# Such a job is PENDING but will NEVER run, and since PD counts as "still in
# the queue" a head that waited on it would poll until killed.
#
# Jobs THIS backend submits are reaped by Slurm itself — see
# `_dependency_argv`, which passes --kill-on-invalid-dep=yes — so `wait_all`'s
# sweep over this reason is the fallback, not the primary mechanism: it covers
# a site that turns the flag back off via sbatch-args, and a Slurm that
# ignores it. Absent both, the job pends until the site's
# `kill_invalid_depend` reaps it, which is off by default (#358, #372).
_NEVER_SATISFIED = "DependencyNeverSatisfied"

# What each poll asks squeue for: id | reason | state | time-used | name.
# The first two are what `_reap_never_satisfied` has always needed; the
# rest are the progress line's running/pending split and its "longest
# running job" (#435). Parsing stays tolerant of a short line so a Slurm
# that renders fewer columns degrades to a plainer progress line rather
# than breaking the wait.
_SQUEUE_FORMAT = "%i|%r|%T|%M|%j"
_SQUEUE_RUNNING_STATE = "RUNNING"

# One element per manifest line, indexed by SLURM_ARRAY_TASK_ID. Lines
# are shlex-quoted, so eval reconstructs the exact argv. A missing line
# (short/rewritten manifest) fails the element loudly rather than exiting
# 0 with no envelope, which would surface as a misleading "produced no
# result (killed/crashed)" in the collector.
_ARRAY_SCRIPT = """#!/bin/bash
set -uo pipefail
cmd=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$1")
if [ -z "$cmd" ]; then
  echo "rb: no manifest line ${SLURM_ARRAY_TASK_ID} in $1" >&2
  exit 2
fi
eval "$cmd"
"""

_SACCT_FORMAT = "JobID,State,ElapsedRaw,TimelimitRaw,AllocCPUS,ReqMem,TotalCPU,MaxRSS"

# `MaxRSS` is a high-water mark over samples, so a job shorter than the
# sampling interval reports whatever the first sample caught — near zero.
# The stock `JobAcctGatherFrequency` is 30 s and dispatch exists to produce
# jobs far shorter than that, so right-sizing was reading peaks 17-27x below
# the truth and advising reservations from them (#365). Ask for the sampling
# the advice needs instead of inheriting the site default; one sample per
# second per job is cheap next to being wrong in the unsafe direction.
_ACCT_FREQ_OPT = "--acctg-freq"
_ACCT_FREQ_DEFAULT = f"{_ACCT_FREQ_OPT}=task=1"
_DEFAULT_ACCT_INTERVAL_S = 1.0


def _parse_mem_to_bytes(text: str) -> int | None:
    """Parse sacct memory strings like ``2948K`` / ``1.5G`` / ``4Gn``."""
    text = text.strip().rstrip("nc")  # legacy per-node/per-cpu suffixes
    if not text:
        return None
    scale = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    unit = text[-1].upper()
    if unit in scale:
        try:
            return int(float(text[:-1]) * scale[unit])
        except ValueError:
            return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_cpu_time_to_seconds(text: str) -> float | None:
    """Parse sacct TotalCPU ``[DD-]HH:MM:SS[.ms]`` / ``MM:SS[.ms]``."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return days * 86400 + seconds


def _parse_squeue_line(line: str) -> dict | None:
    """One ``_SQUEUE_FORMAT`` line as a record; ``None`` if it has no id.

    Tolerant of a line with fewer fields than asked for: the id and the
    reason are load-bearing (they decide what is still queued and what is
    doomed), the rest only decorate the progress line.
    """
    job_id, _, rest = line.partition("|")
    job_id = job_id.strip()
    if not job_id:
        return None
    fields = rest.split("|")

    def at(index: int) -> str:
        return fields[index].strip() if len(fields) > index else ""

    return {
        "id": job_id,
        "reason": at(0),
        "state": at(1),
        "time": at(2),
        "name": at(3),
    }


def _expand_squeue_id(
    job_id: str, handle_ids: Sequence[str] | None = None
) -> list[str]:
    """Handle ids one squeue id stands for.

    squeue speaks four shapes and only one of them is one job:
    ``1235`` (a non-array job, or a whole array before Slurm splits it),
    ``1235_3`` (one element), ``1235_[1-40]`` / ``1235_[1,3-5]`` (the
    still-pending elements of an array), and ``1235_[1-40%4]`` (the same
    with the concurrency throttle attached). Counting lines instead of
    expanding them is what made ``remaining`` a number about the queue
    rather than about the run.

    A bare base id with array handles is expanded **conservatively** to
    every handle sharing it: the alternative — assuming it means one job —
    would under-report a whole array as a single outstanding job.
    """
    base, sep, element = job_id.partition("_")
    if not sep:
        if handle_ids is None:
            return [job_id]
        matches = [h for h in handle_ids if h == base or h.startswith(f"{base}_")]
        return matches or [job_id]
    if not element.startswith("["):
        return [job_id]
    body = element.strip("[]")
    body = body.split("%", 1)[0]  # drop the --array=%N throttle suffix
    expanded = []
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        low, dash, high = part.partition("-")
        if dash and low.isdigit() and high.isdigit():
            expanded.extend(str(i) for i in range(int(low), int(high) + 1))
        else:
            expanded.append(part)
    return [f"{base}_{index}" for index in expanded] or [job_id]


def _task_sampling_interval(value: str) -> float | None:
    """Seconds between task samples in an ``--acctg-freq`` value.

    Accepts both forms Slurm takes: a bare interval (``30``) and the
    typed, comma-separated form (``task=5,energy=0``). Only the ``task``
    datatype samples memory, so the others are ignored.

    Returns ``None`` when the value says nothing about task sampling — an
    unparsable interval, or one naming only other datatypes — and
    ``math.inf`` when it explicitly *disables* task sampling (``task=0``).
    Those are different answers: "unknown" leaves the peak trusted, while
    "never sampled" must distrust every peak, and mapping the explicit
    disable onto the first is the one reading that cannot be right.
    """
    intervals = []
    for part in value.split(","):
        datatype, _, interval = part.rpartition("=")
        if datatype not in ("", "task"):
            continue
        try:
            intervals.append(float(interval))
        except ValueError:
            return None
    if not intervals:
        return None
    if min(intervals) <= 0:
        return math.inf
    return min(intervals)


class SlurmDispatchBackend(DispatchBackend):
    name = "slurm"

    def __init__(self, dispatch_cfg):
        # Fail with the manifest's install hint, not a raw FileNotFoundError
        # from the first subprocess.run, when the Slurm client is absent.
        require_tool("slurm")
        self.sbatch_args = list(dispatch_cfg.sbatch_args)
        self.poll_interval = dispatch_cfg.poll_interval
        # How often the wait says something, and how long it is willing to
        # wait at all (#435). Both default-safe: 60 s of console cadence and
        # an unbounded wait, i.e. today's behaviour plus a heartbeat.
        self.progress_interval = getattr(dispatch_cfg, "progress_interval", 60.0)
        self.max_wait = getattr(dispatch_cfg, "max_wait", None)
        self._acct_interval_s = self._resolve_accounting_frequency()

    def _resolve_accounting_frequency(self) -> float | None:
        """Request per-second task sampling, unless the user asked for a rate.

        Prepended rather than appended so it keeps the documented
        precedence — user ``sbatch-args`` are last and win — which also
        means a site that must not raise the rate can put its own
        ``--acctg-freq`` in ``sbatch-args`` and be obeyed.

        Returns the interval that will actually apply to the jobs this
        backend submits; right-sizing uses it to decide whether a job ran
        long enough to have been sampled at all.

        The presence check and the interval must be judged at the same
        granularity, or one flag disarms both guards at once:
        ``--acctg-freq=energy=30`` says nothing about task sampling, so
        deferring to it would leave tasks on the site default *and* report
        the interval as unknown — which reads as "no evidence the peak is
        untrustworthy", putting #365 straight back. A user value that
        yields no usable task interval is therefore reported at WARNING and
        the default is still requested, so the trust decision is visible
        rather than silently inverted.
        """
        for index, arg in enumerate(self.sbatch_args):
            if arg == _ACCT_FREQ_OPT:
                following = self.sbatch_args[index + 1 :]
                value = following[0] if following else ""
            elif arg.startswith(f"{_ACCT_FREQ_OPT}="):
                value = arg.split("=", 1)[1]
            else:
                continue
            interval = _task_sampling_interval(value)
            if interval is not None:
                return interval
            log_event(
                logger,
                logging.WARNING,
                "dispatch.accounting_frequency_unusable",
                backend=self.name,
                sbatch_arg=f"{_ACCT_FREQ_OPT} {value}".strip(),
                default=_ACCT_FREQ_DEFAULT,
            )
            break
        self.sbatch_args.insert(0, _ACCT_FREQ_DEFAULT)
        log_event(
            logger,
            logging.DEBUG,
            "dispatch.accounting_frequency_requested",
            backend=self.name,
            sbatch_arg=_ACCT_FREQ_DEFAULT,
        )
        return _DEFAULT_ACCT_INTERVAL_S

    def accounting_interval_s(self) -> float | None:
        return self._acct_interval_s

    @staticmethod
    def _cwd_of(handles: Sequence[JobHandle | None]) -> str | None:
        # Skip None handles for the same reason _base_ids does: cancel_all
        # must not be disarmed by a bad caller (#361).
        for h in handles:
            if h is not None:
                return h.spec.suite_dir
        return None

    def _reservation_argv(self, resources, *, job_name, chdir, log_path) -> list[str]:
        """Common sbatch reservation flags shared by build and sim jobs."""
        cmd = [
            "sbatch",
            "--parsable",
            f"--job-name={job_name}",
            f"--chdir={chdir}",
            # Always explicit: right-sizing needs a defined time limit,
            # and site partitions may default to UNLIMITED.
            f"--time={resources.time}",
            f"--cpus-per-task={resources.cpus}",
        ]
        if resources.mem is not None:
            cmd.append(f"--mem={resources.mem}")
        if log_path is not None:
            # stderr merges into --output when --error is not given.
            cmd.append(f"--output={log_path}")
        return cmd

    @staticmethod
    def _dependency_argv(dependency: str | None) -> list[str]:
        """The ``afterok`` gate, plus self-reaping if it can never be met.

        ``--kill-on-invalid-dep=yes`` makes **Slurm** remove the job the moment
        the dependency becomes unsatisfiable. Without it such a job sits
        ``PENDING`` with reason ``DependencyNeverSatisfied`` indefinitely unless
        the site sets ``kill_invalid_depend`` in ``SchedulerParameters`` (off by
        default), and the head is then the only thing that would clean it up —
        which is exactly what fails when the head is killed rather than
        interrupted, since ``cancel_all`` never runs. Asking Slurm to own the
        cleanup is the only form that survives a ``SIGKILL``ed head.
        """
        if dependency is None:
            return []
        return [f"--dependency=afterok:{dependency}", "--kill-on-invalid-dep=yes"]

    def submit_build(self, spec: BuildJobSpec) -> JobHandle:
        cmd = self._reservation_argv(
            spec.resources,
            job_name="rb-build",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(build_job_argv(spec))]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=spec.suite_dir)
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch failed for build job (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        job_id = proc.stdout.strip().split(";")[0]
        if not job_id:
            raise FatalRtlBuddyError("sbatch returned no job id for build job")
        log_event(
            logger,
            logging.INFO,
            "dispatch.build_submitted",
            backend=self.name,
            job_id=job_id,
            suite_dir=spec.suite_dir,
            time=spec.resources.time,
            cpus=spec.resources.cpus,
            mem=spec.resources.mem,
        )
        return JobHandle(job_id=job_id, spec=spec)

    @staticmethod
    def _begin_argv(delay_sec: float) -> list[str]:
        """The retry backoff, served by Slurm rather than by the head (#405).

        ``--begin=now+<n>`` (bare units are seconds) leaves the job
        ``PENDING`` with reason ``BeginTime`` until then: it holds no
        allocation while it waits, which is the whole point — the license
        pool that killed the first attempt is not made freer by a second
        allocation sitting on it. Sub-second delays round to whole seconds
        because that is the only granularity Slurm takes; a delay that
        rounds to 0 emits no flag at all rather than an inert ``now+0``.
        """
        seconds = int(round(delay_sec)) if delay_sec and delay_sec > 0 else 0
        return [f"--begin=now+{seconds}"] if seconds > 0 else []

    def _sbatch_argv(
        self, spec: TestJobSpec, dependency: str | None, delay_sec: float = 0.0
    ) -> list[str]:
        cmd = self._reservation_argv(
            spec.resources,
            job_name=f"rb:{spec.display_name()}",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        # afterok: the sim only runs if the shared build succeeded.
        cmd += self._dependency_argv(dependency)
        cmd += self._begin_argv(delay_sec)
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(test_job_argv(spec))]
        return cmd

    def submit(
        self,
        spec: TestJobSpec,
        *,
        dependency: str | None = None,
        delay_sec: float = 0.0,
    ) -> JobHandle:
        argv = self._sbatch_argv(spec, dependency, delay_sec)
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=spec.suite_dir)
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch failed for {spec.display_name()} "
                f"(rc={proc.returncode}): {proc.stderr.strip()}"
            )
        # --parsable prints "jobid" or "jobid;cluster".
        job_id = proc.stdout.strip().split(";")[0]
        if not job_id:
            raise FatalRtlBuddyError(
                f"sbatch returned no job id for {spec.display_name()}"
            )
        log_event(
            logger,
            logging.INFO,
            "dispatch.submitted",
            backend=self.name,
            job_id=job_id,
            test=spec.test_name,
            run_id=spec.run_id,
            dependency=dependency,
            begin_delay_sec=delay_sec or None,
            time=spec.resources.time,
            cpus=spec.resources.cpus,
            mem=spec.resources.mem,
        )
        return JobHandle(job_id=job_id, spec=spec)

    def submit_array(
        self,
        specs: list[TestJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None = None,
        dependency: str | None = None,
    ) -> list[JobHandle]:
        if len(specs) <= 1:
            return [self.submit(spec, dependency=dependency) for spec in specs]

        array_dir = Path(array_dir)
        array_dir.mkdir(parents=True, exist_ok=True)
        manifest = array_dir / "manifest.txt"
        manifest.write_text(
            "".join(shlex.join(test_job_argv(spec)) + "\n" for spec in specs)
        )
        script = array_dir / "array.sh"
        script.write_text(_ARRAY_SCRIPT)
        script.chmod(0o755)
        # Element logs are deterministic (%a = 1-based manifest line), so
        # collection can point at the exact log on failure.
        for i, spec in enumerate(specs, start=1):
            spec.log_path = array_dir / f"slurm-{i}.log"

        array_range = f"1-{len(specs)}"
        if max_parallel is not None and max_parallel < len(specs):
            array_range += f"%{max_parallel}"
        resources = specs[0].resources
        cmd = [
            "sbatch",
            "--parsable",
            f"--array={array_range}",
            f"--job-name=rb:{specs[0].test_name}+{len(specs) - 1}",
            f"--chdir={specs[0].suite_dir}",
            f"--time={resources.time}",
            f"--cpus-per-task={resources.cpus}",
        ]
        if resources.mem is not None:
            cmd.append(f"--mem={resources.mem}")
        cmd.append(f"--output={array_dir}/slurm-%a.log")
        # afterok: array elements only run if the shared build succeeded.
        cmd += self._dependency_argv(dependency)
        cmd += self.sbatch_args
        cmd += [str(script), str(manifest)]

        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=specs[0].suite_dir
        )
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch array submit failed ({len(specs)} jobs, "
                f"rc={proc.returncode}): {proc.stderr.strip()}"
            )
        base_id = proc.stdout.strip().split(";")[0]
        if not base_id:
            raise FatalRtlBuddyError("sbatch returned no job id for array submit")
        log_event(
            logger,
            logging.INFO,
            "dispatch.array_submitted",
            backend=self.name,
            job_id=base_id,
            jobs=len(specs),
            array=array_range,
            time=resources.time,
            cpus=resources.cpus,
            mem=resources.mem,
        )
        return [
            JobHandle(job_id=f"{base_id}_{i}", spec=spec)
            for i, spec in enumerate(specs, start=1)
        ]

    @staticmethod
    def _base_ids(handles: Sequence[JobHandle | None]) -> list[str]:
        """Unique base job ids — one per array, not per element.

        Skips ``None`` handles: ``cancel_all`` is the last thing standing
        between a head-side failure and an orphaned fleet, so it must not be
        disarmed by a caller that let a ``None`` (e.g. a zero-test suite's
        absent build handle, #361) into the list.
        """
        seen: dict[str, None] = {}
        for h in handles:
            if h is None:
                continue
            seen.setdefault(h.job_id.split("_")[0], None)
        return list(seen)

    def _reap_never_satisfied(self, lines, *, cwd) -> list[dict]:
        """Split queued jobs into those still coming and those already dead.

        A job whose ``afterok`` build failed is reported PENDING with reason
        ``DependencyNeverSatisfied``. :meth:`_dependency_argv` asks Slurm to
        reap those itself, so normally none are seen here; this is the fallback
        for a site that disabled that flag through ``sbatch-args`` or a Slurm
        that ignores it, where the job would otherwise sit until the site's
        ``kill_invalid_depend`` (off by default) removed it. Cancel them so
        they leave the queue instead of being waited on; collection then
        reports them as producing no result, which is exactly what happened.

        Returns the surviving **records** (see :func:`_parse_squeue_line`),
        not just their ids: the caller needs each survivor's state and
        elapsed time for the progress line, and parsing the same output
        twice invites the two parses to disagree.
        """
        remaining, doomed = [], []
        for line in lines:
            record = _parse_squeue_line(line)
            if record is None:
                continue
            # Substring, not equality: %r is unpadded today, but a site whose
            # Slurm renders the reason with surrounding text must not silently
            # fall back into the infinite poll this method exists to remove.
            # Matched against the reason column alone, so a job *named* after
            # the reason cannot be reaped by mistake.
            if _NEVER_SATISFIED in record["reason"]:
                doomed.append(record["id"])
            else:
                remaining.append(record)
        if doomed:
            log_event(
                logger,
                logging.WARNING,
                "dispatch.dependency_never_satisfied",
                backend=self.name,
                jobs=doomed,
            )
            # Cancel by base id: one scancel clears a whole pending array.
            base_ids = list(dict.fromkeys(j.split("_")[0] for j in doomed))
            proc = subprocess.run(
                ["scancel", *base_ids], capture_output=True, text=True, cwd=cwd
            )
            if proc.returncode != 0:
                # These jobs are already out of `remaining`, so the run will
                # finish and leave them queued. Say so — a transient
                # slurmctld failure here is only recoverable by hand.
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.cancel_failed",
                    backend=self.name,
                    jobs=base_ids,
                    returncode=proc.returncode,
                    error=proc.stderr.strip()[:200],
                )
        return remaining

    def _outstanding(self, records, handle_ids):
        """Queue records → ({outstanding handle id: state}, longest running).

        The queue speaks in lines and the run is counted in jobs, so every
        record is expanded to the handle ids it covers (an array pending as
        ``9_[1-3]`` is three jobs, not one). A record whose expansion names
        no known handle still counts as itself: dropping it would let the
        wait end while that job is queued, and being conservative here
        costs at most an over-count for one poll.
        """
        known = set(handle_ids)
        outstanding: dict[str, str] = {}
        longest = None
        for record in records:
            running = record["state"] == _SQUEUE_RUNNING_STATE
            expanded = [
                job_id
                for job_id in _expand_squeue_id(record["id"], handle_ids)
                if job_id in known
            ] or [record["id"]]
            for job_id in expanded:
                outstanding[job_id] = "running" if running else "pending"
            if running:
                # %M is the same [DD-]HH:MM:SS shape sacct's TotalCPU uses.
                elapsed = _parse_cpu_time_to_seconds(record["time"])
                if elapsed is not None and (longest is None or elapsed > longest[1]):
                    longest = (record["name"] or record["id"], elapsed)
        return outstanding, longest

    def wait_all(self, handles: list[JobHandle], *, extra_wait: float = 0.0) -> None:
        if not handles:
            return
        ids = ",".join(self._base_ids(handles))
        cwd = self._cwd_of(handles)
        handle_ids = [h.job_id for h in handles if h is not None]
        progress = DispatchProgress(
            handles,
            backend=self.name,
            interval=self.progress_interval,
            # A job held on `--begin` is PENDING for the whole backoff and
            # squeue reports it outstanding, so the deadline must allow for
            # the hold the head itself asked for (#405).
            max_wait=(
                None if self.max_wait is None else self.max_wait + max(0.0, extra_wait)
            ),
            # Resolved here rather than taken as a default, so the clock the
            # reporter reads is the same one this module sleeps against.
            clock=time.monotonic,
        )
        while True:
            proc = subprocess.run(
                [
                    "squeue",
                    "--noheader",
                    f"--format={_SQUEUE_FORMAT}",
                    f"--states={_ACTIVE_STATES}",
                    "--jobs",
                    ids,
                ],
                capture_output=True,
                text=True,
                cwd=cwd,
            )
            # squeue errors ("Invalid job id specified") once every job
            # has aged out of the queue — that is completion, not failure.
            records = (
                self._reap_never_satisfied(proc.stdout.splitlines(), cwd=cwd)
                if proc.returncode == 0
                else []
            )
            if not records:
                progress.finish()
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(handles),
                )
                return
            states, longest = self._outstanding(records, handle_ids)
            progress.observe(states.keys(), states=states, longest=longest)
            time.sleep(self.poll_interval)

    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        if not handles:
            return
        # Base ids: cancelling an array id cancels every element.
        subprocess.run(
            ["scancel", *self._base_ids(handles)],
            capture_output=True,
            text=True,
            cwd=self._cwd_of(handles),
        )
        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(handles),
            # An interrupted or failed run must leave the ids on the console:
            # they are the only route to `squeue`/`sacct` afterwards (#435).
            job_ids=group_job_ids(h.job_id for h in handles if h is not None),
        )

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Reserved-vs-used per job from ``sacct``, keyed by handle job id.

        Queries WITHOUT ``-X``: ``MaxRSS``/``TotalCPU`` only populate on
        step rows (``.batch`` etc.), never the allocation row — usage is
        folded up to its parent job. Values per job:
        ``state``, ``elapsed_s``, ``timelimit_s`` (TimelimitRaw is in
        MINUTES; normalized here), ``alloc_cpus``, ``req_mem_bytes``,
        ``total_cpu_s``, ``max_rss_bytes``. Missing accounting (no
        slurmdbd) returns ``{}`` and right-sizing degrades gracefully.
        """
        if not handles:
            return {}
        # Telemetry is strictly additive — no failure mode of it may fail a
        # run whose jobs have all completed. sacct may be absent (client
        # packaging varies; sbatch present does not guarantee sacct) or wedged
        # against a slow slurmdbd, so guard both and time-box the call.
        try:
            proc = subprocess.run(
                [
                    "sacct",
                    "--parsable2",
                    "--noheader",
                    f"--format={_SACCT_FORMAT}",
                    "--jobs",
                    ",".join(self._base_ids(handles)),
                ],
                capture_output=True,
                text=True,
                cwd=self._cwd_of(handles),
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log_event(
                logger,
                logging.INFO,
                "dispatch.telemetry_unavailable",
                backend=self.name,
                error=str(e)[:200],
            )
            return {}
        if proc.returncode != 0:
            log_event(
                logger,
                logging.INFO,
                "dispatch.telemetry_unavailable",
                backend=self.name,
                error=proc.stderr.strip()[:200],
            )
            return {}

        wanted = {h.job_id for h in handles}
        telemetry: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            fields = line.split("|")
            if len(fields) != len(_SACCT_FORMAT.split(",")):
                continue
            job_id, state, elapsed, limit, cpus, req_mem, total_cpu, max_rss = fields
            base = job_id.split(".")[0]
            if base not in wanted:
                continue
            entry = telemetry.setdefault(base, {})
            if "." not in job_id:
                # Allocation row: state + reservation-side numbers.
                entry["state"] = state
                try:
                    entry["elapsed_s"] = int(elapsed)
                except ValueError:
                    pass
                try:
                    # sacct's TimelimitRaw is minutes, unlike ElapsedRaw.
                    entry["timelimit_s"] = int(limit) * 60
                except ValueError:
                    pass
                try:
                    entry["alloc_cpus"] = int(cpus)
                except ValueError:
                    pass
                if (req_mem_bytes := _parse_mem_to_bytes(req_mem)) is not None:
                    entry["req_mem_bytes"] = req_mem_bytes
            else:
                # Step rows. TotalCPU is per step, so a job's CPU time is the
                # SUM over steps (.batch + .extern + any srun steps) — max
                # would under-report once a hook/builder uses srun. MaxRSS is
                # a high-water mark and folds with max.
                if (cpu_s := _parse_cpu_time_to_seconds(total_cpu)) is not None:
                    entry["total_cpu_s"] = entry.get("total_cpu_s", 0.0) + cpu_s
                if (rss := _parse_mem_to_bytes(max_rss)) is not None:
                    entry["max_rss_bytes"] = max(entry.get("max_rss_bytes", 0), rss)
        return telemetry
