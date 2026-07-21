"""Tests for #335 — structured dependency error when genhtml is unavailable.

``VlogCov._require_lcov()`` calls ``tool_manifest.require("lcov", root_cfg)``
before every genhtml/HTML coverage path. ``require()`` raises
``FatalRtlBuddyError`` pointing at ``rb tool-check --explain lcov`` when the
``genhtml`` binary (manifest tool name ``lcov``) cannot be found.
"""

from __future__ import annotations

import shutil

import pytest

from rtl_buddy import tool_manifest as tm
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import vlog_cov


def test_require_raises_when_genhtml_missing(monkeypatch: pytest.MonkeyPatch):
    """``tool_manifest.require("lcov", ...)`` raises with install guidance."""
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "genhtml":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(tm.shutil, "which", fake_which)

    with pytest.raises(FatalRtlBuddyError) as exc_info:
        tm.require("lcov", None)

    assert "tool-check --explain lcov" in str(exc_info.value)


def test_vlog_cov_require_lcov_delegates_to_tool_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    """``VlogCov._require_lcov`` must call ``tool_manifest.require("lcov", root_cfg)``."""
    calls = []

    def spy(name, root_cfg=None):
        calls.append((name, root_cfg))

    monkeypatch.setattr(vlog_cov.tm, "require", spy)

    root_cfg = object()
    vc = vlog_cov.VlogCov("verilator", root_cfg=root_cfg)
    vc._require_lcov()

    assert calls == [("lcov", root_cfg)]


def test_vlog_cov_require_lcov_propagates_fatal_error(monkeypatch: pytest.MonkeyPatch):
    """If genhtml is missing, ``_require_lcov`` propagates the FatalRtlBuddyError."""
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "genhtml":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(tm.shutil, "which", fake_which)

    vc = vlog_cov.VlogCov("verilator", root_cfg=None)
    with pytest.raises(FatalRtlBuddyError) as exc_info:
        vc._require_lcov()

    assert "lcov" in str(exc_info.value)
