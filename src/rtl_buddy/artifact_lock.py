"""Advisory per-artefact-tree lock so concurrent runs fail loud.

Two ``rtl-buddy`` processes sharing one suite artefact tree
(``<command_root>/artefacts/``) would interleave compile workspaces,
``run-NNNN`` dirs, and the latest-run symlinks. Rather than detect the
corruption afterwards, each command takes an exclusive non-blocking
``flock(2)`` on ``<artifact_root>/.rtl-buddy.lock`` when it enters its
execution context and raises :class:`FatalRtlBuddyError` immediately if
another process already holds it.

The lock is advisory and kernel-managed: it disappears when the holding
process exits for any reason, so crashes cannot leave stale locks. The
lock *file* persists and carries holder metadata (pid, command, start
time) purely so the contention error can say who is in the way; a
leftover file with no live flock is harmless.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .errors import FatalRtlBuddyError
from .logging_utils import log_event

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".rtl-buddy.lock"


class ArtifactLocks:
    """Locks held by this process, keyed by lock-file path.

    One instance lives on the CLI object for the process lifetime.
    ``acquire`` is idempotent per path — ``rb regression`` re-enters the
    same suite context freely — and every lock is held until the process
    exits (flock has no inheritance across fork/exec of child tools that
    close inherited fds, and external tools run with their own cwd, so
    holding for the full run is the simple, safe choice).
    """

    def __init__(self) -> None:
        self._held: dict[Path, int] = {}

    def acquire(self, artifact_root: Path, *, command: str | None = None) -> None:
        """Take the exclusive lock for ``artifact_root``, failing loud.

        Creates ``artifact_root`` (and the lock file) if needed. Raises
        :class:`FatalRtlBuddyError` naming the holder when another
        process has the lock.
        """
        artifact_root = Path(artifact_root)
        lock_path = artifact_root / LOCK_FILENAME
        if lock_path in self._held:
            return

        artifact_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder(fd)
            os.close(fd)
            log_event(
                logger,
                logging.ERROR,
                "artifact_lock.contended",
                path=str(artifact_root),
                holder_pid=holder.get("pid"),
                holder_command=holder.get("command"),
                holder_started=holder.get("started"),
            )
            raise FatalRtlBuddyError(
                f"{artifact_root}: another rtl-buddy run is already using "
                f"this artefact tree{_describe_holder(holder)} — wait for "
                "it to finish or kill it"
            )

        os.ftruncate(fd, 0)
        os.write(
            fd,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "command": command,
                    "started": datetime.now().isoformat(timespec="seconds"),
                }
            ).encode(),
        )
        os.fsync(fd)
        self._held[lock_path] = fd
        log_event(
            logger,
            logging.DEBUG,
            "artifact_lock.acquired",
            path=str(artifact_root),
            command=command,
        )

    def release_all(self) -> None:
        """Drop every held lock. Only tests need this; real runs rely on
        the kernel releasing flocks at process exit."""
        for fd in self._held.values():
            os.close(fd)
        self._held.clear()

    @staticmethod
    def _read_holder(fd: int) -> dict:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096)
            holder = json.loads(raw.decode())
        except (OSError, ValueError):
            return {}
        return holder if isinstance(holder, dict) else {}


def _describe_holder(holder: dict) -> str:
    parts = []
    if holder.get("pid") is not None:
        parts.append(f"pid {holder['pid']}")
    if holder.get("command"):
        parts.append(f"rb {holder['command']}")
    if holder.get("started"):
        parts.append(f"started {holder['started']}")
    return f" ({', '.join(parts)})" if parts else ""
