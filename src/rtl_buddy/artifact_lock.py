"""Advisory flocks: one per artefact tree, one per shared build dir.

Two ``rtl-buddy`` processes sharing one suite artefact tree
(``<command_root>/artefacts/``) would interleave compile workspaces,
``run-NNNN`` dirs, and the latest-run symlinks. Rather than detect the
corruption afterwards, each command takes an exclusive non-blocking
``flock(2)`` on ``<artifact_root>/.rtl-buddy.lock`` when it enters its
execution context and raises :class:`FatalRtlBuddyError` immediately if
another process already holds it.

The scope is one *artefact root*, which is where ``--run-tag`` comes in
(#541): a tagged run's root is ``<command_root>/artefacts/.runs/<tag>/``,
so two tagged runs of one suite lock different files and no longer
serialise on each other. Nothing here knows about the tag — the root
arrives already namespaced, from
:class:`~rtl_buddy.exec_context.ExecutionContext`.

The lock is advisory and kernel-managed: it disappears when the holding
process exits for any reason, and :meth:`ArtifactLocks.release_all` is
called from the CLI's outermost ``finally`` so an interrupted or failed
run (a SIGINT during OpenROAD, an OpenROAD Tcl error) gives it back on
the way out rather than only at teardown (#609). The lock *file*
persists and carries holder metadata (pid, command, start time, host,
process start token); a leftover file with no live flock is harmless.

That metadata is also the fallback. A filesystem whose lock state can
outlive its owner — and any state a reader cannot explain — would
otherwise wedge a tree forever, so a lock we cannot take is re-read: if
it names *this* host and a pid that is gone (or has been reused by a
different process, which the start token catches), it is reclaimed by
atomically replacing the lock file under a second ``.reclaim`` flock
that serialises reclaimers. A lock recorded on another host is never
judged by a pid here, and a record with no host is left alone.

:func:`build_dir_lock` applies the same idiom at a second, narrower
scope — one *shared build directory*, held only for the compile that
populates it (#494). The two differ deliberately: the tree lock is
whole-process and fails loud, while a build lock is scoped, blocks
rather than refuses, and degrades to unlocked when the filesystem cannot
lock at all.
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

# Serialises the reclaim of a stale tree lock (#609). A sibling of the
# lock file rather than the lock file itself, because reclaiming means
# replacing that file: two processes that both diagnosed the same dead
# holder must not both install their own lock over it.
RECLAIM_LOCK_SUFFIX = ".reclaim"

# errnos that mean "someone else holds it" rather than "this filesystem
# cannot lock". Only the former is contention; the latter is a broken
# mount and says so (the tree lock fails loud either way, but the two
# need different answers from a reader).
_CONTENTION_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})

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
            # A reclaimer judged the *previous* holder's record, which
            # this file still carried between our flock and our write,
            # and replaced the file under us. It holds the tree; we hold
            # an unlinked inode.
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
        """Drop every held lock.

        Called from the CLI's outermost ``finally`` (#609) so that a tool
        failure, a ``FatalRtlBuddyError``, or a ``KeyboardInterrupt``
        during a long ``rb pnr`` gives the tree back on the way out
        rather than relying on process teardown alone. The kernel still
        releases the flock if the process dies without unwinding; this is
        the deterministic path, and it is what tests use too.

        Idempotent, and never raises: this runs while another exception
        may already be propagating, and a failed close must not replace
        the real error with a lock-teardown one.
        """
        for fd in self._held.values():
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
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


def _write_holder(fd: int, command: str | None) -> None:
    """Stamp this process's identity into the lock file it just locked.

    Diagnostics *and*, since #609, evidence: ``host`` and ``start_token``
    are what a later process needs to decide whether a lock whose flock
    it cannot take belongs to a holder that no longer exists. A record
    without them (a lock written by an older rtl-buddy) is never
    reclaimed — it is read as "identity unknown", which is the safe
    reading on a shared filesystem.
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
    """Does a process with this pid exist, whoever owns it?

    ``EPERM`` means it exists and is someone else's — a shared compute
    node is exactly where that happens, and "not mine" must never read
    as "not there".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown answer; the conservative one is "still running".
        return True
    return True


def _process_start_token(pid: int) -> str | None:
    """A stable-per-process stamp for ``pid``, or None if unobtainable.

    Guards the reclaim against pid reuse: a dead holder's number can be
    handed to an unrelated process before anyone looks, and the liveness
    check alone would then read a stranger as the holder forever. Linux
    reads field 22 of ``/proc/<pid>/stat`` (start time in clock ticks);
    everything else shells out to ``ps -o lstart=`` once per pid. None
    from either is not an error — the caller falls back to liveness
    alone, which is the pre-#609 answer.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        # The comm field is parenthesised and may contain spaces, so the
        # split point is its LAST ')': the fields after it start at
        # ``state`` (field 3), which puts starttime (field 22) at index 19.
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
    """Is this lock record's owner definitely gone from this machine?

    Three gates, all of which must pass, because the cost of a wrong
    "yes" is two rtl-buddy runs interleaving one artefact tree:

    * a pid we can read;
    * ``host`` recorded AND equal to ours — a lock taken on another node
      of a shared filesystem is never judged by a pid on this one, and a
      record with no host at all (pre-#609, or truncated) is unknown
      identity, so it is left alone;
    * the pid is not alive, or it is alive as a *different* process than
      the one that wrote the record (pid reuse, caught by the start
      token when both the record and the live process have one).
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
    """Take a lock whose recorded holder is dead, or return None (#609).

    Returns a locked file descriptor for ``lock_path`` on success. None
    means "treat this as contention", which is the answer for every case
    that is not provably reclaimable.

    Race safety has three parts:

    * reclaimers serialise on a sibling ``.reclaim`` flock, so two runs
      that spot the same corpse do not both install a lock;
    * inside that section the lock is re-opened and re-tried — a holder
      that exited in the meantime is simply locked, no replacement
      needed — and the record is re-read and re-judged against the file
      as it is *now*, not as it was before the wait;
    * the replacement itself is an ``os.replace`` of a fresh file that
      is already flocked by us, so the lock path never exists unlocked
      and never exists twice. The dead holder's inode is unlinked, which
      is safe precisely because nothing living holds it.
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
            # Another process is mid-reclaim. It wins; we are its
            # contender and report contention as usual.
            return None

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass
        else:
            # The holder let go between our two attempts (or a reclaimer
            # ahead of us finished and exited). Nothing to replace.
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
    """Is ``fd`` still the file at ``lock_path``, now that our record is in it?

    Closes the one window the reclaim leaves open (#609). A process that
    wins the flock on a file still carrying a dead holder's record looks,
    until its own record lands, exactly like that dead holder — and a
    reclaimer may replace the file in that window. Checked under the same
    ``.reclaim`` flock the reclaimer works under, *after* our record is
    written: a reclaimer that ran before this check has already replaced
    the path (we see a different inode and stand down); one that runs
    after it reads our live record and stands down itself.

    A guard that cannot be opened or locked means reclaim cannot run here
    either, so the answer falls back to the inode comparison alone.
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
        # Either this filesystem cannot lock at all or the rename failed;
        # in both cases we do not hold the tree and must not claim to.
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
    if holder.get("host"):
        parts.append(f"on {holder['host']}")
    return f" ({', '.join(parts)})" if parts else ""
