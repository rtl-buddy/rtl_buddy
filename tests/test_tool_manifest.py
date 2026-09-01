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


def test_python_sibling_detector_falls_back_to_a_legacy_dist_name():
    """A renamed dist is found under its old name too, current first.

    The viewer's distribution was renamed rtl-buddy-view ->
    rtl-buddy-sch (rtl-buddy-sch#157); the detector must read whichever
    is installed, and prefer the current name when both are.
    """
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
    # Current name is absent; the legacy name carries the version.
    assert result.found is True
    assert result.version
    assert result.kind == "python"

    # Both present: the current name wins, so a stale frozen dist left
    # behind by an upgrade cannot mask the installed one.
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

    `uv tool install rtl-buddy-sch` — what the docs now recommend — puts
    the current dist in an isolated env, so a project venv that still
    holds the abandoned wheel would otherwise report that frozen version
    for a demonstrably newer binary. Leaving `version=None` sends
    `check_tool` to `probe_version()`, which asks the executable.
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

    # The current name is authoritative and keeps its metadata version:
    # there the dist and the binary it installed cannot disagree.
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


def test_axi_profile_vpd_converters_declared():
    """The VPD-conversion tools behind `rb axi-profile run` are declared.

    `axi_profile_rtl_buddy._convert_vpd` shells out to `vpd2vcd` (VCS)
    and `vcd2fst` (GTKWave) — both must stay visible to `rb tool-check`
    so a missing converter surfaces before a long profile run.
    """
    by_name = {s.name: s for s in tm.get_manifest()}

    vpd2vcd = by_name["vpd2vcd"]
    assert vpd2vcd.optional
    assert "axi-profile" in vpd2vcd.used_by
    assert vpd2vcd.binaries == ("vpd2vcd",)

    gtkwave = by_name["gtkwave"]
    assert "vcd2fst" in gtkwave.binaries
    assert "axi-profile" in gtkwave.used_by


def test_icarus_simulator_declared():
    """Icarus (iverilog compile + vvp runtime) is declared as a sim backend.

    `VlogSim` shells out to `iverilog` and the simv wrapper execs `vvp`
    when a `cfg-rtl-builder` entry / `builder:` selects simulator-family
    icarus. Both must stay visible to `rb tool-check` so a missing binary
    surfaces as structured guidance rather than a shell-exec error.
    """
    by_name = {s.name: s for s in tm.get_manifest()}

    icarus = by_name["icarus"]
    assert icarus.binaries == ("iverilog", "vvp")
    # Opt-in alternate backend: missing Icarus must not gate test readiness
    # for the default (Verilator) path.
    assert icarus.optional
    assert icarus.used_by == ("test", "randtest", "regression")


def test_slurm_gates_test_as_well_as_regression():
    """`rb test --dispatch slurm` needs the client too (#440).

    `--required-for test` and `--explain slurm` are the gate the bundled
    SKILL.md tells agents to check before dispatching, so `used_by` has
    to name every command that can dispatch.
    """
    by_name = {s.name: s for s in tm.get_manifest()}

    slurm = by_name["slurm"]
    assert slurm.optional  # the default --dispatch local needs nothing
    assert set(slurm.used_by) == {"regression", "randtest", "test"}
    assert "rb test --dispatch slurm" in slurm.notes


def test_slurm_explains_scontrol_as_an_optional_probe():
    """`rb tool-check --explain slurm` must name scontrol and what it buys.

    The backend shells out to `scontrol show config` for the cluster's
    MaxArraySize (#509). Missing scontrol is not a gate — chunking simply
    stays off — but a site that hits `Invalid job array specification`
    needs the manifest to say so and to name the config fallback, since
    --explain is what the bundled skill tells agents to read.
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
    # Still optional overall: sbatch is the version probe and the gate.
    assert slurm.version_cmd[0] == "sbatch"
    assert slurm.optional


def test_an_optional_binary_alone_does_not_make_a_tool_present(monkeypatch, tmp_path):
    """A host with `scontrol` but no `sbatch` cannot dispatch (#509 review).

    Detection is any-of over `binaries` and `probe_version` substitutes the
    found path into `version_cmd`, so listing the auxiliary binary there
    would report `ok` — and run `scontrol --version` to say so — on a host
    that cannot submit a single job.
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
    """--explain must not let an optional binary read as the tool's status."""
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
    """rtl-buddy-view carries a 0.3.0 FLOOR (no upper cap) and a probe.

    rtl_buddy pins no rtl-buddy-view version in pyproject — the manifest
    floor plus the runtime guards are the policy. The probe depends on
    `rtl-buddy-view --version` (rtl-buddy-view#121); the regex must pull
    X.Y.Z out of its `rtl-buddy-view 0.3.0` output.
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["rtl-buddy-view"]
    assert spec.minimum_version == "0.3.0"
    assert spec.version_cmd == ("rtl-buddy-view", "--version")
    assert spec.version_regex is not None
    m = re.search(spec.version_regex, "rtl-buddy-view 0.3.0")
    assert m is not None and m.group(1) == "0.3.0"
    # The tagless hatch-vcs dev build still resolves to its base version.
    m = re.search(spec.version_regex, "rtl-buddy-view 0.2.2.dev0+g0f37a432d")
    assert m is not None and m.group(1) == "0.2.2"


def test_rtl_buddy_view_spec_probes_both_distribution_names():
    """Executable contracts unchanged; dist metadata read under both names.

    The PyPI distribution was renamed rtl-buddy-view -> rtl-buddy-sch at
    0.7.0 (rtl-buddy-sch#157). The tool key, the binary, the version
    command and its output literal are unchanged contracts — only the
    metadata lookup and the install hint move.
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

    # `rb tool-check --explain` must send people to the dist that still
    # gets releases, not the one frozen at 0.5.0.
    assert "rtl-buddy-sch" in spec.install_hint["any"]


def _alias_spec(name: str, aliases: tuple[str, ...] = ()) -> tm.ToolSpec:
    """Minimal spec for exercising name/alias lookup and collisions."""
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
    """`rtl-buddy-sch` is the string users type; the spec answers to it.

    The dist renamed at 0.7.0 and our own install hint names it, so the
    lookup has to accept it — while `name` stays `rtl-buddy-view`, the
    frozen executable / probe-literal / wire-origin contract
    (rtl_buddy#445).
    """
    by_name = {s.name: s for s in tm.get_manifest()}
    assert by_name["rtl-buddy-view"].aliases == ("rtl-buddy-sch",)
    assert "rtl-buddy-sch" not in by_name


def test_resolve_spec_matches_name_then_alias():
    specs = tm.get_manifest()
    canonical = tm.resolve_spec(specs, "rtl-buddy-view")
    aliased = tm.resolve_spec(specs, "rtl-buddy-sch")
    assert canonical is not None
    # Same spec object, and the identity it reports is the canonical one.
    assert aliased is canonical
    assert aliased.name == "rtl-buddy-view"
    assert tm.resolve_spec(specs, "does-not-exist") is None


def test_resolve_spec_prefers_a_canonical_name_over_an_alias():
    """A name always outranks another spec's alias for the same string.

    The manifest assert forbids that overlap, but resolve_spec is a
    public helper — callers passing their own list get the deterministic
    answer rather than list order.
    """
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
    """A shadowed lookup key is a manifest bug, caught at build time."""
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
    """Reconciliation must not dedupe the collision away before the check.

    `_reconcile_with_root_cfg` rebuilds the list through ``{s.name: s}``,
    which silently collapses a duplicate name — so with a
    ``root_config.yaml`` present a post-reconcile assert could only ever
    catch the alias shapes (#445 review).
    """
    monkeypatch.setattr(
        tm,
        "_builtin_manifest",
        lambda: [_alias_spec("alpha"), _alias_spec("alpha")],
    )
    with pytest.raises(AssertionError, match="duplicate lookup key 'alpha'"):
        tm.get_manifest(root_cfg=object())


def test_builtin_manifest_lookup_keys_are_unique():
    """The shipped manifest itself satisfies the invariant."""
    specs = tm.get_manifest()
    keys = [s.name for s in specs] + [a for s in specs for a in s.aliases]
    assert len(keys) == len(set(keys))


def test_require_resolves_the_alias_and_reports_the_canonical_name():
    """`require("rtl-buddy-sch")` must never be the unknown-tool path.

    Whether the viewer is installed here decides which branch runs; both
    have to name `rtl-buddy-view`, since that is what the user must
    `--explain` and what --machine consumers are keyed on.
    """
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
    """Probe order: rtl-buddy-sch, then rtl-buddy-view, then None."""
    installed: dict[str, str] = {}
    real_version = tm.importlib_metadata.version

    # The patch lands on the stdlib module object, so only the viewer's
    # own names are answered from the fixture; everything else defers to
    # the real lookup and stays usable inside the patched window.
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
    """A pre-floor view reports `outdated`; a release at/above is `ok`.

    Drives the manifest's real version_cmd / version_regex / floor for
    rtl-buddy-view through ``check_tool`` against a stub binary on PATH
    whose ``--version`` prints a sub-floor string, then an at-floor one.
    The PathDetector (not the metadata sibling detector) is substituted
    so the subprocess probe — the part that depends on view's --version —
    is the thing under test.
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
    """The `vivado` entry gates the optional `rb fpga` flow (#284).

    The version regex must pull the dotted version out of both the
    `vivado -version` banner ("Vivado v2022.1.2 (64-bit)") and the
    stylized report-header form ("Vivado v.2022.1.2 (lin64) ...").
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


def test_cli_tool_check_machine_emits_envelope(tmp_path: Path):
    """The global --machine flag yields a single JSON envelope on stdout.

    Regression for the cross-phase finding: an agent driving the SKILL.md
    loop calls `rb --machine tool-check`; the first stdout byte must be `{`
    (a parseable envelope), not the human text table.
    """
    result = _run_rb("--machine", "tool-check", "--no-probe-versions", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.lstrip().startswith("{")
    env = json.loads(result.stdout)
    assert env["command"] == "tool-check"
    assert env["exit_code"] == 0
    payload = env["payload"]
    assert "tools" in payload and "subcommands" in payload
    # Bare tool-check is informational: process exits 0 regardless of the
    # manifest readiness verdict, which rides separately in the payload.
    assert "readiness_exit_code" in payload
    assert len(payload["tools"]) > 0


def test_cli_tool_check_machine_required_for_missing_exits_2(tmp_path: Path):
    """--machine + --required-for surfaces the gate verdict in the envelope."""
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
    """--machine --explain wraps the per-tool view + install text in an envelope."""
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
    """--explain rtl-buddy-sch resolves, and answers as rtl-buddy-view."""
    result = _run_rb(
        "tool-check", "--explain", "rtl-buddy-sch", "--no-probe-versions", cwd=tmp_path
    )
    assert result.returncode == 0
    assert "unknown tool" not in result.stderr
    assert result.stdout.startswith("rtl-buddy-view")


def test_cli_tool_check_machine_explain_alias_keeps_canonical_name(tmp_path: Path):
    """The alias is an input courtesy — the JSON key must not drift."""
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
    """Optional binaries are documentation, not a state to gate on (#509).

    They appear in the human explanation — which `--machine` mirrors in
    `instructions` — and nowhere in the structured `tools` entry, so no
    consumer can build a readiness check on one.
    """
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
    """The rejection tells the user which spellings exist."""
    result = _run_rb(
        "tool-check", "--explain", "does-not-exist", "--no-probe-versions", cwd=tmp_path
    )
    assert result.returncode == 1
    # The console word-wraps the hint, so compare on collapsed whitespace.
    hint = " ".join(result.stderr.split())
    assert "rtl-buddy-view (alias: rtl-buddy-sch)" in hint


def test_cli_tool_check_machine_explain_unknown_carries_aliases(tmp_path: Path):
    """The --machine rejection is discoverable too, without moving `known`.

    An agent that guessed `rtl-buddy-sch` hits this envelope, so the
    mapping has to be in it — as an additive sibling, because `known`
    stays bare canonical names that consumers are keyed on
    (rtl_buddy#445 review).
    """
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


def test_graph_extract_spec_is_optional_with_anchored_regex():
    """The bundled binding-tier extractor (rtl_buddy#391): optional=True
    with used_by graph — its absence must not fail `rb tool-check
    --required-for graph` (the design promise: the binding tier is
    skipped and the build still succeeds). Version-regex discipline:
    odd formats yield NO version, never a wrong one."""
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
    # Editable/git installs report PEP 440 dev+local versions; the full
    # string must land in the fingerprint, not a truncated prefix.
    m = re.search(spec.version_regex, "rb-graph-extract 0.1.dev1+g0d74f48e0")
    assert m is not None and m.group(1) == "0.1.dev1+g0d74f48e0"
    assert re.search(spec.version_regex, "rb-graph-extract (python 3.12) 0.2.0") is None
    # The floor mirrors the graph-extract extra's `>= 0.1.0` for installs
    # that bypass pip's resolver — and a dev build of 0.1 must satisfy it
    # (the digit-tuple comparator extends past the floor), or an editable
    # checkout would probe as "outdated".
    assert spec.minimum_version == "0.1.0"
    assert tm._version_satisfies("0.1.dev1+g0d74f48e0", spec.minimum_version)


def test_rtl_buddy_view_is_required_for_graph():
    """The design tier's exporter is a hard requirement of the graph
    flow: used_by must carry graph so --required-for graph enforces it."""
    by_name = {s.name: s for s in tm.get_manifest()}
    assert "graph" in by_name["rtl-buddy-view"].used_by


def test_mcp_sdk_detects_via_python_package_with_floor():
    """The mcp SDK is a library: no binaries contract, PythonPackage
    detection only, and the documented 1.2.0 floor."""
    by_name = {s.name: s for s in tm.get_manifest()}
    spec = by_name["mcp"]
    assert spec.optional
    assert spec.binaries == ()
    assert spec.minimum_version == "1.2.0"
    assert len(spec.detection) == 1
    assert isinstance(spec.detection[0], tm.PythonPackageDetector)
    assert "mcp" in spec.used_by


# ---------------------------------------------------------------------------
# Manifest reconciliation — cfg-platforms tool routing (#439)


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
    """`cfg-*-tools` is not routable, and saying so beats doing nothing.

    tool-check must report the binary the run uses. A routed `*-tools`
    entry could only ever change tool-check, because every flow yaml
    names its own `tool:` — so routing one would make the report *dis*agree
    with the run. The supported pin is a candidate list in the entry, which
    both sides read (#439).
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
    """No routing keys → the pre-#439 chain, unchanged.

    Asserted against the routable block. `cfg-*-tools` never contributed
    a detector in the first place, routed or not, so asserting on `yosys`
    here would pass whatever routing did.
    """
    shared = tmp_path / "shared" / "bin"
    _write_routed_root_config(tmp_path, shared, "")
    monkeypatch.chdir(tmp_path)

    from rtl_buddy.config.root import RootConfig

    rc = RootConfig(name="unrouted")
    unrouted = {s.name: s for s in tm.get_manifest(rc)}["surfer"]

    # Routing absent must be exactly routing to `surfer-default`, which is
    # the entry the unrouted accessor falls back to. Compared against the
    # explicit form rather than against a fixed shape, because what
    # `surfer-default` (a bare name) resolves to depends on the host's PATH.
    _write_routed_root_config(tmp_path, shared, '    surfer: "surfer-default"\n')
    routed_to_default = {s.name: s for s in tm.get_manifest(RootConfig(name="routed"))}[
        "surfer"
    ]

    assert unrouted.detection == routed_to_default.detection
    # …and not the routed-elsewhere chain, or the comparison proves nothing.
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
