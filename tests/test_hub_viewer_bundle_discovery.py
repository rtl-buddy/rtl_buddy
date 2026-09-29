"""Tests for ``rtl_buddy.hub.loop._discover_viewer_bundle``.

rtl-buddy-view is a soft dependency: if it is missing or ``viewer_bundle.path()`` raises, discovery returns ``None`` and the hub serves its placeholder page. The tests inject a fake ``rtl_buddy_view`` module into ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from rtl_buddy import tool_manifest
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.hub.loop import _check_view_version, _discover_viewer_bundle


@pytest.fixture
def fake_viewer_pkg(monkeypatch):
    """Install a stub ``rtl_buddy_view`` package whose ``viewer_bundle.path()`` is configurable."""

    pkg = types.ModuleType("rtl_buddy_view")
    submod = types.ModuleType("rtl_buddy_view.viewer_bundle")

    # Default to no bundle; tests override.
    submod.path = lambda: None  # type: ignore[attr-defined]
    pkg.viewer_bundle = submod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rtl_buddy_view", pkg)
    monkeypatch.setitem(sys.modules, "rtl_buddy_view.viewer_bundle", submod)
    return submod


def test_returns_none_when_rtl_buddy_view_not_installed(monkeypatch):
    """A failed import of rtl_buddy_view returns None."""

    # Hide any real install so the import fails.
    monkeypatch.setitem(sys.modules, "rtl_buddy_view", None)
    monkeypatch.setitem(sys.modules, "rtl_buddy_view.viewer_bundle", None)
    assert _discover_viewer_bundle() is None


def test_returns_none_when_package_reports_no_bundle(fake_viewer_pkg):
    """An installed rtl-buddy-view with no staged bundle returns None."""

    fake_viewer_pkg.path = lambda: None  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() is None


def test_returns_bundle_path_when_package_ships_it(fake_viewer_pkg, tmp_path: Path):
    """A real bundle path from rtl-buddy-view is returned."""

    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html>shipped</html>")
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() == bundle


def test_swallows_unexpected_exception_from_peer(fake_viewer_pkg):
    """An exception from the peer's path() returns None instead of crashing the hub."""

    def boom() -> Path:
        raise RuntimeError("simulated API drift")

    fake_viewer_pkg.path = boom  # type: ignore[attr-defined]
    assert _discover_viewer_bundle() is None


# In-env version floor (_check_view_version). It mirrors runner.mut_runner._check_xeno_version and probes both dist names, rtl-buddy-view and rtl-buddy-sch. The tests fake `importlib.metadata.version` so they check probe order.


def _fake_installed(monkeypatch, installed: dict[str, str]) -> None:
    """Make the viewer's dist probe see exactly ``installed``.

    The patch is process-wide because ``tool_manifest.importlib_metadata`` is the stdlib module; only the viewer's dist names are answered from the fixture and other lookups use the real function.
    """
    real_version = tool_manifest.importlib_metadata.version

    def _version(name: str) -> str:
        if name in installed:
            return installed[name]
        if name in tool_manifest.VIEWER_DIST_NAMES:
            raise PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(tool_manifest.importlib_metadata, "version", _version)


def test_check_view_version_skips_when_neither_dist_installed(monkeypatch):
    """No distribution metadata under either name skips the check."""
    _fake_installed(monkeypatch, {})
    # Must not raise.
    _check_view_version()


def test_check_view_version_reads_the_renamed_dist(monkeypatch):
    """A `rtl-buddy-sch`-only install is read and clears the floor."""
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-sch", "0.7.0")
    _check_view_version()


def test_check_view_version_falls_back_to_the_old_dist(monkeypatch):
    """A `rtl-buddy-view`-only install is still read."""
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.5.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-view", "0.5.0")
    _check_view_version()


def test_check_view_version_prefers_the_renamed_dist(monkeypatch):
    """With both installed, the `rtl-buddy-sch` version is used, not the stale `rtl-buddy-view` metadata."""
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0", "rtl-buddy-view": "0.2.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-sch", "0.7.0")
    _check_view_version()


def test_check_view_version_passes_at_floor(monkeypatch):
    """Exactly the floor, including dev/rc suffixes, is accepted."""
    for version in ("0.3.0", "0.3.0.dev3+g0f37a43", "1.0.0"):
        _fake_installed(monkeypatch, {"rtl-buddy-view": version})
        _check_view_version()


def test_check_view_version_raises_when_too_old(monkeypatch):
    """Below the floor raises FatalRtlBuddyError naming the floor and an upgrade hint for `rtl-buddy-sch`, quoting the dist actually read."""
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.2.3"})
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-sch >= 0\.3\.0"):
        _check_view_version()
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-view 0\.2\.3"):
        _check_view_version()


def test_discover_bundle_enforces_floor(fake_viewer_pkg, monkeypatch, tmp_path: Path):
    """A too-old in-env view fails bundle discovery instead of returning None."""
    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.2.0"})
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-sch >= 0\.3\.0"):
        _discover_viewer_bundle()


def test_discover_bundle_accepts_the_renamed_dist(
    fake_viewer_pkg, monkeypatch, tmp_path: Path
):
    """A `rtl-buddy-sch`-only install serves the bundle."""
    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0"})
    assert _discover_viewer_bundle() == bundle
