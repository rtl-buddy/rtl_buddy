"""Tests for the per-artefact-tree advisory lock (#73).

flock(2) treats file descriptors from separate ``open()`` calls as
independent even within one process, so a second ``ArtifactLocks``
instance in the same test process genuinely contends with the first —
no subprocess gymnastics needed.

The second half covers the scoped build-directory lock (#494), which
shares the idiom but not the policy: it blocks instead of refusing, and
degrades to unlocked when the filesystem cannot lock.
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
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy import artifact_lock as artifact_lock_module
from rtl_buddy.artifact_lock import (
    BUILD_LOCK_FILENAME,
    BUILD_LOCK_POLL_SEC,
    LOCK_FILENAME,
    ArtifactLocks,
    build_dir_lock,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy


@pytest.fixture
def locks():
    """An ArtifactLocks manager that always drops its locks on teardown."""
    managers = []

    def make():
        m = ArtifactLocks()
        managers.append(m)
        return m

    yield make
    for m in managers:
        m.release_all()


def test_acquire_creates_lock_file_with_holder_metadata(tmp_path, locks):
    root = tmp_path / "artefacts"
    locks().acquire(root, command="test")
    lock_file = root / LOCK_FILENAME
    assert lock_file.is_file()
    holder = json.loads(lock_file.read_text())
    assert holder["pid"] == os.getpid()
    assert holder["command"] == "test"
    assert holder["started"]


def test_contended_acquire_fails_loud_naming_holder(tmp_path, locks):
    root = tmp_path / "artefacts"
    locks().acquire(root, command="regression")
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        locks().acquire(root, command="test")
    msg = str(excinfo.value)
    assert "another rtl-buddy run" in msg
    assert str(root) in msg
    assert f"pid {os.getpid()}" in msg
    assert "rb regression" in msg


def test_reacquire_same_root_is_idempotent(tmp_path, locks):
    root = tmp_path / "artefacts"
    manager = locks()
    manager.acquire(root, command="regression")
    manager.acquire(root, command="regression")  # same suite re-entered


def test_distinct_roots_do_not_contend(tmp_path, locks):
    locks().acquire(tmp_path / "suite_a" / "artefacts", command="test")
    locks().acquire(tmp_path / "suite_b" / "artefacts", command="test")


def test_release_all_frees_the_lock(tmp_path, locks):
    root = tmp_path / "artefacts"
    first = locks()
    first.acquire(root, command="test")
    first.release_all()
    locks().acquire(root, command="test")


def test_corrupt_holder_metadata_still_fails_loud(tmp_path, locks):
    root = tmp_path / "artefacts"
    locks().acquire(root, command="test")
    (root / LOCK_FILENAME).write_text("not json{")
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run"):
        locks().acquire(root, command="test")


# ---------------------------------------------------------------------------
# CLI wiring: _enter_command_context takes the lock; --list paths don't
# ---------------------------------------------------------------------------


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_artifact_lock")


def test_cli_command_fails_loud_when_artefacts_locked(minimal_project: Path, locks):
    locks().acquire(minimal_project / "artefacts", command="regression")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["filelist", "example", "run.f", "-c", "models.yaml"]
    )
    assert result.exit_code != 0
    assert "another rtl-buddy run" in str(result.exception)


def test_cli_list_path_ignores_held_lock(minimal_project: Path, locks):
    locks().acquire(minimal_project / "artefacts", command="regression")
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "--list"])
    assert result.exit_code == 0, result.output
    assert "basic" in result.output


def test_cli_command_acquires_lock_in_artifact_root(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["filelist", "example", "run.f", "-c", "models.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert (minimal_project / "artefacts" / LOCK_FILENAME).is_file()
    rb._artifact_locks.release_all()


# ---------------------------------------------------------------------------
# The scoped shared-build lock (#494)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forget_degrade_warnings():
    """The "cannot lock" warning is claimed once per directory per PROCESS,
    and one pytest process is many runs."""
    artifact_lock_module._reset_degrade_warnings()
    yield
    artifact_lock_module._reset_degrade_warnings()


def _nonblocking_acquire(lock_file: Path) -> bool:
    """Could a *separate* file description take the lock right now?

    Separate ``open()``, so flock treats it as another holder even inside
    this process — the same property the module docstring relies on.
    """
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def test_build_dir_lock_holds_the_directory_and_releases_it_on_exit(tmp_path):
    build_dir = tmp_path / "obj_dir_abc"
    build_dir.mkdir()
    lock_file = build_dir / BUILD_LOCK_FILENAME

    with build_dir_lock(build_dir, test="test_a") as held:
        assert held is True
        assert not _nonblocking_acquire(lock_file)
        holder = json.loads(lock_file.read_text())
        assert holder["pid"] == os.getpid()
        assert holder["test"] == "test_a"
        assert holder["started"]

    # Scoped, unlike the tree lock: the compile is over, so the next
    # process may have the directory.
    assert _nonblocking_acquire(lock_file)


def test_build_dir_lock_degrades_when_the_filesystem_cannot_lock(
    tmp_path, monkeypatch, caplog
):
    """ENOLCK (some NFS mounts) or a read-only tree must not fail a build.

    The exit-0 contract outranks the serialisation: losing the lock costs
    the cross-process guarantee, and the warning says exactly that.
    """
    build_dir = tmp_path / "obj_dir_abc"
    build_dir.mkdir()

    def _no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", _no_locks)
    with caplog.at_level(logging.WARNING):
        with build_dir_lock(build_dir, test="test_a") as held:
            assert held is False

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_lock_unavailable"
    )
    assert record.levelno == logging.WARNING
    assert "No locks available" in record.getMessage()
    assert "not serialised" in record.getMessage()


def test_a_filesystem_that_cannot_lock_warns_once_per_directory(
    tmp_path, monkeypatch, caplog
):
    """One warning per build directory, not one per compile.

    A tree nobody can flock (a shared team checkout this user cannot
    create the lock file in, an NFS mount answering ENOLCK) cannot flock
    for the whole run, so the loss of guarantee is a fact about the
    configuration. Repeating it for every test of every suite would bury
    the one line that matters.
    """
    first = tmp_path / "obj_dir_abc"
    first.mkdir()
    second = tmp_path / "obj_dir_def"
    second.mkdir()

    def _no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", _no_locks)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with build_dir_lock(first, test="test_a") as held:
                assert held is False
        with build_dir_lock(second, test="test_a") as held:
            assert held is False

    warned = [
        r.rtl_fields["build_path"]
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_lock_unavailable"
    ]
    assert warned == [str(first), str(second)]


@pytest.mark.skipif(os.name != "posix", reason="flock(2) is POSIX")
def test_a_long_wait_keeps_saying_it_is_a_wait(tmp_path, monkeypatch):
    """A wait announced once and then silent for an hour reads as a hang.

    The intervals are the module's, so this drives them down rather than
    sleeping through the real five minutes. Separate ``open()``, which
    flock counts as another holder even in this process.
    """
    build_dir = tmp_path / "obj_dir_abc"
    build_dir.mkdir()
    monkeypatch.setattr(artifact_lock_module, "BUILD_LOCK_POLL_SEC", 0.01)
    monkeypatch.setattr(artifact_lock_module, "BUILD_LOCK_ANNOUNCE_SEC", 0.02)
    seen = []
    real = artifact_lock_module.log_console_event

    def _spy(spy_logger, level, event, **fields):
        seen.append((event, fields))
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(artifact_lock_module, "log_console_event", _spy)

    fd = os.open(build_dir / BUILD_LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o644)
    result = {}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _take():
            with build_dir_lock(build_dir, test="waiter") as held:
                result["held"] = held

        waiter = threading.Thread(target=_take)
        waiter.start()
        deadline = time.time() + 60
        while len(seen) < 3 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        os.close(fd)  # releases the flock, so the waiter gets it
    waiter.join(60)
    assert not waiter.is_alive()
    assert result == {"held": True}

    assert len(seen) >= 3, "the wait was announced once and then went quiet"
    assert {event for event, _ in seen} == {"compile.build_lock_wait"}
    # Every line names the directory being waited for, and carries the
    # wait so far — 0 on the first, which is what the human message drops
    # rather than printing "(0s so far)".
    assert {fields["build_path"] for _, fields in seen} == {str(build_dir)}
    assert seen[0][1]["waited_sec"] == 0


def test_build_dir_lock_degrades_when_the_lock_file_cannot_be_created(tmp_path, caplog):
    """A directory that vanished under us is the same class of problem:
    say so and let the compile decide, rather than raising out of a path
    that may not change an exit code."""
    with caplog.at_level(logging.WARNING):
        with build_dir_lock(tmp_path / "gone" / "obj_dir_abc", test="test_a") as held:
            assert held is False
    assert any(
        getattr(r, "rtl_event", None) == "compile.build_lock_unavailable"
        for r in caplog.records
    )


@pytest.mark.skipif(os.name != "posix", reason="flock(2) is POSIX")
def test_another_process_blocks_on_the_lock_until_it_is_released(tmp_path):
    """The case the issue reports: several ``rb`` processes started
    together against a cold shared build tree (#494).

    A real second process, because that is the whole claim — the #495
    in-job grouping already serialises threads, and only flock reaches
    across processes.
    """
    build_dir = tmp_path / "obj_dir_abc"
    build_dir.mkdir()
    marker = tmp_path / "child-at-the-lock"
    # The child announces that it is *about to* take the lock, so the
    # parent releases only once the child is provably queued rather than
    # after a sleep it might still be importing through — the wait it
    # reports is then a wait and not a slow interpreter start.
    script = textwrap.dedent(
        f"""
        import json, time
        from pathlib import Path
        from rtl_buddy.artifact_lock import build_dir_lock

        Path({str(marker)!r}).write_text("here")
        start = time.time()
        with build_dir_lock({str(build_dir)!r}, test="child") as held:
            print("RESULT " + json.dumps(
                {{"held": held, "waited": time.time() - start}}
            ))
        """
    )

    child = None
    try:
        with build_dir_lock(build_dir, test="parent") as held:
            assert held is True
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.time() + 60
            while not marker.exists() and time.time() < deadline:
                assert child.poll() is None, "the second process exited early"
                time.sleep(0.05)
            assert marker.exists(), "the second process never reached the lock"
            # The marker says the child is about to take the lock, not
            # that it has tried yet; releasing on the spot could let it
            # succeed first time and report a wait of zero. This is the
            # slack for its next statement, and it is what makes the
            # elapsed-time assertion below deterministic.
            time.sleep(0.3)
            assert child.poll() is None, "the second process did not wait for the lock"
        out = child.communicate(timeout=60)[0]
    finally:
        # An assertion inside the `with` would otherwise leave a child
        # blocked on a flock in tmp_path with nobody to reap it.
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=60)
    assert child.returncode == 0, out
    result = json.loads(
        next(line for line in out.splitlines() if line.startswith("RESULT "))[7:]
    )
    assert result["held"] is True
    # It blocked: a lock taken on the first try costs nothing, while one
    # waited for costs at least the poll interval.
    assert result["waited"] >= BUILD_LOCK_POLL_SEC, result
    # And it said so on its own stdout while waiting — a dispatched job
    # log that simply stops for a compile reads as a hang.
    assert "waiting for another rtl-buddy" in out
    assert f"pid {os.getpid()}" in out


# ---------------------------------------------------------------------------
# Stale-lock reclaim (#609)
#
# Every test here needs the same unnatural state: a lock file whose flock
# is genuinely held (so `acquire` cannot take it) but whose *record* names
# a holder that is gone. A second file description in this process supplies
# the flock — the property the module docstring opens with — and the record
# is written underneath it.
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """A pid that existed and does not any more."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait(timeout=60)
    return proc.pid


def _wedge_lock(root: Path, holder: dict) -> int:
    """Hold ``root``'s tree flock and write ``holder`` into the file.

    Returns the descriptor holding the flock; the caller closes it.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock_file = root / LOCK_FILENAME
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_file.write_text(json.dumps(holder))
    return fd


@pytest.fixture
def wedged():
    """`_wedge_lock`, with the descriptors closed on the way out."""
    fds = []

    def make(root: Path, holder: dict) -> int:
        fd = _wedge_lock(root, holder)
        fds.append(fd)
        return fd

    yield make
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def test_stale_same_host_dead_pid_is_reclaimed(tmp_path, locks, wedged, caplog):
    """The #609 case: the owner is gone, so the next run takes the tree."""
    root = tmp_path / "artefacts"
    dead = _dead_pid()
    wedged(
        root,
        {
            "pid": dead,
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": socket.gethostname(),
            "start_token": "whatever-it-was",
        },
    )
    with caplog.at_level(logging.WARNING):
        locks().acquire(root, command="synth")

    holder = json.loads((root / LOCK_FILENAME).read_text())
    assert holder["pid"] == os.getpid()
    assert holder["command"] == "synth"
    assert holder["host"] == socket.gethostname()
    assert "artifact_lock reclaimed" in caplog.text

    # Reclaimed means *held*, not merely overwritten: the next process
    # in still finds the tree taken.
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run"):
        locks().acquire(root, command="pnr")


def test_live_holder_is_never_reclaimed(tmp_path, locks, wedged):
    """A pid that is alive is the holder, whatever else the record says."""
    root = tmp_path / "artefacts"
    wedged(
        root,
        {
            "pid": os.getpid(),
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": socket.gethostname(),
            "start_token": artifact_lock_module._process_start_token(os.getpid()),
        },
    )
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run") as excinfo:
        locks().acquire(root, command="synth")
    assert f"pid {os.getpid()}" in str(excinfo.value)


def test_lock_from_another_host_is_not_reclaimed_by_pid(tmp_path, locks, wedged):
    """A shared filesystem: a dead pid *here* says nothing about a pid there."""
    root = tmp_path / "artefacts"
    wedged(
        root,
        {
            "pid": _dead_pid(),
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": "some-other-node",
            "start_token": "12345",
        },
    )
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run") as excinfo:
        locks().acquire(root, command="synth")
    assert "some-other-node" in str(excinfo.value)


def test_lock_without_a_host_is_not_reclaimed(tmp_path, locks, wedged):
    """A pre-#609 record has unknown identity, which is not "reclaim me"."""
    root = tmp_path / "artefacts"
    wedged(
        root,
        {"pid": _dead_pid(), "command": "pnr", "started": "2026-09-20T10:00:00"},
    )
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run"):
        locks().acquire(root, command="synth")


def test_reused_pid_is_treated_as_a_dead_holder(tmp_path, locks, wedged):
    """Alive, but not the process that wrote the record: the start token says so."""
    root = tmp_path / "artefacts"
    wedged(
        root,
        {
            "pid": os.getpid(),
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": socket.gethostname(),
            # This process started once; a token from another era means
            # the recorded holder is gone and its number was handed on.
            "start_token": "0",
        },
    )
    locks().acquire(root, command="synth")
    assert json.loads((root / LOCK_FILENAME).read_text())["command"] == "synth"


def test_reclaim_does_not_race_a_concurrent_reclaimer(tmp_path, locks, wedged):
    """Two runs diagnosing one corpse: exactly one of them gets the tree."""
    root = tmp_path / "artefacts"
    wedged(
        root,
        {
            "pid": _dead_pid(),
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": socket.gethostname(),
            "start_token": "gone",
        },
    )
    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt(manager):
        barrier.wait(timeout=30)
        try:
            manager.acquire(root, command="synth")
        except FatalRtlBuddyError as exc:
            results.append(exc)
        else:
            results.append(True)

    managers = [locks(), locks()]
    threads = [threading.Thread(target=attempt, args=(m,)) for m in managers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert sorted(r is True for r in results) == [False, True], results


def test_reclaim_picks_up_a_lock_released_between_the_two_attempts(
    tmp_path, locks, monkeypatch
):
    """The holder exits mid-diagnosis: lock it, do not replace the file."""
    root = tmp_path / "artefacts"
    fd = _wedge_lock(
        root,
        {
            "pid": _dead_pid(),
            "command": "pnr",
            "started": "2026-09-20T10:00:00",
            "host": socket.gethostname(),
            "start_token": "gone",
        },
    )
    before = (root / LOCK_FILENAME).stat().st_ino
    real_reclaim = artifact_lock_module._reclaim_if_stale

    def _release_then_reclaim(*args, **kwargs):
        os.close(fd)  # the "holder" exits while we are inside the reclaim
        return real_reclaim(*args, **kwargs)

    monkeypatch.setattr(
        artifact_lock_module, "_reclaim_if_stale", _release_then_reclaim
    )
    locks().acquire(root, command="synth")
    assert (root / LOCK_FILENAME).stat().st_ino == before


def test_unlockable_filesystem_is_not_reported_as_contention(
    tmp_path, locks, monkeypatch
):
    """ENOLCK is a broken mount, not another run — say which."""
    root = tmp_path / "artefacts"

    def _no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(artifact_lock_module.fcntl, "flock", _no_locks)
    with pytest.raises(FatalRtlBuddyError, match="cannot lock this artefact tree"):
        locks().acquire(root, command="synth")


# ---------------------------------------------------------------------------
# Release paths: the lock comes back on failure and on interrupt (#609)
# ---------------------------------------------------------------------------


def _rb_running(raiser):
    """An RtlBuddy whose CLI takes the tree lock and then raises."""
    rb = RtlBuddy(name="test_artifact_lock_release")

    def _app(*args, **kwargs):
        raiser(rb)

    rb.app = _app
    return rb


def test_run_releases_the_tree_lock_when_a_tool_fails(tmp_path):
    """An OpenROAD Tcl error surfaces as FatalRtlBuddyError — and unlocks."""
    root = tmp_path / "artefacts"

    def _fail(rb):
        rb._artifact_locks.acquire(root, command="pnr")
        raise FatalRtlBuddyError("openroad: Tcl error in macro placement")

    assert _rb_running(_fail).run() == 2
    assert _nonblocking_acquire(root / LOCK_FILENAME)


def test_run_releases_the_tree_lock_on_keyboard_interrupt(tmp_path, capsys):
    """Ctrl-C during OpenROAD: exit 130, tree given back."""
    root = tmp_path / "artefacts"

    def _interrupt(rb):
        rb._artifact_locks.acquire(root, command="pnr")
        raise KeyboardInterrupt

    assert _rb_running(_interrupt).run() == 130
    captured = capsys.readouterr()
    assert "interrupted" in captured.out + captured.err
    assert _nonblocking_acquire(root / LOCK_FILENAME)


def test_run_releases_the_tree_lock_on_success(tmp_path):
    root = tmp_path / "artefacts"

    def _ok(rb):
        rb._artifact_locks.acquire(root, command="test")

    assert _rb_running(_ok).run() == 0
    assert _nonblocking_acquire(root / LOCK_FILENAME)


def test_acquirer_that_loses_the_path_to_a_reclaimer_stands_down(tmp_path, monkeypatch):
    """#609: a reclaimer may replace the file between our flock and our record."""
    from rtl_buddy import artifact_lock

    real_write = artifact_lock._write_holder

    def write_then_lose_the_path(fd, command):
        real_write(fd, command)
        # What a concurrent reclaimer's os.replace does to the path.
        usurper = tmp_path / "usurper"
        usurper.write_text("{}")
        os.replace(usurper, tmp_path / artifact_lock.LOCK_FILENAME)

    monkeypatch.setattr(artifact_lock, "_write_holder", write_then_lose_the_path)
    locks = artifact_lock.ArtifactLocks()
    with pytest.raises(FatalRtlBuddyError, match="already using"):
        locks.acquire(tmp_path, command="pnr")
    assert not locks._held
