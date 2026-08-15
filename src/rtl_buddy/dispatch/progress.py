# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Backend-independent liveness reporting for a draining fleet (#435).

A dispatched regression used to print nothing between "submitted" and
"drained": on a CI console a healthy 30-minute run, a hung one and one
whose head had been killed were indistinguishable, and the numbers that
answer the question were already computed — only logged at DEBUG, which
no default-verbosity console shows.

:class:`DispatchProgress` is what both backends' ``wait_all`` feed once
per poll. It owns three decisions so neither backend has to:

* **When to speak.** On the first observation (the moment the head enters
  the wait), on every change in the outstanding count, and as a heartbeat
  every ``progress-interval`` seconds while jobs are still queued — at
  most one console line per interval, so a 10 s poll cadence does not
  produce 180 lines an hour. ``progress-interval: 0`` silences the
  console entirely while every change still reaches ``rtl_buddy.log``.
* **What "a suite finished" means.** Suite membership comes off the
  handles' ``spec.suite_dir``, so a partially-drained fleet reports which
  suite left the queue as it happens rather than only at the end. The
  wording is "finished", never "passed": results are collected later, and
  this reporter has seen none of them.
* **When to stop waiting.** ``max-wait`` turns an unbounded ``while
  True`` into a diagnosable failure that names the outstanding ids in a
  form ``squeue``/``sacct`` accept.

Counts are in **jobs**, not in scheduler queue lines: one pending Slurm
array line stands for as many jobs as it has elements, and a progress
line that said "1 remaining" for a 40-element array would be the same
kind of misinformation as saying nothing.
"""

import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_console_event, log_event

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
    """Collapse handle ids into the greppable form a scheduler speaks.

    ``1235_1 1235_2 1235_3 1236`` becomes ``["1235_[1-3]", "1236"]`` — one
    entry per submitted job rather than one per array element, which is
    both what ``squeue -j`` takes back and short enough to survive in a
    console line. Non-numeric elements are kept verbatim so a backend with
    another id shape (the local pool's ``lp-7``) still round-trips.
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
    """Display label per suite directory: short, but never ambiguous.

    One suite is named by its basename (there is nothing to disambiguate
    against); several are named relative to their common ancestor, so
    ``verif/tb_a`` and ``verif/tb_b`` stay distinguishable where two
    basenames could collide.
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
        # Different drives on Windows: no common ancestor to relativize to.
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
        # Tolerate None entries for the reason cancel_all does: a caller
        # that let one through (a zero-test suite's absent build handle,
        # #361) must not turn liveness reporting into a crash.
        handles = [h for h in handles if h is not None]
        self._backend = backend
        self._interval = max(0.0, float(interval or 0.0))
        self._max_wait = max_wait
        self._clock = clock
        self._logger = logger
        self._start = clock()
        self._last_console: float | None = None
        self._last_remaining: int | None = None
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
            self._suite_jobs.setdefault(label, set()).add(handle.job_id)
        self._drained: set[str] = set()

    # ---- reporting ---------------------------------------------------

    def observe(
        self,
        remaining: Iterable[str],
        *,
        states: Mapping[str, str] | None = None,
        longest: tuple[str, float] | None = None,
    ) -> None:
        """Record one poll: ``remaining`` is the outstanding job ids.

        Raises :class:`FatalRtlBuddyError` when ``max-wait`` has elapsed;
        the caller's existing ``except BaseException: cancel_all(...)``
        takes the fleet down.
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
                heartbeat=not (self._first or changed),
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

        ``wait_all`` returns as soon as it sees an empty queue, so the
        observation that emptied it is never passed to :meth:`observe` —
        without this the last suite would never be reported as finished.
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
            # The developer's quiet terminal: no console line, but the run's
            # log still carries the whole trail at INFO.
            log_event(self._logger, logging.INFO, "dispatch.progress", **fields)
            return
        if (
            self._last_console is not None
            and (now - self._last_console) < self._interval
        ):
            # Throttled: a change worth recording, too soon to print.
            log_event(self._logger, logging.INFO, "dispatch.progress", **fields)
            return
        log_console_event(self._logger, logging.INFO, "dispatch.progress", **fields)
        self._last_console = now

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
        grouped = group_job_ids(outstanding)
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
        )
        raise FatalRtlBuddyError(
            f"dispatch: {len(outstanding)} of {self._total} job(s) were still "
            f"outstanding after cfg-dispatch.max-wait ({self._max_wait}s) on the "
            f"{self._backend} backend — cancelling the fleet. Outstanding job "
            f"ids: {' '.join(grouped)} (query them with squeue/sacct for a "
            "post-mortem; raise max-wait if the run legitimately takes longer)."
        )
