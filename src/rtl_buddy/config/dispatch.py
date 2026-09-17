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
      orphans: warn            # what an interrupted run's surviving jobs get
                               # on the next invocation: warn, cancel, adopt
      max-jobs-per-array: 200  # %N throttle on EACH submitted Slurm array
      max-array-size: 1001     # the cluster's Slurm MaxArraySize; omit it to
                               # read the value from `scontrol show config`
      max-array-tasks: 1000    # its SchedulerParameters=max_array_tasks, if the
                               # cluster caps tasks-per-array below that
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
      parallel: 1            # ...and how many builds it runs at once
    testbenches:
      - name: tb_chip_small   # ~6 GB, ~4 minutes
        filelist: [...]
      - name: tb_chip_t1      # the product geometry: ~130 GB, ~2 hours
        filelist: [...]
        compile:              # ...so it says so here (#551)
          mem: 256G
          time: "06:00:00"

:func:`resolve_compile_resources` layers its reservation fields
field-by-field over ``cfg-dispatch.compile`` over ``cfg-dispatch.resources``,
with a testbench's own ``compile:`` block the most specific layer of all
(#551); :func:`compile_parallel` layers ``parallel`` the same way (#547).
The build job is per suite, so a suite that compiles one key says
``parallel: 1`` and reserves ``cpus`` rather than ``cpus x`` the
cluster-wide value.

That one build job compiles every planned testbench, so its reservation is
AGGREGATED over them — :func:`aggregate_compile_resources`. The two tests.yaml
layers differ in what they describe: a suite block is whole-job and floors
the result, a testbench block is per build and is summed (``mem``) or
queued (``time``) with its siblings.
"""

import logging
import math
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

# What an interrupted run's surviving jobs get on the next invocation
# (#521). `warn` keeps every release before this one's behaviour: the
# orphans are named and a fresh fleet goes out beside them. It is the
# default because the other two act on jobs the user has not looked at
# yet — `cancel` destroys a fleet that may be minutes from finishing,
# and `adopt` binds this run's verdict to results it did not submit.
ORPHANS_POLICIES = ("warn", "cancel", "adopt")
ORPHANS_DEFAULT = "warn"

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

    The suite-level ``compile:`` block has its own class again
    (:class:`SuiteCompileFile`) for the same reason and one more: there
    ``parallel`` must be optional, so that a suite overriding only ``mem``
    does not also pin the concurrency to this class's default of 1 (#547).
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


def _validate_compile_mem(value):
    """:func:`_validate_mem`, plus the parse the build job depends on.

    A compile reservation is not only passed to sbatch: the build job's is
    AGGREGATED — summed across the testbench blocks that can compile at the
    same time and floored at the whole-job value — so every one of them has
    to be a number rtl_buddy can add up (#551 review). A spelling
    :func:`mem_to_bytes` cannot read would otherwise drop silently out of
    that sum and shrink the reservation, which is the one outcome the
    aggregation exists to prevent. Rejected at load instead, with the same
    parser the aggregation uses so the two can never disagree about what a
    legal value is.
    """
    text = _validate_mem(value)
    if text is None:
        return None
    parsed = mem_to_bytes(text)
    if parsed is None:
        raise FatalRtlBuddyError(
            f"dispatch resources: mem {value!r} is not a value Slurm "
            "understands (expected bytes, or a number with a K/M/G/T suffix "
            "such as 512M or 16G)."
        )
    if parsed <= 0:
        # `-8G` parses cleanly and would be SUBTRACTED from the build job's
        # sum, quietly shrinking a reservation the rest of the suite needs;
        # `0` reserves nothing at all (#551 review round 3).
        raise FatalRtlBuddyError(
            f"dispatch resources: mem {value!r} must be greater than zero; a "
            "compile reservation is summed across the builds that run at "
            "once, and a negative one would shrink it."
        )
    return text


def _validate_compile_time(value):
    """:func:`_validate_time`, plus the "greater than zero" the sum needs.

    ``_TIME_RE`` already refuses a leading ``-``, so the case this adds is
    ``"0"`` (and ``"00:00:00"``): a build allowed no wall clock at all
    contributes nothing to the schedule and is killed the moment it starts
    (#551 review round 3).
    """
    text = _validate_time(value)
    if text is None:
        return None
    if not time_to_seconds(text):
        raise FatalRtlBuddyError(
            f'dispatch resources: time "{value}" must be greater than zero.'
        )
    return text


def _validate_compile_cpus(value):
    """A compile reservation's ``cpus``; ``None`` in, ``None`` out."""
    if value is not None and value < 1:
        raise FatalRtlBuddyError(
            f"dispatch resources: cpus {value!r} must be at least 1."
        )
    return value


@serde
class TestbenchCompileFile:
    """A testbench's own ``compile:`` block in tests.yaml (#551).

    The same three reservation fields as :class:`DispatchResourcesFile`,
    and a ``parallel`` that exists only to be REJECTED. Unknown keys are
    dropped silently by serde, so a project writing ``parallel:`` on a
    testbench would otherwise get no reservation change and no error — and
    the key reads as if it meant something here, because it does one level
    up. There is exactly one build job per suite and it compiles every
    testbench, so "how many builds at once" cannot be a property of one of
    them; :func:`validate_testbench_compile_block` says so at load.

    These three fields are PER BUILD, unlike the suite-level block's, which
    stay whole-job — see :func:`aggregate_compile_resources`.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # Accepted by the schema, refused by the validator. See the class
    # docstring: silence here would be worse than an error.
    parallel: int | None = None


def validate_testbench_compile_block(res):
    """Validate a raw testbench ``compile:`` block; return a fresh copy.

    :func:`validate_resources_block`'s rules, with ``mem`` held to the
    stricter parse the build job's aggregation needs, plus the refusal of
    ``parallel`` (#551). The caller prefixes the testbench name, matching
    the other errors ``TestbenchConfig`` raises.

    ``None`` in, ``None`` out.
    """
    if res is None:
        return None
    if getattr(res, "parallel", None) is not None:
        raise FatalRtlBuddyError(
            "parallel is not accepted on a testbench compile block; set it "
            "at suite level (compile.parallel) or in cfg-dispatch.compile."
        )
    return TestbenchCompileFile(
        cpus=_validate_compile_cpus(res.cpus),
        mem=_validate_compile_mem(res.mem),
        time=_validate_compile_time(res.time),
    )


@serde
class SuiteCompileFile:
    """A suite's own top-level ``compile:`` block in tests.yaml (#497, #547).

    :class:`DispatchResourcesFile`'s three reservation fields plus
    ``parallel``, which layers over ``cfg-dispatch.compile.parallel``
    exactly as the other three layer over their root counterparts. The
    build job is per suite — there is no allocation two suites' compiles
    share — so the suite is entitled to say how many builds its own job
    runs at once, and a one-key suite that says ``parallel: 1`` reserves
    ``cpus`` instead of ``cpus x`` the cluster-wide value (#547).

    Its own class, deliberately, and not either of the two neighbours:

    * not :class:`DispatchResourcesFile`, which is also the serde type
      behind every per-test and per-testbench ``resources:`` block, where
      "compile N builds at once" means nothing (#495);
    * not :class:`DispatchCompileFile`, whose ``parallel`` defaults to 1
      rather than to ``None``. Here the two have to stay distinguishable:
      a suite that overrides only ``mem`` must keep inheriting the root
      concurrency, and a class defaulting to 1 would silently pin every
      such suite's build job to one build at a time.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # ``None`` means "inherit cfg-dispatch.compile.parallel", which is the
    # whole difference from DispatchCompileFile — see the class docstring.
    parallel: int | None = None


def validate_compile_block(res):
    """Validate a raw suite-level ``compile:`` block; return a fresh copy.

    :func:`validate_resources_block` plus ``parallel`` (#547): the same
    single home for the YAML 1.1 sexagesimal trap, and the same ``>= 1``
    rule ``cfg-dispatch.compile.parallel`` is held to, so the two layers
    cannot disagree about what a legal value is. The caller prefixes the
    suite path, matching the root key's message otherwise word for word.

    ``None`` in, ``None`` out.
    """
    if res is None:
        return None
    parallel = getattr(res, "parallel", None)
    if parallel is not None and parallel < 1:
        raise FatalRtlBuddyError(
            f"compile parallel must be >= 1 (got {parallel}); a build job "
            "allowed zero concurrent builds would compile nothing."
        )
    return SuiteCompileFile(
        cpus=res.cpus,
        mem=_validate_mem(res.mem),
        time=_validate_time(res.time),
        parallel=parallel,
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
    # What the next invocation does about a previous run's jobs that
    # outlived their head — a Ctrl-C too late to cancel, a killed session
    # (#521). `warn` (the default) names them and submits a fresh fleet,
    # exactly as every release before this one did; `cancel` scancels them
    # first; `adopt` collects them instead of submitting anything. Only
    # ever consulted for a scheduler-backed backend: a local-parallel
    # pool's jobs are the head's own children and die with it.
    orphans: str = ORPHANS_DEFAULT
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
    # The cluster's ``SchedulerParameters=max_array_tasks``, the SECOND
    # ceiling on one array: slurm.conf calls it "the maximum number of
    # tasks that be included in a job array", an inclusive COUNT rather
    # than ``MaxArraySize``'s exclusive index bound, and a cluster may set
    # it well below. It has its own field because it is its own limit —
    # pinning a smaller ``max-array-size`` to stand in for it would state
    # the wrong MaxArraySize and mislead every message derived from it.
    # ``None`` (the default) reads it from ``scontrol show config``
    # alongside MaxArraySize; the effective slice is the smaller of the two
    # ceilings, whichever layer each came from (#509).
    max_array_tasks: int | None = field(rename="max-array-tasks", default=None)
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
        if self.max_array_tasks is not None and self.max_array_tasks < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-array-tasks must be >= 1 when set (got "
                f"{self.max_array_tasks}); it is Slurm's max_array_tasks, a "
                "COUNT of the tasks one array may hold, so 1 is the smallest "
                "value that still permits an array."
            )
        if self.orphans not in ORPHANS_POLICIES:
            raise FatalRtlBuddyError(
                f"cfg-dispatch orphans must be one of "
                f"{', '.join(ORPHANS_POLICIES)} (got {self.orphans!r})."
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
            orphans=self.orphans,
            max_jobs_per_array=self.max_jobs_per_array,
            max_array_size=self.max_array_size,
            max_array_tasks=self.max_array_tasks,
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
    orphans: str = ORPHANS_DEFAULT
    max_jobs_per_array: int = 200
    max_array_size: int | None = None
    max_array_tasks: int | None = None
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


def compile_parallel(dispatch_cfg, suite_compile=None) -> int:
    """How many distinct builds one build job may compile concurrently (#495).

    Deliberately NOT a field of :class:`JobResources`: the resolved compile
    reservation is also what sizes an in-job compile's sim job and the
    right-sizing compile floor, and both of those are one serial build. The
    concurrency belongs to the build job alone, so it is read separately —
    and only by the code that builds that job's spec.

    ``suite_compile`` is the suite's own ``compile:`` block (a
    :class:`SuiteCompileFile`, from ``SuiteConfig.get_compile()``), and its
    ``parallel`` wins outright where it is set — the same "most specific
    layer" rule :func:`resolve_compile_resources` applies to the
    reservation fields, and for the same reason: the build job is per suite
    (#547). ``getattr``, so a caller still holding an older
    ``DispatchResourcesFile``-shaped block reads as "inherit" rather than
    raising.
    """
    suite_parallel = getattr(suite_compile, "parallel", None)
    if suite_parallel is not None:
        return suite_parallel
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


def _compile_mem_bytes(value, *, testbench=None):
    """Parse a compile reservation's ``mem`` to bytes, or fail loudly (#551).

    :func:`aggregate_compile_resources` ADDS these up, so a spelling
    :func:`mem_to_bytes` cannot read has no safe fallback: dropping it from
    the sum shrinks the reservation and the build is OOM-killed with
    nothing in the log to say why. The same parser
    :func:`_validate_compile_mem` holds a testbench block to at load, so a
    value that loads always aggregates and this can only fire for a layer
    validated before that rule existed.

    ``None`` in, ``None`` out.
    """
    if value is None:
        return None
    parsed = mem_to_bytes(value)
    whose = f"testbench {testbench!r}: " if testbench else ""
    if parsed is None:
        raise FatalRtlBuddyError(
            f"{whose}compile mem {value!r} is not a value Slurm understands "
            "(expected bytes, or a number with a K/M/G/T suffix such as 512M "
            "or 16G); the build job's reservation is summed from these, so an "
            "unparseable one cannot be sized around."
        )
    if parsed <= 0:
        # Belt and braces beside the load-time rule: a value that reached
        # the sum negative would be SUBTRACTED from it, so the build job
        # would reserve less than the suite that has nothing wrong with it.
        raise FatalRtlBuddyError(
            f"{whose}compile mem {value!r} must be greater than zero; the "
            "build job's reservation is summed from these, and a negative "
            "one would shrink it."
        )
    return parsed


def format_mem(bytes_val: int) -> str:
    """Bytes → sbatch-friendly integer ``M``/``G`` string (rounded up).

    The inverse of :func:`mem_to_bytes`, and beside it (#551): the build
    job's reservation is now summed in bytes and has to be written back as
    an sbatch spelling, so the pair belongs in one place rather than one
    here and one in the right-sizing module that also imports it.
    """
    mb = math.ceil(bytes_val / 2**20)
    if mb >= 4096:
        return f"{math.ceil(mb / 1024)}G"
    return f"{mb}M"


def format_time(seconds: float) -> str:
    """Seconds → ``HH:MM:SS`` rounded up to the whole minute."""
    minutes = math.ceil(seconds / 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


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


def resolve_compile_resources(
    dispatch_cfg, suite_compile=None, tb_compile=None
) -> JobResources:
    """Resolve the compile reservation for ONE testbench.

    The testbench's own ``compile:`` block over the suite's over
    ``cfg-dispatch.compile`` over ``cfg-dispatch.resources`` over the
    built-in defaults, field by field — so the build inherits the sim
    defaults unless the compile is called out separately, and a suite whose
    verilation is nothing like the rest of the repo's sizes only the fields
    it actually needs (#497).

    ``suite_compile`` is the suite-level block (a
    :class:`SuiteCompileFile`, from ``SuiteConfig.get_compile()``);
    ``None`` where there is no suite in hand or the suite declared none.

    ``tb_compile`` is a testbench's own ``compile:`` block (a
    :class:`DispatchResourcesFile`, from ``TestbenchConfig.compile``) and is
    the MOST specific layer (#551): one suite can hold two entries whose
    verilations differ by an order of magnitude — the same top level at two
    geometries — and forcing the suite block to state the larger one fences
    that reservation off for every build in the suite. ``None`` for a
    testbench that declared none, and for every caller that resolves a
    suite-wide figure rather than one build's.

    ``parallel`` is not resolved here and never reaches the returned
    :class:`JobResources`: this reservation also sizes an in-job compile's
    sim job and the right-sizing compile floor, and both of those are one
    serial build. :func:`compile_parallel` layers that key instead (#547).

    Note this is a *scheduling* fact only: nothing here reaches the compile
    fingerprint or the shared-build key, so writing a ``compile:`` block
    never invalidates a stamp.
    """
    resolved = JobResources()
    layers = []
    if dispatch_cfg is not None:
        layers += [dispatch_cfg.resources, dispatch_cfg.compile]
    layers += [suite_compile, tb_compile]
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


def compile_resource_origins(suite_compile, tb_compile=None) -> dict:
    """Which tests.yaml layer won each resolved compile field.

    ``{field: "suite"}`` for every field the suite block set and
    ``{field: "testbench"}`` for every field the testbench block set — the
    latter last, because it is the more specific layer and
    :func:`resolve_compile_resources` applies it last too. Fields neither
    set are simply absent, meaning cfg-dispatch (or the built-in default)
    still governs them. Reservation advice reads this to point an edit hint
    at the file *and the key* that actually hold the winning value (#497,
    #551) — computed here, beside the layering it mirrors, so the two can
    never drift apart.

    ``parallel`` is in the map too (#547), even though nothing suggests a
    value for it: the `cpus` advice names the key in prose as the other
    lever, and saying ``cfg-dispatch.compile.parallel`` where the suite's
    own block governs would send a reader to a value editing which moves
    this job's reservation not at all. It has no testbench layer — one
    build job per suite runs every testbench's builds, so the concurrency
    cannot be a property of one of them.
    """
    origins = {}
    for name in ("cpus", "mem", "time", "parallel"):
        if getattr(suite_compile, name, None) is not None:
            origins[name] = "suite"
    for name in ("cpus", "mem", "time"):
        if getattr(tb_compile, name, None) is not None:
            origins[name] = "testbench"
    return origins


def compile_parallel_origin(suite_owned: bool, suite_path=None) -> str:
    """How to spell the key that governs ``compile.parallel`` (#547).

    One home for the spelling, because four things say it and they must
    agree: the build job's pool line, the ``cpus`` advice's "other lever"
    sentence, the withheld-advice line, and the advice table's footer. A
    reader told to size ``cfg-dispatch.compile.parallel`` when their own
    tests.yaml sets the key would edit a value that moves that job not at
    all — the same unappliable-advice failure the per-field ``edit_hint``
    origins exist to prevent (#497).

    ``suite_owned`` is whether the suite's ``compile:`` block set
    ``parallel``; ``suite_path`` is that suite's config path, named by its
    basename so the line says which file to open even where several suites
    are in one report. Without a path there is nothing honest to name, so
    the root key stands — it is what governs whenever the suite does not.
    """
    if suite_owned and suite_path:
        return f"{os.path.basename(str(suite_path))} compile.parallel"
    return "cfg-dispatch.compile.parallel"


def greedy_schedule(durations, parallel):
    """Schedule ``durations`` over ``parallel`` workers, in list order (#551).

    The build job hands its groups to a ThreadPool in plan order, so each
    build goes to whichever worker frees up first and the job ends when the
    last worker does. Returns ``(makespan, workers, finish)`` — the worker
    lists hold indices into ``durations``, and ``finish`` is each worker's
    end time, so a caller can ask which workers the job is waiting on.

    Shared with right-sizing, which re-runs it on a PROPOSED edit: raising
    one repeated build can push a later copy of it behind a neighbour and
    move less wall clock than the arithmetic predicted, so the only honest
    check is to schedule the change and look (#551 review round 6).
    """
    durations = list(durations)
    if not durations:
        return 0, [], []
    slots = max(1, min(int(parallel or 1), len(durations)))
    workers = [[] for _ in range(slots)]
    finish = [0] * slots
    for index, value in enumerate(durations):
        slot = min(range(slots), key=lambda k: finish[k])
        workers[slot].append(index)
        finish[slot] += value
    return max(finish), workers, finish


def aggregate_compile_resources(
    dispatch_cfg, suite_compile=None, testbenches=(), parallel=1
) -> tuple[JobResources, dict]:
    """The build job's reservation over the testbenches it will compile (#551).

    Named for what it does: the fields are combined, not compared. `mem`
    adds up and `time` queues; only `cpus` is a maximum.

    One build job per suite compiles every planned config, so its single
    allocation has to cover all of them at once. The two tests.yaml layers
    mean different things here, and the aggregation is what keeps them
    honest (#551 review):

    * the suite-level ``compile:`` block is WHOLE-JOB, exactly as it has
      been since #497 — a project sizes it for the job it watches in
      ``squeue``, and nothing below may take the reservation under it;
    * a testbench ``compile:`` block is PER BUILD — it describes one
      verilation, so several of them have to be combined rather than
      compared.

    Per field, therefore, over the testbenches that state a block of their
    own, floored at the suite-resolved (whole-job) value:

    ``cpus``
        the largest block's, because the head multiplies this by
        ``parallel`` afterwards; a build needing 8 cores needs 8 whether or
        not its neighbour needs 2.
    ``mem``
        the sum of the largest ``min(parallel, n)`` per-build figures.
        Memory is additive: the builds in flight together each hold their
        own peak, and the widest such set is the one to survive. Once ANY
        planned build states its own, the builds that state none take the
        figure the whole-job value implies for one build, so an annotated
        testbench sharing a slot with an unannotated one is sized for both
        (#551 review round 5).
    ``time``
        the makespan of the build job's own work queue: each build goes to
        whichever of the ``parallel`` workers frees up first, in plan
        order, which is the schedule its ThreadPool actually runs. At
        ``parallel: 1`` that is the serial total, and with a worker per
        build it is the longest one. ``ceil(sum / parallel)`` is only a
        LOWER bound in between — 30, 30 and 20 minutes over two workers
        finish in 50, not 40 — and a lower bound is the wrong side to
        reserve from (#551 review round 2).

    A build with no block of its own contributes to neither the ``cpus``
    nor the ``time`` figure: it is covered by the suite-level whole-job
    value, and adding an inherited wall clock per build would make a suite
    of five plain benches with ``time: 30m`` reserve two and a half hours.
    ``mem`` is the exception above, and only once some build has stated
    one: memory is the field where two builds genuinely need their peaks
    at the same instant, while an unannotated build's wall clock is
    unknown and the whole-job figure already describes the queue it runs
    in. A suite that states no per-testbench ``mem`` at all is untouched,
    so ``compile.mem`` keeps the whole-job meaning #497 gave it.

    ``testbenches`` is ``(name, tb_compile)`` once per planned BUILD — not
    once per testbench in the file, and not once per selected test. Two
    tests on one testbench that differ in plusdefines, builder, model or
    assertions compile separately and each hold their own peak, so the
    caller keys the list on those ingredients; two that share them are one
    compile however many tests they are, unless the caller cannot know
    that (a `preproc:` hook, or a builder that compiles per test). A testbench nobody selected contributes no build,
    and letting it inflate the reservation is exactly the fencing-off this
    issue removes. ``()`` resolves the suite-wide figure alone, which is
    what a caller with no plan in hand (and every suite whose testbenches
    declare nothing) gets today.

    Returns the reservation and, beside it, the provenance of each field::

        {field: {"origin": "testbench"|"suite"|"cfg-dispatch",
                 "testbench": name|None,
                 "sources": [{"origin": ..., "testbench": ...}, ...],
                 "aggregated": bool,
                 "contributors": [{"origin": ..., "testbench": ...}, ...]}}

    ``origin``/``testbench`` name the one place to edit. ``sources`` lists
    EVERY source that independently produces the winning value, and
    ``contributors``/``aggregated`` say whether it is a SUM of several
    builds at all. Right-sizing withholds a ``reduce`` for either — no
    single edit lowers a tied value, and a whole-job suggestion written
    into one contributor of a sum leaves the total where it was (#551
    review).

    Raises :class:`FatalRtlBuddyError` for a ``mem`` it cannot parse: the
    sum is taken in bytes, and a value silently dropped out of it would
    shrink the reservation.
    """
    parallel = max(1, int(parallel or 1))
    # Only planned builds that state a block of their own take part in the
    # aggregation; the rest ride on the whole-job floor below.
    blocks = [
        (name, block)
        for name, block in testbenches
        if any(getattr(block, f, None) is not None for f in ("cpus", "mem", "time"))
    ]
    floor = resolve_compile_resources(dispatch_cfg, suite_compile)
    floor_origins = compile_resource_origins(suite_compile)
    resolved = JobResources(cpus=floor.cpus, mem=floor.mem, time=floor.time)
    origins = {}

    def _floor_source(field_name):
        return {
            "origin": floor_origins.get(field_name, "cfg-dispatch"),
            "testbench": None,
        }

    def _tb_source(name):
        return {"origin": "testbench", "testbench": name}

    def _dedupe(sources):
        """Collapse repeats: two planned builds can share one YAML key."""
        out = []
        for source in sources:
            if source not in out:
                out.append(source)
        return out

    def _record(field_name, winners, contributors=(), primary_value=None, **extra):
        """Record what produced this field's value, and how.

        ``winners`` are the sources that INDEPENDENTLY produce it — more
        than one and no single edit can lower it. ``contributors`` are the
        builds whose values were ADDED to reach it; more than one and the
        number cannot be decomposed back into an edit at all (#551 review).
        """
        winners = _dedupe(winners) or [_floor_source(field_name)]
        # NOT deduplicated: two planned builds can share one YAML key, and
        # their values still add up twice. The count is the whole point.
        contributors = list(contributors)
        origins[field_name] = {
            "origin": winners[0]["origin"],
            "testbench": winners[0]["testbench"],
            # Every source that produces the same number, so a `reduce` no
            # single edit could apply is withheld rather than aimed at one
            # of several tied sources.
            "sources": winners,
            # ...and, separately, whether the number is a sum at all: a
            # whole-job suggestion written into one contributor's key
            # leaves the aggregate where it was.
            "aggregated": len(contributors) > 1,
            "contributors": contributors,
            # The primary contributor's OWN value, in the field's native
            # units (bytes for mem, seconds for time). Right-sizing needs
            # it to turn a whole-job suggestion into the number to write:
            # a `raise` naming one contributor of a sum has to say what
            # THAT key becomes, or applying it re-aggregates over the
            # target (#551 review round 3).
            "contributor_value": primary_value,
            **extra,
        }

    # --- cpus: the widest single build, never below the whole-job value ---
    # Not summed: the head multiplies this by `parallel` afterwards, so a
    # build needing 8 cores needs 8 whether or not its neighbour needs 2.
    cpus_bids = [(block.cpus, name) for name, block in blocks if block.cpus is not None]
    resolved.cpus = max([value for value, _ in cpus_bids] + [floor.cpus])
    cpus_winners = [_tb_source(n) for v, n in cpus_bids if v == resolved.cpus]
    if floor.cpus == resolved.cpus:
        cpus_winners.append(_floor_source("cpus"))
    _record("cpus", cpus_winners)

    # --- mem: the builds that can overlap each hold their own peak -------
    floor_mem = _compile_mem_bytes(floor.mem)
    stated_mem = [
        (_compile_mem_bytes(block.mem, testbench=name), name, str(block.mem))
        for name, block in testbenches
        if getattr(block, "mem", None) is not None
    ]
    # Once ANY planned build states its own mem, the suite has moved to
    # per-build sizing — and a build that states none still occupies a slot
    # beside it. Sizing only the stated ones would let a 256G testbench
    # overlap an unannotated build and request 256G for both (#551 review
    # round 5), so every other planned build contributes the figure the
    # whole-job value implies for one build.
    #
    # Gated on there being a stated one, deliberately. A suite that names
    # no per-testbench mem is a pure #497 suite, where `compile.mem` means
    # what the docs have always asked projects to write — the whole job at
    # `parallel` concurrent builds — and multiplying it here would
    # double-size every such suite that ever set `parallel`.
    implicit_mem = (
        [(floor_mem, None, floor.mem)]
        * sum(1 for _, block in testbenches if getattr(block, "mem", None) is None)
        if stated_mem and floor_mem
        else []
    )
    mem_bids = sorted(
        stated_mem + implicit_mem,
        key=lambda bid: bid[0],
        reverse=True,
    )
    # Only `parallel` of them are ever in flight together, so only the
    # widest that many add up; the rest wait for a slot.
    overlapping = mem_bids[:parallel]
    summed_mem = sum(value for value, _, _ in overlapping)
    winning_mem = max(summed_mem, floor_mem or 0)
    mem_winners = []
    mem_contributors = []
    mem_primary = None
    if overlapping and summed_mem == winning_mem:
        # The largest contributor is named as the lever, but the whole set
        # is recorded: a sum of several cannot be decomposed into one edit.
        top = overlapping[0][0]
        mem_winners = [
            _tb_source(n) if n else _floor_source("mem")
            for v, n, _ in overlapping
            if v == top
        ]
        mem_primary = top
        mem_contributors = [
            _tb_source(n) if n else _floor_source("mem") for _, n, _ in overlapping
        ]
    floor_binds_mem = floor_mem is not None and floor_mem == winning_mem
    if floor_binds_mem:
        mem_winners.append(_floor_source("mem"))
        # Keep the spelling the config already uses where the whole-job
        # value binds — the common case, which must stay byte-identical.
        resolved.mem = floor.mem
    elif len(overlapping) == 1:
        resolved.mem = overlapping[0][2]
    elif overlapping:
        resolved.mem = format_mem(winning_mem)
    _record("mem", mem_winners, mem_contributors, mem_primary)

    # --- time: the makespan of the build job's own work queue ------------
    time_bids = [
        (time_to_seconds(block.time), name, str(block.time))
        for name, block in blocks
        if block.time is not None
    ]
    time_bids = [bid for bid in time_bids if bid[0] is not None]
    floor_time = time_to_seconds(floor.time)
    # The build job hands its groups to a ThreadPool in plan order, so the
    # schedule is a greedy list schedule: each build goes to whichever
    # worker frees up first, and the job ends when the last worker does.
    # `ceil(sum / parallel)` is only a LOWER bound on that — 30, 30 and 20
    # minutes over two workers finish in 50, not 40 — and a lower bound is
    # exactly the wrong side to reserve from (#551 review).
    makespan, workers, finish = greedy_schedule(
        [value for value, _, _ in time_bids], parallel
    )
    # No separate "longest single build" floor: a greedy schedule never
    # finishes before its longest element, so the makespan already contains
    # it — and at `parallel: 1` it is the serial total.
    winning_time = max(makespan, floor_time or 0)
    time_winners = []
    time_contributors = []
    time_primary = None
    if time_bids and makespan == winning_time:
        # The builds on the worker that finishes last are what the job is
        # waiting for: one of them is a lever, several are a sum.
        #
        # ...and SEVERAL workers can finish at the makespan at once — two
        # 60-minute builds over two workers, say. Each of them holds the
        # job there on its own, so shortening the builds on one leaves the
        # wall clock exactly where it was: every critical worker's longest
        # build is a tied source, and the `reduce` is withheld rather than
        # aimed at one of them (#551 review round 4). Sources that resolve
        # to the same testbench collapse in `_record`, which is right —
        # one edit does move every worker running that build.
        critical_workers = [
            group for group, done in zip(workers, finish) if done == makespan
        ]
        critical = critical_workers[0]
        time_contributors = [_tb_source(time_bids[i][1]) for i in critical]
        longest = max(time_bids[i][0] for i in critical)
        for group in critical_workers:
            group_longest = max(time_bids[i][0] for i in group)
            time_winners += [
                _tb_source(time_bids[i][1])
                for i in group
                if time_bids[i][0] == group_longest
            ]
        time_primary = longest
        if len(critical) == 1:
            # One build decides the wall clock: its own spelling is the
            # answer, and no reformatting can drift from the config.
            resolved.time = time_bids[critical[0]][2]
        else:
            resolved.time = format_time(winning_time)
    if floor_time is not None and floor_time == winning_time:
        time_winners.append(_floor_source("time"))
        # The whole-job value binds: keep the spelling the config uses.
        resolved.time = floor.time
    _record(
        "time",
        time_winners,
        time_contributors,
        time_primary,
        # The queue itself, in plan order, so right-sizing can re-run the
        # schedule on a proposed edit instead of assuming the assignment
        # survives it (#551 review round 6). Names, because the edit is to
        # a testbench's key and every build of that testbench moves with
        # it. Internal to the provenance map — no machine-output surface.
        schedule=[(name, value) for value, name, _ in time_bids],
        parallel=parallel,
    )
    return resolved, origins
