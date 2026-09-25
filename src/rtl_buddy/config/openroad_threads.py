"""OpenROAD worker-thread count for the flows that launch OpenROAD (#654).

OpenROAD starts single-threaded and stays that way until a script calls
`set_thread_count`, so a run inside an eight-CPU Slurm allocation used one
of them: detailed routing, the dominant stage of a real block, ran on one
thread. The count is a resource of the run, like the CPUs reserved for it,
so it is a per-run `threads:` key in `pnr.yaml`, `power.yaml` and
`synth.yaml` rather than a platform or tool property.

The contract, shared by every OpenROAD-driven flow:

- unset keeps OpenROAD's own default of one thread and emits nothing, so
  the generated script of a configuration that never names the key is
  byte-for-byte what it has always been;
- a positive integer is emitted as `set_thread_count N` ahead of the first
  tool command; zero, negatives, booleans, floats and strings other than
  ``auto`` fail configuration loading — `set_thread_count 0` means *all
  host cores* to OpenROAD, which is exactly what this must never ask for;
- ``auto`` is the CPU count of the allocation the process runs in, and one
  when there is none: never the host's core count;
- a count above a detected allocation is clamped to it with a WARNING
  naming both numbers, rather than oversubscribing the reservation.

What counts as an allocation is deliberately narrow (see
:func:`detect_allocation`): a Slurm job's CPU count, or a CPU affinity mask
smaller than the machine. A container CPU *quota* (`docker --cpus`) is not
visible here and is not detected.
"""

import logging
import os
import re
from dataclasses import dataclass

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

AUTO = "auto"

#: Slurm variables consulted, most specific first, and only inside a job
#: (`SLURM_JOB_ID` set). `SLURM_CPUS_PER_TASK` is what `srun -c` / `sbatch
#: -c` reserved for this task; `SLURM_CPUS_ON_NODE` is the job's CPUs on
#: this node, the answer when no per-task count was requested.
_SLURM_CPU_VARS = ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE")

#: Source label recorded when the process's CPU affinity is the bound.
AFFINITY_SOURCE = "sched_getaffinity"


def validate_threads(value, *, where: str) -> int | str | None:
    """Return a validated `threads:` value, or raise FatalRtlBuddyError.

    ``where`` names the entry for the message (``pnr run 'x'``). ``bool``
    is refused explicitly: YAML `threads: true` would otherwise pass as
    the integer one.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == AUTO:
        return AUTO
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    log_event(
        logger,
        logging.ERROR,
        "openroad_threads.invalid",
        where=where,
        value=repr(value),
    )
    raise FatalRtlBuddyError(
        f"{where}: 'threads' must be a positive integer or '{AUTO}', got {value!r}"
    )


def _positive_int(text) -> int | None:
    try:
        n = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _affinity_count() -> int | None:
    getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        return None
    try:
        return len(getter(0)) or None
    except OSError:
        return None


def detect_allocation(
    env=None, *, affinity: int | None = None, cpu_count: int | None = None
) -> tuple[int | None, str | None]:
    """The CPUs this process was granted, and where that number came from.

    Two sources, the smaller winning when both apply:

    - inside a Slurm job (`SLURM_JOB_ID` set), the first positive integer
      among `SLURM_CPUS_PER_TASK` and `SLURM_CPUS_ON_NODE`. A malformed
      value is skipped rather than trusted;
    - a CPU affinity mask (`os.sched_getaffinity`, Linux) *smaller than the
      machine* — `taskset`, a cpuset cgroup, a Slurm step bound with task
      affinity. A mask covering every core is no allocation at all, and
      treating it as one would make ``auto`` mean "all host cores".

    ``(None, None)`` when neither applies. The keyword arguments exist for
    tests; production calls pass nothing and read the live process.
    """
    if env is None:
        env = os.environ
        affinity = _affinity_count() if affinity is None else affinity
        cpu_count = os.cpu_count() if cpu_count is None else cpu_count

    candidates: list[tuple[int, str]] = []
    if env.get("SLURM_JOB_ID"):
        for var in _SLURM_CPU_VARS:
            n = _positive_int(env.get(var))
            if n is not None:
                candidates.append((n, var))
                break
    if affinity is not None and cpu_count is not None and affinity < cpu_count:
        candidates.append((affinity, AFFINITY_SOURCE))
    if not candidates:
        return None, None
    return min(candidates, key=lambda c: c[0])


@dataclass(frozen=True)
class ThreadPlan:
    """How many threads one OpenROAD invocation is given, and why.

    ``count`` is what the script sets — or OpenROAD's default of one when
    ``emit`` is false because nothing was requested.
    """

    requested: int | str | None
    count: int
    emit: bool
    allocation: int | None = None
    allocation_source: str | None = None
    capped: bool = False

    def tcl(self) -> str:
        """The command to put ahead of the first tool command, or ""."""
        return f"set_thread_count {self.count}" if self.emit else ""

    def fields(self, reported: int | None = None) -> dict:
        """The provenance recorded in the run's results.

        ``reported`` is what OpenROAD itself logged (`[INFO ORD-0030] Using
        N thread(s).`), which can be lower than ``count``: OpenROAD clamps
        to the host's hardware concurrency on its own. It wins when
        present, since it is what the tool actually ran with. It is ignored
        when nothing was emitted: OpenROAD then logs no count, so one found
        in the log can only be a previous run's.
        """
        if not self.emit:
            reported = None
        return {
            "requested": self.requested,
            "effective": reported if reported is not None else self.count,
            "allocation": self.allocation,
            "allocation_source": self.allocation_source,
        }


def plan_threads(
    requested: int | str | None,
    *,
    flow: str,
    run: str,
    allocation: tuple[int | None, str | None] | None = None,
) -> ThreadPlan:
    """Resolve a validated `threads:` value against the live allocation.

    ``flow`` (``pnr`` / ``power`` / ``synth``) and ``run`` name the entry
    in the events. A clamp is logged at WARNING as
    ``openroad.threads_capped``; the resolved plan at INFO as
    ``openroad.threads``. ``allocation`` overrides detection for tests.
    """
    alloc, source = allocation if allocation is not None else detect_allocation()
    if requested is None:
        plan = ThreadPlan(
            requested=None,
            count=1,
            emit=False,
            allocation=alloc,
            allocation_source=source,
        )
    elif requested == AUTO:
        plan = ThreadPlan(
            requested=AUTO,
            count=alloc if alloc is not None else 1,
            emit=True,
            allocation=alloc,
            allocation_source=source,
        )
    else:
        capped = alloc is not None and requested > alloc
        plan = ThreadPlan(
            requested=requested,
            count=alloc if capped else requested,
            emit=True,
            allocation=alloc,
            allocation_source=source,
            capped=capped,
        )
        if capped:
            log_event(
                logger,
                logging.WARNING,
                "openroad.threads_capped",
                flow=flow,
                run=run,
                requested=requested,
                allocation=alloc,
                allocation_source=source,
            )
    log_event(
        logger,
        logging.INFO,
        "openroad.threads",
        flow=flow,
        run=run,
        requested=plan.requested,
        threads=plan.count,
        allocation=plan.allocation,
        allocation_source=plan.allocation_source,
    )
    return plan


def parse_reported_threads(log_text: str) -> int | None:
    """The last `[INFO ORD-0030] Using N thread(s).` count in a log."""
    matches = re.findall(r"\[INFO ORD-0030\] Using (\d+) thread", log_text)
    return int(matches[-1]) if matches else None
