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

from dataclasses import dataclass

from serde import field, serde

# A defined time limit is load-bearing, not cosmetic: reservation
# right-sizing computes time utilization as Elapsed/Timelimit, which is
# undefined on partitions whose default is UNLIMITED. Every dispatched
# job therefore gets an explicit --time, from this default if nothing
# else sets one.
DEFAULT_JOB_TIME = "01:00:00"
DEFAULT_JOB_CPUS = 1


@serde
class DispatchResourcesFile:
    """Per-job resource reservation fields; ``None`` means "inherit".

    Scheduler-agnostic on purpose: the Slurm backend maps them to
    ``--cpus-per-task`` / ``--mem`` / ``--time``, and any later backend
    reuses the same schema.
    """

    cpus: int | None = None
    mem: str | None = None
    time: str | None = None


@serde
class DispatchConfigFile:
    """``cfg-dispatch`` section of root_config.yaml."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    sbatch_args: list[str] = field(rename="sbatch-args", default_factory=list)
    poll_interval: float = field(rename="poll-interval", default=10.0)


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
            resolved.mem = layer.mem
        if layer.time is not None:
            resolved.time = layer.time
    return resolved
