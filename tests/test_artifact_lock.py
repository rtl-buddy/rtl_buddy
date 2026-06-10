"""Tests for the per-artefact-tree advisory lock (#73).

flock(2) treats file descriptors from separate ``open()`` calls as
independent even within one process, so a second ``ArtifactLocks``
instance in the same test process genuinely contends with the first —
no subprocess gymnastics needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.artifact_lock import LOCK_FILENAME, ArtifactLocks
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
