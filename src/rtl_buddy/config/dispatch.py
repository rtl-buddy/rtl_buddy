# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch (remote test execution) configuration (#351).

``cfg-dispatch`` in root_config.yaml selects and parameterizes the
execution backend for regression test runs:

.. code-block:: yaml

    cfg-dispatch:
      backend: slurm           # default: local (in-process, today's behavior)
      resources:               # cluster-wide per-job defaults
        cpus: 2
        mem: 4G
        time: 01:00:00
      compile:                 # the shared compile's own reservation
        cpus: 4
        mem: 16G
        time: 02:00:00
        parallel: 4            # distinct builds compiled at once in the
                               # build job; its cpus reservation is scaled
                               # by this (4 x 4 = 16 above), mem/time are not
      sbatch-args:             # passed through to sbatch verbatim
        - --partition=verif
      poll-interval: 10        # seconds between queue polls while collecting
      progress-interval: 60    # seconds between console progress lines (0 = quiet)
      max-wait: 7200           # seconds the head waits before failing loudly
      max-jobs-per-array: 200  # %N throttle on EACH submitted Slurm array
      max-array-size: 1001     # the cluster's Slurm MaxArraySize; omit it to
                               # read the value from `scontrol show config`
      jobs: 4                  # local-parallel only: concurrent subprocesses
      retry:                   # optional; entirely off unless attempts > 0
        attempts: 2            # EXTRA attempts after the first
        backoff-sec: 60        # first delay, doubling per attempt
        backoff-max-sec: 600   # cap
        jitter: 0.5            # +/- fraction, to decorrelate a batch
        classifiers: [license-queue]   # which kills may be retried

Per-test reservation overrides use the same ``resources`` shape in
tests.yaml at testbench and test level; :func:`resolve_resources` layers
them field-by-field (test over testbench over ``cfg-dispatch`` defaults).

The compile phase has the same escape hatch one level up, at the top of
tests.yaml — the dispatched build job is per suite, so the suite is the
right owner (#497):

.. code-block:: yaml

    rtl-buddy-filetype: test_config
    compile:                 # THIS suite's build job only
      mem: 48G               # a big top-level TB; cpus/time inherited
    testbenches: ...

:func:`resolve_compile_resources` layers it field-by-field over
``cfg-dispatch.compile`` over ``cfg-dispatch.resources``. ``parallel`` is
not accepted there: it sizes the build job against the partition, which is
a cluster fact and not a suite one.
"""

import logging
import os
import re
from dataclasses import dataclass

from serde import field, serde

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

# A defined time limit is load-bearing, not cosmetic: reservation
# right-sizing computes time utilization as Elapsed/Timelimit, which is
# undefined on partitions whose default is UNLIMITED. Every dispatched
# job therefore gets an explicit --time, from this default if nothing
# else sets one.
DEFAULT_JOB_TIME = "01:00:00"
DEFAULT_JOB_CPUS = 1

# Accept the Slurm --time spellings we pass through verbatim:
# minutes, MM:SS, HH:MM:SS, DD-HH, DD-HH:MM, DD-HH:MM:SS.
_TIME_RE = re.compile(r"^\d+(-\d{1,2}(:\d{2}){0,2}|(:\d{2}){1,2})?$")


@serde
class DispatchResourcesFile:
    """Per-job resource reservation fields; ``None`` means "inherit".

    Scheduler-agnostic on purpose: the Slurm backend maps them to
    ``--cpus-per-task`` / ``--mem`` / ``--time``, and any later backend
    reuses the same schema.

    ``time`` and ``mem`` accept ``int`` as well as ``str`` so YAML 1.1's
    sexagesimal resolver — which turns an unquoted ``4:00:00`` into the
    integer ``14400`` — is caught at validation with a clear message
    rather than silently sent to Slurm as 14400 minutes (10 days).
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None


@serde
class DispatchCompileFile:
    """``cfg-dispatch.compile`` — the compile's reservation *and* its concurrency.

    A separate class from :class:`DispatchResourcesFile` even though the
    three reservation fields are identical, because that class is also the
    serde type behind every ``resources:`` block in tests.yaml (testbench
    and test level). ``parallel`` there would mean nothing: a per-test
    reservation sizes one sim job, and "compile N builds at once" is a
    property of the one build job per suite.

    Keeping the shapes apart is schema hygiene, not a guard rail — serde
    drops unknown keys, so ``resources: {parallel: 2}`` is silently
    discarded wherever it is written rather than rejected. What the split
    buys is that the field cannot be *documented* onto a per-test block by
    accident, and that a later strict-key pass has one class to make
    strict. A project that writes it in the wrong place is told by the
    docs, not by an error.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # Distinct builds (unique compile keys) the dispatched build job may
    # Verilate concurrently (#495). A suite with 8 plusdefines sets held
    # its whole sim fan-out behind 8 serial ~1.1-core compiles inside one
    # 16-CPU reservation; this is what spends that reservation. 1 is
    # today's serial loop, and the default.
    parallel: int = 1


def _validate_time(value):
    """Coerce/validate a ``--time`` value; reject the sexagesimal trap."""
    if value is None:
        return None
    if isinstance(value, int):
        raise FatalRtlBuddyError(
            f"dispatch resources: time {value!r} parsed as an integer — an "
            "unquoted HH:MM:SS is read by YAML as sexagesimal (4:00:00 -> "
            '14400). Quote it: time: "4:00:00" (or write bare minutes as a '
            "string)."
        )
    if not _TIME_RE.match(value):
        raise FatalRtlBuddyError(
            f'dispatch resources: time "{value}" is not a valid Slurm time '
            "(expected minutes, MM:SS, HH:MM:SS, or DD-HH[:MM[:SS]])."
        )
    return value


def _validate_mem(value):
    if value is None:
        return None
    return str(value)


def validate_resources_block(res):
    """Validate a raw ``{cpus, mem, time}`` block; return a fresh copy.

    The public entry point for any *other* config file that carries a
    reservation block — today the suite-level ``compile:`` in tests.yaml
    (#497). It exists so the YAML 1.1 sexagesimal trap (``4:00:00`` read as
    the integer 14400) is rejected in exactly one place, at load, rather
    than being re-derived by every loader that grows a reservation.

    ``None`` in, ``None`` out.
    """
    if res is None:
        return None
    return DispatchResourcesFile(
        cpus=res.cpus,
        mem=_validate_mem(res.mem),
        time=_validate_time(res.time),
    )


@serde
class RightsizeConfigFile:
    """``rightsize:`` sub-block — reservation right-sizing thresholds (#351 P3).

    Utilization below ``over-threshold`` flags a resource over-reserved;
    above ``near-limit`` (or a TIMEOUT/OOM kill) flags it under-reserved.
    Suggested reservation = observed peak × ``margin``.
    """

    report: bool = True
    over_threshold: float = field(rename="over-threshold", default=0.5)
    near_limit: float = field(rename="near-limit", default=0.9)
    margin: float = field(rename="margin", default=1.5)


# The classifiers ``retry.classifiers`` accepts. Only one exists today: a job the
# scheduler killed while its simulation was still sitting in the VCS
# license queue (#405). Retrying anything else — a hung testbench, an
# undersized reservation — would re-run work that failed on its own merits
# and burn the reservation twice, so the list is closed rather than free
# text: an unknown entry is a config error, not a silently inert one.
RETRY_CLASSIFIER_LICENSE_QUEUE = "license-queue"
RETRY_CLASSIFIERS = (RETRY_CLASSIFIER_LICENSE_QUEUE,)


@serde
class RetryConfigFile:
    """``retry:`` sub-block — a retry budget for resource-condition kills (#405).

    Default-inert: ``attempts`` is 0, so a project that never writes the
    block (or writes it without ``attempts``) keeps exactly today's
    behaviour — a job that left no result envelope is a failure, first and
    only try.

    ``attempts`` counts EXTRA attempts after the first, so ``attempts: 2``
    means at most three submissions of the same job. The delay before
    attempt *n* is ``min(backoff-max-sec, backoff-sec * 2 ** (n - 1))``,
    multiplied by ``uniform(1 - jitter, 1 + jitter)``. The jitter is not
    decoration: the jobs that lose a license-seat race lose it together,
    and a fixed delay would put the whole batch back in front of the same
    exhausted pool in lockstep.
    """

    attempts: int = 0
    backoff_sec: float = field(rename="backoff-sec", default=60.0)
    backoff_max_sec: float = field(rename="backoff-max-sec", default=600.0)
    jitter: float = 0.5
    # Not spelled ``on:``: PyYAML is a YAML 1.1 parser, so an unquoted
    # ``on`` key deserialises as the *boolean* ``True`` and never reaches
    # this field — the pin would silently do nothing and the
    # unknown-classifier check below could never fire (#405 review).
    classifiers: list[str] = field(
        default_factory=lambda: [RETRY_CLASSIFIER_LICENSE_QUEUE]
    )

    def validated(self) -> "RetryConfigFile":
        """Reject a budget that cannot mean what it says."""
        if self.attempts < 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry attempts must be >= 0 (got {self.attempts}); "
                "0 (or omitting the block) disables retry."
            )
        if self.backoff_sec < 0 or self.backoff_max_sec < 0:
            raise FatalRtlBuddyError(
                "cfg-dispatch retry backoff-sec/backoff-max-sec must be >= 0 "
                f"(got {self.backoff_sec}/{self.backoff_max_sec})."
            )
        if self.backoff_max_sec < self.backoff_sec:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry backoff-max-sec ({self.backoff_max_sec}) is "
                f"below backoff-sec ({self.backoff_sec}); the cap would shorten "
                "the very first delay."
            )
        if not 0 <= self.jitter < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry jitter must be in [0, 1) (got {self.jitter}); "
                "1 or more would allow a zero or negative delay."
            )
        unknown = [c for c in self.classifiers if c not in RETRY_CLASSIFIERS]
        if unknown:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry classifiers: unknown classifier(s) "
                f"{unknown} — known: {list(RETRY_CLASSIFIERS)}."
            )
        return RetryConfigFile(
            attempts=self.attempts,
            # float(), because the runtime object is arithmetic input: YAML
            # coerces on the way in, but nothing else does, and an int here
            # would trip the type-checked constructor.
            backoff_sec=float(self.backoff_sec),
            backoff_max_sec=float(self.backoff_max_sec),
            jitter=float(self.jitter),
            classifiers=list(self.classifiers),
        )

    @property
    def enabled(self) -> bool:
        """Would this budget ever retry anything?

        An ``attempts`` with an empty ``classifiers:`` retries nothing:
        there is no classifier left that could match, and treating that as
        "on" would make the head re-submit jobs no rule selected.
        """
        return self.attempts > 0 and bool(self.classifiers)


@serde
class DispatchConfigFile:
    """``cfg-dispatch`` section of root_config.yaml (raw serde form)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    # Reservation for the compile, wherever it runs. Normally that is the
    # head-dispatched build job (the compile runs on a compute node, never
    # the submit host); for a builder that cannot share a build the compile
    # happens inside each sim job instead, and this block is folded into
    # that job's reservation (see combine_for_in_job_compile). Defaults to
    # `resources` when unset; give it its own cpus/mem/time when the compile
    # is heavier than the sims — a large Verilation or a VCS elaboration
    # usually is. It also carries `parallel` (#495), which no per-job
    # `resources:` block has — hence its own serde class.
    compile: DispatchCompileFile | None = None
    sbatch_args: list[str] = field(rename="sbatch-args", default_factory=list)
    poll_interval: float = field(rename="poll-interval", default=10.0)
    # Cadence of the console progress/heartbeat line while the fleet drains
    # (#435). Deliberately NOT `poll-interval`: that paces the scheduler
    # query (10 s, so ~180 identical lines in a half-hour regression), while
    # this paces what a reader sees. 0 keeps a developer's terminal quiet;
    # the log file still records every change at INFO.
    progress_interval: float = field(rename="progress-interval", default=60.0)
    # Wall-clock bound on the collect wait. `None` (the default) is
    # unbounded — today's behaviour. Set it and a fleet that never leaves the
    # queue becomes a diagnosable failure naming the outstanding job ids,
    # instead of a head that blocks forever and silently.
    max_wait: float | None = field(rename="max-wait", default=None)
    # Cap on concurrently *running* elements PER submitted array
    # (sbatch --array=1-N%cap). Peak concurrency across a run is roughly
    # this times the number of arrays (resource groups x suites).
    max_jobs_per_array: int = field(rename="max-jobs-per-array", default=200)
    # The cluster's Slurm ``MaxArraySize`` (slurm.conf), which bounds how
    # many elements ONE array may hold. Slurm documents it as an exclusive
    # bound on the task index — "the maximum job array task index value
    # will be one less than MaxArraySize" — and rtl_buddy's manifests are
    # 1-based, so 1001 permits ``--array=1-1000``. A resource group larger
    # than that is split across several arrays rather than being refused by
    # sbatch (#509). ``None`` (the default) reads the value from ``scontrol
    # show config``; set it where the submit host cannot run scontrol, or to
    # split groups more finely than the cluster requires.
    max_array_size: int | None = field(rename="max-array-size", default=None)
    # Concurrent subprocesses for the `local-parallel` backend — one global
    # pool, not a per-array throttle (there are no arrays off a scheduler).
    # `None` means the backend's own default, min(4, cpu_count); `--jobs`
    # overrides this per invocation.
    jobs: int | None = None
    rightsize: RightsizeConfigFile | None = None
    # Retry budget for jobs the scheduler killed under a resource condition
    # while they were queueing for a license seat (#405). Absent = off.
    retry: RetryConfigFile | None = None

    def initialise(self) -> "DispatchConfig":
        """Validate and freeze into the runtime :class:`DispatchConfig`.

        This is where cross-field validation lives, mirroring every other
        ``*File.initialise()`` on ``RootConfig`` — so consumers never see
        the raw serde dataclass or an unvalidated ``poll-interval: 0``.
        """
        if self.poll_interval <= 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch poll-interval must be > 0 (got {self.poll_interval}); "
                "a zero interval turns collection into a squeue busy-loop."
            )

        def _validated(res):
            if res is None:
                return None
            return DispatchResourcesFile(
                cpus=res.cpus,
                mem=_validate_mem(res.mem),
                time=_validate_time(res.time),
            )

        def _validated_compile(res):
            """The compile block, through the same mem/time validators."""
            if res is None:
                return None
            return DispatchCompileFile(
                cpus=res.cpus,
                mem=_validate_mem(res.mem),
                time=_validate_time(res.time),
                parallel=res.parallel,
            )

        if self.progress_interval < 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch progress-interval must be >= 0 "
                f"(got {self.progress_interval}); use 0 to disable console "
                "progress lines."
            )
        if self.max_wait is not None and self.max_wait <= 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-wait must be > 0 when set (got {self.max_wait}); "
                "omit it for an unbounded wait."
            )
        if self.max_jobs_per_array < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-jobs-per-array must be >= 1 "
                f"(got {self.max_jobs_per_array})."
            )
        if self.max_array_size is not None and self.max_array_size < 2:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-array-size must be >= 2 when set (got "
                f"{self.max_array_size}); it is Slurm's MaxArraySize, whose "
                "largest task index is one BELOW it, so 2 is the smallest "
                "value that still permits a one-element array."
            )
        if self.jobs is not None and self.jobs < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch jobs must be >= 1 (got {self.jobs}); a pool of "
                "zero would never start a job."
            )
        if self.compile is not None and self.compile.parallel < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch compile parallel must be >= 1 (got "
                f"{self.compile.parallel}); a build job allowed zero concurrent "
                "builds would compile nothing."
            )
        return DispatchConfig(
            backend=self.backend,
            resources=_validated(self.resources),
            compile=_validated_compile(self.compile),
            sbatch_args=list(self.sbatch_args),
            poll_interval=self.poll_interval,
            progress_interval=self.progress_interval,
            max_wait=self.max_wait,
            max_jobs_per_array=self.max_jobs_per_array,
            max_array_size=self.max_array_size,
            jobs=self.jobs,
            rightsize=self.rightsize,
            retry=self.retry.validated() if self.retry is not None else None,
        )


@dataclass
class DispatchConfig:
    """Validated runtime dispatch configuration (see DispatchConfigFile)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    compile: DispatchCompileFile | None = None
    sbatch_args: list = None
    poll_interval: float = 10.0
    progress_interval: float = 60.0
    max_wait: float | None = None
    max_jobs_per_array: int = 200
    max_array_size: int | None = None
    jobs: int | None = None
    rightsize: RightsizeConfigFile | None = None
    retry: RetryConfigFile | None = None

    def __post_init__(self):
        if self.sbatch_args is None:
            self.sbatch_args = []

    def effective_rightsize(self) -> RightsizeConfigFile:
        return self.rightsize if self.rightsize is not None else RightsizeConfigFile()

    def effective_retry(self) -> RetryConfigFile:
        """The retry budget, present or not — the absent one retries nothing."""
        return self.retry if self.retry is not None else RetryConfigFile()


@dataclass
class JobResources:
    """Fully resolved reservation for one dispatched job.

    ``mem`` stays optional: on clusters where memory is not a schedulable
    resource an unconditional ``--mem`` would be rejected, so the flag is
    only emitted when a reservation was configured somewhere.
    """

    cpus: int = DEFAULT_JOB_CPUS
    mem: str | None = None
    time: str = DEFAULT_JOB_TIME


def resolve_resources(dispatch_cfg, test_cfg=None) -> JobResources:
    """Resolve a test's effective job reservation.

    Field-wise layering, most specific wins:
    test ``resources:`` > testbench ``resources:`` >
    ``cfg-dispatch.resources`` > built-in defaults.
    """
    resolved = JobResources()
    layers = [dispatch_cfg.resources if dispatch_cfg is not None else None]
    if test_cfg is not None:
        layers.append(getattr(test_cfg.get_testbench(), "resources", None))
        layers.append(getattr(test_cfg, "resources", None))
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            # Per-test/testbench resources: are raw serde and may carry the
            # YAML sexagesimal/int trap; validate as they are applied.
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved


# sbatch options that change what a job REQUESTS in cpus, i.e. that can make
# `ReqCPUS` differ from the cpus-per-task the head resolved. Two families:
# the cpu count itself, and the task/node counts `ReqCPUS` multiplies it by
# (`ReqCPUS` = tasks x cpus-per-task).
#
# The set is deliberately NARROW, because a false positive is not free: it
# discards a request the head knows, retargets the edit hint away from the
# YAML field that really governs, and disables the compile cpus floor. Only
# options that Slurm documents as changing the cpu REQUEST belong here.
# Three near misses, all excluded:
#
# - `--exclusive` and `--overcommit` change what is *allocated*, not what is
#   requested, so `ReqCPUS` — the fallback — still describes the reservation.
# - `--threads-per-core` and `-B`/`--extra-node-info` are node-SELECTION
#   constraints: they restrict which nodes and hardware threads may be used,
#   while the generated `--cpus-per-task` still states the request. The head
#   therefore still knows it, and must not throw it away (#505 review).
# - `--cpus-per-gpu` is documented as mutually exclusive with
#   `--cpus-per-task`, which `SlurmDispatchBackend._reservation_argv` emits
#   unconditionally on every job — so sbatch rejects the pair and the
#   "override" can never take effect. Detecting it would only degrade the
#   advice for a submission that never runs.
# - `--ntasks-per-core` and `--ntasks-per-socket` are documented as placement
#   MAXIMA ("request the maximum ntasks be invoked on each core/socket ...
#   meant to be used with the --ntasks option"): they cap where the tasks
#   `--ntasks` asked for may land, and a lone one requests nothing. The
#   `--ntasks` they accompany is in this set, so a real task-count change is
#   still caught. `--ntasks-per-gpu` is left out of THIS table on the same
#   footing — on its own it moves no cpu request — but it is not simply
#   ignored: paired with a GPU count and no `--ntasks` it derives the task
#   count, which `_gpu_derived_task_count` below picks up (#505 review).
#
# Keyed by the long form, valued by the short one, because the two are the
# SAME option: `[-c 4, --cpus-per-task=8]` is one option written twice (the
# last wins), not two multiplying each other.
#
# Split in two, because the difference decides whether advice can name one
# of them: a DIRECT cpu count states the request outright, so a whole-job
# suggestion can be written straight into it. A task or node count only
# *scales* it, so the suggested number is not a value that argument takes.
_DIRECT_CPU_COUNT_OPTS = {
    "--cpus-per-task": "-c",
}
_CPU_SCALING_OPTS = {
    # The task and node counts that raise the cpu request above one
    # cpus-per-task. `--ntasks-per-node` earns its place because sbatch
    # documents it as a REQUEST when `--ntasks` is absent ("request that
    # ntasks be invoked on each node ... meant to be used with the --nodes
    # option"), so `--nodes=2 --ntasks-per-node=4` asks for eight tasks; it
    # degrades to a maximum only when `--ntasks` is also given, and that
    # option is in this set too, so the pair is caught either way.
    "--ntasks": "-n",
    "--ntasks-per-node": None,
    "--nodes": "-N",
}
_CPU_REQUEST_OPTS = {**_DIRECT_CPU_COUNT_OPTS, **_CPU_SCALING_OPTS}
_CPU_REQUEST_SHORT_TO_LONG = {
    short: long for long, short in _CPU_REQUEST_OPTS.items() if short
}


def sbatch_arg_sets_cpu_count_directly(arg: str) -> bool:
    """Does this rendered ``sbatch-args`` entry state the cpu count itself?

    ``-c``/``--cpus-per-task`` names a number of cpus, so a whole-job
    suggestion can be written straight into it. ``--ntasks``,
    ``--ntasks-per-node`` and ``-N``/``--nodes`` are task and node counts:
    they raise the request rather than stating it, and telling a reader to
    put a cpu count into one of them would be advice that cannot be
    applied (#505 review).

    Takes an entry as :func:`sbatch_args_cpu_request_options` renders it —
    ``--cpus-per-task=4``, ``--cpus-per-task 4``, ``-c 4``, ``-c4``,
    ``-c=4`` — so the caller never has to re-parse sbatch syntax.
    """
    # Whichever separator came first, the option token is what precedes it.
    token = arg.split("=", 1)[0].split(" ", 1)[0]
    if token in _DIRECT_CPU_COUNT_OPTS:
        return True
    short = _DIRECT_CPU_COUNT_OPTS["--cpus-per-task"]
    # `-c`, `-c 4` and `-c=4` reduce to the bare short form; `-c4` keeps its
    # value, which must be numeric or this is some other option entirely.
    return token == short or (token.startswith(short) and token[len(short) :].isdigit())


# The `SBATCH_*` input environment variables that sbatch documents as "same
# as" one of the options above. They reach sbatch through `subprocess.run`,
# which inherits the head's environment, and rtl-buddy deliberately does NOT
# sanitize it — a site that exports these means them.
#
# `SBATCH_CPUS_PER_TASK` is absent for the same reason `--cpus-per-gpu` is:
# sbatch's documented precedence is command line > environment > script, and
# both submit paths emit `--cpus-per-task` unconditionally
# (`_reservation_argv` and the array submit), so the variable is always
# beaten by the flag rtl-buddy itself passes. It changes nothing, and
# treating it as an override would discard a request the head knows
# (#505 review). `tests/test_dispatch_slurm.py` pins that both paths still
# emit the flag, so this stays true.
_CPU_REQUEST_ENV_VARS = {
    "SBATCH_NTASKS": "--ntasks",
    "SBATCH_NTASKS_PER_NODE": "--ntasks-per-node",
    "SBATCH_NODES": "--nodes",
}


# `--ntasks-per-gpu` is a placement cap on its own (see above), but sbatch
# documents a second mode for it: "specify the GPUs wanted (e.g. via --gpus
# or --gres) without specifying --ntasks, and the total task count will be
# automatically determined". So a GPU count and `--ntasks-per-gpu` in the
# same verbatim `sbatch-args` list, with no `--ntasks` anywhere, derives
# tasks = gpus x ntasks-per-gpu — a task-count override exactly like
# `--ntasks`, and one the generated `--cpus-per-task` is then multiplied by
# (#505 review).
#
# `--gpus-per-task` is absent deliberately: sbatch documents it as mutually
# exclusive with `--ntasks-per-gpu`, so that pair never runs.
_GPU_COUNT_OPTS = {
    "--gpus": "-G",
    "--gpus-per-node": None,
    "--gpus-per-socket": None,
    # Only when it actually asks for gpus — `--gres=gpu:2`, not `--gres=fs:1`.
    "--gres": None,
}
_GPU_COUNT_ENV_VARS = {
    "SBATCH_GPUS": "--gpus",
    "SBATCH_GPUS_PER_NODE": "--gpus-per-node",
    "SBATCH_GPUS_PER_SOCKET": "--gpus-per-socket",
    "SBATCH_GRES": "--gres",
}
_NTASKS_PER_GPU_OPT = {"--ntasks-per-gpu": None}
_NTASKS_PER_GPU_ENV_VAR = "SBATCH_NTASKS_PER_GPU"


def _gpu_derived_task_count(sbatch_args, env) -> dict[str, str]:
    """The ``--gpus`` + ``--ntasks-per-gpu`` pair, when it sets the tasks.

    Both halves may come from either source, since sbatch reads both.
    Returns them together — the note has to name the pair, because neither
    argument alone did this and pointing at one of them would send a reader
    to a setting that is only half the cause.
    """
    per_gpu = _scan_options(sbatch_args, _NTASKS_PER_GPU_OPT)
    if not per_gpu:
        value = (env.get(_NTASKS_PER_GPU_ENV_VAR) or "").strip()
        if value:
            per_gpu = {"--ntasks-per-gpu": f"{_NTASKS_PER_GPU_ENV_VAR}={value}"}
    if not per_gpu:
        return {}
    gpus = _scan_options(sbatch_args, _GPU_COUNT_OPTS)
    # `--gres` carries many resource kinds; only a gpu one counts.
    gres = _scan_options(sbatch_args, {"--gres": None}, value_must_contain="gpu")
    gpus = {k: v for k, v in gpus.items() if k != "--gres"} | gres
    for var, option in _GPU_COUNT_ENV_VARS.items():
        value = (env.get(var) or "").strip()
        if not value or option in gpus:
            continue
        if option == "--gres" and "gpu" not in value.lower():
            continue
        gpus[option] = f"{var}={value}"
    if not gpus:
        # A lone `--ntasks-per-gpu` requests nothing: it caps placement of
        # tasks something else asked for. Round 10's exclusion stands.
        return {}
    return {**gpus, **per_gpu}


def cpu_request_overrides(sbatch_args, env=None) -> list[str]:
    """Everything that supersedes the cpus reservation the head resolved.

    The union of :func:`sbatch_args_cpu_request_options` and the
    ``SBATCH_*`` input environment variables that mean the same thing.
    Both reach sbatch — ``sbatch-args`` because it is appended after the
    generated flags, the environment because ``subprocess.run`` inherits
    it — so both can make ``ReqCPUS`` differ from what rtl-buddy resolved,
    and neither may be taken for the request (#505 review).

    Command line beats environment, which is sbatch's own precedence: a
    variable whose option is already written in ``sbatch-args`` is not
    reported, because it is not what the job ran with. An unset or blank
    variable is not an override at all.

    Environment entries are rendered ``NAME=value`` and argument entries
    keep their leading dash, so a caller can tell them apart by their first
    character.
    """
    found = _scan_cpu_request_args(sbatch_args)
    env = os.environ if env is None else env
    for var, option in _CPU_REQUEST_ENV_VARS.items():
        value = (env.get(var) or "").strip()
        if not value or option in found:
            continue
        found[option] = f"{var}={value}"
    # ...and the one combination that derives a task count rather than
    # stating it. Only when nothing states one: with `--ntasks` present
    # sbatch reads `--ntasks-per-gpu` the other way round, as the GPU count
    # to satisfy, and `--ntasks` is already in `found` (#505 review).
    if "--ntasks" not in found:
        found.update(_gpu_derived_task_count(sbatch_args, env))
    return list(found.values())


def sbatch_args_cpu_request_options(sbatch_args) -> list[str]:
    """The ``sbatch-args`` entries that decide the job's cpu request.

    ``cfg-dispatch.sbatch-args`` is appended verbatim *after* the generated
    reservation flags, so an entry there wins — which is the documented
    contract, and the reason right-sizing cannot always trust the
    reservation it resolved. A non-empty result means the resolved ``cpus``
    is not what the job was submitted with, so it must not be recorded as
    the request: the analysis falls back to the scheduler's own ``ReqCPUS``,
    and the ``cpus`` finding's edit hint names this key rather than the YAML
    field it masks (#505 review).

    Two families of option qualify, because ``ReqCPUS`` is *tasks x
    cpus-per-task*: the cpu count (``-c``/``--cpus-per-task``) and the
    task/node counts that raise it (``-n``/``--ntasks``,
    ``--ntasks-per-node``, ``-N``/``--nodes``). Placement maxima
    (``--ntasks-per-core``/``-socket``/``-gpu``), node-selection constraints
    (``--threads-per-core``, ``-B``/``--extra-node-info``), allocation
    modifiers (``--exclusive``, ``--overcommit``) and ``--cpus-per-gpu``
    (which sbatch rejects alongside the ``--cpus-per-task`` every job
    carries) are deliberately excluded — see the comment on
    ``_CPU_REQUEST_OPTS``.

    Returns one entry per DISTINCT option, in order of first appearance,
    each rendered as written. Within an option the LAST occurrence wins,
    because that is the one sbatch obeys — ``[-c, 4, --cpus-per-task=8]``
    runs with 8, and is one option, not two. Across options there is no
    "winner" at all: ``--ntasks`` and ``--cpus-per-task`` multiply, so a
    caller holding two entries knows the request is their product and that
    no single argument can be named as the one to edit.

    Only ``cpus`` needs this. ``mem`` and ``time`` advice is already
    measured against ``ReqMem``/``TimelimitRaw``, which sacct reports from
    the allocation any override actually produced.
    """
    return list(_scan_cpu_request_args(sbatch_args).values())


def _scan_cpu_request_args(sbatch_args) -> dict[str, str]:
    """Canonical long option -> the entry that set it, as written.

    Keyed so the environment layer in :func:`cpu_request_overrides` can
    apply sbatch's command-line-beats-environment precedence per option
    without re-parsing what this already worked out.
    """
    return _scan_options(sbatch_args, _CPU_REQUEST_OPTS)


def _scan_options(sbatch_args, long_to_short, *, value_must_contain=None):
    """Match one table of sbatch options against a verbatim argument list.

    Handles every spelling sbatch's getopt takes: ``--long=value``,
    ``--long value``, ``-x value`` and ``-x4``. ``value_must_contain``
    narrows a match to values mentioning a substring, which is how
    ``--gres`` is counted only when it asks for gpus.
    """
    args = list(sbatch_args or [])
    short_to_long = {short: long for long, short in long_to_short.items() if short}
    # Insertion-ordered by first appearance; re-assignment keeps that
    # position, so a repeated option stays where it was first written and
    # carries its last value.
    found: dict[str, str] = {}

    def keep(value):
        return value_must_contain is None or value_must_contain in (value or "").lower()

    for index, arg in enumerate(args):
        if arg in long_to_short or arg in short_to_long:
            # Value-in-the-next-argument form. A trailing flag with no value
            # is malformed sbatch input, but it is still an override of
            # intent, and sbatch — not right-sizing — is where it should be
            # reported.
            following = args[index + 1 : index + 2]
            if following and not keep(following[0]):
                continue
            canonical = short_to_long.get(arg, arg)
            found[canonical] = f"{arg} {following[0]}" if following else arg
            continue
        for long in long_to_short:
            if arg.startswith(f"{long}=") and keep(arg.split("=", 1)[1]):
                found[long] = arg
                break
        else:
            for short, long in short_to_long.items():
                # `-c4`/`-n4`, and `-c=4` defensively. A numeric value is
                # required, so an unrelated `-cfoo` is not matched.
                value = arg[len(short) :].lstrip("=") if arg.startswith(short) else ""
                if value and value[0].isdigit() and keep(value):
                    found[long] = arg
                    break
    return found


def compile_parallel(dispatch_cfg) -> int:
    """How many distinct builds one build job may compile concurrently (#495).

    Deliberately NOT a field of :class:`JobResources`: the resolved compile
    reservation is also what sizes an in-job compile's sim job and the
    right-sizing compile floor, and both of those are one serial build. The
    concurrency belongs to the build job alone, so it is read separately —
    and only by the code that builds that job's spec.
    """
    if dispatch_cfg is None or dispatch_cfg.compile is None:
        return 1
    return dispatch_cfg.compile.parallel


def mem_to_bytes(value) -> int | None:
    """Parse an sbatch ``--mem`` spelling to bytes; ``None`` if unparseable.

    Slurm's default unit is megabytes, so a bare number is MB — not bytes.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    scale = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    unit = text[-1].upper()
    if unit in scale:
        text, factor = text[:-1], scale[unit]
    else:
        factor = 2**20
    try:
        return int(float(text) * factor)
    except ValueError:
        return None


def time_to_seconds(value) -> int | None:
    """Parse an sbatch ``--time`` spelling to seconds; ``None`` if unparseable.

    Handles every form :data:`_TIME_RE` accepts. The ambiguity that matters
    is colon count: two colons is ``HH:MM:SS`` but one is ``MM:SS``, and a
    bare number is MINUTES.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not _TIME_RE.match(text):
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        days = int(day_part)
        # After DD-, the fields read left-to-right from hours: DD-HH[:MM[:SS]].
        fields = [int(p) for p in text.split(":")] if text else [0]
        fields += [0] * (3 - len(fields))
        hours, minutes, seconds = fields
    else:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 1:
            hours, minutes, seconds = 0, parts[0], 0  # bare number = minutes
        elif len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]  # MM:SS
        else:
            hours, minutes, seconds = parts  # HH:MM:SS
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def combine_for_in_job_compile(
    sim: JobResources, compile_: JobResources
) -> tuple[JobResources, dict]:
    """Reservation for a sim job that also compiles, and what governs it (#358).

    Compile and sim run inside the **same** scheduler job when the builder
    cannot share a build, and one allocation cannot carry two different
    reservations. The only safe combination is the element-wise maximum: a
    compile-sized ``mem`` paired with a sim-sized ``time`` still gets killed
    during a long elaboration, and the reverse OOMs during it.

    The second return value maps each field to the layer that supplied it
    (``"compile"`` where the compile reservation won, ``"test"`` otherwise),
    so reservation advice can name the field that actually governs rather
    than one the max has masked.
    """
    combined = JobResources(cpus=sim.cpus, mem=sim.mem, time=sim.time)
    governed_by = {"cpus": "test", "mem": "test", "time": "test"}

    if compile_.cpus > sim.cpus:
        combined.cpus = compile_.cpus
        governed_by["cpus"] = "compile"

    # An absent sim mem means "no --mem reservation"; a compile mem must
    # still take effect, since the compile is the phase that needs it.
    sim_mem, compile_mem = mem_to_bytes(sim.mem), mem_to_bytes(compile_.mem)
    if compile_.mem is not None and compile_mem is None:
        # Dropping the compile reservation from the max is the one outcome
        # this function exists to prevent, so an unparseable spelling must not
        # do it quietly. sbatch would reject the value at submit anyway.
        log_event(
            logger,
            logging.WARNING,
            "dispatch.compile_mem_unparseable",
            mem=compile_.mem,
        )
    if compile_mem is not None and (sim_mem is None or compile_mem > sim_mem):
        combined.mem = compile_.mem
        governed_by["mem"] = "compile"

    sim_time, compile_time = time_to_seconds(sim.time), time_to_seconds(compile_.time)
    if compile_time is not None and (sim_time is None or compile_time > sim_time):
        combined.time = compile_.time
        governed_by["time"] = "compile"

    return combined, governed_by


def resolve_compile_resources(dispatch_cfg, suite_compile=None) -> JobResources:
    """Resolve the reservation for the dispatched build job.

    The suite's own ``compile:`` block over ``cfg-dispatch.compile`` over
    ``cfg-dispatch.resources`` over the built-in defaults, field by field —
    so the build inherits the sim defaults unless the compile is called out
    separately, and a suite whose verilation is nothing like the rest of the
    repo's sizes only the fields it actually needs (#497).

    ``suite_compile`` is the suite-level block (a
    :class:`DispatchResourcesFile`, from ``SuiteConfig.get_compile()``);
    ``None`` where there is no suite in hand or the suite declared none. It
    is the MOST specific layer because the dispatched build job is per
    suite — there is no allocation a suite block could be sharing with
    another suite's compile.

    Note this is a *scheduling* fact only: nothing here reaches the compile
    fingerprint or the shared-build key, so writing a ``compile:`` block
    never invalidates a stamp.
    """
    resolved = JobResources()
    layers = []
    if dispatch_cfg is not None:
        layers += [dispatch_cfg.resources, dispatch_cfg.compile]
    layers.append(suite_compile)
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved


def compile_resource_origins(suite_compile) -> dict:
    """Which resolved compile fields the suite's ``compile:`` block won.

    ``{field: "suite"}`` for every field the suite block set; fields it
    left ``None`` are simply absent, meaning cfg-dispatch (or the built-in
    default) still governs them. Reservation advice reads this to point an
    edit hint at the file that actually holds the winning value (#497) —
    computed here, beside the layering it mirrors, so the two can never
    drift apart.
    """
    origins = {}
    if suite_compile is None:
        return origins
    for name in ("cpus", "mem", "time"):
        if getattr(suite_compile, name, None) is not None:
            origins[name] = "suite"
    return origins
