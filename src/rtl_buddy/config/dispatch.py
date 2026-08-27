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
"""

import logging
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


def resolve_compile_resources(dispatch_cfg) -> JobResources:
    """Resolve the reservation for the dispatched build job.

    ``cfg-dispatch.compile`` over ``cfg-dispatch.resources`` over the
    built-in defaults, field by field — so the build inherits the sim
    defaults unless the compile is called out separately.
    """
    resolved = JobResources()
    if dispatch_cfg is None:
        return resolved
    for layer in [dispatch_cfg.resources, dispatch_cfg.compile]:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved
