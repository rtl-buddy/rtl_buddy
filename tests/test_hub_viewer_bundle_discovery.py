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
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from rtl_buddy import tool_manifest
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.hub.loop import _check_view_version, _discover_viewer_bundle


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


# ---------------------------------------------------------------------------
# In-env version floor (_check_view_version)
#
# rtl_buddy declares no viewer pin, so this runtime guard is the
# only floor for the in-process SPA-bundle path. It mirrors
# runner.mut_runner._check_xeno_version: read the installed dist version,
# skip when there's no metadata, raise a friendly hint when too old.
#
# The dist was renamed rtl-buddy-view -> rtl-buddy-sch at 0.7.0
# (rtl-buddy-sch#157), so the lookup probes both names. These tests fake
# the environment at `importlib.metadata.version` — the layer both names
# funnel through — so what is under test is the probe ORDER, not a stub's
# idea of it.


def _fake_installed(monkeypatch, installed: dict[str, str]) -> None:
    """Make the viewer's dist probe see exactly ``installed``.

    ``tool_manifest.importlib_metadata`` *is* the stdlib module, so the
    patch is process-wide; only the viewer's own names are answered from
    the fixture and every other lookup defers to the real function, so
    an unrelated ``version("rtl-buddy")`` elsewhere in the call graph
    keeps working.
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
    """No distribution metadata under either name → skip.

    A PATH-only or source install has no dist to read; there a
    successful import stands in for the floor.
    """
    _fake_installed(monkeypatch, {})
    # Must not raise.
    _check_view_version()


def test_check_view_version_reads_the_renamed_dist(monkeypatch):
    """New dist only (`rtl-buddy-sch`, the post-rename name) → read it.

    Every rtl-buddy-sch release is >= 0.7.0, so this clears the floor;
    the point of the test is that it is FOUND at all, which is what the
    old single-name lookup got wrong.
    """
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-sch", "0.7.0")
    _check_view_version()


def test_check_view_version_falls_back_to_the_old_dist(monkeypatch):
    """Old dist only (`rtl-buddy-view`, frozen at 0.5.0) → still read."""
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.5.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-view", "0.5.0")
    _check_view_version()


def test_check_view_version_prefers_the_renamed_dist(monkeypatch):
    """Both installed → the renamed dist's version is the answer.

    Someone who installed the new dist over the old one (pip leaves the
    frozen 0.5.0 metadata behind) must not be judged by the stale one.
    """
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0", "rtl-buddy-view": "0.2.0"})
    assert tool_manifest.viewer_dist_version() == ("rtl-buddy-sch", "0.7.0")
    _check_view_version()


def test_check_view_version_passes_at_floor(monkeypatch):
    """Exactly the floor (and dev/rc suffixes of it) is accepted."""
    for version in ("0.3.0", "0.3.0.dev3+g0f37a43", "1.0.0"):
        _fake_installed(monkeypatch, {"rtl-buddy-view": version})
        _check_view_version()


def test_check_view_version_raises_when_too_old(monkeypatch):
    """Below the floor → FatalRtlBuddyError naming the floor + upgrade hint.

    The hint names the CURRENT dist (`rtl-buddy-sch`) whichever dist was
    found, because that is what an upgrade has to install; the dist that
    was actually read is quoted alongside its version.
    """
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.2.3"})
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-sch >= 0\.3\.0"):
        _check_view_version()
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-view 0\.2\.3"):
        _check_view_version()


def test_discover_bundle_enforces_floor(fake_viewer_pkg, monkeypatch, tmp_path: Path):
    """A too-old in-env view fails the bundle discovery, not silently None.

    This is the integration the guard exists for: the import succeeds (an
    old editable install is present) so the soft-dep ImportError branch
    does not catch it — the version floor must.
    """
    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    _fake_installed(monkeypatch, {"rtl-buddy-view": "0.2.0"})
    with pytest.raises(FatalRtlBuddyError, match=r"rtl-buddy-sch >= 0\.3\.0"):
        _discover_viewer_bundle()


def test_discover_bundle_accepts_the_renamed_dist(
    fake_viewer_pkg, monkeypatch, tmp_path: Path
):
    """New-dist-only install serves the bundle instead of erroring.

    Before the dual probe this environment read as "no metadata" and got
    the skip branch by accident; now it is read, compared and passed.
    """
    bundle = tmp_path / "_viewer_bundle"
    bundle.mkdir()
    fake_viewer_pkg.path = lambda: bundle  # type: ignore[attr-defined]
    _fake_installed(monkeypatch, {"rtl-buddy-sch": "0.7.0"})
    assert _discover_viewer_bundle() == bundle
