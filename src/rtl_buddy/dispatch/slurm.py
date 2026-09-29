# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Slurm dispatch backend.

The head submits one build job per suite (``rb _build-job``) and one ``sbatch --wrap``
sim job per (test, run_id) (``rb _test-job``), each sim gated on the build with
``--dependency=afterok``. Waiting polls ``squeue`` until the queue drains; loading
result envelopes is the caller's job. Slurm client calls use plain ``subprocess.run``
with an explicit ``cwd``, since the head cwd is re-anchored per suite.
"""

import getpass
import hashlib
import logging
import math
import os
import re
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tool_manifest import require as require_tool
from .argv import build_job_argv, elab_job_argv, test_job_argv
from .base import (
    BUILD_PHASE_VERILATE,
    BuildJobSpec,
    DispatchBackend,
    JobHandle,
    RunnableJobSpec,
    TestJobSpec,
    telemetry_key,
)
from .progress import DispatchProgress, group_job_ids

logger = logging.getLogger(__name__)


def _runnable_job_argv(spec: RunnableJobSpec) -> list[str]:
    if isinstance(spec, TestJobSpec):
        return test_job_argv(spec)
    return elab_job_argv(spec)


# Job states in which Slurm still holds a submitted job alive (job_state_codes(7)),
# minus the retained results in `_TERMINAL_RETAINED_STATES`. `wait_all` waits on this
# set: a missing state hides a live job from the wait, an extra one holds the wait on a
# finished record.
#
# Spelled out because `squeue` without `--states` lists only pending, running and
# completing jobs, and `--states=all` also names finished ones. Long names because
# several states have no short code.
_LIVE_STATES = (
    "PENDING",
    "RUNNING",
    "SUSPENDED",
    # A stopped job retains its CPUs; it has not terminated.
    "STOPPED",
    "CONFIGURING",
    "COMPLETING",
    "STAGE_OUT",
    "SIGNALING",
    "RESIZING",
    "REQUEUED",
    "REQUEUE_HOLD",
    "RESV_DEL_HOLD",
    "REQUEUE_FED",
    "SPECIAL_EXIT",
)

# Results Slurm keeps after the job is over. The drain poll must not wait on them: retry
# treats PREEMPTED as a finished resource kill (`RESOURCE_KILL_STATES`), and REVOKED is
# a federation sibling that will never progress. The dedup probe includes both, since
# naming a predecessor whose record is still queued is harmless there.
_TERMINAL_RETAINED_STATES = ("PREEMPTED", "REVOKED")

# Both filters derive from `_LIVE_STATES` so they cannot drift apart.
_DEDUP_STATES = _LIVE_STATES + _TERMINAL_RETAINED_STATES
_DRAIN_FILTER = ",".join(_LIVE_STATES)
_DEDUP_FILTER = ",".join(_DEDUP_STATES)

# squeue answers a state name its Slurm predates with `Invalid job state specified:
# <NAME>`. Dropping only that name keeps every state the cluster knows; dropping
# `--states` would fall back to squeue's narrower default and hide held jobs. An empty
# capture names nothing.
_INVALID_STATE_RE = re.compile(
    r"invalid job state[s]?(?: specified)?\s*:?\s*([A-Za-z_,]*)", re.I
)
# squeue's answer once none of the ids are queued any more: the jobs aged out, which is
# completion.
_GONE_FROM_QUEUE = "invalid job id"


def _rejected_states(stderr: str) -> tuple[str, ...] | None:
    """State names squeue refused, or ``None`` when it refused none.

    An empty tuple means it rejected the filter without naming a name, which
    the caller can only answer by dropping the filter rather than one entry.
    """
    match = _INVALID_STATE_RE.search(stderr or "")
    if match is None:
        return None
    named = (match.group(1) or "").replace(",", " ").split()
    return tuple(name.upper() for name in named)


# squeue reason for a job whose `afterok` dependency failed: PENDING but never runnable.
# Slurm reaps our jobs itself through `--kill-on-invalid-dep` (see `_dependency_argv`);
# `wait_all` sweeps this reason as the fallback for sites or Slurms that ignore the
# flag.
_NEVER_SATISFIED = "DependencyNeverSatisfied"

# Columns per poll: id | reason | state | time-used | name. Parsing tolerates short
# lines.
_SQUEUE_FORMAT = "%i|%r|%T|%M|%j"
_SQUEUE_RUNNING_STATE = "RUNNING"

# One element per manifest line, indexed by SLURM_ARRAY_TASK_ID; lines are shlex-quoted,
# so eval rebuilds the argv. A missing line fails the element loudly instead of exiting
# 0 with no envelope.
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

# `AllocCPUS` can exceed the request: whole-core allocation with `ThreadsPerCore=2`
# turns `--cpus-per-task=1` into 2, which would cap a single-threaded job's cpu
# efficiency at 0.5. `ReqCPUS` is what the project YAML controls, so right-sizing ratios
# against it.

# `scontrol show config` prints padded `Key = Value` lines.
_MAX_ARRAY_SIZE_RE = re.compile(r"^MaxArraySize\s*=\s*(\d+)\s*$", re.MULTILINE)
# `SchedulerParameters=max_array_tasks=N` is a second ceiling: a task count, inclusive,
# where MaxArraySize bounds the task index exclusively. `scontrol show config` keeps
# reporting the larger MaxArraySize, so slices sized from it alone can be refused.
_SCHEDULER_PARAMS_RE = re.compile(r"^SchedulerParameters\s*=\s*(.*)$", re.MULTILINE)
_MAX_ARRAY_TASKS_RE = re.compile(r"\bmax_array_tasks\s*=\s*(\d+)\b")


def _max_array_tasks(config_text: str) -> int | None:
    """``max_array_tasks`` from a ``scontrol show config`` dump, or ``None``.

    Read from the ``SchedulerParameters`` line only.
    """
    params = _SCHEDULER_PARAMS_RE.search(config_text)
    if params is None:
        return None
    match = _MAX_ARRAY_TASKS_RE.search(params.group(1))
    return int(match.group(1)) if match is not None else None


# The probe is optional: a slow or unreachable slurmctld costs a bounded wait and leaves
# chunking off, never a hang.
_SCONTROL_TIMEOUT_S = 30
# Options by which `sbatch-args` sends jobs to another cluster; the probe must follow
# them, since `scontrol show config` alone reads the local slurmctld.
#
# Only exact spellings are matched. sbatch accepts any unambiguous long-option prefix,
# but every proper prefix of `--clusters` is also a prefix of `--cluster-constraint` (a
# feature list), so sbatch rejects them as ambiguous. `--cluster` is kept as a tolerated
# alias.
_CLUSTER_OPTS = ("-M", "--clusters", "--cluster")
# Equivalent of `--clusters`, with the command line winning.
_CLUSTER_ENV = "SBATCH_CLUSTERS"
# Reserved value selecting every registered cluster.
_CLUSTER_ALL = "all"


def _is_multi_cluster(value: str) -> bool:
    """Whether this cluster selection names more than one cluster (``a,b`` or ``all``).

    No single ``MaxArraySize`` applies then, and ``scontrol -M all show config`` returns
    one config block per cluster.
    """
    return "," in value or value.strip().lower() == _CLUSTER_ALL


def _selected_cluster(sbatch_args: Sequence[str]) -> str | None:
    """The cluster ``sbatch-args`` submits to, or ``None`` for the local one.

    Accepts ``-M name``, ``-Mname``, ``--clusters=name``, ``--clusters name`` and the
    ``--cluster`` alias (see :data:`_CLUSTER_OPTS`); the last occurrence wins, as in
    sbatch. The value is returned verbatim, including multi-cluster lists.
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


# Time budget for releasing one compile key's jobs, across all clusters. The build job
# holds one deadline per key and passes the remainder as `budget_s`, so an unreachable
# controller cannot hold it for hours.
RELEASE_BUDGET_S = 60


class ReleaseOutcome(NamedTuple):
    """Result of one :func:`release_dependency` batch.

    ``systemic`` is set when the batch stopped early (timeout, or ``scontrol`` not
    executable); ``skipped`` holds the ids never attempted, and the caller should stop
    releasing for the run. A per-id refusal is an ordinary entry in ``failures`` and the
    batch continues.
    """

    released: list[str]
    failures: list[tuple[str, str]]
    skipped: list[str]
    systemic: str | None


def release_dependency(
    job_ids: Sequence[str],
    *,
    cluster: str | None = None,
    cwd: str | None = None,
    budget_s: float | None = None,
) -> ReleaseOutcome:
    """Clear these jobs' scheduler dependency and report what happened.

    Runs ``scontrol update JobId=<id> Dependency=`` per id. Plain and array-element ids
    (``1234``, ``1234_3``) both work, and siblings stay pending. ``cluster`` is the
    cluster that issued the ids, since job ids are unique only within a cluster. Called
    from the build job against ids from the gates manifest.

    The batch shares ``budget_s`` (default :data:`RELEASE_BUDGET_S`) and each call is
    time-boxed to what is left. Never raises: an unreleased job keeps its ``afterok``
    gate and only loses the early start.
    """
    cluster_argv = [] if cluster is None else ["-M", cluster]
    released: list[str] = []
    failures: list[tuple[str, str]] = []
    systemic: str | None = None
    deadline = time.monotonic() + (RELEASE_BUDGET_S if budget_s is None else budget_s)
    pending = list(job_ids)
    while pending:
        job_id = pending.pop(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            systemic = "release budget exhausted"
            pending.insert(0, job_id)
            break
        try:
            proc = subprocess.run(
                [
                    "scontrol",
                    *cluster_argv,
                    "update",
                    f"JobId={job_id}",
                    "Dependency=",
                ],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=min(_SCONTROL_TIMEOUT_S, remaining),
            )
        except (OSError, subprocess.SubprocessError) as e:
            # Systemic: every remaining id would fail the same way. Stop the batch.
            systemic = str(e)[:200]
            failures.append((job_id, systemic))
            break
        if proc.returncode != 0:
            failures.append(
                (
                    job_id,
                    (
                        proc.stderr.strip()
                        or f"`scontrol update` failed (rc={proc.returncode})"
                    )[:200],
                )
            )
            continue
        released.append(job_id)
    return ReleaseOutcome(released, failures, pending, systemic)


# `MaxRSS` is a peak over samples, so a job shorter than the sampling interval reports a
# near-zero peak. Request per-second task sampling instead of the site default (30 s).
_ACCT_FREQ_OPT = "--acctg-freq"
_ACCT_FREQ_DEFAULT = f"{_ACCT_FREQ_OPT}=task=1"
_DEFAULT_ACCT_INTERVAL_S = 1.0

# ------------------------------------------------ build-job dedup
#
# Build jobs are named per suite and submitted with `--dependency=singleton`, so Slurm
# runs one build job per (user, name) at a time and a second run of the suite queues
# behind the first. The in-job `flock` also keeps concurrent builds safe, but parks the
# second builder in a compute allocation, and is process-local on NFS mounts with
# `nolock`.
_BUILD_JOB_NAME_PREFIX = "rb-build"
# The verilate half of a split compile has its own name over the same digest.
# `singleton` serialises per name, so a shared name would make each phase wait on the
# other's predecessor and deadlock overlapping runs.
_VERILATE_JOB_NAME_PREFIX = "rb-verilate"
# Names no job ids, so it has no check-then-submit race and works where `squeue` is
# unavailable.
_DEDUP_DEPENDENCY = "singleton"
# sbatch's default for `-d/--dependency`; a command-line option overrides it, so the
# generated dependency composes with it.
_SBATCH_DEPENDENCY_ENV = "SBATCH_DEPENDENCY"
# One expression may use `,` (all) or `?` (any), not both, so an expression using `?`
# cannot be composed with.
_DEPENDENCY_OR_SEPARATOR = "?"
# sbatch accepts any unambiguous long-option prefix. `--dep` is the shortest that cannot
# mean `--deadline`, `--delay-boot` or `--distribution`.
_DEPENDENCY_OPT = "--dependency"
_DEPENDENCY_MIN_ABBREV = "--dep"
# The informational probe counts jobs in `_DEDUP_FILTER` as in flight. It is time-boxed
# because it only feeds a log line; the guarantee is the dependency.
_DEDUP_TIMEOUT_SEC = 20.0


def _is_dependency_opt(arg: str) -> bool:
    """Whether ``arg`` is the long ``--dependency`` option, abbreviated or not.

    Prefixes shorter than :data:`_DEPENDENCY_MIN_ABBREV` are not claimed. The caller
    splits ``=value`` first and passes the flag half.
    """
    return arg.startswith(_DEPENDENCY_MIN_ABBREV) and _DEPENDENCY_OPT.startswith(arg)


def build_job_name(spec: BuildJobSpec) -> str:
    """The Slurm job name for ``spec``: one name per suite directory.

    Deterministic across runs and processes, because ``--dependency=singleton``
    serialises jobs sharing a name and owner. The identity is the suite directory, which
    owns ``artefacts/.shared-builds/``. Finer keys are wrong: different tests or builder
    modes of one suite can compile the same key into the same ``obj_dir``. The compile
    key itself is unavailable, since it is only known after the config's ``pre()`` hook
    runs inside the job. Over-matching costs queue latency, never a wrong build.

    The verilate and build halves of a split compile use ``rb-verilate-<hash>`` and
    ``rb-build-<hash>`` over one digest.
    """
    # os.fsencode, not .encode("utf-8"): a non-UTF-8 path byte arrives surrogate-escaped
    # and would raise UnicodeEncodeError.
    digest = hashlib.sha256(os.fsencode(os.path.abspath(spec.suite_dir))).hexdigest()[
        :12
    ]
    prefix = (
        _VERILATE_JOB_NAME_PREFIX
        if getattr(spec, "phase", None) == BUILD_PHASE_VERILATE
        else _BUILD_JOB_NAME_PREFIX
    )
    return f"{prefix}-{digest}"


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
    """One ``_SQUEUE_FORMAT`` line as a record, or ``None`` when it has no id.

    Tolerates short lines: only the id and reason drive decisions.
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

    Handles ``1235``, ``1235_3``, ``1235_[1-40]``, ``1235_[1,3-5]`` and
    ``1235_[1-40%4]``. A bare base id is expanded to every handle sharing it, so a whole
    array is not counted as one job.
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


# The cfg-dispatch fields an array ceiling can come from. A rejected slice is reported
# against the field that governed it.
_FIELD_MAX_ARRAY_SIZE = "cfg-dispatch.max-array-size"
_FIELD_MAX_ARRAY_TASKS = "cfg-dispatch.max-array-tasks"


class _ArrayLimit(NamedTuple):
    """The resolved slice size and its provenance.

    ``elements`` is ``None`` when no limit is known; ``source`` is ``config`` or
    ``scontrol``; ``governed_by`` is the ceiling that set it.
    """

    elements: int | None
    source: str | None = None
    governed_by: str | None = None


def _parsable_submission(stdout: str) -> tuple[str, str | None]:
    """``(job id, cluster)`` from ``sbatch --parsable`` output.

    The output is ``jobid`` or ``jobid;cluster``. The cluster is kept because ids are
    unique per cluster, and ``scancel`` needs it.
    """
    job_id, _, cluster = stdout.strip().partition(";")
    return job_id.strip(), (cluster.strip() or None)


def _task_sampling_interval(value: str) -> float | None:
    """Seconds between task samples in an ``--acctg-freq`` value.

    Accepts a bare interval (``30``) and the typed form (``task=5,energy=0``); only the
    ``task`` datatype counts. Returns ``None`` when the value says nothing about task
    sampling and ``math.inf`` when it disables it (``task=0``). The two differ: unknown
    leaves the peak trusted, disabled distrusts it.
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
        # Raises with the manifest's install hint when the Slurm client is absent.
        require_tool("slurm")
        self.sbatch_args = list(dispatch_cfg.sbatch_args)
        self.poll_interval = dispatch_cfg.poll_interval
        self.progress_interval = getattr(dispatch_cfg, "progress_interval", 60.0)
        self.max_wait = getattr(dispatch_cfg, "max_wait", None)
        # Resolved on the first array submit, so constructing a backend never shells
        # out.
        self.max_array_size = getattr(dispatch_cfg, "max_array_size", None)
        # `SchedulerParameters=max_array_tasks` caps tasks per array independently of
        # MaxArraySize, so it has its own field.
        self.max_array_tasks = getattr(dispatch_cfg, "max_array_tasks", None)
        # Cached per cluster selection. The selection is read on demand because
        # $SBATCH_CLUSTERS is part of it.
        self._elements_per_array_by_cluster: dict[str | None, _ArrayLimit] = {}
        self._acct_interval_s = self._resolve_accounting_frequency()
        # Retired after the first failure, so a hung `squeue` costs its timeout once per
        # run.
        self._dedup_probe_available = True
        # Drain-poll state filter per cluster; a missing entry means the full
        # `_DRAIN_FILTER`. Per cluster because federated clusters can run different
        # Slurm versions, and narrowing to the oldest would hide held jobs on newer
        # ones.
        self._wait_states_by_cluster: dict = {}
        # Clusters whose poll already failed: the first failure warns, the rest log at
        # DEBUG.
        self._wait_poll_failed: set = set()

    def _resolve_accounting_frequency(self) -> float | None:
        """Request per-second task sampling unless the user asked for a rate.

        Prepended, so user ``sbatch-args`` come later and win. Returns the interval that
        applies to submitted jobs; right-sizing uses it to decide whether a job ran long
        enough to be sampled. A user value with no usable task interval (for example
        ``--acctg-freq=energy=30``) is logged at WARNING and the default is still
        requested.
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
        """The ``sbatch-args`` as submitted, including the prepended ``--acctg-freq``.

        Right-sizing judges a job's cpu request by it.
        """
        return self.sbatch_args

    def accounting_interval_s(self) -> float | None:
        return self._acct_interval_s

    @staticmethod
    def _cwd_of(handles: Sequence[JobHandle | None]) -> str | None:
        # Skips None handles so cancel_all is not disarmed by a bad caller.
        for h in handles:
            if h is not None:
                return h.spec.suite_dir
        return None

    def _reservation_argv(self, resources, *, job_name, chdir, log_path) -> list[str]:
        """Common sbatch reservation flags for build and sim jobs.

        ``job_name`` may be ``None`` for a caller that appends its own after
        ``sbatch_args``.
        """
        cmd = [
            "sbatch",
            "--parsable",
            *([] if job_name is None else [f"--job-name={job_name}"]),
            f"--chdir={chdir}",
            # Explicit: right-sizing needs a defined limit, and partitions may default
            # to UNLIMITED.
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
        """The ``afterok`` gate, plus ``--kill-on-invalid-dep=yes``.

        Slurm then removes the job if the dependency can never be met. Slurm owning the
        cleanup is the only form that survives a SIGKILLed head.
        """
        if dependency is None:
            return []
        return [f"--dependency=afterok:{dependency}", "--kill-on-invalid-dep=yes"]

    def _retire_dedup_probe(self, error: str, **fields) -> list[str]:
        """Log that the probe failed, stop asking, and return no ids.

        Failures are properties of the submit host, not the suite, so retrying would
        repeat the cost. The guarantee (``--dependency=singleton``) is unaffected.
        """
        self._dedup_probe_available = False
        log_event(
            logger,
            logging.DEBUG,
            "dispatch.build_dedup_unavailable",
            **fields,
            error=(
                f"{error}; not asking again this run — the build job is still "
                "serialised by --dependency=singleton, only the warning naming "
                "the job it waits for is lost"
            ),
        )
        return []

    def _queued_build_ids(self, job_name: str, *, cwd: str) -> list[str]:
        """This user's build jobs still in flight under ``job_name``.

        Informational only: the serialisation is ``--dependency=singleton``; this probe
        lets the warning name the jobs being waited on. The filter is
        :data:`_DEDUP_FILTER`, which includes held states. Every failure returns no ids,
        logs at DEBUG and disables the probe for the run (see
        :meth:`_retire_dedup_probe`). Scoped to this user and to the cluster
        ``sbatch-args`` selects.
        """
        if not self._dedup_probe_available:
            return []
        fields = {"job_name": job_name, "suite_dir": cwd}
        # The raw selection, not `self.cluster`, which collapses multi-cluster
        # selections to None.
        selection = self._cluster_selection()
        if selection is not None and _is_multi_cluster(selection):
            # Slurm picks the cluster at submit, so a job id from one queue would be
            # ambiguous. With `DependencyParameters=disable_remote_singleton`,
            # `singleton` is fulfilled per cluster only; the message below says so, once
            # per run.
            return self._retire_dedup_probe(
                f"sbatch-args select several clusters ({selection}), so a job "
                "id from any one of them would be ambiguous; note that with "
                "DependencyParameters=disable_remote_singleton the build job's "
                "singleton is fulfilled on its own cluster only, so builds "
                "routed to different clusters are serialised by the shared "
                "build directory's flock alone",
                **fields,
            )
        try:
            user = getpass.getuser()
        except Exception as e:  # noqa: BLE001 - no login name, no probe
            return self._retire_dedup_probe(str(e), **fields)
        # Follow `sbatch-args` to the submit cluster; a bare `squeue` reads the local
        # queue.
        cluster_argv = [] if self.cluster is None else ["-M", self.cluster]

        def _argv(states: str | None) -> list[str]:
            return [
                "squeue",
                *cluster_argv,
                "--noheader",
                "--format=%i",
                f"--user={user}",
                f"--name={job_name}",
                *([] if states is None else [f"--states={states}"]),
            ]

        try:
            proc = subprocess.run(
                _argv(_DEDUP_FILTER),
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=_DEDUP_TIMEOUT_SEC,
            )
            if proc.returncode != 0:
                # The state list is the one thing squeue can reject: newer names
                # postdate older Slurms. Retry without it, which falls back to squeue's
                # narrower default. Only on an error return; a timeout or missing binary
                # stays a single cost.
                proc = subprocess.run(
                    _argv(None),
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=_DEDUP_TIMEOUT_SEC,
                )
        except (OSError, subprocess.SubprocessError) as e:
            return self._retire_dedup_probe(str(e), **fields)
        if proc.returncode != 0:
            return self._retire_dedup_probe(
                proc.stderr.strip() or f"squeue exited {proc.returncode}", **fields
            )
        # `--noheader` keeps the `CLUSTER: name` banner `-M` prints; take the lines that
        # start like an id (`123`, `123_4`, `123_[1-4]`).
        return [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip() and line.strip()[0].isdigit()
        ]

    def _configured_dependency(self) -> str | None:
        """The dependency expression already in force for this submission, raw.

        Composed with rather than overwritten, since a site may gate every job behind a
        reservation or staging job. Sources, in sbatch's precedence: the last matching
        option in ``sbatch-args`` (abbreviations included, see
        :func:`_is_dependency_opt`), then :data:`_SBATCH_DEPENDENCY_ENV`. The
        environment is read at submit time. :meth:`_sbatch_args_dependency` reads the
        first source alone, for callers that need what actually holds the job, since a
        command-line option beats the environment.
        """
        found = self._sbatch_args_dependency()
        if found is not None:
            return found
        # A blank export is no gate.
        return os.environ.get(_SBATCH_DEPENDENCY_ENV, "").strip() or None

    def _sbatch_args_dependency(self) -> str | None:
        """The dependency ``sbatch-args`` supplies, or ``None``.

        It outranks the generated ``afterok``, because ``sbatch-args`` come later in
        :meth:`_sbatch_argv` and Slurm takes the last copy. The per-key release reads it
        so as not to clear an expression the site meant. Matches every abbreviation (see
        :func:`_is_dependency_opt`) and takes the last occurrence.
        """
        args = self.sbatch_args
        found = None
        skip = -1
        for index, arg in enumerate(args):
            if index == skip:
                # A value consumed by the separated spelling above.
                continue
            flag, equals, value = arg.partition("=")
            if equals and _is_dependency_opt(flag):
                found = value
            elif arg == "-d" or _is_dependency_opt(arg):
                if index + 1 < len(args):
                    found = args[index + 1]
                    skip = index + 1
            elif arg.startswith("-d") and arg[1] != "-":
                # `-dafterok:7`, the joined short spelling.
                found = arg[2:]
        return found

    def _dedup_dependency(self, *, suite_dir: str) -> str | None:
        """The ``--dependency`` value that serialises this build job.

        ``singleton``, ANDed onto any dependency already in force (from ``sbatch-args``
        or ``SBATCH_DEPENDENCY``). ``None`` when that expression uses ``?``, because
        Slurm rejects mixed separators; the in-job build lock still keeps concurrent
        builders safe, and the user's own gate is left as it was.
        """
        configured = self._configured_dependency()
        if configured is None:
            return _DEDUP_DEPENDENCY
        if _DEPENDENCY_OR_SEPARATOR in configured:
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.build_dedup_unavailable",
                suite_dir=suite_dir,
                error=(
                    f"the dependency already in force ({configured}) uses the "
                    f"{_DEPENDENCY_OR_SEPARATOR!r} (any-of) separator, which "
                    "Slurm will not let a second clause be added to"
                ),
            )
            return None
        if _DEDUP_DEPENDENCY in configured.split(","):
            # Already requested by the user.
            return configured
        return f"{configured},{_DEDUP_DEPENDENCY}"

    def submit_build(
        self, spec: BuildJobSpec, *, dependency: str | None = None
    ) -> JobHandle:
        """Submit one build job, optionally chained behind another.

        ``dependency`` is the verilate job's id for the build half of a split compile.
        It is ANDed onto :meth:`_dedup_dependency` and adds
        ``--kill-on-invalid-dep=yes``, so a failed verilate job reaps the build job.
        """
        job_name = build_job_name(spec)
        cmd = self._reservation_argv(
            spec.resources,
            job_name=None,
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        cmd += self.sbatch_args
        # The name goes after `sbatch_args`: `--dependency=singleton` serialises on it,
        # and a `--job-name` in `sbatch_args` would win and collapse every suite onto
        # one singleton. `sbatch-args` therefore cannot rename the build job.
        cmd.append(f"--job-name={job_name}")
        # Also after `sbatch_args`, because Slurm takes the last `--dependency`; it
        # carries the user's expression too. `singleton` cannot become unsatisfiable, so
        # only the chained `afterok` adds `--kill-on-invalid-dep`.
        dedup = self._dedup_dependency(suite_dir=spec.suite_dir)
        # The chained `afterok` comes first, in phase order, and is kept even when dedup
        # is refused.
        clauses = [f"afterok:{dependency}"] if dependency is not None else []
        if dedup is not None:
            clauses.append(dedup)
        expression = ",".join(clauses) or None
        if expression is not None:
            cmd.append(f"--dependency={expression}")
        if dependency is not None:
            cmd.append("--kill-on-invalid-dep=yes")
        # Probed before the submit so it cannot see this run's own job.
        inflight = (
            self._queued_build_ids(job_name, cwd=spec.suite_dir)
            if dedup is not None
            else []
        )
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
        if inflight:
            # WARNING so the console shows why this build waits, with ids to inspect.
            # Cancel only a held or abandoned predecessor; cancelling a healthy one
            # discards the build this run will reuse.
            log_event(
                logger,
                logging.WARNING,
                "dispatch.build_job_deduped",
                backend=self.name,
                job_id=job_id,
                suite_dir=spec.suite_dir,
                job_name=job_name,
                job_ids=inflight,
                dependency=expression,
            )
        log_event(
            logger,
            logging.INFO,
            # One event per phase, with identical fields.
            "dispatch.verilate_submitted"
            if spec.phase == BUILD_PHASE_VERILATE
            else "dispatch.build_submitted",
            backend=self.name,
            job_id=job_id,
            # The name `--dependency=singleton` serialises on. Slurm-only: the
            # local-parallel backend omits the field.
            job_name=job_name,
            suite_dir=spec.suite_dir,
            time=spec.resources.time,
            cpus=spec.resources.cpus,
            mem=spec.resources.mem,
            # `cpus` is already scaled by this.
            parallel=spec.parallel,
            cluster=cluster,
        )
        return JobHandle(job_id=job_id, spec=spec, cluster=cluster)

    def _accepted_on(self, stdout: str) -> tuple[str, str | None]:
        """``(job id, cluster)`` for a submission just made.

        Falls back to the selected cluster when sbatch names none. The selection is
        ``None`` for multi-cluster values, where sbatch's own answer is the only source.
        """
        job_id, cluster = _parsable_submission(stdout)
        return job_id, cluster or self.cluster

    @staticmethod
    def _begin_argv(delay_sec: float) -> list[str]:
        """The retry backoff as ``--begin=now+<n>`` seconds, served by Slurm.

        The job pends with reason ``BeginTime`` and holds no allocation. Delays round to
        whole seconds; one that rounds to 0 emits no flag.
        """
        seconds = int(round(delay_sec)) if delay_sec and delay_sec > 0 else 0
        return [f"--begin=now+{seconds}"] if seconds > 0 else []

    def _sbatch_argv(
        self, spec: RunnableJobSpec, dependency: str | None, delay_sec: float = 0.0
    ) -> list[str]:
        cmd = self._reservation_argv(
            spec.resources,
            job_name=f"rb:{spec.display_name()}",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        cmd += self._dependency_argv(dependency)
        cmd += self._begin_argv(delay_sec)
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(_runnable_job_argv(spec))]
        return cmd

    def submit(
        self,
        spec: RunnableJobSpec,
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
        fields = {
            "backend": self.name,
            "job_id": job_id,
            "dependency": dependency,
            "begin_delay_sec": delay_sec or None,
            "time": spec.resources.time,
            "cpus": spec.resources.cpus,
            "mem": spec.resources.mem,
            "cluster": cluster,
        }
        if isinstance(spec, TestJobSpec):
            fields.update(test=spec.test_name, run_id=spec.run_id)
        else:
            fields.update(model=spec.model_name, profile=spec.profile_name)
        log_event(logger, logging.INFO, "dispatch.submitted", **fields)
        return JobHandle(job_id=job_id, spec=spec, cluster=cluster)

    def _cluster_selection(self) -> str | None:
        """Cluster selection as written, or ``None`` for the local cluster.

        ``sbatch-args`` first, then ``$SBATCH_CLUSTERS`` (blank selects nothing),
        matching sbatch's precedence. Read at probe time, not at construction.
        Multi-cluster values (``a,b``, ``all``) are returned verbatim.
        """
        selected = _selected_cluster(self.sbatch_args)
        if selected is not None:
            return selected
        return (os.environ.get(_CLUSTER_ENV) or "").strip() or None

    @property
    def cluster(self) -> str | None:
        """The one cluster this backend addresses, or ``None``.

        ``None`` for no selection (local cluster), a list, or ``all``: Slurm picks the
        cluster at submit, so no query can be qualified with ``-M``. See
        :meth:`_cluster_selection` for the raw value.
        """
        selection = self._cluster_selection()
        if selection is None or _is_multi_cluster(selection):
            return None
        return selection

    def _max_elements_per_array(self, *, cwd: str | None) -> int | None:
        """Elements one array may hold, resolved once per cluster selection.

        ``MaxArraySize`` bounds the task index exclusively and manifests are 1-based, so
        the largest array is ``MaxArraySize - 1`` elements.
        ``cfg-dispatch.max-array-size`` wins where set; otherwise ``scontrol show
        config`` is read. ``None`` means unknown and the group is submitted whole.
        """
        selection = self._cluster_selection()
        if selection not in self._elements_per_array_by_cluster:
            self._elements_per_array_by_cluster[selection] = self._probe_max_elements(
                cwd=cwd
            )
        return self._elements_per_array_by_cluster[selection].elements

    def _probe_max_elements(self, *, cwd: str | None) -> "_ArrayLimit":
        """Effective elements-per-array, layering config over the probe.

        ``cfg-dispatch.max-array-size`` overrides the probed ``MaxArraySize`` and
        ``cfg-dispatch.max-array-tasks`` overrides the probed ``max_array_tasks``,
        independently; the slice is the smaller result. The probe is skipped when both
        are pinned or the selection names several clusters. Returns an
        :class:`_ArrayLimit` recording which ceiling governed.
        """
        selection = self._cluster_selection()
        ambiguous = selection is not None and _is_multi_cluster(selection)
        probed_size = probed_tasks = None
        reason = None
        if self.max_array_size is None or self.max_array_tasks is None:
            if ambiguous:
                # Slurm picks the cluster at submit and `-M all` returns several config
                # blocks, so the limit is unknown unless pinned.
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
        # A cap below 1 cannot be submitted under; drop it (only a probed value can be
        # one).
        if tasks is not None and tasks < 1:
            tasks = None
        if size is None and tasks is None:
            self._log_unknown(reason or "no array ceiling available")
            return _ArrayLimit(None)

        # MaxArraySize bounds the index exclusively, max_array_tasks counts tasks. The
        # smaller known value applies, and either alone is a ceiling.
        limit = source = governed_by = None
        if size is not None:
            limit = size - 1
            source = "config" if self.max_array_size is not None else "scontrol"
            governed_by = _FIELD_MAX_ARRAY_SIZE
        if tasks is not None and (limit is None or tasks < limit):
            limit = tasks
            source = "config" if self.max_array_tasks is not None else "scontrol"
            governed_by = _FIELD_MAX_ARRAY_TASKS
        log_event(
            logger,
            logging.DEBUG,
            "dispatch.max_array_size",
            backend=self.name,
            max_array_size=size,
            # Absent only when no task cap is known, since `log_event` drops `None`
            # fields.
            max_array_tasks=tasks,
            max_elements=limit,
            source=source,
            # The ceiling that produced `max_elements`.
            governed_by=governed_by,
            cluster=selection,
        )
        return _ArrayLimit(limit, source, governed_by)

    def _scontrol_ceilings(
        self, *, cwd: str | None, selection: str | None
    ) -> tuple[int | None, int | None, str | None]:
        """``(MaxArraySize, max_array_tasks, why not)`` from one scontrol call.

        Never raises: a limit that cannot be read only disables chunking.
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
            # MaxArraySize < 2 means arrays are disabled; treat as unknown and let
            # sbatch refuse.
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
            # As written: a multi-cluster selection resolves to None, and naming it
            # is the diagnosis.
            cluster=self._cluster_selection(),
            # How to restore chunking when the cluster cannot be asked.
            hint=(
                "set cfg-dispatch.max-array-size (and max-array-tasks, where "
                "the cluster caps tasks per array) to split oversized groups"
            ),
        )

    def _array_limit_hint(self, stderr: str) -> str:
        """Recovery advice appended to a failed array submit, or ``""``.

        Added only when stderr mentions "job array" (case-insensitive), so unrelated
        rejections such as an invalid account get no hint. With the limit unknown, the
        hint names both ``cfg-dispatch`` fields. With it known, the cluster is enforcing
        something it did not report, and the hint names the governing field and where
        the limit came from.
        """
        if "job array" not in stderr.lower():
            return ""
        resolved = self._elements_per_array_by_cluster.get(
            self._cluster_selection(), _ArrayLimit(None)
        )
        if resolved.elements is None:
            # Names both ceilings: either can be binding, and fixing one alone would
            # recur.
            return (
                "; the cluster's array limits could not be read, so this group "
                "was submitted as one array — set cfg-dispatch.max-array-size "
                "(the cluster's MaxArraySize) and cfg-dispatch.max-array-tasks "
                "(its SchedulerParameters=max_array_tasks, where it caps tasks "
                "per array below that) to let rb split a group larger than "
                "either limit; each is layered on its own and either one alone "
                "is enough to split"
            )
        # Names the ceiling that produced the slice.
        return (
            f"; rb sliced this group at {resolved.elements} element(s) per "
            f"array, the {resolved.governed_by} limit it read from "
            f"{resolved.source}, and the cluster refused it anyway — lower "
            f"{resolved.governed_by} to pin a smaller one"
        )

    def submit_array(
        self,
        specs: list[RunnableJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None = None,
        dependency: str | None = None,
    ) -> list[JobHandle]:
        """Submit one resource group, split across arrays if it exceeds the limit.

        Each array has its own manifest. Handles are returned concatenated in spec
        order. Slices already submitted are cancelled if a later one fails.
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
            # One subdirectory per slice when chunked; a single array keeps the unsliced
            # layout.
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
                # The caller only learns of returned handles, so cancelling earlier
                # slices is this method's job.
                if handles:
                    self.cancel_all(handles)
                raise
        return handles

    def _submit_one_array(
        self,
        specs: list[RunnableJobSpec],
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
            "".join(shlex.join(_runnable_job_argv(spec)) + "\n" for spec in specs)
        )
        script = array_dir / "array.sh"
        script.write_text(_ARRAY_SCRIPT)
        script.chmod(0o755)
        # `%a` is the 1-based manifest line, so element logs are deterministic.
        for i, spec in enumerate(specs, start=1):
            spec.log_path = array_dir / f"slurm-{i}.log"

        array_range = f"1-{len(specs)}"
        # The throttle applies per array, so a chunked group's peak concurrency is
        # slices x max_parallel.
        if max_parallel is not None and max_parallel < len(specs):
            array_range += f"%{max_parallel}"
        # The `/k` suffix names the slice in squeue.
        first_name = (
            specs[0].test_name
            if isinstance(specs[0], TestJobSpec)
            else specs[0].display_name()
        )
        job_name = f"rb:{first_name}+{len(specs) - 1}"
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
            # 1/1 for a group that fits in one array.
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
        """Unique base job ids, one per array.

        Skips ``None`` handles so ``cancel_all`` is not disarmed by an absent build
        handle.
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

        One group can span clusters under ``--clusters=a,b``, so cancellation is one
        command per cluster.
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
        """One ``scancel`` per cluster; returns the results for the caller to report.

        Ids accepted on another cluster need the matching ``-M``.
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

    def _reap_never_satisfied(self, lines, *, cwd, cluster=None) -> list[dict]:
        """Split queued jobs into those still coming and those already dead.

        Jobs pending with reason ``DependencyNeverSatisfied`` are cancelled. This is the
        fallback for sites that disabled ``--kill-on-invalid-dep`` or Slurms that ignore
        it (see :meth:`_dependency_argv`). Returns the surviving squeue records (see
        :func:`_parse_squeue_line`), not ids, because the progress line needs their
        state and elapsed time.
        """
        remaining, doomed = [], []
        for line in lines:
            record = _parse_squeue_line(line)
            if record is None:
                continue
            # Substring match on the reason column alone, so a job named after the
            # reason is not reaped.
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
                cluster=cluster,
            )
            # Cancel by base id, on the cluster the ids came from.
            base_ids = list(dict.fromkeys(j.split("_")[0] for j in doomed))
            for cluster, ids, proc in self._scancel({cluster: base_ids}, cwd=cwd):
                if proc.returncode == 0:
                    continue
                # Already out of `remaining`, so a failed cancel leaves them queued
                # after the run.
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

    def _outstanding(self, records, handles, *, cluster=None):
        """Queue records to ``({outstanding key: state}, longest running)``.

        Each record is expanded to the handle ids it covers (``9_[1-3]`` is three jobs);
        a record matching no handle counts as itself. Keys come from
        :func:`telemetry_key` and match only handles of ``cluster``, so ``records`` must
        come from a squeue that named ``cluster``.
        """
        keys = {
            h.job_id: telemetry_key(h)
            for h in handles
            if h is not None and getattr(h, "cluster", None) == cluster
        }
        handle_ids = list(keys)
        outstanding: dict[str, str] = {}
        longest = None
        for record in records:
            if record["state"] in _TERMINAL_RETAINED_STATES:
                # A retained result, not a job to wait for; a fallback poll can still
                # return one.
                continue
            running = record["state"] == _SQUEUE_RUNNING_STATE
            expanded = [
                keys[job_id]
                for job_id in _expand_squeue_id(record["id"], handle_ids)
                if job_id in keys
            ] or [f"{cluster}:{record['id']}" if cluster else record["id"]]
            for key in expanded:
                outstanding[key] = "running" if running else "pending"
            if running:
                # %M is the same [DD-]HH:MM:SS shape sacct's TotalCPU uses.
                elapsed = _parse_cpu_time_to_seconds(record["time"])
                if elapsed is not None and (longest is None or elapsed > longest[1]):
                    longest = (record["name"] or record["id"], elapsed)
        return outstanding, longest

    def _wait_argv(self, base_ids, *, cluster, states) -> list[str]:
        """One drain poll's argv. ``states`` of ``None`` omits the filter."""
        return [
            "squeue",
            "--noheader",
            f"--format={_SQUEUE_FORMAT}",
            *([] if states is None else [f"--states={states}"]),
            *(["-M", cluster] if cluster else []),
            "--jobs",
            ",".join(base_ids),
        ]

    def _poll_queue(
        self, base_ids, *, cluster, cwd, timeout_s=None
    ) -> tuple[list[str], str]:
        """One cluster's live jobs: ``(squeue lines, status)``.

        ``status`` is ``"ok"`` (squeue answered; an empty list means drained),
        ``"drained"`` (squeue holds none of the ids: ``Invalid job id specified``) or
        ``"unknown"`` (the poll failed). ``"unknown"`` must never be read as a drain. A
        rejected state name is recovered from, see :func:`_rejected_states`.

        ``timeout_s`` bounds each ``squeue`` call. ``None`` waits as long as the
        controller takes; a timeout returns ``"unknown"``.
        """
        # Each rejection drops one name, so the loop is bounded by the list length.
        for _ in range(len(_LIVE_STATES) + 1):
            states = self._wait_states_for(cluster)
            try:
                proc = subprocess.run(
                    self._wait_argv(base_ids, cluster=cluster, states=states),
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                # Wedged controller: report no answer, never a drain.
                log_event(
                    logger,
                    logging.DEBUG,
                    "dispatch.wait_poll_timeout",
                    backend=self.name,
                    cluster=cluster,
                    jobs=len(base_ids),
                    timeout_sec=timeout_s,
                )
                return [], "unknown"
            if proc.returncode == 0:
                return proc.stdout.splitlines(), "ok"
            if states is None:
                break
            rejected = _rejected_states(proc.stderr)
            if rejected is None:
                break
            self._narrow_wait_states(rejected, cluster=cluster)
        if _GONE_FROM_QUEUE in (proc.stderr or "").lower():
            return [], "drained"
        # Other failures are usually transient, so keep polling and conclude nothing
        # about the jobs.
        level = logging.DEBUG if cluster in self._wait_poll_failed else logging.WARNING
        self._wait_poll_failed.add(cluster)
        log_event(
            logger,
            level,
            "dispatch.wait_poll_failed",
            backend=self.name,
            cluster=cluster,
            jobs=len(base_ids),
            error=(proc.stderr or "").strip()[:200]
            or f"squeue exited {proc.returncode}",
        )
        return [], "unknown"

    def _wait_states_for(self, cluster) -> str | None:
        """The state filter for ``cluster``: the full set until it rejects a name.

        ``None`` means no ``--states`` at all (see :meth:`_narrow_wait_states`).
        """
        return self._wait_states_by_cluster.get(cluster, _DRAIN_FILTER)

    def _narrow_wait_states(self, rejected: tuple, *, cluster) -> None:
        """Drop the state names this cluster rejected, for the rest of the run.

        When squeue named none, the filter is dropped instead, leaving squeue's narrower
        default; that logs a WARNING. Recorded per cluster because a rejection describes
        one Slurm.
        """
        current = self._wait_states_for(cluster)
        kept = [
            state
            for state in ([] if current is None else current.split(","))
            if state not in rejected
        ]
        if rejected and kept:
            self._wait_states_by_cluster[cluster] = ",".join(kept)
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.wait_states_narrowed",
                backend=self.name,
                cluster=cluster,
                dropped=",".join(rejected),
                states=self._wait_states_by_cluster[cluster],
            )
            return
        self._wait_states_by_cluster[cluster] = None
        log_event(
            logger,
            logging.WARNING,
            "dispatch.wait_states_unfiltered",
            backend=self.name,
            cluster=cluster,
            dropped=",".join(rejected) or None,
        )

    def _assumed_outstanding(self, handles, *, cluster) -> dict:
        """Every handle of ``cluster`` as still outstanding.

        A failed poll must not read as a drain, and this keeps ``max-wait`` armed.
        """
        return {
            telemetry_key(h): "pending"
            for h in handles
            if h is not None and getattr(h, "cluster", None) == cluster
        }

    def wait_all(self, handles: list[JobHandle], *, extra_wait: float = 0.0) -> None:
        if not handles:
            return
        cwd = self._cwd_of(handles)
        # One poll per cluster: an unqualified squeue would report a remote slice as
        # absent, which reads as drained.
        by_cluster = self._base_ids_by_cluster(handles)
        progress = DispatchProgress(
            handles,
            backend=self.name,
            interval=self.progress_interval,
            # A job held on `--begin` stays PENDING for the whole backoff, so the
            # deadline allows for it.
            max_wait=(
                None if self.max_wait is None else self.max_wait + max(0.0, extra_wait)
            ),
            clock=time.monotonic,
        )
        while True:
            states: dict[str, str] = {}
            longest = None
            for cluster, base_ids in by_cluster.items():
                # Three answers: still queued, aged out, or poll failed. Only aged-out
                # is a drain.
                lines, status = self._poll_queue(base_ids, cluster=cluster, cwd=cwd)
                if status == "drained":
                    continue
                if status == "unknown":
                    states.update(self._assumed_outstanding(handles, cluster=cluster))
                    continue
                records = self._reap_never_satisfied(lines, cwd=cwd, cluster=cluster)
                cluster_states, cluster_longest = self._outstanding(
                    records, handles, cluster=cluster
                )
                states.update(cluster_states)
                if cluster_longest is not None and (
                    longest is None or cluster_longest[1] > longest[1]
                ):
                    longest = cluster_longest
            if not states:
                progress.finish()
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(handles),
                )
                return
            progress.observe(states.keys(), states=states, longest=longest)
            time.sleep(self.poll_interval)

    def live_job_ids(
        self, handles: Sequence[JobHandle | None], *, timeout_s=None
    ) -> set[str]:
        """The subset of these ids ``squeue`` still holds.

        Handles are rebuilt from an interrupted run's manifest and queried per cluster.
        A failed poll reports its ids live, never gone: reading "unknown" as gone would
        submit a second fleet beside a running one or skip a requested ``scancel``.
        ``timeout_s`` bounds each query for a caller on a deadline; a timeout counts as
        unknown.
        """
        live: set[str] = set()
        cwd = self._cwd_of(handles)
        for cluster, base_ids in self._base_ids_by_cluster(handles).items():
            if not base_ids:
                continue
            # Expands a squeue row naming a whole array back into the manifest's
            # elements.
            ids_here = [
                h.job_id
                for h in handles
                if h is not None and getattr(h, "cluster", None) == cluster
            ]
            lines, status = self._poll_queue(
                base_ids, cluster=cluster, cwd=cwd, timeout_s=timeout_s
            )
            if status == "unknown":
                live.update(ids_here)
                continue
            for line in lines:
                record = _parse_squeue_line(line)
                if record is None:
                    continue
                live.update(_expand_squeue_id(record["id"], ids_here))
        return live

    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        if not handles:
            return
        # Base ids: cancelling an array id cancels every element. One command per
        # cluster, since an id means something only on the cluster that issued it.
        by_cluster = self._base_ids_by_cluster(handles)
        self._scancel(by_cluster, cwd=self._cwd_of(handles))
        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(handles),
            # Absent on a single-cluster site.
            clusters=sorted(c for c in by_cluster if c) or None,
            # Leaves the ids on the console; they are the only route to squeue/sacct
            # afterwards.
            job_ids=group_job_ids(h.job_id for h in handles if h is not None),
        )

    def build_outcome(self, handle: JobHandle) -> str | None:
        """The build job's scheduler state, or ``None`` without accounting.

        The state is verbatim (``COMPLETED``, ``TIMEOUT``, ``CANCELLED``...). The head
        prefers the sacct row it already fetched and calls this only when that row is
        missing.
        """
        return (self.collect_telemetry([handle]).get(telemetry_key(handle)) or {}).get(
            "state"
        )

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Reserved-vs-used per job from ``sacct``, keyed by :func:`telemetry_key`.

        The key is the bare job id for a local or single-cluster run and
        ``<cluster>:<job id>`` otherwise. One sacct runs per cluster, because
        ``_SACCT_FORMAT`` has no cluster column and rows from a combined query could not
        be told apart. Queries omit ``-X``, since ``MaxRSS`` and ``TotalCPU`` appear
        only on step rows.

        Per job: ``state``, ``elapsed_s``, ``timelimit_s`` (converted from minutes),
        ``alloc_cpus``, ``req_cpus``, ``req_mem_bytes``, ``total_cpu_s``,
        ``max_rss_bytes``. ``alloc_cpus`` is what ``squeue`` shows; ``req_cpus`` is what
        a ``resources.cpus`` edit moves, and right-sizing uses it. Missing accounting
        returns ``{}``.
        """
        if not handles:
            return {}
        telemetry: dict[str, dict] = {}
        for cluster, base_ids in self._base_ids_by_cluster(handles).items():
            # One cluster's failure must not discard the others' rows.
            telemetry.update(
                self._telemetry_on(handles, cluster=cluster, base_ids=base_ids)
            )
        return telemetry

    def _telemetry_on(
        self,
        handles: Sequence[JobHandle | None],
        *,
        cluster: str | None,
        base_ids: list[str],
    ) -> dict[str, dict]:
        """One cluster's ``sacct`` rows, keyed by :func:`telemetry_key`."""
        # Only this cluster's handles match, so a shared job number cannot cross over.
        wanted = {
            h.job_id: telemetry_key(h)
            for h in handles
            if h is not None and getattr(h, "cluster", None) == cluster
        }
        # Telemetry is additive: sacct may be absent or hang, and neither may fail a run
        # whose jobs completed.
        try:
            proc = subprocess.run(
                [
                    "sacct",
                    "--parsable2",
                    "--noheader",
                    f"--format={_SACCT_FORMAT}",
                    # Job ids repeat across clusters, so qualify the query.
                    *(["-M", cluster] if cluster else []),
                    "--jobs",
                    ",".join(base_ids),
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
                cluster=cluster,
                error=str(e)[:200],
            )
            return {}
        if proc.returncode != 0:
            log_event(
                logger,
                logging.INFO,
                "dispatch.telemetry_unavailable",
                backend=self.name,
                cluster=cluster,
                error=proc.stderr.strip()[:200],
            )
            return {}

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
            key = wanted.get(base)
            if key is None:
                continue
            entry = telemetry.setdefault(key, {})
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
                # Step rows: CPU time sums over steps (.batch, .extern, srun steps);
                # MaxRSS is a peak and folds with max.
                if (cpu_s := _parse_cpu_time_to_seconds(total_cpu)) is not None:
                    entry["total_cpu_s"] = entry.get("total_cpu_s", 0.0) + cpu_s
                if (rss := _parse_mem_to_bytes(max_rss)) is not None:
                    entry["max_rss_bytes"] = max(entry.get("max_rss_bytes", 0), rss)
        return telemetry
