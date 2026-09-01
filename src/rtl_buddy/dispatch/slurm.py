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
import os
import re
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

_SACCT_FORMAT = (
    "JobID,State,ElapsedRaw,TimelimitRaw,AllocCPUS,ReqCPUS,ReqMem,TotalCPU,MaxRSS"
)

# `AllocCPUS` is what the scheduler handed out, which is not what the
# reservation asked for. A partition with `SelectTypeParameters=NONE` on
# nodes with `ThreadsPerCore=2` allocates whole cores, so `--cpus-per-task=1`
# comes back as `AllocCPUS=2` — and cpu efficiency measured against it caps a
# single-threaded job at 0.5, firing the default over-threshold on every test
# forever (#505). `ReqCPUS` is the number the project's YAML controls, so it
# is carried alongside and right-sizing ratios against it; the allocated
# figure stays, because it is what `squeue`/`sacct` show.

# `scontrol show config` renders one `Key = Value` per line, padded. The
# value that matters here is the cluster's job-array ceiling (#509).
_MAX_ARRAY_SIZE_RE = re.compile(r"^MaxArraySize\s*=\s*(\d+)\s*$", re.MULTILINE)
# ...and the SECOND ceiling, which a cluster may set below it:
# `SchedulerParameters=max_array_tasks=N`. slurm.conf(5) defines it as "the
# maximum number of tasks that be included in a job array", defaulting to
# MaxArraySize — a COUNT, inclusive, unlike MaxArraySize's exclusive index
# bound. Its own example is the whole trap: max_array_tasks=1000 with
# MaxArraySize=100001 permits task index 100000 but only 1000 tasks in one
# array. `scontrol show config` keeps reporting the larger MaxArraySize, so
# a slice sized from that alone is refused (#509 review).
_SCHEDULER_PARAMS_RE = re.compile(r"^SchedulerParameters\s*=\s*(.*)$", re.MULTILINE)
_MAX_ARRAY_TASKS_RE = re.compile(r"\bmax_array_tasks\s*=\s*(\d+)\b")


def _max_array_tasks(config_text: str) -> int | None:
    """``max_array_tasks`` from a ``scontrol show config`` dump, if set.

    Read out of the ``SchedulerParameters`` line rather than the whole
    dump, so nothing else that happens to mention the key can be taken for
    the cluster's setting.
    """
    params = _SCHEDULER_PARAMS_RE.search(config_text)
    if params is None:
        return None
    match = _MAX_ARRAY_TASKS_RE.search(params.group(1))
    return int(match.group(1)) if match is not None else None


# The probe is a courtesy, not a dependency: a slurmctld that is slow or
# unreachable must cost a bounded wait and then leave chunking off, never
# hang the head before it has submitted anything.
_SCONTROL_TIMEOUT_S = 30
# Options by which `sbatch-args` sends the jobs to another cluster. The
# probe has to follow them: `scontrol show config` with no cluster reads
# the LOCAL slurmctld, whose MaxArraySize says nothing about the cluster
# the arrays are actually submitted to (#509 review).
# sbatch resolves any UNAMBIGUOUS long-option prefix (GNU getopt_long), so
# a selector may legitimately be written short. `--clusters` has no such
# abbreviation: every proper prefix of it, `--cl` through `--cluster`, is
# also a prefix of `--cluster-constraint` — sbatch's only other `--cl`
# option — so getopt_long calls them ambiguous and sbatch exits rather than
# submitting. (The rest of its `c` options diverge at `--c` already:
# --chdir, --comment, --constraint, --container*, --contiguous, --core-spec,
# --cores-per-socket, --cpu-freq, --cpus-per-*.) Matching a prefix here
# would therefore read a selection out of a command line that cannot run,
# and `--cluster-constraint=<list>` — a FEATURE list, not a cluster — is
# exactly what a loose prefix match would swallow.
#
# `--cluster` singular is kept because the cost is asymmetric: several
# Slurm clients register it explicitly as an alias of `--clusters`, and if
# sbatch is not one of them it is one of those ambiguous prefixes, so the
# submit fails at sbatch before this probe's answer could matter. Missing a
# real selection, by contrast, silently sizes slices from the wrong
# cluster's limit — the bug this probe exists to avoid.
_CLUSTER_OPTS = ("-M", "--clusters", "--cluster")
# Slurm documents this as the equivalent of `--clusters`, with the command
# line winning — so `sbatch-args` is consulted first and this only fills in
# for a site that exports the selection instead of writing it (#509 review).
_CLUSTER_ENV = "SBATCH_CLUSTERS"
# The reserved value: query EVERY registered cluster and submit to whichever
# can start first. Like a comma-separated list it names no single cluster.
_CLUSTER_ALL = "all"


def _is_multi_cluster(value: str) -> bool:
    """Does this selection name more than one cluster?

    Both spellings Slurm gives for "let the scheduler choose": an explicit
    ``a,b`` list and the reserved ``all``. Which cluster runs the array is
    then decided at submit, so no single ``MaxArraySize`` describes it —
    and ``scontrol -M all show config`` answers with one config block per
    cluster, where a first-match regex would silently pick a limit
    belonging to whichever cluster sorted first.
    """
    return "," in value or value.strip().lower() == _CLUSTER_ALL


def _selected_cluster(sbatch_args: Sequence[str]) -> str | None:
    """The cluster ``sbatch-args`` submits to, or ``None`` for the local one.

    All four spellings Slurm takes: ``-M name``, ``-Mname``,
    ``--clusters=name`` and ``--clusters name``, plus the ``--cluster``
    singular (see :data:`_CLUSTER_OPTS`). The LAST occurrence wins, which
    is how sbatch itself resolves a repeated option — so a project
    appending an override to a shared list gets the same answer here as at
    submit.

    Shorter abbreviations are NOT accepted, and that is not an omission:
    ``--clusters`` has no unambiguous prefix, because ``--cluster-constraint``
    shares every one of them. Reading ``--clus=x`` as a selection would be
    reading it out of a command line sbatch refuses to run, and it is the
    same match that would mistake a ``--cluster-constraint`` feature list
    for a cluster name.

    The value is returned verbatim, comma-separated multi-cluster lists
    included: deciding what to do about those belongs to the caller, which
    is the only place that can say what it costs.
    """
    selected = None
    for index, arg in enumerate(sbatch_args):
        if arg in _CLUSTER_OPTS:
            following = sbatch_args[index + 1 :]
            if following:
                selected = following[0]
        elif arg.startswith(("--clusters=", "--cluster=")):
            selected = arg.split("=", 1)[1]
        elif arg.startswith("-M") and len(arg) > 2:
            selected = arg[2:]
    return selected or None


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


def _parsable_submission(stdout: str) -> tuple[str, str | None]:
    """``(job id, cluster)`` from ``sbatch --parsable`` output.

    It prints ``jobid`` for a local submission and ``jobid;cluster`` for one
    the multi-cluster path accepted elsewhere. The cluster half used to be
    split off and dropped, which is fine for identifying the job and wrong
    for acting on it: ids are unique per cluster, so a later ``scancel``
    issued without it hits the LOCAL cluster's job of that number —
    cancelling nothing, or something else entirely (#509 review).
    """
    job_id, _, cluster = stdout.strip().partition(";")
    return job_id.strip(), (cluster.strip() or None)


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
        # The cluster's MaxArraySize, when the project pinned one (#509).
        # Resolution itself is deferred to the first array submit — see
        # _max_elements_per_array — so constructing a backend never shells
        # out, and a run with no array never probes at all.
        self.max_array_size = getattr(dispatch_cfg, "max_array_size", None)
        # The second ceiling, for a site that cannot run `scontrol` at all:
        # `SchedulerParameters=max_array_tasks` caps the tasks in ONE array
        # independently of MaxArraySize, so it needs its own field — pinning
        # a smaller max-array-size instead would state the wrong ceiling and
        # still be wrong the moment the real MaxArraySize matters (#509).
        self.max_array_tasks = getattr(dispatch_cfg, "max_array_tasks", None)
        # ...cached per cluster selection: the answer is a property of the
        # cluster probed, not of this process. The selection itself is
        # resolved on demand (see `cluster`), not frozen here, because
        # $SBATCH_CLUSTERS is part of it and is read when the probe runs.
        # {selection: (elements per array or None, where it came from)}.
        self._elements_per_array_by_cluster: dict[
            str | None, tuple[int | None, str | None]
        ] = {}
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

    @property
    def effective_sbatch_args(self) -> list:
        """What every submission of this backend really appends.

        The list this backend was constructed with, plus the
        ``--acctg-freq`` it prepends — i.e. the passthrough as submitted,
        which is what right-sizing must judge a job's cpu request by
        (#505 review).
        """
        return self.sbatch_args

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
        job_id, cluster = self._accepted_on(proc.stdout)
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
            # The cpus above are already scaled by this (#495); logging both
            # is what makes a 16-CPU build job's reservation legible.
            parallel=spec.parallel,
            cluster=cluster,
        )
        return JobHandle(job_id=job_id, spec=spec, cluster=cluster)

    def _accepted_on(self, stdout: str) -> tuple[str, str | None]:
        """``(job id, cluster)`` for a submission this backend just made.

        Falls back to the cluster this backend SELECTED when sbatch names
        none: a Slurm that omits the ``;cluster`` half still put the job
        where ``-M`` pointed, and cancelling it locally would be the same
        miss. The selection is ``None`` for a multi-cluster one (``a,b``,
        ``all``) — precisely the case where sbatch's own answer is the only
        way to know where the job landed, which is why it is preferred.
        """
        job_id, cluster = _parsable_submission(stdout)
        return job_id, cluster or self.cluster

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
        job_id, cluster = self._accepted_on(proc.stdout)
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
            cluster=cluster,
        )
        return JobHandle(job_id=job_id, spec=spec, cluster=cluster)

    def _cluster_selection(self) -> str | None:
        """Cluster selection as written, or ``None`` for the local cluster.

        ``sbatch-args`` first, then ``$SBATCH_CLUSTERS``: Slurm documents
        the variable as the equivalent of ``--clusters`` with the command
        line taking precedence, and this backend passes ``sbatch-args``
        verbatim to sbatch, so the same precedence has to hold here or the
        probe would describe a different cluster than the submit. Read at
        probe time rather than frozen at construction, since the
        environment a head runs in is not this object's to snapshot. An
        empty or whitespace-only variable selects nothing, exactly as it
        does for sbatch.

        Multi-cluster values (``a,b``, ``all``) come back verbatim — they
        are still what the user wrote, and the diagnostics name them.
        """
        selected = _selected_cluster(self.sbatch_args)
        if selected is not None:
            return selected
        return (os.environ.get(_CLUSTER_ENV) or "").strip() or None

    @property
    def cluster(self) -> str | None:
        """The ONE cluster this backend addresses, or ``None``.

        ``None`` covers three cases that a per-cluster scheduler query must
        treat alike: no selection (the local cluster), a comma-separated
        list, and the reserved ``all``. In the latter two Slurm picks the
        cluster at submit, so there is no single name any probe could be
        qualified with — a caller that appended ``-M <this>`` to a query
        would be asking about a cluster nothing was necessarily submitted
        to. Read :meth:`_cluster_selection` for what the user actually
        wrote.
        """
        selection = self._cluster_selection()
        if selection is None or _is_multi_cluster(selection):
            return None
        return selection

    def _max_elements_per_array(self, *, cwd: str | None) -> int | None:
        """Elements one array may hold, resolved once per backend instance.

        Slurm's ``MaxArraySize`` bounds the task *index* exclusively — "the
        maximum job array task index value will be one less than
        MaxArraySize to allow for an index value of zero" (slurm.conf(5)) —
        and this backend's manifests are 1-based, since ``%a`` is a manifest
        line number. The largest array it may submit is therefore
        ``1-(MaxArraySize-1)``: ``MaxArraySize - 1`` elements, not
        ``MaxArraySize``.

        ``cfg-dispatch.max-array-size`` wins where it is set (a submit host
        with no working ``scontrol``, or a site that wants a finer split);
        otherwise the value is read from ``scontrol show config``.
        ``None`` means "unknown" — submit the group whole, exactly as
        before #509 — because guessing a ceiling would split groups on
        clusters that never needed it.
        """
        selection = self._cluster_selection()
        if selection not in self._elements_per_array_by_cluster:
            self._elements_per_array_by_cluster[selection] = self._probe_max_elements(
                cwd=cwd
            )
        return self._elements_per_array_by_cluster[selection][0]

    def _probe_max_elements(self, *, cwd: str | None) -> tuple[int | None, str | None]:
        """Effective elements-per-array, layering config over the probe.

        TWO ceilings decide it and they are configured independently, so
        they are layered independently: ``cfg-dispatch.max-array-size``
        over the probed ``MaxArraySize``, ``cfg-dispatch.max-array-tasks``
        over the probed ``max_array_tasks``, and the slice is the smaller
        of what each layer yields. A pinned ``max-array-size`` therefore no
        longer hides a cluster's task cap: it wins for its OWN ceiling and
        the probe still supplies the other (#509 review). The probe is
        skipped only when it can add nothing — both values pinned — or when
        there is no single cluster to ask.

        Returns ``(elements, source)`` where ``source`` names where the
        GOVERNING value came from, so the submit-failure hint can say which
        knob produced the slice it used.
        """
        selection = self._cluster_selection()
        ambiguous = selection is not None and _is_multi_cluster(selection)
        probed_size = probed_tasks = None
        reason = None
        if self.max_array_size is None or self.max_array_tasks is None:
            if ambiguous:
                # `--clusters=a,b` (and the reserved `all`) let Slurm pick
                # whichever can run the job soonest, and the decision is
                # made at submit. Probing one of them would pin a limit the
                # others may not have — and `-M all` answers with several
                # config blocks, so the regex would take whichever came
                # first. The honest answer is "unknown", recovered by the
                # pinned config values.
                reason = (
                    f"the cluster selection ({selection}) names several "
                    "clusters; which one runs the array is decided at "
                    "submit, so no single MaxArraySize applies"
                )
            else:
                probed_size, probed_tasks, reason = self._scontrol_ceilings(
                    cwd=cwd, selection=selection
                )

        size = self.max_array_size if self.max_array_size is not None else probed_size
        tasks = (
            self.max_array_tasks if self.max_array_tasks is not None else probed_tasks
        )
        if size is None:
            self._log_unknown(reason or "no MaxArraySize available")
            return None, None

        # MaxArraySize bounds the INDEX exclusively; max_array_tasks counts
        # the tasks, inclusively. The smaller of the two is what an array
        # may actually hold.
        limit = size - 1
        source = "config" if self.max_array_size is not None else "scontrol"
        if tasks is not None and 1 <= tasks < limit:
            limit = tasks
            source = "config" if self.max_array_tasks is not None else "scontrol"
        log_event(
            logger,
            logging.DEBUG,
            "dispatch.max_array_size",
            backend=self.name,
            max_array_size=size,
            # Emitted on every source path, so a reader never has to guess
            # whether a task cap was consulted. Absent only when no cap is
            # known — neither configured nor probed — since `log_event`
            # drops `None` fields for every event in this package.
            max_array_tasks=tasks,
            max_elements=limit,
            source=source,
            cluster=selection,
        )
        return limit, source

    def _scontrol_ceilings(
        self, *, cwd: str | None, selection: str | None
    ) -> tuple[int | None, int | None, str | None]:
        """``(MaxArraySize, max_array_tasks, why not)`` from one scontrol call.

        Best effort by construction: every failure mode returns ``None``
        ceilings and a reason string rather than raising, because a limit
        that cannot be read must cost the run nothing more than the
        chunking it disables.
        """
        cluster_argv = [] if selection is None else ["-M", selection]
        try:
            proc = subprocess.run(
                ["scontrol", *cluster_argv, "show", "config"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=_SCONTROL_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return None, None, str(e)[:200]
        if proc.returncode != 0:
            return (
                None,
                None,
                (
                    proc.stderr.strip()
                    or f"`scontrol show config` failed (rc={proc.returncode})"
                )[:200],
            )
        match = _MAX_ARRAY_SIZE_RE.search(proc.stdout)
        value = int(match.group(1)) if match is not None else 0
        tasks = _max_array_tasks(proc.stdout)
        if value < 2:
            # A cluster reporting MaxArraySize < 2 has arrays disabled; no
            # slice size would submit, so treat that as unknown and let
            # sbatch give the authoritative refusal.
            return (
                None,
                tasks,
                (
                    proc.stderr.strip()
                    or "no usable MaxArraySize in `scontrol show config` (rc=0)"
                )[:200],
            )
        return value, tasks, None

    def _log_unknown(self, reason: str) -> None:
        log_event(
            logger,
            logging.INFO,
            "dispatch.max_array_size_unknown",
            backend=self.name,
            error=reason,
            # As written, not the resolved single cluster: a multi-cluster
            # selection resolves to None and naming it is the diagnosis.
            cluster=self._cluster_selection(),
            # An oversized group is what this probe exists to split, so say
            # how to get chunking back when the cluster cannot be asked.
            hint="set cfg-dispatch.max-array-size to split oversized groups",
        )

    def _array_limit_hint(self, stderr: str) -> str:
        """What to do about a rejected array, for a failed array submit.

        ``Batch job submission failed: Invalid job array specification`` is
        what sbatch answers an array the cluster will not take, and the
        recovery is always the same knob — but the reason differs, so the
        sentence does. The probe's own diagnostic is INFO, which a
        default-verbosity console never shows, while THIS message is the one
        that fails the run in front of the user (#509).

        Gated on the wording, because a hint on every failed submit is noise
        that buries the real recovery action: an invalid account, partition
        or QoS is rejected in its own words and has nothing to do with array
        size. Matched case-insensitively on "job array" rather than on the
        full sentence, so a Slurm that words the rest of it differently
        still gets the hint.

        With the limit KNOWN the array was already within everything the
        probe could see — ``MaxArraySize`` and ``max_array_tasks`` both —
        so the cluster is enforcing something it did not report (a site
        patch, a newer limit, an association ceiling). Saying "the limit
        could not be read" there would be false, and saying nothing would
        leave the one actionable knob unnamed; the variant names the
        effective limit, where it came from, and the override (#509
        review).
        """
        if "job array" not in stderr.lower():
            return ""
        limit, source = self._elements_per_array_by_cluster.get(
            self._cluster_selection(), (None, None)
        )
        if limit is None:
            return (
                "; the cluster's MaxArraySize could not be read, so this group "
                "was submitted as one array — set cfg-dispatch.max-array-size to "
                "let rb split a group larger than that limit"
            )
        return (
            f"; rb sliced this group at {limit} element(s) per array, the limit "
            f"it read from {source}, and the cluster refused it anyway — lower "
            "cfg-dispatch.max-array-size to pin a smaller one"
        )

    def submit_array(
        self,
        specs: list[TestJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None = None,
        dependency: str | None = None,
    ) -> list[JobHandle]:
        """Submit one resource group, split across arrays if it must be (#509).

        A group larger than the cluster's ``MaxArraySize`` is not a legal
        ``--array=1-N``: sbatch refuses it outright, which used to fail the
        whole run at the first oversized group. It is submitted as several
        arrays instead, each with its own manifest, and the handles are
        returned concatenated in spec order so collection, cancellation and
        the right-sizing table still see one logical group.
        """
        if len(specs) <= 1:
            return [self.submit(spec, dependency=dependency) for spec in specs]

        array_dir = Path(array_dir)
        limit = self._max_elements_per_array(cwd=specs[0].suite_dir)
        if limit is None or len(specs) <= limit:
            slices = [specs]
        else:
            slices = [specs[i : i + limit] for i in range(0, len(specs), limit)]

        handles: list[JobHandle] = []
        for index, slice_specs in enumerate(slices, start=1):
            # One subdirectory per slice when chunked, so `%a` keeps mapping
            # 1:1 onto a manifest line and `slurm-%a.log` cannot collide
            # between slices. A group that fits in one array keeps exactly
            # today's layout — no `slice-1/` — so unchunked artefact paths
            # do not move.
            slice_dir = array_dir if len(slices) == 1 else array_dir / f"slice-{index}"
            try:
                handles += self._submit_one_array(
                    slice_specs,
                    array_dir=slice_dir,
                    max_parallel=max_parallel,
                    dependency=dependency,
                    slice_index=index,
                    slice_count=len(slices),
                )
            except BaseException:
                # The caller only learns of the handles this call RETURNS, so
                # its own cancel-on-failure cannot cover slices submitted
                # here. Cancelling them is this method's job.
                if handles:
                    self.cancel_all(handles)
                raise
        return handles

    def _submit_one_array(
        self,
        specs: list[TestJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None,
        dependency: str | None,
        slice_index: int,
        slice_count: int,
    ) -> list[JobHandle]:
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
        # The throttle caps each ARRAY, so a chunked group's peak
        # concurrency is slices x max_parallel — documented, not hidden.
        if max_parallel is not None and max_parallel < len(specs):
            array_range += f"%{max_parallel}"
        # `/k` names the slice, so a split group is legible in squeue
        # instead of looking like several unrelated arrays.
        job_name = f"rb:{specs[0].test_name}+{len(specs) - 1}"
        if slice_count > 1:
            job_name += f"/{slice_index}"
        resources = specs[0].resources
        cmd = [
            "sbatch",
            "--parsable",
            f"--array={array_range}",
            f"--job-name={job_name}",
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
        where = f" (slice {slice_index}/{slice_count})" if slice_count > 1 else ""
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch array submit failed{where} ({len(specs)} jobs, "
                f"rc={proc.returncode}): {proc.stderr.strip()}"
                f"{self._array_limit_hint(proc.stderr)}"
            )
        base_id, cluster = self._accepted_on(proc.stdout)
        if not base_id:
            raise FatalRtlBuddyError(
                f"sbatch returned no job id for array submit{where}"
            )
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
            # Additive: 1/1 for a group that fits in one array, so a reader
            # (and the log) can always tell a split from a whole group.
            slice=slice_index,
            slices=slice_count,
            cluster=cluster,
        )
        return [
            JobHandle(job_id=f"{base_id}_{i}", spec=spec, cluster=cluster)
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

    @staticmethod
    def _base_ids_by_cluster(
        handles: Sequence[JobHandle | None],
    ) -> dict[str | None, list[str]]:
        """Unique base ids grouped by the cluster that accepted them.

        A run can span clusters even within one resource group: a
        ``--clusters=a,b`` submission lets Slurm place each array wherever
        it can start first, so the slices of ONE group may live on
        different clusters. Cancelling them therefore cannot be one command
        with one ``-M``; it is one command per cluster.
        """
        grouped: dict[str | None, dict[str, None]] = {}
        for h in handles:
            if h is None:
                continue
            base = h.job_id.split("_")[0]
            grouped.setdefault(getattr(h, "cluster", None), {}).setdefault(base, None)
        return {cluster: list(ids) for cluster, ids in grouped.items()}

    def _scancel(
        self, ids_by_cluster: dict[str | None, list[str]], *, cwd
    ) -> list[tuple[str | None, list[str], subprocess.CompletedProcess]]:
        """One ``scancel`` per cluster; the results, for the caller to report.

        ``scancel`` acts on the local cluster unless ``-M`` says otherwise,
        the same rule sbatch follows, so ids accepted elsewhere have to be
        cancelled with the matching selection or the command reaches a
        different cluster's job numbering (#509 review).
        """
        results = []
        for cluster, ids in ids_by_cluster.items():
            argv = ["scancel", *(["-M", cluster] if cluster else []), *ids]
            results.append(
                (
                    cluster,
                    ids,
                    subprocess.run(argv, capture_output=True, text=True, cwd=cwd),
                )
            )
        return results

    def _reap_never_satisfied(self, lines, *, cwd, clusters=None) -> list[dict]:
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
            # Grouped by cluster, because squeue reports ids without saying
            # which cluster they belong to and an unqualified scancel would
            # aim at the local one (#509 review); `clusters` carries what
            # the submissions recorded.
            base_ids = list(dict.fromkeys(j.split("_")[0] for j in doomed))
            by_cluster: dict[str | None, list[str]] = {}
            for base in base_ids:
                by_cluster.setdefault((clusters or {}).get(base), []).append(base)
            for cluster, ids, proc in self._scancel(by_cluster, cwd=cwd):
                if proc.returncode == 0:
                    continue
                # These jobs are already out of `remaining`, so the run will
                # finish and leave them queued. Say so — a transient
                # slurmctld failure here is only recoverable by hand.
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.cancel_failed",
                    backend=self.name,
                    jobs=ids,
                    cluster=cluster,
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
        # base id -> the cluster that accepted it, so a reaping scancel can
        # be aimed where the job actually is.
        clusters = {
            base: cluster
            for cluster, ids in self._base_ids_by_cluster(handles).items()
            for base in ids
        }
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
                self._reap_never_satisfied(
                    proc.stdout.splitlines(), cwd=cwd, clusters=clusters
                )
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
        # Base ids: cancelling an array id cancels every element. One
        # command per cluster, since an id only means anything on the
        # cluster that issued it.
        by_cluster = self._base_ids_by_cluster(handles)
        self._scancel(by_cluster, cwd=self._cwd_of(handles))
        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(handles),
            # Additive, and absent on a single-cluster site: which clusters
            # the cancellation had to reach.
            clusters=sorted(c for c in by_cluster if c) or None,
            # An interrupted or failed run must leave the ids on the console:
            # they are the only route to `squeue`/`sacct` afterwards (#435).
            job_ids=group_job_ids(h.job_id for h in handles if h is not None),
        )

    @staticmethod
    def _telemetry_cluster_argv(handles: Sequence[JobHandle | None]) -> list[str]:
        """``-M`` for every cluster the fleet was accepted on, if any."""
        clusters = sorted(
            {
                cluster
                for cluster in (getattr(h, "cluster", None) for h in handles if h)
                if cluster
            }
        )
        return ["-M", ",".join(clusters)] if clusters else []

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Reserved-vs-used per job from ``sacct``, keyed by handle job id.

        Queries WITHOUT ``-X``: ``MaxRSS``/``TotalCPU`` only populate on
        step rows (``.batch`` etc.), never the allocation row — usage is
        folded up to its parent job. Values per job:
        ``state``, ``elapsed_s``, ``timelimit_s`` (TimelimitRaw is in
        MINUTES; normalized here), ``alloc_cpus``, ``req_cpus``,
        ``req_mem_bytes``, ``total_cpu_s``, ``max_rss_bytes``. Missing
        accounting (no slurmdbd) returns ``{}`` and right-sizing degrades
        gracefully.

        ``alloc_cpus`` and ``req_cpus`` are both reported because they
        differ wherever the site allocates whole cores: the first is what
        `squeue` shows, the second is what a ``resources.cpus`` edit can
        actually move, and right-sizing needs the second (#505).
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
                    # Accounting is per cluster too, and a job id repeats
                    # across clusters — an unqualified query would find
                    # nothing for a remote fleet, or the wrong local job of
                    # that number (#509 review). sacct takes the whole set
                    # at once, so this stays one call.
                    *self._telemetry_cluster_argv(handles),
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
            (
                job_id,
                state,
                elapsed,
                limit,
                cpus,
                req_cpus,
                req_mem,
                total_cpu,
                max_rss,
            ) = fields
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
                try:
                    entry["req_cpus"] = int(req_cpus)
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
