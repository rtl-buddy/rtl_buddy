"""Tests for verible executable resolution in ``config/verible.py``.

Covers the PATH fallback that lets a site expose verible via ``module load``
/ an env script without editing the committed ``cfg-verible.path``:

- ``get_exe_path`` prefers ``<path>/<exe>`` when that file exists.
- ``get_exe_path`` falls back to PATH when the configured dir lacks the exe.
- ``get_exe_path`` returns the configured join as a last resort (so a genuine
  "not found" still points at the expected location).
- ``initialise`` marks the config available when the dir is absent but
  verible is on PATH.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from rtl_buddy.config.verible import VeribleConfig, VeribleConfigFile


def _make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def test_get_exe_path_prefers_configured_dir(tmp_path):
    bindir = tmp_path / "bin"
    exe = _make_exe(bindir, "verible-verilog-syntax")
    cfg = VeribleConfig("v", str(bindir), {}, True)
    assert cfg.get_exe_path("verible-verilog-syntax") == str(exe)


def test_get_exe_path_falls_back_to_path(tmp_path, monkeypatch):
    # configured dir exists but does NOT contain the exe; PATH does.
    cfgdir = tmp_path / "cfgdir"
    cfgdir.mkdir()
    pathdir = tmp_path / "pathdir"
    exe = _make_exe(pathdir, "verible-verilog-syntax")
    monkeypatch.setenv("PATH", str(pathdir), prepend=False)

    cfg = VeribleConfig("v", str(cfgdir), {}, True)
    assert cfg.get_exe_path("verible-verilog-syntax") == str(exe)


def test_get_exe_path_last_resort_is_configured_join(tmp_path, monkeypatch):
    # neither the configured dir nor PATH has the exe -> configured join.
    cfgdir = tmp_path / "cfgdir"
    cfgdir.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cfg = VeribleConfig("v", str(cfgdir), {}, True)
    assert cfg.get_exe_path("verible-verilog-syntax") == os.path.join(
        str(cfgdir), "verible-verilog-syntax"
    )


def test_initialise_available_via_configured_dir(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    root_cfg = tmp_path / "root_config.yaml"
    root_cfg.write_text("")
    cfg = VeribleConfigFile("v", str(bindir), {}).initialise(str(root_cfg))
    assert cfg.available is True


def test_initialise_available_via_path(tmp_path, monkeypatch):
    # configured dir is absent, but verible is on PATH -> still available.
    pathdir = tmp_path / "pathdir"
    _make_exe(pathdir, "verible-verilog-syntax")
    monkeypatch.setenv("PATH", str(pathdir))
    root_cfg = tmp_path / "root_config.yaml"
    root_cfg.write_text("")
    cfg = VeribleConfigFile("v", "/nonexistent/verible/bin", {}).initialise(
        str(root_cfg)
    )
    assert cfg.available is True


def test_initialise_unavailable_when_missing_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    root_cfg = tmp_path / "root_config.yaml"
    root_cfg.write_text("")
    cfg = VeribleConfigFile("v", "/nonexistent/verible/bin", {}).initialise(
        str(root_cfg)
    )
    assert cfg.available is False
