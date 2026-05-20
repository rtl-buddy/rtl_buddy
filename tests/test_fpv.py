"""Tests for the FPV config surface: tool config, per-verification
config, suite/regression YAML loading, sby driver helpers. Mirrors the
structure of ``test_cdc.py``."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from rtl_buddy.config.fpv import (
    FpvConfig,
    FpvRegConfig,
    FpvSuiteConfig,
    FpvToolConfig,
    FpvToolConfigFile,
    FpvToolOptsFile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_cfg(name="sby", exe="sby", timeout=None, extra_args=""):
    opts = FpvToolOptsFile(timeout=timeout, extra_args=extra_args)
    return FpvToolConfig(FpvToolConfigFile(name=name, tool=exe, opts=opts))


def _make_fpv_cfg(
    *,
    name="test_fpv",
    model_name="my_module",
    model_path="/fake/models.yaml",
    tool="sby",
    top=None,
    properties=None,
    constraints=None,
    mode="bmc",
    depth=20,
    engines=None,
    reglvl=None,
    tool_overrides=None,
):
    from rtl_buddy.config.model import ModelConfig

    model = ModelConfig(name=model_name, filelist=[], path=model_path)
    return FpvConfig(
        name=name,
        desc="test fpv",
        model=model,
        tool=tool,
        top=top or model_name,
        properties=list(properties or []),
        constraints=constraints,
        mode=mode,
        depth=depth,
        engines=list(engines or ["smtbmc yices"]),
        _reglvl=reglvl,
        tool_overrides=tool_overrides,
    )


# ---------------------------------------------------------------------------
# FpvToolConfig — opts and overrides
# ---------------------------------------------------------------------------


def test_fpv_tool_config_returns_base_opts():
    cfg = _tool_cfg(timeout=300, extra_args="--verbose")
    opts = cfg.get_opts()
    assert opts.timeout == 300
    assert opts.extra_args == "--verbose"


def test_fpv_tool_config_overrides_merge_over_base():
    cfg = _tool_cfg(timeout=120, extra_args="")
    opts = cfg.get_opts({"timeout": 600, "extra_args": "--debug"})
    assert opts.timeout == 600
    assert opts.extra_args == "--debug"


def test_fpv_tool_config_partial_override_keeps_unset_base():
    cfg = _tool_cfg(timeout=120, extra_args="--baseline")
    opts = cfg.get_opts({"timeout": 300})
    assert opts.timeout == 300
    assert opts.extra_args == "--baseline"


def test_fpv_tool_config_none_override_returns_base():
    cfg = _tool_cfg(timeout=120)
    assert cfg.get_opts(None).timeout == 120
    assert cfg.get_opts({}).timeout == 120


# ---------------------------------------------------------------------------
# FpvConfig — basic accessors and reglvl semantics
# ---------------------------------------------------------------------------


def test_fpv_config_top_defaults_to_model_name():
    cfg = _make_fpv_cfg(model_name="my_top", top=None)
    assert cfg.get_top() == "my_top"


def test_fpv_config_top_explicit_wins_over_model_name():
    cfg = _make_fpv_cfg(model_name="my_top", top="inner_block")
    assert cfg.get_top() == "inner_block"


def test_fpv_config_engines_default_to_yices_smtbmc():
    cfg = _make_fpv_cfg()
    assert cfg.get_engines() == ["smtbmc yices"]


def test_fpv_config_reglvl_int():
    cfg = _make_fpv_cfg(reglvl=500)
    assert cfg.get_reglvl("sby") == 500


def test_fpv_config_reglvl_none_defaults_to_zero():
    cfg = _make_fpv_cfg(reglvl=None)
    assert cfg.get_reglvl("sby") == 0


def test_fpv_config_reglvl_dict_tool_specific():
    cfg = _make_fpv_cfg(reglvl={"sby": 100, "jaspergold": 200, "default": 50})
    assert cfg.get_reglvl("sby") == 100
    assert cfg.get_reglvl("jaspergold") == 200
    assert cfg.get_reglvl("vc-formal") == 50  # default fallback


def test_fpv_config_reglvl_dict_default_only():
    cfg = _make_fpv_cfg(reglvl={"default": 100})
    assert cfg.get_reglvl("sby") == 100
    assert cfg.get_reglvl("anything") == 100


def test_fpv_config_reglvl_malformed_dict_raises():
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg = _make_fpv_cfg(reglvl={"some-other-tool": 100})
    with pytest.raises(FatalRtlBuddyError, match="reglvl"):
        cfg.get_reglvl("sby")


# ---------------------------------------------------------------------------
# FpvConfig — tool_overrides (nested by tool name)
# ---------------------------------------------------------------------------


def test_fpv_config_tool_overrides_for_matching_tool():
    cfg = _make_fpv_cfg(tool_overrides={"sby": {"extra_args": "--strict"}})
    assert cfg.get_tool_overrides_for("sby") == {"extra_args": "--strict"}


def test_fpv_config_tool_overrides_for_non_matching_tool():
    cfg = _make_fpv_cfg(tool_overrides={"sby": {"extra_args": "--strict"}})
    assert cfg.get_tool_overrides_for("jaspergold") is None


def test_fpv_config_tool_overrides_none():
    cfg = _make_fpv_cfg(tool_overrides=None)
    assert cfg.get_tool_overrides_for("sby") is None


def test_fpv_config_tool_overrides_merge_through_tool_cfg():
    """End-to-end: a per-verification tool_overrides entry overrides the root
    config baseline when passed through FpvToolConfig.get_opts()."""
    fpv_cfg = _make_fpv_cfg(
        tool_overrides={"sby": {"timeout": 600, "extra_args": "--strict"}}
    )
    tool_cfg = _tool_cfg(timeout=120, extra_args="")
    opts = tool_cfg.get_opts(fpv_cfg.get_tool_overrides_for(tool_cfg.get_name()))
    assert opts.timeout == 600
    assert opts.extra_args == "--strict"


# ---------------------------------------------------------------------------
# FpvSuiteConfig — YAML loading + path resolution
# ---------------------------------------------------------------------------

_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: fpv_config

    verifications:
      - name: "fpv_a"
        desc: "First verification"
        model: "mod_a"
        model_path: "{models_path}"
        tool: "sby"
        top: "mod_a"
        properties:
          - "mod_a_props.sv"
        mode: "bmc"
        depth: 32
        engines:
          - "smtbmc yices"
        reglvl: 0
      - name: "fpv_b"
        desc: "Second verification"
        model: "mod_b"
        model_path: "{models_path}"
        tool: "sby"
        properties:
          - "mod_b_props.sv"
        mode: "prove"
        depth: 16
        engines:
          - "smtbmc z3"
          - "abc pdr"
        reglvl: 1000
""")

_MODELS_YAML = dedent("""\
    rtl-buddy-filetype: model_config

    models:
      - name: "mod_a"
        filelist: ["top_a.sv"]
      - name: "mod_b"
        filelist: ["top_b.sv"]
""")


def _write_suite(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "fpv.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))
    return suite_yaml


def test_fpv_suite_config_loads_all_verifications(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = FpvSuiteConfig(str(suite_yaml))
    assert cfg.get_verification_names() == ["fpv_a", "fpv_b"]


def test_fpv_suite_config_get_by_name(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = FpvSuiteConfig(str(suite_yaml))
    results = cfg.get_verifications("fpv_a")
    assert len(results) == 1
    assert results[0].get_name() == "fpv_a"
    assert results[0].get_top() == "mod_a"
    assert results[0].get_mode() == "bmc"
    assert results[0].get_depth() == 32


def test_fpv_suite_config_paths_resolved_relative_to_yaml(tmp_path):
    """Properties paths must be resolved relative to the fpv.yaml file."""
    suite_yaml = _write_suite(tmp_path)
    cfg = FpvSuiteConfig(str(suite_yaml))
    fpv_a = cfg.get_verifications("fpv_a")[0]
    fpv_b = cfg.get_verifications("fpv_b")[0]
    assert Path(fpv_a.get_properties()[0]) == tmp_path / "mod_a_props.sv"
    assert Path(fpv_b.get_properties()[0]) == tmp_path / "mod_b_props.sv"


def test_fpv_config_constraints_default_none():
    cfg = _make_fpv_cfg()
    assert cfg.get_constraints() is None


def test_fpv_config_constraints_set_via_make():
    cfg = _make_fpv_cfg(constraints="/abs/clock_reset.sv")
    assert cfg.get_constraints() == "/abs/clock_reset.sv"


def test_fpv_suite_config_constraints_resolved_relative_to_yaml(tmp_path):
    """A `constraints:` field in fpv.yaml is resolved relative to the yaml."""
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "fpv.yaml"
    suite_yaml.write_text(
        dedent("""\
        rtl-buddy-filetype: fpv_config

        verifications:
          - name: "fpv_with_constraints"
            desc: "Has a shared constraints file"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "sby"
            top: "mod_a"
            constraints: "shared_clock_reset.sv"
            properties:
              - "mod_a_props.sv"
            mode: "bmc"
            depth: 16
            engines:
              - "smtbmc yices"
            reglvl: 0
    """)
    )
    cfg = FpvSuiteConfig(str(suite_yaml))
    verif = cfg.get_verifications("fpv_with_constraints")[0]
    assert Path(verif.get_constraints()) == tmp_path / "shared_clock_reset.sv"


def test_fpv_suite_config_missing_name_raises(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError

    suite_yaml = _write_suite(tmp_path)
    cfg = FpvSuiteConfig(str(suite_yaml))
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        cfg.get_verifications("nonexistent")


def test_fpv_suite_config_invalid_mode_raises(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError

    bad_yaml = dedent("""\
        rtl-buddy-filetype: fpv_config

        verifications:
          - name: "fpv_bad"
            desc: "Bad mode"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "sby"
            properties: ["mod_a_props.sv"]
            mode: "telepathy"
            depth: 16
            engines: ["smtbmc yices"]
    """)
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "fpv.yaml"
    suite_yaml.write_text(bad_yaml)
    with pytest.raises(FatalRtlBuddyError, match="mode"):
        FpvSuiteConfig(str(suite_yaml))


# ---------------------------------------------------------------------------
# FpvRegConfig — YAML loading + per-suite path resolution
# ---------------------------------------------------------------------------

_REG_YAML = dedent("""\
    rtl-buddy-filetype: fpv_reg_config

    fpv-configs:
      - "sandbox/fpv.yaml"
""")


def test_fpv_reg_config_loads_suite_paths(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = sandbox / "fpv.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))

    reg_yaml = tmp_path / "fpv_regression.yaml"
    reg_yaml.write_text(_REG_YAML)

    reg_cfg = FpvRegConfig(name="reg", path=str(reg_yaml))
    suites = reg_cfg.get_suite_configs()
    assert len(suites) == 1
    assert suites[0].get_verification_names() == ["fpv_a", "fpv_b"]


# ---------------------------------------------------------------------------
# SbyFpv driver — config-file rendering + status parsing
# ---------------------------------------------------------------------------


def test_sby_fpv_writes_sby_file_with_expected_sections(tmp_path):
    """The generated .sby file must contain options/engines/script/files
    sections derived from FpvConfig."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    # Stand up a real filelist on disk so VlogFilelist's write_output
    # has something to chew on. We avoid running write_output here by
    # bypassing _write_filelist via _parse_filelist directly.
    src = tmp_path / "design.sv"
    src.write_text("module design(); endmodule\n")
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    fl = tmp_path / "artefacts" / "demo" / "fpv.f"
    fl.parent.mkdir(parents=True)
    fl.write_text(f"{src}\n")

    fpv_cfg = _make_fpv_cfg(
        name="demo",
        top="design",
        properties=[str(props)],
        mode="bmc",
        depth=42,
        engines=["smtbmc yices", "abc pdr"],
    )
    tool_cfg = _tool_cfg(timeout=300)
    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=fpv_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
    )
    sources, incdirs = sby._parse_filelist(str(fl))
    sby_path = sby._write_sby_file(sources, incdirs)

    content = Path(sby_path).read_text()
    assert "[options]" in content
    assert "mode bmc" in content
    assert "depth 42" in content
    assert "timeout 300" in content
    assert "[engines]" in content
    assert "smtbmc yices" in content
    assert "abc pdr" in content
    assert "[script]" in content
    assert "prep -top design" in content
    assert "read -sv -formal design.sv" in content
    assert "read -sv -formal props.sv" in content
    assert "[files]" in content
    assert str(src) in content
    assert str(props) in content


def test_sby_fpv_writes_constraints_before_properties(tmp_path):
    """When `constraints:` is set, it must be read into the sby script
    BEFORE properties so the assumes are in scope when the asserts
    elaborate."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("module design(); endmodule\n")
    constraints = tmp_path / "clock_reset.sv"
    constraints.write_text("// clock + reset assumes\n")
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    fl = tmp_path / "artefacts" / "demo" / "fpv.f"
    fl.parent.mkdir(parents=True)
    fl.write_text(f"{src}\n")

    fpv_cfg = _make_fpv_cfg(
        name="demo",
        top="design",
        properties=[str(props)],
        constraints=str(constraints),
    )
    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=fpv_cfg,
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    sources, incdirs = sby._parse_filelist(str(fl))
    sby_path = sby._write_sby_file(sources, incdirs)
    content = Path(sby_path).read_text()

    # All three files appear in [script] and [files].
    assert "read -sv -formal design.sv" in content
    assert "read -sv -formal clock_reset.sv" in content
    assert "read -sv -formal props.sv" in content
    assert str(constraints) in content
    # Constraints come before properties.
    script_section = content.split("[script]")[1].split("[files]")[0]
    constraints_pos = script_section.index("read -sv -formal clock_reset.sv")
    props_pos = script_section.index("read -sv -formal props.sv")
    assert constraints_pos < props_pos
    # Files section preserves the same order.
    files_section = content.split("[files]")[1]
    files_constraints_pos = files_section.index(str(constraints))
    files_props_pos = files_section.index(str(props))
    assert files_constraints_pos < files_props_pos


def test_sby_fpv_constraints_optional_default_unchanged(tmp_path):
    """Without `constraints:` the script must not gain an extra read."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("module design(); endmodule\n")
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    fl = tmp_path / "artefacts" / "demo" / "fpv.f"
    fl.parent.mkdir(parents=True)
    fl.write_text(f"{src}\n")

    fpv_cfg = _make_fpv_cfg(
        name="demo",
        top="design",
        properties=[str(props)],
        constraints=None,
    )
    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=fpv_cfg,
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    sources, incdirs = sby._parse_filelist(str(fl))
    content = Path(sby._write_sby_file(sources, incdirs)).read_text()
    # Exactly two read statements: design + props.
    assert content.count("read -sv -formal ") == 2


def test_sby_fpv_parse_filelist_extracts_incdirs(tmp_path):
    """+incdir+ entries from the filelist must be resolved and surfaced
    as include directories, separate from source files."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+incdir+./rtl/inc\n{src.name}\n")

    fpv_cfg = _make_fpv_cfg()
    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=fpv_cfg,
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    sources, incdirs = sby._parse_filelist(str(fl))
    assert sources == [str((tmp_path / "design.sv").resolve())] or sources == [
        str(tmp_path / "design.sv")
    ]
    assert incdirs == [str(tmp_path / "rtl" / "inc")]


def test_sby_fpv_read_status_returns_first_token(tmp_path):
    """The status file may contain extra info after the verdict — we
    only care about the first token."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    workdir.mkdir()
    (workdir / "status").write_text("PASS (engine_0)\n")
    assert SbyFpv._read_status(str(workdir)) == "PASS"

    (workdir / "status").write_text("FAIL")
    assert SbyFpv._read_status(str(workdir)) == "FAIL"


def test_sby_fpv_read_status_missing_returns_none(tmp_path):
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    workdir.mkdir()
    assert SbyFpv._read_status(str(workdir)) is None


def test_sby_fpv_counterexample_desc_points_at_trace(tmp_path):
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    (workdir / "engine_0").mkdir(parents=True)
    (workdir / "engine_0" / "trace.vcd").write_text("$dummy\n")
    desc = SbyFpv._counterexample_desc(str(workdir))
    assert "trace.vcd" in desc


def test_sby_fpv_counterexample_desc_no_engine_dir(tmp_path):
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    workdir.mkdir()
    desc = SbyFpv._counterexample_desc(str(workdir))
    assert "no counterexample" in desc


# ---------------------------------------------------------------------------
# FpvRunner — dispatch & skip semantics (no real sby invocation)
# ---------------------------------------------------------------------------


class _StubRootCfg:
    def __init__(self, tool_cfg):
        self._tool_cfg = tool_cfg

    def get_fpv_tool_cfg(self, name):
        return self._tool_cfg


def test_fpv_runner_dispatches_to_sby_backend(tmp_path):
    """FpvRunner should look up the tool config from root_cfg and hand
    a real SbyFpv instance the per-verification config."""
    from rtl_buddy.runner.fpv_runner import FpvRunner
    from rtl_buddy.runner.fpv_results import FpvPassResults

    fpv_cfg = _make_fpv_cfg(tool="sby")
    tool_cfg = _tool_cfg()
    root_cfg = _StubRootCfg(tool_cfg)

    runner = FpvRunner(
        name="t/runner",
        root_cfg=root_cfg,
        fpv_cfg=fpv_cfg,
        suite_dir=str(tmp_path),
    )

    fake_result = FpvPassResults(
        name="test_fpv", mode="bmc", depth=20, engines=["smtbmc yices"]
    )
    with patch(
        "rtl_buddy.tools.sby_fpv.SbyFpv.run", return_value=fake_result
    ) as mocked:
        result = runner.run()
    assert mocked.called
    assert result.results["result"] == "PASS"
    assert result.results["mode"] == "bmc"


# ---------------------------------------------------------------------------
# FpvToolOpts — solver_versions pin field carries through
# ---------------------------------------------------------------------------


def test_fpv_tool_config_solver_versions_default_empty():
    cfg = _tool_cfg()
    assert cfg.get_opts().solver_versions == {}


def test_fpv_tool_config_solver_versions_round_trip():
    opts_file = FpvToolOptsFile(solver_versions={"yices": "2.6.4"})
    tool_cfg = FpvToolConfig(FpvToolConfigFile(name="sby", tool="sby", opts=opts_file))
    assert tool_cfg.get_opts().solver_versions == {"yices": "2.6.4"}


def test_fpv_tool_config_solver_versions_override_replaces_base():
    """Per-verification overrides should replace, not merge — the pin
    semantics are "use exactly this set", not "add to whatever's pinned"."""
    opts_file = FpvToolOptsFile(solver_versions={"yices": "2.6.4", "z3": "4.13.0"})
    tool_cfg = FpvToolConfig(FpvToolConfigFile(name="sby", tool="sby", opts=opts_file))
    opts = tool_cfg.get_opts({"solver_versions": {"z3": "4.12.0"}})
    assert opts.solver_versions == {"z3": "4.12.0"}


def test_fpv_tool_config_solver_versions_yaml_dash_separator(tmp_path):
    """The YAML key is `solver-versions` (dash); confirm round-trip from
    a real fpv.yaml-style cfg loads it into the dict."""
    from serde.yaml import from_yaml

    yaml_text = dedent("""\
        name: sby
        tool: sby
        opts:
          solver-versions:
            yices: "2.6.4"
            z3: "4.13.0"
    """)
    parsed = from_yaml(FpvToolConfigFile, yaml_text)
    assert parsed.opts.solver_versions == {"yices": "2.6.4", "z3": "4.13.0"}


# ---------------------------------------------------------------------------
# fpv_solver_pin — version probe + pin enforcement
# ---------------------------------------------------------------------------


def _fake_completed(stdout="", stderr="", returncode=0):
    """Minimal stand-in for subprocess.CompletedProcess."""
    from types import SimpleNamespace

    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_probe_solver_version_yices_extracts_first_token():
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin.subprocess,
        "run",
        return_value=_fake_completed(stdout="Yices 2.6.4\nCopyright ..."),
    ):
        assert fpv_solver_pin.probe_solver_version("yices") == "2.6.4"


def test_probe_solver_version_z3_extracts_version():
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin.subprocess,
        "run",
        return_value=_fake_completed(stdout="Z3 version 4.13.0 - 64 bit\n"),
    ):
        assert fpv_solver_pin.probe_solver_version("z3") == "4.13.0"


def test_probe_solver_version_unknown_returns_none():
    from rtl_buddy.tools import fpv_solver_pin

    assert fpv_solver_pin.probe_solver_version("not-a-real-solver") is None


def test_probe_solver_version_binary_missing_returns_none():
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin.subprocess,
        "run",
        side_effect=FileNotFoundError("yices-smt2"),
    ):
        assert fpv_solver_pin.probe_solver_version("yices") is None


def test_probe_solver_version_unparseable_output_returns_none():
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin.subprocess,
        "run",
        return_value=_fake_completed(stdout="garbage output"),
    ):
        assert fpv_solver_pin.probe_solver_version("yices") is None


def test_check_solver_pins_all_match_returns_resolved():
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin,
        "probe_solver_version",
        side_effect=lambda s: {"yices": "2.6.4", "z3": "4.13.0"}[s],
    ):
        resolved = fpv_solver_pin.check_solver_pins({"yices": "2.6.4", "z3": "4.13.0"})
    assert resolved == {"yices": "2.6.4", "z3": "4.13.0"}


def test_check_solver_pins_mismatch_raises_with_all_failures():
    """All failures should be listed in one error so the user reruns once."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(
        fpv_solver_pin,
        "probe_solver_version",
        side_effect=lambda s: {"yices": "2.6.3", "z3": "4.13.0"}[s],
    ):
        with pytest.raises(FatalRtlBuddyError) as exc_info:
            fpv_solver_pin.check_solver_pins({"yices": "2.6.4", "z3": "4.12.0"})
    msg = str(exc_info.value)
    assert "yices" in msg and "2.6.3" in msg and "2.6.4" in msg
    assert "z3" in msg and "4.12.0" in msg


def test_check_solver_pins_missing_solver_raises():
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools import fpv_solver_pin

    with patch.object(fpv_solver_pin, "probe_solver_version", return_value=None):
        with pytest.raises(FatalRtlBuddyError, match="yices"):
            fpv_solver_pin.check_solver_pins({"yices": "2.6.4"})


def test_check_solver_pins_empty_is_noop():
    from rtl_buddy.tools import fpv_solver_pin

    assert fpv_solver_pin.check_solver_pins({}) == {}


# ---------------------------------------------------------------------------
# fpv_cex_finder — CEX VCD path resolution for `rb wave-fpv`
# ---------------------------------------------------------------------------


def test_find_cex_vcd_returns_trace_from_first_engine(tmp_path):
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    workdir = tmp_path / "artefacts" / "demo_safety" / "sby_workdir"
    (workdir / "engine_0").mkdir(parents=True)
    (workdir / "engine_0" / "trace.vcd").write_text("$dummy\n")

    assert find_cex_vcd(str(tmp_path), "demo_safety") == str(
        workdir / "engine_0" / "trace.vcd"
    )


def test_find_cex_vcd_prefers_lowest_engine_number(tmp_path):
    """Multiple engines can each emit a trace; the sorted-first one wins."""
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    workdir = tmp_path / "artefacts" / "demo_safety" / "sby_workdir"
    for engine in ("engine_0", "engine_1", "engine_2"):
        (workdir / engine).mkdir(parents=True)
        (workdir / engine / "trace.vcd").write_text("$dummy\n")

    assert find_cex_vcd(str(tmp_path), "demo_safety") == str(
        workdir / "engine_0" / "trace.vcd"
    )


def test_find_cex_vcd_skips_engines_without_trace(tmp_path):
    """Engine dirs without trace.vcd (e.g. proof passed in that engine)
    should be skipped, not returned as a hit."""
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    workdir = tmp_path / "artefacts" / "demo_safety" / "sby_workdir"
    (workdir / "engine_0").mkdir(parents=True)  # no trace.vcd
    (workdir / "engine_1").mkdir(parents=True)
    (workdir / "engine_1" / "trace.vcd").write_text("$dummy\n")

    assert find_cex_vcd(str(tmp_path), "demo_safety") == str(
        workdir / "engine_1" / "trace.vcd"
    )


def test_find_cex_vcd_returns_none_when_workdir_missing(tmp_path):
    """Verification hasn't been run yet -> no workdir -> None."""
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    assert find_cex_vcd(str(tmp_path), "never_ran") is None


def test_find_cex_vcd_returns_none_when_no_engine_has_trace(tmp_path):
    """Proof passed (no CEX emitted) -> engine dirs present but no trace -> None."""
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    workdir = tmp_path / "artefacts" / "demo_safety" / "sby_workdir"
    (workdir / "engine_0").mkdir(parents=True)  # no trace.vcd
    (workdir / "engine_1").mkdir(parents=True)  # no trace.vcd

    assert find_cex_vcd(str(tmp_path), "demo_safety") is None


def test_find_cex_vcd_ignores_non_engine_dirs(tmp_path):
    """Sby writes `src/`, `model/`, etc. alongside `engine_N/` — those
    should not be probed for trace files."""
    from rtl_buddy.tools.fpv_cex_finder import find_cex_vcd

    workdir = tmp_path / "artefacts" / "demo_safety" / "sby_workdir"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "trace.vcd").write_text("$dummy\n")
    (workdir / "model").mkdir(parents=True)

    assert find_cex_vcd(str(tmp_path), "demo_safety") is None
