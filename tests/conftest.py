"""Shared pytest fixtures for the rtl_buddy test suite."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


_FIXTURES_ROOT = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_environ():
    """Restore ``os.environ`` after every test.

    ``RootConfig.__init__`` calls ``apply_env_file``, which writes into
    ``os.environ`` directly — so *any* test constructing a RootConfig in
    a project carrying ``.rtl-buddy/.env`` mutates the process
    environment for the rest of the session. ``monkeypatch`` cannot undo
    that: ``delenv(..., raising=False)`` on a key that did not exist
    records nothing to restore. Autouse because the leak is a property of
    the constructor, not of the tests that happen to know about it.

    Teardown ordering makes this safe for tests that swap ``os.environ``
    for a plain dict via ``monkeypatch.setattr``: function-scoped
    ``monkeypatch`` is set up after this fixture and torn down before it,
    so the real environment is back in place by the time the snapshot is
    restored.
    """
    snapshot = dict(os.environ)
    yield
    if os.environ != snapshot:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def reset_cancellation_latch():
    """Un-latch ``process_utils`` cancellation between tests.

    The latch is one-way *per process* by design — nothing resumes a
    cancelled run — so a test that sets it (directly, or by calling
    ``terminate_live_managed_processes``) would otherwise stop every later
    test in the session from spawning a tool process at all. Autouse for the
    same reason as the fixtures above: the state belongs to the module, not
    to the tests that know about it.
    """
    from rtl_buddy import process_utils

    process_utils._reset_cancellation_latch()
    yield
    process_utils._reset_cancellation_latch()


@pytest.fixture(autouse=True)
def reset_tool_path_warning_dedupe():
    """Forget the process-global "already warned" sets between tests.

    ``config.toolpath._UNRESOLVED_WARNED`` and
    ``config.verible._EXE_FALLBACK_WARNED`` dedupe a diagnostic that is a
    static property of the config plus the environment, so they never
    clear themselves in a real run. Under pytest that makes any test
    asserting on one of those warnings order-dependent: an earlier test
    resolving the same key (the fixture projects reuse names like
    ``surfer-default``) swallows the warning the later one asserts on,
    and it only fails under a particular ``-k`` / random ordering.
    Autouse for the same reason as ``clean_environ``: the leak belongs to
    the module, not to the tests that happen to know about it
    (#439 review).
    """
    from rtl_buddy.config import toolpath, verible

    toolpath.reset_unresolved_warnings()
    verible.reset_exe_fallback_warnings()
    yield
    toolpath.reset_unresolved_warnings()
    verible.reset_exe_fallback_warnings()


@pytest.fixture
def minimal_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the minimal_project fixture to a tmp dir, chdir into it, and return its path.

    The fixture provides a valid root_config.yaml + regression.yaml + tests.yaml
    + models.yaml so commands that walk through RootConfig load can be exercised
    end-to-end without touching real EDA tooling.
    """
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES_ROOT / "minimal_project", target)
    monkeypatch.chdir(target)
    return target
