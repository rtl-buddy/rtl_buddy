"""Unit tests for :mod:`rtl_buddy.tool_manifest` and ``rb tool-check``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from rtl_buddy import tool_manifest as tm


# ---------------------------------------------------------------------------
# Version helpers


def test_version_tuple_extracts_integers():
    assert tm._version_tuple("v0.0-3724") == (0, 0, 3724)
    assert tm._version_tuple("Yosys 0.40") == (0, 40)
    assert tm._version_tuple("5.048") == (5, 48)
    assert tm._version_tuple("no digits") == ()


def test_version_satisfies():
    # No minimum → always satisfies.
    assert tm._version_satisfies("anything", None) is True
    # Minimum but no detected version → cannot prove → outdated.
    assert tm._version_satisfies(None, "1.0") is False
    # Same / greater / lesser
    assert tm._version_satisfies("v0.0-3724", "v0.0-3724") is True
    assert tm._version_satisfies("v0.0-3800", "v0.0-3724") is True
    assert tm._version_satisfies("v0.0-3600", "v0.0-3724") is False
    # Non-digit minimum → bail out as satisfied (we can't compare).
    assert tm._version_satisfies("1.0", "anything") is True


# ---------------------------------------------------------------------------
# Detectors


@pytest.fixture
def fake_bin(tmp_path: Path) -> Iterator[Path]:
    """Drop an executable on PATH for the duration of the test."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
    try:
        yield bindir
    finally:
        os.environ["PATH"] = old_path


def _make_exe(path: Path, body: str = "#!/bin/sh\necho stub\n") -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_path_detector_hits_on_path(fake_bin: Path):
    _make_exe(fake_bin / "stub-tool")
    spec = tm.ToolSpec(
        name="stub",
        binaries=("stub-tool",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    result = tm.detect_tool(spec)
    assert result.found is True
    assert result.path is not None
    assert result.path.endswith("stub-tool")


def test_path_detector_misses_when_absent():
    spec = tm.ToolSpec(
        name="never",
        binaries=("definitely-not-a-real-binary-xyz",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    assert tm.detect_tool(spec).found is False


def test_vendor_detector_hits(tmp_path: Path):
    vendor_bin = tmp_path / "vendor" / "stub" / "bin"
    vendor_bin.mkdir(parents=True)
    _make_exe(vendor_bin / "stub-tool")
    spec = tm.ToolSpec(
        name="stub",
        binaries=("stub-tool",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.VendorDetector(rel_path="vendor/stub/bin"),),
    )
    result = tm.detect_tool(spec, project_root=tmp_path)
    assert result.found is True
    assert result.kind == "vendor"


def test_absolute_path_detector_hits(tmp_path: Path):
    target_dir = tmp_path / "vbn" / "bin"
    target_dir.mkdir(parents=True)
    _make_exe(target_dir / "verible-verilog-syntax")
    spec = tm.ToolSpec(
        name="verible",
        binaries=("verible-verilog-syntax",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.AbsolutePathDetector(abs_path=str(target_dir)),),
    )
    result = tm.detect_tool(spec)
    assert result.found is True
    assert result.kind == "vendor"
    assert "verible-verilog-syntax" in (result.path or "")


def test_python_package_detector_hits_on_pytest():
    # pytest is a hard dev dependency — guaranteed present here.
    spec = tm.ToolSpec(
        name="pytest",
        binaries=("pytest",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PythonPackageDetector("pytest"),),
    )
    result = tm.detect_tool(spec)
    assert result.found is True
    assert result.kind == "python"
    assert result.version  # importlib.metadata always returns something


def test_python_sibling_detector_returns_both_version_and_path(fake_bin: Path):
    """When a python sibling is installed AND on PATH, show both.

    Uses ``pytest`` as the test subject — its package is installed (it's
    running this test) and its script entry-point is on PATH. We add the
    fake_bin shim only to confirm the detector's binary-lookup path runs.
    """
    spec = tm.ToolSpec(
        name="pytest",
        binaries=("pytest",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PythonSiblingDetector("pytest"),),
    )
    result = tm.detect_tool(spec)
    assert result.found is True
    # Version comes from importlib.metadata.
    assert result.version
    # Path comes from shutil.which — pytest installs a console entry-point.
    assert result.path
    assert result.path.endswith("pytest")
    # kind is "path" when the binary is on PATH (so the table shows the
    # absolute path instead of "(python)").
    assert result.kind == "path"


def test_python_sibling_detector_misses_when_neither_present(tmp_path: Path):
    spec = tm.ToolSpec(
        name="fake",
        binaries=("nonexistent-cmd-zzz",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PythonSiblingDetector("nonexistent-package-zzz"),),
    )
    assert tm.detect_tool(spec).found is False


def test_python_package_detector_misses_on_unknown():
    spec = tm.ToolSpec(
        name="fake",
        binaries=("fake",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PythonPackageDetector("definitely-not-installed-xyz-pkg"),),
    )
    assert tm.detect_tool(spec).found is False


# ---------------------------------------------------------------------------
# Version probe


def test_probe_version_parses_output(fake_bin: Path):
    bin_path = _make_exe(
        fake_bin / "stubver",
        body="#!/bin/sh\necho 'stubver v0.0-3724'\n",
    )
    spec = tm.ToolSpec(
        name="stub",
        binaries=("stubver",),
        version_cmd=("stubver", "--version"),
        version_regex=r"v\d+\.\d+-\d+",
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    version = tm.probe_version(spec, str(bin_path), cache={})
    assert version == "v0.0-3724"


def test_probe_version_uses_capture_group_when_present(fake_bin: Path):
    bin_path = _make_exe(
        fake_bin / "stubver2",
        body="#!/bin/sh\necho 'Yosys 0.40 stable'\n",
    )
    spec = tm.ToolSpec(
        name="stub",
        binaries=("stubver2",),
        version_cmd=("stubver2", "-V"),
        version_regex=r"Yosys\s+([\d.]+)",
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    assert tm.probe_version(spec, str(bin_path), cache={}) == "0.40"


def test_probe_version_returns_none_when_unparsable(fake_bin: Path):
    bin_path = _make_exe(
        fake_bin / "noversion",
        body="#!/bin/sh\necho 'no clue what version'\n",
    )
    spec = tm.ToolSpec(
        name="stub",
        binaries=("noversion",),
        version_cmd=("noversion", "--version"),
        version_regex=r"v\d+\.\d+-\d+",
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    assert tm.probe_version(spec, str(bin_path), cache={}) is None


def test_probe_version_cache_hit(fake_bin: Path):
    bin_path = _make_exe(
        fake_bin / "cachedver",
        body="#!/bin/sh\necho 'IGNORE THIS' >&2\nexit 0\n",
    )
    spec = tm.ToolSpec(
        name="stub",
        binaries=("cachedver",),
        version_cmd=("cachedver", "--version"),
        version_regex=r"(\d+\.\d+)",
        minimum_version=None,
        detection=(tm.PathDetector(),),
    )
    mtime = int(os.path.getmtime(bin_path))
    cache = {
        f"{bin_path}@{mtime}": {
            "regex": spec.version_regex,
            "version": "1.2",
        }
    }
    # Cache key matches → returns cached value WITHOUT executing the script
    # (the script would yield None since stdout/stderr have no digits).
    assert tm.probe_version(spec, str(bin_path), cache=cache) == "1.2"


# ---------------------------------------------------------------------------
# check_tool / check_all / subcommand_readiness


def test_check_tool_ok(fake_bin: Path):
    _make_exe(
        fake_bin / "okt",
        body="#!/bin/sh\necho 'okt 2.0'\n",
    )
    spec = tm.ToolSpec(
        name="okt",
        binaries=("okt",),
        version_cmd=("okt", "--version"),
        version_regex=r"okt\s+([\d.]+)",
        minimum_version="1.0",
        detection=(tm.PathDetector(),),
    )
    status = tm.check_tool(spec, probe_versions=True, cache={})
    assert status.status == "ok"
    assert status.version == "2.0"


def test_check_tool_outdated(fake_bin: Path):
    _make_exe(
        fake_bin / "oldt",
        body="#!/bin/sh\necho 'oldt 1.0'\n",
    )
    spec = tm.ToolSpec(
        name="oldt",
        binaries=("oldt",),
        version_cmd=("oldt", "--version"),
        version_regex=r"oldt\s+([\d.]+)",
        minimum_version="9.0",
        detection=(tm.PathDetector(),),
    )
    status = tm.check_tool(spec, probe_versions=True, cache={})
    assert status.status == "outdated"
    assert status.version == "1.0"
    assert status.minimum_version == "9.0"


def test_check_tool_missing():
    spec = tm.ToolSpec(
        name="ghost",
        binaries=("ghost-binary-that-doesnt-exist",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
        optional=True,
    )
    status = tm.check_tool(spec)
    assert status.status == "missing"
    assert status.path is None


def test_subcommand_readiness_aggregates():
    spec_required = tm.ToolSpec(
        name="missing-required",
        binaries=("never",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
        used_by=("test", "hier"),
        optional=False,
    )
    spec_optional = tm.ToolSpec(
        name="missing-optional",
        binaries=("never2",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
        used_by=("test",),
        optional=True,
    )
    specs = [spec_required, spec_optional]
    statuses = tm.check_all(specs, probe_versions=False)
    readiness = tm.subcommand_readiness(statuses, specs)
    assert readiness["test"]["status"] == "missing"
    assert "missing-required" in readiness["test"]["missing"]
    # Optional misses do not flip status.
    assert "missing-optional" not in readiness["test"]["missing"]
    # hier only depends on the required tool — also missing.
    assert readiness["hier"]["status"] == "missing"


# ---------------------------------------------------------------------------
# Manifest reconciliation with root_config.yaml


def _write_minimal_root_config(target: Path, *, extra: str = "") -> None:
    """Drop a usable root_config.yaml + regression.yaml at ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "root_config.yaml").write_text(
        """\
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "test-host"
    unames: ["Darwin", "Linux"]
    builder: "stub"
    verible: "stub-verible"

cfg-rtl-builder:
  - name: "stub"
    builder: "echo"
    builder-simv: "obj_dir/simv"
    sim-rand-seed: 1
    sim-rand-seed-prefix: "+seed="
    builder-opts:
      debug:
        compile-time: "--no-op"
        run-time: "--no-op"

cfg-verible:
  - name: "stub-verible"
    path: "/usr/bin"
    extra_args: {}

cfg-rtl-reg:
  reg-cfg-path: "regression.yaml"
"""
        + extra
    )
    (target / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs: []\n"
    )


def test_root_cfg_tools_min_version_overrides_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_minimal_root_config(
        tmp_path,
        extra=(
            "\ncfg-tools:\n"
            "  - name: verible\n"
            '    min-version: "v9.9-9999"\n'
            "  - name: yosys\n"
            '    min-version: "99.0"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="test")
    specs = tm.get_manifest(rc)
    by_name = {s.name: s for s in specs}
    assert by_name["verible"].minimum_version == "v9.9-9999"
    assert by_name["yosys"].minimum_version == "99.0"
    # Unaffected tools keep their manifest default (None for now).
    assert by_name["surfer"].minimum_version is None


def test_fpv_solvers_present_in_manifest():
    """Every solver tracked by fpv_solver_pin must have a manifest entry.

    Catches drift between the runtime pin probe and the tool-check view —
    adding a solver in one place must add it in both.
    """
    from rtl_buddy.tools.fpv_solver_pin import _PROBES

    names = {s.name for s in tm.get_manifest()}
    for solver in _PROBES:
        assert solver in names, f"FPV solver '{solver}' missing from manifest"


def test_fpv_solver_pin_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """cfg-fpv-tools.opts.solver-versions must surface as minimum_version."""
    _write_minimal_root_config(
        tmp_path,
        extra=(
            "\ncfg-fpv-tools:\n"
            '  - name: "sby"\n'
            '    tool: "sby"\n'
            "    opts:\n"
            "      solver-versions:\n"
            '        yices: "99.0.0"\n'
            '        z3: "99.0"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="test")
    specs = tm.get_manifest(rc)
    by_name = {s.name: s for s in specs}
    assert by_name["yices"].minimum_version == "99.0.0"
    assert by_name["z3"].minimum_version == "99.0"
    # Unpinned solvers keep their default (None).
    assert by_name["boolector"].minimum_version is None


def test_root_cfg_unknown_pin_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_minimal_root_config(
        tmp_path,
        extra=('\ncfg-tools:\n  - name: not-a-real-tool\n    min-version: "99.0"\n'),
    )
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="test")
    # Should not raise — unknown pins are logged at DEBUG and skipped.
    specs = tm.get_manifest(rc)
    assert any(s.name == "verible" for s in specs)


# ---------------------------------------------------------------------------
# CLI integration


def _run_rb(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "rtl_buddy", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)


def test_cli_tool_check_runs_outside_project(tmp_path: Path):
    """tool-check must not require a root_config.yaml."""
    result = _run_rb("tool-check", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 0
    assert "Tools (" in result.stdout
    assert "Subcommand readiness" in result.stdout


def test_cli_tool_check_json(tmp_path: Path):
    result = _run_rb(
        "tool-check", "--format", "json", "--no-probe-versions", cwd=tmp_path
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "tools" in payload and "subcommands" in payload
    assert "exit_code" in payload
    # Manifest is non-empty.
    assert len(payload["tools"]) > 0


def test_cli_tool_check_explain(tmp_path: Path):
    result = _run_rb("tool-check", "--explain", "verible", cwd=tmp_path)
    assert result.returncode == 0
    assert "verible" in result.stdout
    assert "Install" in result.stdout


def test_cli_tool_check_explain_unknown_exits_1(tmp_path: Path):
    result = _run_rb("tool-check", "--explain", "does-not-exist", cwd=tmp_path)
    assert result.returncode == 1


def test_cli_tool_check_required_for_present(tmp_path: Path):
    # `tool-check` itself has no deps in the manifest; pick a sub that
    # depends only on a tool we are confident is installed (pytest is
    # always present here).
    # Use the manifest to find a likely-satisfied subcommand: pick the
    # first one whose required tools are all present.
    statuses = tm.check_all(
        tm.get_manifest(), probe_versions=False, include_optional=True
    )
    readiness = tm.subcommand_readiness(statuses, tm.get_manifest())
    ok_sub = next(
        (sub for sub, info in readiness.items() if info["status"] == "ok"),
        None,
    )
    if ok_sub is None:
        pytest.skip("no subcommand has all required tools installed")

    result = _run_rb("tool-check", "--required-for", ok_sub, cwd=tmp_path)
    assert result.returncode == 0


def test_cli_tool_check_required_for_missing_exits_2(tmp_path: Path):
    """--required-for must exit 2 if any required tool is missing."""
    # axi-profile depends on rtl-buddy-axi-profiler, which we don't install
    # in the rtl_buddy CI venv. If for some reason it *is* present, skip.
    if shutil.which("axi-profiler") is not None:
        pytest.skip("axi-profiler is installed; cannot exercise miss path")
    try:
        from importlib import metadata as md

        md.version("rtl-buddy-axi-profiler")
        pytest.skip("rtl-buddy-axi-profiler is installed; cannot exercise miss path")
    except md.PackageNotFoundError:
        pass

    result = _run_rb("tool-check", "--required-for", "axi-profile", cwd=tmp_path)
    assert result.returncode == 2


def test_cli_tool_check_strict_exits_1_on_miss(tmp_path: Path):
    """--strict must exit 1 if any required tool is missing."""
    # As above — axi-profiler is the most reliably missing tool in CI.
    if shutil.which("axi-profiler") is not None:
        pytest.skip("axi-profiler is installed; cannot exercise miss path")
    try:
        from importlib import metadata as md

        md.version("rtl-buddy-axi-profiler")
        pytest.skip("rtl-buddy-axi-profiler is installed; cannot exercise miss path")
    except md.PackageNotFoundError:
        pass

    result = _run_rb("tool-check", "--strict", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 1


def test_cli_tool_check_default_exit_is_0(tmp_path: Path):
    """Default behavior: no --strict, no --required-for → exit 0 always."""
    result = _run_rb("tool-check", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 0
