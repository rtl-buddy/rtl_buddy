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
      sbatch-args:             # passed through to sbatch verbatim
        - --partition=verif
      poll-interval: 10        # seconds between queue polls while collecting

Per-test reservation overrides use the same ``resources`` shape in
tests.yaml at testbench and test level; :func:`resolve_resources` layers
them field-by-field (test over testbench over ``cfg-dispatch`` defaults).
"""

import re
from dataclasses import dataclass

from serde import field, serde

from ..errors import FatalRtlBuddyError

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


@serde
class DispatchConfigFile:
    """``cfg-dispatch`` section of root_config.yaml (raw serde form)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    # Reservation for the head-dispatched build job (Verilation runs on a
    # compute node, never the submit host). Defaults to `resources` when
    # unset; give it its own cpus/mem/time when the compile is heavier than
    # the sims (a large Verilation often is).
    compile: DispatchResourcesFile | None = None
    sbatch_args: list[str] = field(rename="sbatch-args", default_factory=list)
    poll_interval: float = field(rename="poll-interval", default=10.0)
    # Cap on concurrently *running* elements PER submitted array
    # (sbatch --array=1-N%cap). Peak concurrency across a run is roughly
    # this times the number of arrays (resource groups x suites).
    max_jobs_per_array: int = field(rename="max-jobs-per-array", default=200)
    rightsize: RightsizeConfigFile | None = None

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

        if self.max_jobs_per_array < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-jobs-per-array must be >= 1 "
                f"(got {self.max_jobs_per_array})."
            )
        return DispatchConfig(
            backend=self.backend,
            resources=_validated(self.resources),
            compile=_validated(self.compile),
            sbatch_args=list(self.sbatch_args),
            poll_interval=self.poll_interval,
            max_jobs_per_array=self.max_jobs_per_array,
            rightsize=self.rightsize,
        )


@dataclass
class DispatchConfig:
    """Validated runtime dispatch configuration (see DispatchConfigFile)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    compile: DispatchResourcesFile | None = None
    sbatch_args: list = None
    poll_interval: float = 10.0
    max_jobs_per_array: int = 200
    rightsize: RightsizeConfigFile | None = None

    def __post_init__(self):
        if self.sbatch_args is None:
            self.sbatch_args = []

    def effective_rightsize(self) -> RightsizeConfigFile:
        return self.rightsize if self.rightsize is not None else RightsizeConfigFile()


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
