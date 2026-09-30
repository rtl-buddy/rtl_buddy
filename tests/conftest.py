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

    Autouse because ``RootConfig.__init__`` writes ``.rtl-buddy/.env`` into ``os.environ`` directly, and ``monkeypatch`` cannot undo that.
    """
    snapshot = dict(os.environ)
    yield
    if os.environ != snapshot:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def no_inherited_job_tag(monkeypatch):
    """A CI host exporting ``RTL_BUDDY_JOB_TAG`` would rename every job."""
    monkeypatch.delenv("RTL_BUDDY_JOB_TAG", raising=False)


@pytest.fixture(autouse=True)
def reset_cancellation_latch():
    """Un-latch ``process_utils`` cancellation between tests.

    The latch is one-way per process, so a test that sets it would otherwise block every later test from spawning tool processes.
    """
    from rtl_buddy import process_utils

    process_utils._reset_cancellation_latch()
    yield
    process_utils._reset_cancellation_latch()


@pytest.fixture(autouse=True)
def reset_tool_path_warning_dedupe():
    """Forget the process-global "already warned" sets between tests.

    Without this, a warning asserted in one test is swallowed when an earlier test already emitted it for the same key.
    """
    from rtl_buddy.config import toolpath, verible

    toolpath.reset_unresolved_warnings()
    verible.reset_exe_fallback_warnings()
    yield
    toolpath.reset_unresolved_warnings()
    verible.reset_exe_fallback_warnings()


@pytest.fixture(autouse=True)
def reset_print_failures_only():
    """Clear the process-global summary filter between tests."""
    from rtl_buddy import logging_utils

    logging_utils.set_print_failures_only(False)
    yield
    logging_utils.set_print_failures_only(False)


@pytest.fixture
def minimal_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the minimal_project fixture to a tmp dir, chdir into it, and return its path."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES_ROOT / "minimal_project", target)
    monkeypatch.chdir(target)
    return target


def _force_constraint_backend(monkeypatch: pytest.MonkeyPatch, backend: str) -> str:
    """Pin the SDC/XDC reader backend for one test."""
    from rtl_buddy.constraints import tcl_reader

    if backend == tcl_reader.TCL_BACKEND and not tcl_reader.tcl_available():
        pytest.skip("no Tcl reader worker runs here, so the interp backend is out")
    monkeypatch.setenv(tcl_reader.BACKEND_ENV, backend)
    return backend


@pytest.fixture(params=["tcl", "tokenizer"])
def constraint_backend(request, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the test once per constraint reader backend.

    ``tcl`` is skipped where the worker cannot start a Tcl interpreter (Python without ``_tkinter``).
    """
    return _force_constraint_backend(monkeypatch, request.param)


@pytest.fixture
def tcl_backend(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin the Tcl safe-interp backend, skipping without a usable worker."""
    return _force_constraint_backend(monkeypatch, "tcl")


@pytest.fixture
def tokenizer_backend(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin the vendored-tokenizer backend, which every Python can run."""
    return _force_constraint_backend(monkeypatch, "tokenizer")
