"""Tests for ``rtl_buddy.hub.loop._discover_viewer_bundle``.

The discovery helper lets ``rb hub start --serve-viewer`` find the SPA
shipped inside the installed ``rtl-buddy-view`` package, so users don't
have to pass ``--viewer-bundle PATH`` for the common case.

The helper is a *soft* dependency: rtl-buddy-view is not a hard runtime
dep of rtl-buddy. If the package isn't installed, or its API drifts and
``viewer_bundle.path()`` raises unexpectedly, the helper returns
``None`` and the hub falls back to its placeholder page. These tests
exercise both branches by feeding a fake ``rtl_buddy_view`` module into
``sys.modules`` for the duration of each test.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from rtl_buddy.hub.loop import _discover_viewer_bundle


@pytest.fixture
def fake_viewer_pkg(monkeypatch):
    """Install a stub ``rtl_buddy_view`` package backed by an in-memory
    ``viewer_bundle`` submodule with a configurable ``path()`` callable."""

    pkg = types.ModuleType("rtl_buddy_view")
    submod = types.ModuleType("rtl_buddy_view.viewer_bundle")

    # Default to "no bundle"; individual tests override.
    submod.path = lambda: None  # type: ignore[attr-defined]
    pkg.viewer_bundle = submod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rtl_buddy_view", pkg)
    monkeypatch.setitem(sys.modules, "rtl_buddy_view.viewer_bundle", submod)
    return submod


def test_returns_none_when_rtl_buddy_view_not_installed(monkeypatch):
    """Import fails (peer package absent) → None.

    Most CI environments installing only rtl-buddy will hit this path.
    The hub must not crash; it must fall through to the placeholder.
    """

    # Hide any real install so the import lookup actually fails.
    monkeypatch.setitem(sys.modules, "rtl_buddy_view", None)
    monkeypatch.setitem(sys.modules, "rtl_buddy_view.viewer_bundle", None)
    assert _discover_viewer_bundle() is None


def test_returns_none_when_package_reports_no_bundle(fake_viewer_pkg):
    """rtl-buddy-view installed but no bundle staged (e.g. running from
    a clean checkout without scripts/prebuild_viewer.py) → None."""

    fake_viewer_pkg.path = lambda: None  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() is None


def test_returns_bundle_path_when_package_ships_it(fake_viewer_pkg, tmp_path: Path):
    """rtl-buddy-view returns a real bundle path → helper forwards it."""

    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html>shipped</html>")
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() == bundle


def test_swallows_unexpected_exception_from_peer(fake_viewer_pkg):
    """Defensive: if the peer package's path() raises (API drift, broken
    install, …) we return None rather than crashing the hub.

    Without this, a future rename in rtl-buddy-view would break every
    rtl-buddy install that has both packages.
    """

    def boom() -> Path:
        raise RuntimeError("simulated API drift")

    fake_viewer_pkg.path = boom  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() is None
