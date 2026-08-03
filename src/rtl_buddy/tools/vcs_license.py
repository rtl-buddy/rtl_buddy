# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
Detects VCS ``-licqueue`` license-queue waits in a running sim's output so
the per-sim timeout clock can be paused while the sim is legitimately
waiting for a license seat rather than hanging.
"""

import re
import time
from pathlib import Path
from typing import Callable

# VCS prints this banner (and keeps appending dots while it polls) when
# ``-licqueue`` is set and no seat is free yet.
_MARKERS = ("Queuing for License", "Licensed number of users already reached")

_DOTS_ONLY_RE = re.compile(r"^[.\s]*$")


def _is_marker_line(line: str) -> bool:
    # Delegates so the marker-matching rule has exactly one implementation:
    # the live monitor and the post-hoc compile check must never drift apart
    # on what a queue banner looks like.
    return has_license_queue_marker(line)


def has_license_queue_marker(text: str) -> bool:
    """Did this captured output ever sit in the VCS license queue?

    A one-shot check over text already in hand, for the compile phase:
    ``vcs`` elaboration honours ``-licqueue`` exactly as ``simv`` does, but
    ``compile()`` captures its output through pipes, which rules out the
    live :class:`VcsLicenseQueueMonitor` (see ``run_managed_process``'s
    ``timeout_pauser`` restriction). Used to attribute a slow compile — and
    a build job that hit its ``--time`` — to a busy license server rather
    than to an undersized reservation (rtl-buddy/rtl_buddy#329, #358).
    """
    return any(marker in text for marker in _MARKERS)


class _FileTail:
    """Incremental reader: tracks a byte offset and buffers partial lines."""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._offset = 0
        self._buffer = ""

    def read_new_lines(self) -> list[str]:
        try:
            with open(self._path, "r", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        data = self._buffer + chunk
        lines = data.split("\n")
        self._buffer = lines.pop()
        return lines

    @property
    def pending(self) -> str:
        """Buffered partial line (data appended without a trailing newline)."""
        return self._buffer


class VcsLicenseQueueMonitor:
    """Tracks whether a VCS sim is currently queuing for a license.

    Call :meth:`is_waiting` periodically (e.g. as ``run_managed_process``'s
    ``timeout_pauser``); it reads any newly appended output from the sim's
    log/err files and returns ``True`` while the sim is judged to be
    blocked in the VCS license queue rather than actually running.
    """

    def __init__(
        self,
        log_path,
        err_path,
        *,
        max_queue_wait_sec: float = 3600,
        on_enter_queue: Callable[[], None] | None = None,
        on_exit_queue: Callable[[float], None] | None = None,
    ) -> None:
        self._tails = [_FileTail(log_path), _FileTail(err_path)]
        self._max_queue_wait_sec = max_queue_wait_sec
        self._on_enter_queue = on_enter_queue
        self._on_exit_queue = on_exit_queue

        self._queued = False
        self._queue_started_at: float | None = None
        self._completed_queue_sec = 0.0
        self._disabled = False

        self.queue_wait_sec = 0.0
        self.cap_exceeded = False

    def is_waiting(self) -> bool:
        if self._disabled:
            return False

        for tail in self._tails:
            for line in tail.read_new_lines():
                self._process_line(line)

        # VCS appends queue-polling dots to the banner without a newline, so
        # the marker may only ever exist as a partial line. Entering the
        # queued state must not wait for line completion; exiting still
        # requires a complete non-marker line.
        if not self._queued and any(
            _is_marker_line(tail.pending) for tail in self._tails
        ):
            self._enter_queue()

        if self._queued:
            self.queue_wait_sec = self._completed_queue_sec + (
                time.time() - self._queue_started_at
            )
            if self.queue_wait_sec > self._max_queue_wait_sec:
                self.cap_exceeded = True
                self._disabled = True
                self._queued = False
                self._queue_started_at = None
                return False

        return self._queued

    def _enter_queue(self) -> None:
        self._queued = True
        self._queue_started_at = time.time()
        if self._on_enter_queue is not None:
            self._on_enter_queue()

    def _process_line(self, line: str) -> None:
        if not self._queued:
            if _is_marker_line(line):
                self._enter_queue()
            return

        stripped = line.strip()
        if not stripped:
            return  # blank line: still queuing
        if _is_marker_line(line):
            return  # still queuing
        if _DOTS_ONLY_RE.match(line):
            return  # VCS's queue-polling dots: still queuing

        # Real simulator output resumed: license was granted.
        queued_sec = time.time() - self._queue_started_at
        self._completed_queue_sec += queued_sec
        self.queue_wait_sec = self._completed_queue_sec
        self._queued = False
        self._queue_started_at = None
        if self._on_exit_queue is not None:
            self._on_exit_queue(queued_sec)
