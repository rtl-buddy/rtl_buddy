"""Advisory flocks: one per artefact tree, one per shared build dir.

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

:func:`build_dir_lock` applies the same idiom at a second, narrower
scope — one *shared build directory*, held only for the compile that
populates it (#494). The two differ deliberately: the tree lock is
whole-process and fails loud, while a build lock is scoped, blocks
rather than refuses, and degrades to unlocked when the filesystem cannot
lock at all.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
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

# How often a blocked compile retries, and how often it says so. The poll
# is cheap (one non-blocking flock) and the announcement interval is what
# keeps a long wait legible without turning a job log into a wait log.
BUILD_LOCK_POLL_SEC = 0.2
BUILD_LOCK_ANNOUNCE_SEC = 300.0

# Build directories this process has already reported as unlockable.
_DEGRADED_BUILD_DIRS: set[str] = set()
_DEGRADED_BUILD_DIRS_LOCK = threading.Lock()


def _claim_degrade_warning(build_dir: Path) -> bool:
    """Is this the first time this process could not lock ``build_dir``?

    A filesystem that cannot flock cannot flock for the whole run, so the
    warning is about a *configuration*, not about a compile: emitted once
    per directory rather than once per test, or a shared tree nobody can
    lock would warn in every element of every suite forever. ``realpath``'d
    for the reason :func:`~rtl_buddy.tools.vlog_sim._claim_rebuild` is —
    two spellings of one directory are one directory.
    """
    key = os.path.realpath(build_dir)
    with _DEGRADED_BUILD_DIRS_LOCK:
        if key in _DEGRADED_BUILD_DIRS:
            return False
        _DEGRADED_BUILD_DIRS.add(key)
        return True


def _reset_degrade_warnings() -> None:
    """Forget every claim. Tests only — one pytest process is many runs."""
    with _DEGRADED_BUILD_DIRS_LOCK:
        _DEGRADED_BUILD_DIRS.clear()


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
            holder = _read_holder(fd)
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


@contextlib.contextmanager
def build_dir_lock(build_dir: Path | str, *, test: str | None = None) -> Iterator[bool]:
    """Serialise compiles into one shared build directory across processes.

    Several ``rb`` processes started together — a suite's Slurm array
    elements, or just two terminals — find no stamp in a freshly created
    (or freshly deleted) shared build tree and all compile into it at
    once. That is #369 across processes rather than within one, and it
    surfaced in #494 as ``ld returned 1 exit status`` from three of eight
    tests after a manual ``rm -rf .shared-builds``. The #495 in-job
    grouping serialises the compiles of ONE process; this serialises the
    rest.

    Scoped, unlike the artefact-tree lock: it is held for the stamp
    check and the compile that may follow, and released as soon as the
    stamp is written. The caller therefore keeps its stamp check
    *inside* the ``with`` — double-checked locking, so a waiter that
    blocked while another process compiled exactly what it needs reuses
    that build instead of rebuilding it. This lock is for *populating* a
    build directory; a caller that has already decided it will reuse a
    valid stamp must not take it (see :meth:`VlogSim.compile`), or every
    reuser in a fan-out would queue behind whatever compile happens to
    hold it.

    Holder metadata (pid, test, start time) goes into the lock file so
    the waiting line can say who is ahead; it is advisory and possibly
    stale, exactly as for the tree lock.

    Yields ``True`` when the lock is held. A filesystem that cannot lock
    (read-only, ``ENOLCK`` on some NFS mounts) yields ``False`` after a
    warning: a broken lock degrades to today's unlocked behaviour rather
    than turning a working build red, because nothing in this path may
    change a build job's exit code. Nothing in-tree branches on the
    yielded flag — the compile that follows is the same either way — so
    it exists for tests, and for a caller that wants to record which
    guarantee it had.

    Lock ordering: a thread holds at most one build lock (one compile,
    one directory), and the artefact-tree lock is taken when a command
    starts — never while a build lock is held — so the two cannot cycle.

    Two bounds worth knowing, both documented in docs/known-issues.md:
    an NFS mount with ``nolock`` or ``local_lock=flock``/``all`` makes
    ``flock`` process-local, and it *succeeds* — so there is nothing to
    warn about and the cross-node guarantee silently does not hold. And
    the lock file lives inside the directory it guards, so an
    ``rm -rf`` of the shared tree that races a live run unlinks it from
    under the holder, after which the next process locks a fresh inode:
    delete a shared tree between runs, not during one.
    """
    build_dir = Path(build_dir)
    fields = {
        # The schema the other compile.* build events carry (vlog_sim's
        # `_build_dir_fields`, shared case): basename in `build_dir`
        # because that is what a reader compares against an `ls` of
        # `.shared-builds/`, absolute path alongside it.
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
            # Suppressed: everything from here to the yield exists to
            # degrade, and a close that failed must not be the exception
            # this path swore not to raise.
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
        # Diagnostics only, so a failure here must not cost the lock we
        # just took.
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
        # Closing the descriptor releases the flock; the file stays, with
        # its holder metadata, for the next process to read.
        if fd is not None:
            os.close(fd)


def _wait_for_lock(fd: int, fields: dict) -> None:
    """Block until ``fd``'s flock is ours, saying so while we wait.

    A poll loop rather than one blocking ``flock(fd, LOCK_EX)`` for the
    reason the non-blocking first attempt exists at all: a compile can
    take minutes — longer when the holder is queued for a VCS licence —
    and a job log that goes silent for them is the shape of #494's
    original complaint. The line repeats every
    :data:`BUILD_LOCK_ANNOUNCE_SEC` with the wait so far, so a long wait
    keeps reading as a wait rather than as a hang.

    No cap, deliberately: the only alternative to waiting is compiling
    into a directory another process is linking in, which is the bug this
    lock exists to prevent. A holder that dies releases the flock in the
    kernel, so the wait ends without anyone cleaning up; a holder that
    wedges hangs this job either way, lock or no lock.

    ``OSError`` propagates to :func:`build_dir_lock`'s degrade path,
    which is where every "this filesystem cannot lock" answer belongs.
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


def _read_holder(fd: int) -> dict:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        holder = json.loads(raw.decode())
    except (OSError, ValueError):
        return {}
    return holder if isinstance(holder, dict) else {}


def _describe_holder(holder: dict) -> str:
    """The parenthetical both lock messages name their holder with.

    Every key is optional — a tree lock records the ``command``, a build
    lock the ``test``, and a file written by a process that has since
    died records whatever it managed to — so a holder it can say nothing
    about renders as the empty string rather than as blanks.
    """
    parts = []
    if holder.get("pid") is not None:
        parts.append(f"pid {holder['pid']}")
    if holder.get("command"):
        parts.append(f"rb {holder['command']}")
    if holder.get("test"):
        parts.append(f"test {holder['test']}")
    if holder.get("started"):
        parts.append(f"started {holder['started']}")
    return f" ({', '.join(parts)})" if parts else ""
