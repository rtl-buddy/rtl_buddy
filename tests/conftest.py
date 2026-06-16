"""Shared pytest fixtures for the rtl_buddy test suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


_FIXTURES_ROOT = Path(__file__).parent / "fixtures"


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
