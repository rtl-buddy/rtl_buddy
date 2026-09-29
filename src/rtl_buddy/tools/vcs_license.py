# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Detects VCS ``-licqueue`` license-queue waits in sim output so the timeout clock can pause during them."""

import re
import time
from pathlib import Path
from typing import Callable

# Banner VCS prints under ``-licqueue`` when no seat is free.
_MARKERS = ("Queuing for License", "Licensed number of users already reached")

_DOTS_ONLY_RE = re.compile(r"^[.\s]*$")

# Banner lines that are not markers but are not simulation output either (the
# CTRL-C hint follows the banner). An unlisted banner line ends the pause early;
# ``max_queue_wait_sec`` bounds the damage.
_BANNER_NOISE_RES = (re.compile(r"^\s*HIT\s+CTRL-C\s+to\s+exit\s*$", re.IGNORECASE),)


def _is_marker_line(line: str) -> bool:
    # Delegates so the live monitor and the compile check share one marker rule.
    return has_license_queue_marker(line)


def _is_banner_noise(line: str) -> bool:
    """Return whether the line is banner text other than the marker."""
    return any(pattern.match(line) for pattern in _BANNER_NOISE_RES)


def is_queue_banner_line(line: str) -> bool:
    """Return whether a complete line keeps a queued sim in the queue.

    True for the marker, polling dots, blank lines and other banner text. Any
    other line is simulation output, meaning the seat was granted. The monitor
    and the dispatch classifier both use this to tell "killed while queueing"
    from "queued, ran, then hung".
    """
    if _is_marker_line(line):
        return True
    if _DOTS_ONLY_RE.match(line):
        return True
    return _is_banner_noise(line)


def has_license_queue_marker(text: str) -> bool:
    """Return whether captured output ever sat in the VCS license queue.

    For the compile phase, where output is piped and the live
    :class:`VcsLicenseQueueMonitor` cannot be used.
    """
    return any(marker in text for marker in _MARKERS)


class _FileTail:
    """Reads newly appended lines of a file, buffering a trailing partial line."""

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
        """Text after the last newline read so far."""
        return self._buffer


class VcsLicenseQueueMonitor:
    """Tracks whether a VCS sim is queuing for a license.

    Call :meth:`is_waiting` periodically (e.g. as ``run_managed_process``'s
    ``timeout_pauser``). It reads new log/err output and returns ``True``
    while the sim is in the queue. Once total queue time exceeds
    ``max_queue_wait_sec`` it sets ``cap_exceeded`` and stops pausing.
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

        # VCS appends polling dots without a newline, so the marker can stay a
        # partial line. Entering the queue must not wait for a newline.
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

        if is_queue_banner_line(line):
            return

        queued_sec = time.time() - self._queue_started_at
        self._completed_queue_sec += queued_sec
        self.queue_wait_sec = self._completed_queue_sec
        self._queued = False
        self._queue_started_at = None
        if self._on_exit_queue is not None:
            self._on_exit_queue(queued_sec)
