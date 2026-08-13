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

    def _sbatch_argv(self, spec: TestJobSpec, dependency: str | None) -> list[str]:
        cmd = self._reservation_argv(
            spec.resources,
            job_name=f"rb:{spec.display_name()}",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        # afterok: the sim only runs if the shared build succeeded.
        cmd += self._dependency_argv(dependency)
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(test_job_argv(spec))]
        return cmd

    def submit(self, spec: TestJobSpec, *, dependency: str | None = None) -> JobHandle:
        argv = self._sbatch_argv(spec, dependency)
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

    def _reap_never_satisfied(self, lines, *, cwd) -> list[str]:
        """Split queued jobs into those still coming and those already dead.

        A job whose ``afterok`` build failed is reported PENDING with reason
        ``DependencyNeverSatisfied``. :meth:`_dependency_argv` asks Slurm to
        reap those itself, so normally none are seen here; this is the fallback
        for a site that disabled that flag through ``sbatch-args`` or a Slurm
        that ignores it, where the job would otherwise sit until the site's
        ``kill_invalid_depend`` (off by default) removed it. Cancel them so
        they leave the queue instead of being waited on; collection then
        reports them as producing no result, which is exactly what happened.
        """
        remaining, doomed = [], []
        for line in lines:
            job_id, _, reason = line.partition("|")
            job_id = job_id.strip()
            if not job_id:
                continue
            # Substring, not equality: %r is unpadded today, but a site whose
            # Slurm renders the reason with surrounding text must not silently
            # fall back into the infinite poll this method exists to remove.
            if _NEVER_SATISFIED in reason:
                doomed.append(job_id)
            else:
                remaining.append(job_id)
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

    def wait_all(self, handles: list[JobHandle]) -> None:
        if not handles:
            return
        ids = ",".join(self._base_ids(handles))
        cwd = self._cwd_of(handles)
        while True:
            proc = subprocess.run(
                [
                    "squeue",
                    "--noheader",
                    "--format=%i|%r",
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
            remaining = (
                self._reap_never_satisfied(proc.stdout.splitlines(), cwd=cwd)
                if proc.returncode == 0
                else []
            )
            if not remaining:
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(handles),
                )
                return
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.waiting",
                backend=self.name,
                remaining=len(remaining),
                total=len(handles),
            )
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
