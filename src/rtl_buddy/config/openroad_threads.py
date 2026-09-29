"""OpenROAD worker-thread count (`threads:`) for the pnr, power and synth flows.

- Unset: OpenROAD's default of one thread; nothing is emitted.
- A positive integer: emitted as `set_thread_count N` ahead of the first tool command. Anything else except ``auto`` fails config loading.
- ``auto``: the CPU count of the current allocation, or one when there is none. Never the host core count.
- A count above the detected allocation is clamped to it with a WARNING.

An allocation is a Slurm job's CPU count or a CPU affinity mask smaller than the machine (see :func:`detect_allocation`). Container CPU quotas are not detected.
"""

import logging
import os
import re
from dataclasses import dataclass

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

AUTO = "auto"

#: Slurm variables consulted inside a job (`SLURM_JOB_ID` set), most specific first.
_SLURM_CPU_VARS = ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE")

#: Source label recorded when the process's CPU affinity is the bound.
AFFINITY_SOURCE = "sched_getaffinity"


def validate_threads(value, *, where: str) -> int | str | None:
    """Return a validated `threads:` value, or raise FatalRtlBuddyError.

    ``where`` names the entry in the error message. ``bool`` is refused because it is an ``int`` subclass.
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
    """The CPUs this process was granted, and the source of that number.

    Two sources; the smaller wins when both apply:

    - Inside a Slurm job, the first positive integer among `SLURM_CPUS_PER_TASK` and `SLURM_CPUS_ON_NODE`.
    - A CPU affinity mask (`os.sched_getaffinity`) smaller than the machine. A mask covering every core is not an allocation.

    Returns ``(None, None)`` when neither applies. The keyword arguments override live detection for tests.
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
    """The thread count for one OpenROAD invocation.

    ``count`` is what the script sets, or OpenROAD's default of one when ``emit`` is false.
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
        """The thread provenance recorded in the run's results.

        ``reported`` is the count OpenROAD logged; it can be lower than ``count`` and wins when present.
        It is ignored when nothing was emitted, because a count in the log then belongs to a previous run.
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

    ``flow`` and ``run`` name the entry in log events. A clamp logs WARNING ``openroad.threads_capped``; the resolved plan logs INFO ``openroad.threads``. ``allocation`` overrides detection for tests.
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
