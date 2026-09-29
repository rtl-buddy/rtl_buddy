# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Liveness reporting for a draining fleet, shared by both backends' ``wait_all``.

:class:`DispatchProgress` decides when to print a console line (first
observation, every change in the outstanding count, and a heartbeat every
``progress-interval`` seconds; ``0`` silences the console but not
``rtl_buddy.log``), when a suite has finished, and when ``max-wait`` has
expired. Counts are in jobs, not scheduler queue lines, so a pending array
counts once per element. "Finished" means left the queue, not passed.
"""

import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_console_event, log_event
from .base import split_handle_key, telemetry_key

logger = logging.getLogger(__name__)


def _ranges(values: Sequence[int]) -> list[str]:
    """Contiguous runs of sorted ints as ``1-3`` / ``7`` parts."""
    parts: list[str] = []
    start = prev = None
    for value in values:
        if start is None:
            start = prev = value
        elif value == prev + 1:
            prev = value
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = value
    if start is not None:
        parts.append(str(start) if start == prev else f"{start}-{prev}")
    return parts


def group_job_ids(ids: Iterable[str]) -> list[str]:
    """Collapse handle ids into the compact form a scheduler accepts.

    ``1235_1 1235_2 1235_3 1236`` becomes ``["1235_[1-3]", "1236"]``.
    Non-numeric elements (the local pool's ``lp-7``) are kept verbatim.
    """
    bases: dict[str, list[str]] = {}
    for job_id in ids:
        base, sep, element = str(job_id).partition("_")
        elements = bases.setdefault(base, [])
        if sep:
            elements.append(element)
    grouped = []
    for base, elements in bases.items():
        if not elements:
            grouped.append(base)
            continue
        numeric = sorted({int(e) for e in elements if e.isdigit()})
        literal = sorted({e for e in elements if not e.isdigit()})
        parts = _ranges(numeric) + literal
        grouped.append(f"{base}_[{','.join(parts)}]")
    return grouped


def suite_labels(suite_dirs: Iterable[str]) -> dict[str, str]:
    """Display label per suite directory.

    One suite is named by its basename; several by their path relative to the
    common ancestor, so similar basenames stay distinct.
    """
    dirs = sorted({d for d in suite_dirs if d})
    if not dirs:
        return {}
    if len(dirs) == 1:
        only = dirs[0]
        return {only: os.path.basename(only.rstrip(os.sep)) or only}
    try:
        common = os.path.commonpath(dirs)
    except ValueError:
        # Different Windows drives have no common ancestor.
        return {d: d for d in dirs}
    return {d: os.path.relpath(d, common) for d in dirs}


class DispatchProgress:
    """Progress/heartbeat/deadline reporting for one ``wait_all`` call."""

    def __init__(
        self,
        handles,
        *,
        backend: str,
        interval: float,
        max_wait: float | None,
        clock=time.monotonic,
        logger: logging.Logger = logger,
    ):
        # Tolerate None handles, such as a zero-test suite's absent build handle.
        handles = [h for h in handles if h is not None]
        self._backend = backend
        self._interval = max(0.0, float(interval or 0.0))
        self._max_wait = max_wait
        self._clock = clock
        self._logger = logger
        self._start = clock()
        self._last_console: float | None = None
        self._last_remaining: int | None = None
        # A change the throttle kept off the console; the next line is not a `heartbeat`.
        self._pending_change = False
        self._first = True
        self._total = len(handles)

        labels = suite_labels(
            getattr(h.spec, "suite_dir", None) for h in handles if h.spec is not None
        )
        self._suite_jobs: dict[str, set[str]] = {}
        for handle in handles:
            suite_dir = getattr(handle.spec, "suite_dir", None)
            label = labels.get(suite_dir)
            if label is None:
                continue
            # Keyed as the backend reports outstanding jobs; bare ids can repeat across clusters.
            self._suite_jobs.setdefault(label, set()).add(telemetry_key(handle))
        self._drained: set[str] = set()

    # ---- reporting ---------------------------------------------------

    def observe(
        self,
        remaining: Iterable[str],
        *,
        states: Mapping[str, str] | None = None,
        longest: tuple[str, float] | None = None,
    ) -> None:
        """Record one poll; ``remaining`` is the outstanding job ids.

        Raises :class:`FatalRtlBuddyError` when ``max-wait`` has elapsed.
        """
        now = self._clock()
        elapsed = now - self._start
        outstanding = list(dict.fromkeys(str(job_id) for job_id in remaining))
        count = len(outstanding)

        self._report_drained_suites(set(outstanding), elapsed)

        changed = count != self._last_remaining
        due = (
            self._interval > 0
            and count > 0
            and self._last_console is not None
            and (now - self._last_console) >= self._interval
        )
        if self._first or changed or due:
            self._emit_progress(
                count,
                elapsed=elapsed,
                heartbeat=not (self._first or changed or self._pending_change),
                states=states,
                longest=longest,
                now=now,
            )
        self._first = False
        self._last_remaining = count

        if count and self._max_wait is not None and elapsed > self._max_wait:
            self._fail_on_deadline(outstanding, elapsed)

    def finish(self) -> None:
        """Close out the wait: the queue is empty.

        Reports the last suite, since the emptying observation never reaches
        :meth:`observe`.
        """
        self._report_drained_suites(set(), self._clock() - self._start)

    # ---- internals ---------------------------------------------------

    def _emit_progress(self, count, *, elapsed, heartbeat, states, longest, now):
        running = pending = None
        if states is not None:
            running = sum(1 for value in states.values() if value == "running")
            pending = max(0, count - running)
        fields = dict(
            backend=self._backend,
            remaining=count,
            total=self._total,
            running=running,
            pending=pending,
            elapsed_s=round(elapsed, 1),
            heartbeat=heartbeat,
            longest_job=longest[0] if longest else None,
            longest_s=round(longest[1], 1) if longest else None,
        )
        if self._interval <= 0:
            log_event(self._logger, logging.INFO, "dispatch.progress", **fields)
            return
        if (
            self._last_console is not None
            and (now - self._last_console) < self._interval
        ):
            # Throttled: log it, print later.
            self._pending_change = True
            log_event(self._logger, logging.INFO, "dispatch.progress", **fields)
            return
        log_console_event(self._logger, logging.INFO, "dispatch.progress", **fields)
        self._last_console = now
        self._pending_change = False

    def _report_drained_suites(self, outstanding: set[str], elapsed: float) -> None:
        """One line per suite, the first time none of its jobs are queued."""
        for label, job_ids in self._suite_jobs.items():
            if label in self._drained or job_ids & outstanding:
                continue
            self._drained.add(label)
            emit = log_event if self._interval <= 0 else log_console_event
            emit(
                self._logger,
                logging.INFO,
                "dispatch.suite_drained",
                backend=self._backend,
                suite=label,
                jobs=len(job_ids),
                elapsed_s=round(elapsed, 1),
            )

    def _fail_on_deadline(self, outstanding: Sequence[str], elapsed: float) -> None:
        # Split cluster-qualified keys: Slurm wants the bare id plus `-M <cluster>`.
        by_cluster: dict[str | None, list[str]] = {}
        for key in outstanding:
            cluster, job_id = split_handle_key(key)
            by_cluster.setdefault(cluster, []).append(job_id)
        grouped: list[str] = []
        queries: list[str] = []
        for cluster, job_ids in by_cluster.items():
            ids = group_job_ids(job_ids)
            grouped += ids
            # Quoted so the shell does not glob the brackets.
            selector = f"'{','.join(ids)}'"
            where = f" -M {cluster}" if cluster else ""
            queries.append(f"squeue{where} -j {selector}")
            queries.append(f"sacct{where} -j {selector}")
        clusters = [c for c in by_cluster if c]
        log_event(
            self._logger,
            logging.WARNING,
            "dispatch.max_wait_exceeded",
            backend=self._backend,
            max_wait=self._max_wait,
            remaining=len(outstanding),
            total=self._total,
            elapsed_s=round(elapsed, 1),
            jobs=grouped,
            clusters=clusters or None,
            queries=queries,
        )
        raise FatalRtlBuddyError(
            f"dispatch: {len(outstanding)} of {self._total} job(s) were still "
            f"outstanding after cfg-dispatch.max-wait ({self._max_wait}s) on the "
            f"{self._backend} backend — cancelling the fleet. Outstanding job "
            f"ids: {' '.join(grouped)} (query them with "
            f"{'; '.join(queries)} for a post-mortem; raise max-wait if the "
            "run legitimately takes longer)."
        )
