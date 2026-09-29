"""Advisory flocks: one per artefact tree, one per shared build directory.

Each command takes an exclusive non-blocking ``flock(2)`` on
``<artifact_root>/.rtl-buddy.lock`` and fails fast if another process holds it.
A lock whose recorded holder is dead on this host is reclaimed.
:func:`build_dir_lock` is the narrower, blocking lock around a shared compile.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .errors import FatalRtlBuddyError
from .logging_utils import log_console_event, log_event

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".rtl-buddy.lock"
BUILD_LOCK_FILENAME = ".rb-build.lock"

# Sibling of the lock file, not the lock file itself: reclaiming replaces that file.
RECLAIM_LOCK_SUFFIX = ".reclaim"

# errnos meaning "someone else holds it"; any other errno means the filesystem cannot lock.
_CONTENTION_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})

BUILD_LOCK_POLL_SEC = 0.2
BUILD_LOCK_ANNOUNCE_SEC = 300.0

# Build directories this process has already reported as unlockable.
_DEGRADED_BUILD_DIRS: set[str] = set()
_DEGRADED_BUILD_DIRS_LOCK = threading.Lock()


def _claim_degrade_warning(build_dir: Path) -> bool:
    """Return True the first time this process fails to lock ``build_dir``, so the warning prints once per directory."""
    key = os.path.realpath(build_dir)
    with _DEGRADED_BUILD_DIRS_LOCK:
        if key in _DEGRADED_BUILD_DIRS:
            return False
        _DEGRADED_BUILD_DIRS.add(key)
        return True


def _reset_degrade_warnings() -> None:
    """Forget every claim. Tests only."""
    with _DEGRADED_BUILD_DIRS_LOCK:
        _DEGRADED_BUILD_DIRS.clear()


class ArtifactLocks:
    """Artefact-tree locks held by this process, keyed by lock-file path.

    ``acquire`` is idempotent per path. Locks are held until
    :meth:`release_all` or process exit.
    """

    def __init__(self) -> None:
        self._held: dict[Path, int] = {}

    def acquire(self, artifact_root: Path, *, command: str | None = None) -> None:
        """Take the exclusive lock for ``artifact_root``, creating it if needed.

        Raises :class:`FatalRtlBuddyError` naming the holder when another process has it.
        """
        artifact_root = Path(artifact_root)
        lock_path = artifact_root / LOCK_FILENAME
        if lock_path in self._held:
            return

        artifact_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_holder(fd)
            os.close(fd)
            fd = _reclaim_if_stale(artifact_root, lock_path, holder)
            if fd is None:
                log_event(
                    logger,
                    logging.ERROR,
                    "artifact_lock.contended",
                    path=str(artifact_root),
                    holder_pid=holder.get("pid"),
                    holder_command=holder.get("command"),
                    holder_started=holder.get("started"),
                    holder_host=holder.get("host"),
                    error=str(exc),
                )
                if exc.errno not in _CONTENTION_ERRNOS:
                    raise FatalRtlBuddyError(
                        f"{artifact_root}: cannot lock this artefact tree — "
                        f"{os.strerror(exc.errno) if exc.errno else exc} "
                        "(the filesystem may not support flock; move the "
                        "artefact tree to a filesystem that does)"
                    ) from exc
                raise FatalRtlBuddyError(
                    f"{artifact_root}: another rtl-buddy run is already using "
                    f"this artefact tree{_describe_holder(holder)} — wait for "
                    "it to finish or kill it"
                ) from exc
            log_event(
                logger,
                logging.WARNING,
                "artifact_lock.reclaimed",
                path=str(artifact_root),
                holder_pid=holder.get("pid"),
                holder_command=holder.get("command"),
                holder_started=holder.get("started"),
                holder_host=holder.get("host"),
            )

        _write_holder(fd, command)
        if not _still_owns_lock_path(artifact_root, lock_path, fd):
            # A reclaimer replaced the file between our flock and our write; we hold an unlinked inode.
            os.close(fd)
            raise FatalRtlBuddyError(
                f"{artifact_root}: another rtl-buddy run is already using "
                "this artefact tree (it reclaimed a stale lock at the same "
                "moment) — wait for it to finish or kill it"
            )
        self._held[lock_path] = fd
        log_event(
            logger,
            logging.DEBUG,
            "artifact_lock.acquired",
            path=str(artifact_root),
            command=command,
        )

    def release_all(self) -> None:
        """Drop every held lock. Idempotent and never raises, so it is safe inside ``finally``."""
        for fd in self._held.values():
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
        self._held.clear()


@contextlib.contextmanager
def build_dir_lock(build_dir: Path | str, *, test: str | None = None) -> Iterator[bool]:
    """Serialise compiles into one shared build directory across processes.

    Blocks until the lock is free, logging a wait line every
    ``BUILD_LOCK_ANNOUNCE_SEC``. Callers do the stamp check inside the
    ``with`` so a waiter reuses the build that just finished. Callers that
    already have a valid stamp must not take the lock.

    Yields True when the lock is held. A filesystem that cannot lock yields
    False after one warning per directory; the compile then runs unlocked.

    Limits, see docs/known-issues.md: NFS mounted with ``nolock`` or
    ``local_lock=flock``/``all`` locks per node only, silently; deleting the
    shared tree during a live run unlinks the lock file.
    """
    build_dir = Path(build_dir)
    fields = {
        # Same fields as the other compile.* events (vlog_sim `_build_dir_fields`).
        "test": test,
        "build_dir": build_dir.name,
        "build_path": str(build_dir),
    }
    fd = None
    try:
        fd = os.open(build_dir / BUILD_LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _wait_for_lock(fd, fields)
    except OSError as e:
        if fd is not None:
            # This path must not raise.
            with contextlib.suppress(OSError):
                os.close(fd)
            fd = None
        if _claim_degrade_warning(build_dir):
            log_event(
                logger,
                logging.WARNING,
                "compile.build_lock_unavailable",
                **fields,
                error=str(e),
            )
    else:
        # Holder metadata is diagnostic; a failure must not lose the lock.
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(
                fd,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "test": test,
                        "started": datetime.now().isoformat(timespec="seconds"),
                    }
                ).encode(),
            )
            os.fsync(fd)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _wait_for_lock(fd: int, fields: dict) -> None:
    """Poll until ``fd``'s flock is ours, logging the wait every ``BUILD_LOCK_ANNOUNCE_SEC``.

    Has no timeout: a dead holder releases the flock in the kernel.
    ``OSError`` propagates to :func:`build_dir_lock`.
    """
    started = time.monotonic()
    announced = None
    while True:
        waited = time.monotonic() - started
        if announced is None or waited - announced >= BUILD_LOCK_ANNOUNCE_SEC:
            holder = _read_holder(fd)
            log_console_event(
                logger,
                logging.INFO,
                "compile.build_lock_wait",
                **fields,
                waited_sec=round(waited),
                holder_pid=holder.get("pid"),
                holder_test=holder.get("test"),
                holder_started=holder.get("started"),
            )
            announced = waited
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(BUILD_LOCK_POLL_SEC)
        else:
            return


def _write_holder(fd: int, command: str | None) -> None:
    """Write this process's pid, command, host and start token into the locked file.

    A record without ``host`` is never reclaimed.
    """
    os.ftruncate(fd, 0)
    os.write(
        fd,
        json.dumps(
            {
                "pid": os.getpid(),
                "command": command,
                "started": datetime.now().isoformat(timespec="seconds"),
                "host": _this_host(),
                "start_token": _process_start_token(os.getpid()),
            }
        ).encode(),
    )
    os.fsync(fd)


def _this_host() -> str:
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this pid exists, whoever owns it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown: assume still running.
        return True
    return True


def _process_start_token(pid: int) -> str | None:
    """Return a start-time stamp for ``pid`` (detects pid reuse), or None if unobtainable.

    Reads ``/proc/<pid>/stat`` on Linux and falls back to ``ps -o lstart=``.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        # comm may contain spaces and ')'; split after the last ')'. starttime is then index 19.
        fields = raw.rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError, ValueError):
        pass
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = proc.stdout.strip()
    return token or None


def _holder_is_stale(holder: dict) -> bool:
    """Return True only if the record names a valid pid on this host that is dead or reused.

    Records without a host, or from another host, are never stale.
    """
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    host = holder.get("host")
    if not host or host != _this_host():
        return False
    if not _pid_alive(pid):
        return True
    recorded = holder.get("start_token")
    if not recorded:
        return False
    current = _process_start_token(pid)
    return current is not None and current != recorded


def _reclaim_if_stale(artifact_root: Path, lock_path: Path, holder: dict) -> int | None:
    """Take a lock whose recorded holder is dead and return its locked fd, else None.

    None means treat the lock as contended. Reclaimers serialise on a
    sibling ``.reclaim`` flock, re-check the record under it, and install
    the new file with ``os.replace``.
    """
    if not _holder_is_stale(holder):
        return None

    reclaim_path = artifact_root / (LOCK_FILENAME + RECLAIM_LOCK_SUFFIX)
    try:
        guard = os.open(reclaim_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another process is mid-reclaim.
            return None

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass
        else:
            # The holder exited in the meantime; nothing to replace.
            return fd
        current = _read_holder(fd)
        os.close(fd)
        if not _holder_is_stale(current):
            return None
        return _replace_lock_file(artifact_root, lock_path)
    finally:
        with contextlib.suppress(OSError):
            os.close(guard)


def _still_owns_lock_path(artifact_root: Path, lock_path: Path, fd: int) -> bool:
    """Return True if ``fd`` is still the file at ``lock_path`` after our record is written.

    Runs under the ``.reclaim`` flock. Until our record lands, the file still
    carries the dead holder's record, and a reclaimer may replace it.
    """
    guard = None
    try:
        with contextlib.suppress(OSError):
            guard = os.open(
                artifact_root / (LOCK_FILENAME + RECLAIM_LOCK_SUFFIX),
                os.O_RDWR | os.O_CREAT,
                0o644,
            )
            fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            ours, current = os.fstat(fd), os.stat(lock_path)
        except OSError:
            return False
        return (ours.st_dev, ours.st_ino) == (current.st_dev, current.st_ino)
    finally:
        if guard is not None:
            with contextlib.suppress(OSError):
                os.close(guard)


def _replace_lock_file(artifact_root: Path, lock_path: Path) -> int | None:
    """Install a fresh, already-locked lock file over a stale one."""
    tmp_path = artifact_root / f"{LOCK_FILENAME}.new-{os.getpid()}"
    try:
        fd = os.open(tmp_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.replace(tmp_path, lock_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        return None
    return fd


def _read_holder(fd: int) -> dict:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        holder = json.loads(raw.decode())
    except (OSError, ValueError):
        return {}
    return holder if isinstance(holder, dict) else {}


def _describe_holder(holder: dict) -> str:
    """Format the holder as `` (pid N, rb CMD, ...)`` for lock messages; every key is optional."""
    parts = []
    if holder.get("pid") is not None:
        parts.append(f"pid {holder['pid']}")
    if holder.get("command"):
        parts.append(f"rb {holder['command']}")
    if holder.get("test"):
        parts.append(f"test {holder['test']}")
    if holder.get("started"):
        parts.append(f"started {holder['started']}")
    if holder.get("host"):
        parts.append(f"on {holder['host']}")
    return f" ({', '.join(parts)})" if parts else ""
