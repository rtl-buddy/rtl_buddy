"""Unit tests for :mod:`rtl_buddy.tool_manifest` and ``rb tool-check``."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from rtl_buddy import tool_manifest as tm
from rtl_buddy.errors import FatalRtlBuddyError


# Version helpers


def test_version_tuple_extracts_integers():
    assert tm._version_tuple("v0.0-3724") == (0, 0, 3724)
    assert tm._version_tuple("Yosys 0.40") == (0, 40)
    assert tm._version_tuple("5.048") == (5, 48)
    assert tm._version_tuple("no digits") == ()


def test_version_satisfies():
    # No minimum: always satisfied.
    assert tm._version_satisfies("anything", None) is True
    # A minimum without a detected version is outdated.
    assert tm._version_satisfies(None, "1.0") is False
    # Same, greater, lesser
    assert tm._version_satisfies("v0.0-3724", "v0.0-3724") is True
    assert tm._version_satisfies("v0.0-3800", "v0.0-3724") is True
    assert tm._version_satisfies("v0.0-3600", "v0.0-3724") is False
    # A non-digit minimum cannot be compared and counts as satisfied.
    assert tm._version_satisfies("1.0", "anything") is True


def test_version_below():
    assert tm._version_below("99", None) is True
    assert tm._version_below(None, "12") is True
    assert tm._version_below("11.9.1", "12") is True
    assert tm._version_below("12", "12") is False
    assert tm._version_below("12.0.0", "12") is False
    assert tm._version_below("13.1", "12") is False
    assert tm._version_below("1.0", "anything") is True


def test_check_tool_reports_unsupported_above_maximum(monkeypatch):
    spec = tm.resolve_spec(tm.get_manifest(), "pyslang")
    assert spec is not None
    assert spec.minimum_version == "10.0.0"
    assert spec.maximum_version_exclusive == "12"

    def fake_version(package: str) -> str:
        return fake_version.value

    monkeypatch.setattr(tm.importlib_metadata, "version", fake_version)
    for value, expected in (
        ("9.9", "outdated"),
        ("10.0.0", "ok"),
        ("11.3.0", "ok"),
        ("12.0.0", "unsupported"),
        ("13.0", "unsupported"),
    ):
        fake_version.value = value
        status = tm.check_tool(spec)
        assert status.status == expected, value
        assert status.maximum_version_exclusive == "12"

    fake_version.value = "12.0.0"
    with pytest.raises(FatalRtlBuddyError, match="not supported"):
        tm.require("pyslang", None)
    statuses = [tm.check_tool(spec)]
    readiness = tm.subcommand_readiness(statuses, [spec])
    assert readiness["elab"]["status"] == "unsupported"
    assert readiness["elab"]["unsupported"] == ["pyslang"]
    assert (
        tm.compute_exit_code(statuses, required_for="elab", subcommands=readiness) == 2
    )
    assert "(need < 12)" in tm.render_text(statuses, readiness)
    payload = tm.build_json_payload(statuses, readiness)
    assert payload["tools"]["pyslang"]["maximum_version_exclusive"] == "12"
    assert payload["subcommands"]["elab"]["unsupported"] == ["pyslang"]


# Detectors


@pytest.fixture
def fake_bin(tmp_path: Path) -> Iterator[Path]:
    """Put an executable on PATH for the duration of the test."""
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
    assert result.version


def test_python_sibling_detector_returns_both_version_and_path(fake_bin: Path):
    """A python sibling that is installed and on PATH reports both version and path.

    Uses ``pytest`` as the subject; the fake_bin shim only confirms the binary-lookup path runs.
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
    # Path comes from shutil.which.
    assert result.path
    assert result.path.endswith("pytest")
    # kind is "path" when the binary is on PATH, so the table shows the absolute path.
    assert result.kind == "path"


def test_python_sibling_detector_falls_back_to_a_legacy_dist_name():
    """A renamed dist is found under its legacy name too, and the current name is preferred when both exist."""
    spec = tm.ToolSpec(
        name="fake",
        binaries=("nonexistent-cmd-zzz",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(
            tm.PythonSiblingDetector(
                "nonexistent-package-zzz", legacy_packages=("pytest",)
            ),
        ),
    )
    result = tm.detect_tool(spec)
    assert result.found is True
    assert result.version
    assert result.kind == "python"

    # Both present: the current name wins, so a stale dist left by an upgrade cannot mask it.
    current_first = tm.ToolSpec(
        name="fake",
        binaries=("nonexistent-cmd-zzz",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PythonSiblingDetector("pytest", legacy_packages=("coverage",)),),
    )
    import importlib.metadata as md

    assert tm.detect_tool(current_first).version == md.version("pytest")


def test_legacy_dist_metadata_yields_to_the_executable_probe(fake_bin: Path):
    """A legacy-name version is dropped when the binary is on PATH.

    Leaving `version=None` makes `check_tool` fall through to `probe_version()`, which asks the executable; a stale legacy wheel would otherwise report a frozen version.
    """
    _make_exe(fake_bin / "stub-tool", body="#!/bin/sh\necho 'stub-tool 9.9.9'\n")
    spec = tm.ToolSpec(
        name="fake",
        binaries=("stub-tool",),
        version_cmd=("stub-tool", "--version"),
        version_regex=r"stub-tool\s+([\d.]+)",
        minimum_version=None,
        detection=(
            tm.PythonSiblingDetector(
                "nonexistent-package-zzz", legacy_packages=("pytest",)
            ),
        ),
    )
    detected = tm.detect_tool(spec)
    assert detected.found is True
    assert detected.kind == "path"
    assert detected.version is None
    assert tm.check_tool(spec, probe_versions=True, cache={}).version == "9.9.9"

    # The current name is authoritative and keeps its metadata version.
    current = tm._replace(
        spec, detection=(tm.PythonSiblingDetector("pytest"),), version_cmd=None
    )
    import importlib.metadata as md

    assert tm.detect_tool(current).version == md.version("pytest")


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
    # A matching cache key returns the cached value without running the script (which would yield None).
    assert tm.probe_version(spec, str(bin_path), cache=cache) == "1.2"


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
    # hier depends only on the required tool, which is also missing.
    assert readiness["hier"]["status"] == "missing"


# Manifest reconciliation with root_config.yaml


def _write_minimal_root_config(target: Path, *, extra: str = "") -> None:
    """Write a usable root_config.yaml and regression.yaml at ``target``."""
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
    # Unaffected tools keep their manifest default (None).
    assert by_name["surfer"].minimum_version is None


def test_axi_profile_vpd_converters_declared():
    """The VPD-conversion tools behind `rb axi-profile run` (`vpd2vcd`, `vcd2fst`) are declared."""
    by_name = {s.name: s for s in tm.get_manifest()}

    vpd2vcd = by_name["vpd2vcd"]
    assert vpd2vcd.optional
    assert "axi-profile" in vpd2vcd.used_by
    assert vpd2vcd.binaries == ("vpd2vcd",)

    gtkwave = by_name["gtkwave"]
    assert "vcd2fst" in gtkwave.binaries
    assert "axi-profile" in gtkwave.used_by


def test_icarus_simulator_declared():
    """Icarus (`iverilog` and `vvp`) is declared as a sim backend so a missing binary surfaces in `rb tool-check`."""
    by_name = {s.name: s for s in tm.get_manifest()}

    icarus = by_name["icarus"]
    assert icarus.binaries == ("iverilog", "vvp")
    # Opt-in backend: missing Icarus must not gate readiness for the default Verilator path.
    assert icarus.optional
    assert icarus.used_by == ("test", "randtest", "regression")


def test_slurm_gates_test_as_well_as_regression():
    """`rb test --dispatch slurm` needs the slurm client too.

    `used_by` must name every command that can dispatch, because `--required-for test` and `--explain slurm` are the gate agents check.
    """
    by_name = {s.name: s for s in tm.get_manifest()}

    slurm = by_name["slurm"]
    assert slurm.optional  # the default --dispatch local needs nothing
    assert set(slurm.used_by) == {
        "regression",
        "randtest",
        "test",
        "elab",
        "elab-regression",
    }
    assert "rb test --dispatch slurm" in slurm.notes


def test_slurm_explains_scontrol_as_an_optional_probe():
    """`rb tool-check --explain slurm` names scontrol and what it enables.

    scontrol supplies MaxArraySize for array chunking and the per-key release of the build job. It is optional: without it chunking stays off, and the explanation must name the config fallback.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    slurm = by_name["slurm"]

    # NOT in `binaries`: that tuple is any-of and feeds the version probe.
    assert "scontrol" not in slurm.binaries
    assert "scontrol" in slurm.optional_binaries

    text = tm.explain(slurm)
    assert "scontrol" in text
    assert "MaxArraySize" in text
    assert "cfg-dispatch.max-array-size" in text
    # Both ceilings are named: SchedulerParameters=max_array_tasks can be lower than MaxArraySize.
    assert "max_array_tasks" in text
    assert "cfg-dispatch.max-array-tasks" in text
    # Second role: the release is issued from the compute node running the build job, so scontrol must be on that PATH.
    assert "Dependency=" in text
    assert "compute node" in text
    # Still optional overall: sbatch is the version probe and the gate.
    assert slurm.version_cmd[0] == "sbatch"
    assert slurm.optional


def test_an_optional_binary_alone_does_not_make_a_tool_present(monkeypatch, tmp_path):
    """A host with `scontrol` but no `sbatch` cannot dispatch and must not read as present.

    Detection is any-of over `binaries`, and `probe_version` substitutes the found path into `version_cmd`, so listing scontrol there would report `ok`.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_exe(bindir / "scontrol")
    monkeypatch.setenv("PATH", str(bindir))

    by_name = {s.name: s for s in tm.get_manifest()}
    status = tm.check_tool(by_name["slurm"])
    assert status.status == "missing"
    assert status.path is None

    # ...while the real client on the same PATH is found as usual.
    _make_exe(bindir / "sbatch")
    assert tm.check_tool(by_name["slurm"], probe_versions=False).status == "ok"


def test_optional_binaries_are_listed_with_their_role_not_as_status():
    """--explain lists optional binaries with their role and does not let them read as the tool's status."""
    spec = tm.ToolSpec(
        name="stub",
        binaries=("stub-tool",),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(tm.PathDetector(),),
        optional_binaries={"stub-extra": "buys the extra thing"},
    )
    text = tm.explain(spec)
    assert "stub-extra: buys the extra thing" in text
    assert "not required" in text


def test_rtl_buddy_view_declares_floor_and_version_probe():
    """rtl-buddy-view has a 0.3.0 floor with no upper cap and a `--version` probe.

    The regex must extract X.Y.Z from `rtl-buddy-view 0.3.0`.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["rtl-buddy-view"]
    assert spec.minimum_version == "0.3.0"
    assert spec.version_cmd == ("rtl-buddy-view", "--version")
    assert spec.version_regex is not None
    m = re.search(spec.version_regex, "rtl-buddy-view 0.3.0")
    assert m is not None and m.group(1) == "0.3.0"
    # The tagless hatch-vcs dev build resolves to its base version.
    m = re.search(spec.version_regex, "rtl-buddy-view 0.2.2.dev0+g0f37a432d")
    assert m is not None and m.group(1) == "0.2.2"


def test_rtl_buddy_view_spec_probes_both_distribution_names():
    """The rtl-buddy-view spec probes dist metadata under both rtl-buddy-view and rtl-buddy-sch.

    The tool key, binary, version command and output literal stay rtl-buddy-view; only the metadata lookup and install hint use the new dist name.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["rtl-buddy-view"]
    assert spec.binaries == ("rtl-buddy-view",)
    assert spec.version_cmd == ("rtl-buddy-view", "--version")

    detector = spec.detection[0]
    assert isinstance(detector, tm.PythonSiblingDetector)
    assert detector.package == "rtl-buddy-sch"
    assert "rtl-buddy-view" in detector.legacy_packages
    assert tm.VIEWER_DIST_NAMES == ("rtl-buddy-sch", "rtl-buddy-view")

    # `rb tool-check --explain` must point at the dist that still gets releases.
    assert "rtl-buddy-sch" in spec.install_hint["any"]


def _alias_spec(name: str, aliases: tuple[str, ...] = ()) -> tm.ToolSpec:
    """Minimal spec for name, alias and collision tests."""
    return tm.ToolSpec(
        name=name,
        binaries=(name,),
        version_cmd=None,
        version_regex=None,
        minimum_version=None,
        detection=(),
        aliases=aliases,
    )


def test_viewer_spec_aliases_its_current_dist_name():
    """`rtl-buddy-sch` is accepted as an alias while `name` stays `rtl-buddy-view`, the executable and probe-literal contract."""
    by_name = {s.name: s for s in tm.get_manifest()}
    assert by_name["rtl-buddy-view"].aliases == ("rtl-buddy-sch",)
    assert "rtl-buddy-sch" not in by_name


def test_resolve_spec_matches_name_then_alias():
    specs = tm.get_manifest()
    canonical = tm.resolve_spec(specs, "rtl-buddy-view")
    aliased = tm.resolve_spec(specs, "rtl-buddy-sch")
    assert canonical is not None
    # Same spec object, reporting the canonical identity.
    assert aliased is canonical
    assert aliased.name == "rtl-buddy-view"
    assert tm.resolve_spec(specs, "does-not-exist") is None


def test_resolve_spec_prefers_a_canonical_name_over_an_alias():
    """A name outranks another spec's alias for the same string, independent of list order."""
    specs = [_alias_spec("beta", aliases=("alpha",)), _alias_spec("alpha")]
    assert tm.resolve_spec(specs, "alpha").name == "alpha"


def test_known_tool_names_annotates_aliases():
    rendered = tm.known_tool_names(tm.get_manifest())
    assert "rtl-buddy-view (alias: rtl-buddy-sch)" in rendered
    # Tools without aliases stay bare.
    assert "verible" in rendered
    assert tm.known_tool_names([_alias_spec("x", aliases=("y", "z"))]) == [
        "x (aliases: y, z)"
    ]


def test_manifest_build_rejects_an_alias_colliding_with_a_name(monkeypatch):
    """A name that shadows an alias is rejected at manifest build time."""
    monkeypatch.setattr(
        tm,
        "_builtin_manifest",
        lambda: [_alias_spec("alpha"), _alias_spec("beta", aliases=("alpha",))],
    )
    with pytest.raises(AssertionError, match="duplicate lookup key 'alpha'"):
        tm.get_manifest()


def test_manifest_build_rejects_two_specs_sharing_an_alias(monkeypatch):
    monkeypatch.setattr(
        tm,
        "_builtin_manifest",
        lambda: [
            _alias_spec("alpha", aliases=("shared",)),
            _alias_spec("beta", aliases=("shared",)),
        ],
    )
    with pytest.raises(AssertionError, match="duplicate lookup key 'shared'"):
        tm.get_manifest()


def test_manifest_build_rejects_a_duplicate_name_even_with_a_root_cfg(monkeypatch):
    """A duplicate name is rejected even with a root config.

    `_reconcile_with_root_cfg` rebuilds the list through ``{s.name: s}``, which would collapse a duplicate before a post-reconcile check.
    """
    monkeypatch.setattr(
        tm,
        "_builtin_manifest",
        lambda: [_alias_spec("alpha"), _alias_spec("alpha")],
    )
    with pytest.raises(AssertionError, match="duplicate lookup key 'alpha'"):
        tm.get_manifest(root_cfg=object())


def test_builtin_manifest_lookup_keys_are_unique():
    """The shipped manifest has unique lookup keys."""
    specs = tm.get_manifest()
    keys = [s.name for s in specs] + [a for s in specs for a in s.aliases]
    assert len(keys) == len(set(keys))


def test_require_resolves_the_alias_and_reports_the_canonical_name():
    """`require("rtl-buddy-sch")` reports the canonical name `rtl-buddy-view` whether or not the viewer is installed."""
    from rtl_buddy.errors import FatalRtlBuddyError

    try:
        status = tm.require("rtl-buddy-sch")
    except FatalRtlBuddyError as exc:
        message = str(exc)
        assert "unknown tool" not in message
        assert "rtl-buddy-view" in message
        assert "rtl-buddy-sch" not in message
    else:
        assert status.name == "rtl-buddy-view"


def test_require_still_rejects_an_unknown_name():
    from rtl_buddy.errors import FatalRtlBuddyError

    with pytest.raises(FatalRtlBuddyError, match="unknown tool 'does-not-exist'"):
        tm.require("does-not-exist")


def test_viewer_dist_version_probes_new_name_then_old(monkeypatch):
    """Probe order is rtl-buddy-sch, then rtl-buddy-view, then None."""
    installed: dict[str, str] = {}
    real_version = tm.importlib_metadata.version

    # The patch hits the stdlib module, so only the viewer's names are answered from the fixture; all others defer to the real lookup.
    def _version(name: str) -> str:
        if name in installed:
            return installed[name]
        if name in tm.VIEWER_DIST_NAMES:
            raise tm.importlib_metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(tm.importlib_metadata, "version", _version)

    assert tm.viewer_dist_version() is None

    installed["rtl-buddy-view"] = "0.5.0"
    assert tm.viewer_dist_version() == ("rtl-buddy-view", "0.5.0")

    installed["rtl-buddy-sch"] = "0.7.0"
    assert tm.viewer_dist_version() == ("rtl-buddy-sch", "0.7.0")


def test_rtl_buddy_view_outdated_below_floor(fake_bin: Path):
    """A view below the floor reports `outdated`; one at or above reports `ok`.

    Drives the manifest's real version_cmd, version_regex and floor through ``check_tool`` against a stub binary on PATH.
    """
    spec = next(s for s in tm.get_manifest() if s.name == "rtl-buddy-view")
    probe_spec = tm._replace(spec, detection=(tm.PathDetector(),))

    _make_exe(
        fake_bin / "rtl-buddy-view",
        body="#!/bin/sh\necho 'rtl-buddy-view 0.2.0'\n",
    )
    too_old = tm.check_tool(probe_spec, probe_versions=True, cache={})
    assert too_old.status == "outdated"
    assert too_old.version == "0.2.0"
    assert too_old.minimum_version == "0.3.0"

    _make_exe(
        fake_bin / "rtl-buddy-view",
        body="#!/bin/sh\necho 'rtl-buddy-view 0.3.0'\n",
    )
    at_floor = tm.check_tool(probe_spec, probe_versions=True, cache={})
    assert at_floor.status == "ok"
    assert at_floor.version == "0.3.0"


def test_vivado_spec_declared():
    """The `vivado` entry gates the optional `rb fpga` flow.

    The version regex must handle both `Vivado v2022.1.2 (64-bit)` and `Vivado v.2022.1.2 (lin64) ...`.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["vivado"]
    assert spec.optional
    assert spec.used_by == ("fpga",)
    assert spec.binaries == ("vivado",)
    assert spec.version_cmd == ("vivado", "-version")
    assert spec.install_hint  # --explain must offer install guidance
    assert spec.version_regex is not None
    m = re.search(spec.version_regex, "Vivado v2022.1.2 (64-bit)")
    assert m is not None and m.group(1) == "2022.1.2"
    m = re.search(
        spec.version_regex,
        "Vivado v.2022.1.2 (lin64) Build 3605665 Fri Aug  5 22:52:02 MDT 2022",
    )
    assert m is not None and m.group(1) == "2022.1.2"


def test_vivado_version_probe_via_stub(fake_bin: Path):
    """check_tool drives the real vivado spec against a stub binary."""
    spec = next(s for s in tm.get_manifest() if s.name == "vivado")
    _make_exe(
        fake_bin / "vivado",
        body=(
            "#!/bin/sh\n"
            "echo 'Vivado v2022.1.2 (64-bit)'\n"
            "echo 'SW Build 3605665 on Fri Aug  5 22:52:02 MDT 2022'\n"
        ),
    )
    status = tm.check_tool(spec, probe_versions=True, cache={})
    assert status.status == "ok"
    assert status.version == "2022.1.2"
    assert status.optional is True


def test_fpv_solvers_present_in_manifest():
    """Every solver tracked by fpv_solver_pin has a manifest entry."""
    from rtl_buddy.tools.fpv_solver_pin import _PROBES

    names = {s.name for s in tm.get_manifest()}
    for solver in _PROBES:
        assert solver in names, f"FPV solver '{solver}' missing from manifest"


def test_fpv_solver_pin_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """cfg-fpv-tools.opts.solver-versions surfaces as minimum_version."""
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
    # Must not raise; unknown pins are logged at DEBUG and skipped.
    specs = tm.get_manifest(rc)
    assert any(s.name == "verible" for s in specs)


# CLI integration


def _run_rb(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "rtl_buddy", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)


def test_cli_tool_check_runs_outside_project(tmp_path: Path):
    """tool-check does not require a root_config.yaml."""
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


def test_cli_tool_check_machine_emits_envelope(tmp_path: Path):
    """The global --machine flag yields a single JSON envelope on stdout.

    The first stdout byte must be `{`, not the human text table.
    """
    result = _run_rb("--machine", "tool-check", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.lstrip().startswith("{")
    env = json.loads(result.stdout)
    assert env["command"] == "tool-check"
    assert env["exit_code"] == 0
    payload = env["payload"]
    assert "tools" in payload and "subcommands" in payload
    # Bare tool-check exits 0 regardless of readiness; the verdict rides in the payload.
    assert "readiness_exit_code" in payload
    assert len(payload["tools"]) > 0


def test_cli_tool_check_machine_required_for_missing_exits_2(tmp_path: Path):
    """--machine with --required-for reports the gate verdict in the envelope."""
    if shutil.which("axi-profiler") is not None:
        pytest.skip("axi-profiler is installed; cannot exercise miss path")
    try:
        from importlib import metadata as md

        md.version("rtl-buddy-axi-profiler")
        pytest.skip("rtl-buddy-axi-profiler is installed; cannot exercise miss path")
    except md.PackageNotFoundError:
        pass

    result = _run_rb(
        "--machine", "tool-check", "--required-for", "axi-profile", cwd=tmp_path
    )
    assert result.returncode == 2
    env = json.loads(result.stdout)
    assert env["command"] == "tool-check"
    assert env["exit_code"] == 2
    assert env["payload"]["subcommands"]["axi-profile"]["status"] != "ok"


def test_cli_tool_check_machine_explain(tmp_path: Path):
    """--machine --explain wraps the per-tool view and install text in an envelope."""
    result = _run_rb("--machine", "tool-check", "--explain", "vivado", cwd=tmp_path)
    assert result.returncode == 0
    env = json.loads(result.stdout)
    assert env["command"] == "tool-check"
    assert "vivado" in env["payload"]["tools"]
    assert "rb fpga" in env["payload"]["instructions"]


def test_cli_tool_check_explain(tmp_path: Path):
    result = _run_rb("tool-check", "--explain", "verible", cwd=tmp_path)
    assert result.returncode == 0
    assert "verible" in result.stdout
    assert "Install" in result.stdout


def test_cli_tool_check_explain_vivado(tmp_path: Path):
    """`rb tool-check --explain vivado` reports the fpga gating entry."""
    result = _run_rb("tool-check", "--explain", "vivado", cwd=tmp_path)
    assert result.returncode == 0
    assert "vivado" in result.stdout
    assert "rb fpga" in result.stdout
    assert "Install" in result.stdout
    assert "Optional: yes" in result.stdout


def test_cli_tool_check_explain_unknown_exits_1(tmp_path: Path):
    result = _run_rb("tool-check", "--explain", "does-not-exist", cwd=tmp_path)
    assert result.returncode == 1


def test_cli_tool_check_explain_accepts_the_viewer_alias(tmp_path: Path):
    """--explain rtl-buddy-sch resolves and answers as rtl-buddy-view."""
    result = _run_rb(
        "tool-check", "--explain", "rtl-buddy-sch", "--no-probe-versions", cwd=tmp_path
    )
    assert result.returncode == 0
    assert "unknown tool" not in result.stderr
    assert result.stdout.startswith("rtl-buddy-view")


def test_cli_tool_check_machine_explain_alias_keeps_canonical_name(tmp_path: Path):
    """The alias is input only; the JSON key stays canonical."""
    result = _run_rb(
        "--machine",
        "tool-check",
        "--explain",
        "rtl-buddy-sch",
        "--no-probe-versions",
        cwd=tmp_path,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)["payload"]
    assert list(payload["tools"]) == ["rtl-buddy-view"]
    assert "rtl-buddy-sch" not in payload["tools"]


def test_cli_machine_tool_check_keeps_optional_binaries_out_of_the_payload(
    tmp_path: Path,
):
    """Optional binaries appear in the human explanation (mirrored in `instructions`) and never in the structured `tools` entry."""
    result = _run_rb(
        "--machine",
        "tool-check",
        "--explain",
        "slurm",
        "--no-probe-versions",
        cwd=tmp_path,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)["payload"]
    entry = payload["tools"]["slurm"]
    assert set(entry) <= {"status", "version", "path", "optional", "minimum_version"}
    assert "scontrol" not in json.dumps(payload["tools"])
    assert "scontrol" in payload["instructions"]


def test_cli_tool_check_explain_unknown_hint_surfaces_aliases(tmp_path: Path):
    """The rejection lists the accepted spellings."""
    result = _run_rb(
        "tool-check", "--explain", "does-not-exist", "--no-probe-versions", cwd=tmp_path
    )
    assert result.returncode == 1
    # The console word-wraps the hint, so compare on collapsed whitespace.
    hint = " ".join(result.stderr.split())
    assert "rtl-buddy-view (alias: rtl-buddy-sch)" in hint


def test_cli_tool_check_machine_explain_unknown_carries_aliases(tmp_path: Path):
    """The --machine rejection carries the alias mapping as an additive sibling; `known` stays bare canonical names."""
    result = _run_rb(
        "--machine",
        "tool-check",
        "--explain",
        "does-not-exist",
        "--no-probe-versions",
        cwd=tmp_path,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)["payload"]
    assert "rtl-buddy-view" in payload["known"]
    assert "rtl-buddy-sch" not in payload["known"]
    assert payload["aliases"]["rtl-buddy-view"] == ["rtl-buddy-sch"]


def test_cli_tool_check_required_for_present(tmp_path: Path):
    # Pick a subcommand whose required tools are all present (pytest is always installed here).
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
    """--required-for exits 2 if any required tool is missing."""
    # axi-profile needs rtl-buddy-axi-profiler, which the CI venv does not install; skip if it is present.
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
    """--strict exits 1 if any required tool is missing."""
    # axi-profiler is the most reliably missing tool in CI.
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
    """Without --strict or --required-for the exit code is always 0."""
    result = _run_rb("tool-check", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 0


def test_graph_extract_spec_is_optional_with_anchored_regex():
    """The graph-extract spec is optional with used_by graph, so its absence does not fail `rb tool-check --required-for graph`.

    Odd version formats yield no version rather than a wrong one.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["rtl-buddy-graph-extract"]
    assert spec.optional
    assert "graph" in spec.used_by
    assert spec.binaries == ("rb-graph-extract",)
    assert any(
        isinstance(d, tm.PythonPackageDetector)
        and d.package == "rtl-buddy-graph-extract"
        for d in spec.detection
    )
    m = re.search(spec.version_regex, "rb-graph-extract 0.1.0")
    assert m is not None and m.group(1) == "0.1.0"
    # Editable and git installs report PEP 440 dev+local versions; the full string must reach the fingerprint.
    m = re.search(spec.version_regex, "rb-graph-extract 0.1.dev1+g0d74f48e0")
    assert m is not None and m.group(1) == "0.1.dev1+g0d74f48e0"
    assert re.search(spec.version_regex, "rb-graph-extract (python 3.12) 0.2.0") is None
    # The floor mirrors the graph-extract extra's `>= 0.1.0`; a dev build of 0.1 must satisfy it.
    assert spec.minimum_version == "0.1.0"
    assert tm._version_satisfies("0.1.dev1+g0d74f48e0", spec.minimum_version)


def test_rtl_buddy_view_is_required_for_graph():
    """used_by for the design tier's exporter carries graph, so --required-for graph enforces it."""
    by_name = {s.name: s for s in tm.get_manifest()}
    assert "graph" in by_name["rtl-buddy-view"].used_by


def test_mcp_sdk_detects_via_python_package_with_floor():
    """The mcp SDK is detected through PythonPackage only, with a 1.2.0 floor and no binaries contract."""
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["mcp"]
    assert spec.optional
    assert spec.binaries == ()
    assert spec.minimum_version == "1.2.0"
    assert len(spec.detection) == 1
    assert isinstance(spec.detection[0], tm.PythonPackageDetector)
    assert "mcp" in spec.used_by


# Manifest reconciliation: cfg-platforms tool routing


_SURFER_ROUTING_BLOCKS = """
cfg-surfer:
  - name: "surfer-default"
    path: "surfer"
  - name: "surfer-shared"
    path: "{shared_surfer}"

cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"
  - name: "yosys-shared"
    tool: "{shared_yosys}"
"""


def _write_routed_root_config(target: Path, shared_dir: Path, routing: str) -> None:
    """A root config whose platform routes surfer/synth-tools at ``shared_dir``."""
    shared_dir.mkdir(parents=True, exist_ok=True)
    for binary in ("surfer", "yosys"):
        exe = shared_dir / binary
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    _write_minimal_root_config(
        target,
        extra=_SURFER_ROUTING_BLOCKS.format(
            shared_surfer=shared_dir / "surfer", shared_yosys=shared_dir / "yosys"
        ),
    )
    text = (target / "root_config.yaml").read_text()
    text = text.replace(
        '    verible: "stub-verible"\n', '    verible: "stub-verible"\n' + routing
    )
    (target / "root_config.yaml").write_text(text)


def test_routed_surfer_entry_pins_the_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_reconcile_with_root_cfg` follows the platform, not "surfer-default"."""
    shared = tmp_path / "shared" / "bin"
    _write_routed_root_config(tmp_path, shared, '    surfer: "surfer-shared"\n')
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="routed")
    by_name = {s.name: s for s in tm.get_manifest(rc)}
    detectors = by_name["surfer"].detection
    assert isinstance(detectors[0], tm.AbsolutePathDetector)
    assert detectors[0].abs_path == str(shared / "surfer")


def test_routing_a_tools_block_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`cfg-*-tools` is not routable and routing one is rejected.

    Every flow yaml names its own `tool:`, so a routed `*-tools` entry would make tool-check disagree with the run. The supported pin is a candidate list in the entry.
    """
    from rtl_buddy.errors import FatalRtlBuddyError

    shared = tmp_path / "shared" / "bin"
    _write_routed_root_config(tmp_path, shared, '    synth-tools: "yosys-shared"\n')
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    with pytest.raises(FatalRtlBuddyError, match="cannot be routed per platform"):
        RootConfig(name="routed")


def test_unrouted_surfer_keeps_the_default_entrys_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without routing keys the default entry's chain applies.

    Asserted against the routable block, since `cfg-*-tools` never contributes a detector.
    """
    shared = tmp_path / "shared" / "bin"
    _write_routed_root_config(tmp_path, shared, "")
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="unrouted")
    unrouted = {s.name: s for s in tm.get_manifest(rc)}["surfer"]

    # Absent routing must equal routing to `surfer-default`; compare against the explicit form because a bare name resolves by the host's PATH.
    _write_routed_root_config(tmp_path, shared, '    surfer: "surfer-default"\n')
    routed_to_default = {s.name: s for s in tm.get_manifest(RootConfig(name="routed"))}[
        "surfer"
    ]

    assert unrouted.detection == routed_to_default.detection
    # ...and differ from the routed-elsewhere chain, or the comparison proves nothing.
    _write_routed_root_config(tmp_path, shared, '    surfer: "surfer-shared"\n')
    routed_elsewhere = {s.name: s for s in tm.get_manifest(RootConfig(name="shared"))}[
        "surfer"
    ]
    assert routed_elsewhere.detection != unrouted.detection
    assert routed_elsewhere.detection[0].abs_path == str(shared / "surfer")


def test_root_cfg_tools_min_version_honours_active_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_minimal_root_config(
        tmp_path,
        extra=(
            "\ncfg-tools:\n"
            "  - name: verilator\n"
            '    min-version: "5.049"\n'
            "  - name: verilator\n"
            '    min-version: "5.050"\n'
            '    platform: "test-host"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="pins")
    by_name = {s.name: s for s in tm.get_manifest(rc)}
    assert by_name["verilator"].minimum_version == "5.050"


# Per-subcommand minimum versions


def _viewer_status(version: str | None) -> tuple[tm.ToolSpec, tm.ToolStatus]:
    spec = tm.resolve_spec(tm.get_manifest(), "rtl-buddy-view")
    assert spec is not None
    status = tm.ToolStatus(
        name=spec.name,
        status="ok",
        version=version,
        path="/usr/bin/rtl-buddy-view",
        optional=spec.optional,
        minimum_version=spec.minimum_version,
        kind="path",
        used_by=spec.used_by,
        subcommand_minimum_versions=dict(spec.subcommand_minimum_versions),
    )
    return spec, status


def test_viewer_graph_floor_is_the_one_graph_build_enforces():
    from rtl_buddy.graph import build as graph_build

    spec, _ = _viewer_status("0.3.0")
    floor = spec.subcommand_minimum_versions["graph"]
    assert floor == graph_build.VIEW_GRAPH_MIN_VERSION
    # Whatever tool-check accepts for `graph`, graph build accepts too, and vice versa.
    assert graph_build.check_view_supports_graph(floor) is None
    assert graph_build.check_view_supports_graph("0.3.0") is not None


def test_viewer_below_graph_floor_is_outdated_for_graph_only():
    spec, status = _viewer_status("0.3.0")
    readiness = tm.subcommand_readiness([status], [spec])
    assert readiness["graph"]["status"] == "outdated"
    assert readiness["graph"]["outdated"] == ["rtl-buddy-view"]
    assert readiness["graph"]["minimum_versions"] == {"rtl-buddy-view": "0.4.0"}
    for sub in ("hier", "hier-query", "hub"):
        assert readiness[sub]["status"] == "ok"
        assert readiness[sub]["minimum_versions"] == {}


@pytest.mark.parametrize("version", ["0.4.0", "0.4", "0.10.1", None])
def test_viewer_at_or_above_graph_floor_is_ready(version):
    spec, status = _viewer_status(version)
    readiness = tm.subcommand_readiness([status], [spec])
    assert readiness["graph"]["status"] == "ok"


def test_graph_floor_reaches_the_json_payload_text_and_explain():
    spec, status = _viewer_status("0.3.0")
    readiness = tm.subcommand_readiness([status], [spec])
    payload = tm.build_json_payload([status], readiness)
    tool = payload["tools"]["rtl-buddy-view"]
    assert tool["status"] == "ok"
    assert tool["minimum_version"] == "0.3.0"
    assert tool["subcommand_minimum_versions"] == {"graph": "0.4.0"}
    assert payload["subcommands"]["graph"]["status"] == "outdated"
    assert payload["subcommands"]["graph"]["minimum_versions"] == {
        "rtl-buddy-view": "0.4.0"
    }
    assert "minimum_versions" not in payload["subcommands"]["hier"]

    text = tm.render_text([status], readiness)
    assert "outdated: rtl-buddy-view (need ≥ 0.4.0)" in text

    explained = tm.explain(spec, status)
    assert "Minimum version for rb graph: 0.4.0" in explained
    assert "too old" in explained
    assert "too old" not in tm.explain(spec, _viewer_status("0.4.0")[1])


def test_graph_floor_fails_required_for_graph():
    spec, status = _viewer_status("0.3.0")
    readiness = tm.subcommand_readiness([status], [spec])
    assert (
        tm.compute_exit_code([status], required_for="graph", subcommands=readiness) != 0
    )
    assert (
        tm.compute_exit_code([status], required_for="hier", subcommands=readiness) == 0
    )


def test_release_tools_gate_only_rb_release():
    """`rb release` needs Verible's obfuscator and VCS; no other command does."""
    by_name = {s.name: s for s in tm.get_manifest()}
    for name, binary in (
        ("verible-obfuscate", "verible-verilog-obfuscate"),
        ("vcs", "vcs"),
    ):
        spec = by_name[name]
        assert spec.binaries == (binary,)
        assert spec.used_by == ("release",)
        assert spec.optional and spec.required_by == ("release",)
