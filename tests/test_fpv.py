"""Tests for the FPV config surface: tool config, per-verification
config, suite/regression YAML loading, sby driver helpers. Mirrors the
structure of ``test_cdc.py``."""

import os
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
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


# ---------------------------------------------------------------------------
# covers: — spec coverage claims on formal runs (rtl-buddy/rtl_buddy#385)
# ---------------------------------------------------------------------------

_COVERS_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: fpv_config

    verifications:
      - name: "fpv_covered"
        desc: "Bounded proof claiming a spec coverage item"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        top: "mod_a"
        properties:
          - "mod_a_props.sv"
        mode: "bmc"
        covers:
          - "A-COV-1"
          - "A-COV-2"
      - name: "fpv_uncovered"
        desc: "No coverage claims"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        top: "mod_a"
        mode: "bmc"
""")


def _write_covers_project(tmp_path):
    """A minimal project root with an fpv flow claiming coverage items."""
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    (tmp_path / "fpv.yaml").write_text(_COVERS_SUITE_YAML)
    (tmp_path / "fpv_regression.yaml").write_text(
        "rtl-buddy-filetype: fpv_reg_config\nfpv-configs:\n  - fpv.yaml\n"
    )
    return tmp_path


def test_fpv_config_covers_defaults_to_none():
    assert _make_fpv_cfg().covers is None


def test_fpv_suite_config_parses_covers(tmp_path):
    """`covers:` mirrors tests.yaml: same field name, same attribute, so
    `build_coverage_map` reads a run exactly as it reads a test."""
    _write_covers_project(tmp_path)
    cfg = FpvSuiteConfig(str(tmp_path / "fpv.yaml"))
    assert cfg.get_verifications("fpv_covered")[0].covers == ["A-COV-1", "A-COV-2"]
    assert cfg.get_verifications("fpv_uncovered")[0].covers is None


def test_discover_fpv_verifications_feeds_the_coverage_map(tmp_path):
    from rtl_buddy.tools.spec_trace import (
        build_coverage_map,
        discover_fpv_verifications,
    )

    _write_covers_project(tmp_path)
    entries, failures = discover_fpv_verifications(str(tmp_path))
    assert failures == []
    assert [(Path(p).name, v.get_name()) for p, v in entries] == [
        ("fpv.yaml", "fpv_covered"),
        ("fpv.yaml", "fpv_uncovered"),
    ]
    cov_map = build_coverage_map(entries)
    assert {item: [name for _, name in hits] for item, hits in cov_map.items()} == {
        "A-COV-1": ["fpv_covered"],
        "A-COV-2": ["fpv_covered"],
    }


def test_discover_fpv_verifications_without_a_regression_is_empty(tmp_path):
    from rtl_buddy.tools.spec_trace import discover_fpv_verifications

    assert discover_fpv_verifications(str(tmp_path)) == ([], [])


def test_discover_fpv_verifications_reports_a_broken_regression(tmp_path):
    from rtl_buddy.tools.spec_trace import discover_fpv_verifications

    (tmp_path / "fpv_regression.yaml").write_text(
        "rtl-buddy-filetype: fpv_reg_config\nfpv-configs:\n  - missing/fpv.yaml\n"
    )
    entries, failures = discover_fpv_verifications(str(tmp_path))
    assert entries == []
    assert [Path(f).name for f in failures] == ["fpv_regression.yaml"]


def test_spec_check_coverage_counts_an_fpv_run(tmp_path, monkeypatch):
    """`rb spec check-coverage` sees formal `covers:` claims (#385)."""
    import json

    from typer.testing import CliRunner

    from rtl_buddy.rtl_buddy import RtlBuddy

    _write_covers_project(tmp_path)
    spec_dir = tmp_path / "spec" / "mod_a"
    spec_dir.mkdir(parents=True)
    (spec_dir / "specs.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: spec_config

        blocks:
          - name: "mod_a"
            desc: "Block covered only by a formal run"
            coverage-items:
              - id: "A-COV-1"
                desc: "Claimed by fpv_covered"
              - id: "A-COV-3"
                desc: "Claimed by nothing"
    """)
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    rb = RtlBuddy(name="test_fpv_check_coverage")
    result = runner.invoke(rb.app, ["--machine", "spec", "check-coverage"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    items = {i["id"]: i for i in json.loads(lines[-1])["payload"]["items"]}
    assert items["A-COV-1"]["covered"] is True
    assert [t["test"] for t in items["A-COV-1"]["tests"]] == ["fpv_covered"]
    assert items["A-COV-1"]["tests"][0]["path"].endswith("fpv.yaml")
    assert items["A-COV-3"]["covered"] is False


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
    sources, incdirs, _defines = sby._parse_filelist(str(fl))
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
    sources, incdirs, _defines = sby._parse_filelist(str(fl))
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
    sources, incdirs, _defines = sby._parse_filelist(str(fl))
    content = Path(sby._write_sby_file(sources, incdirs)).read_text()
    # Exactly two read statements: design + props.
    assert content.count("read -sv -formal ") == 2


def test_sby_fpv_incdir_in_model_filelist_reaches_sby_as_include_dir(tmp_path):
    """Regression: a `+incdir+` entry in the model filelist must reach the
    generated sby script as an include directory, not be misclassified as a
    source. _write_filelist emits the filelist and _parse_filelist re-reads it;
    that round-trip must preserve the +incdir+ marker. (write_output strip=True
    used to drop it, collapsing the dir to a bare path that _parse_filelist then
    treated as a missing source file — so the include never resolved.)"""
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.tools.sby_fpv import SbyFpv

    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "defs.svh").write_text("// defs\n")
    src = tmp_path / "top.sv"
    src.write_text('`include "defs.svh"\nmodule top(); endmodule\n')
    models = tmp_path / "models.yaml"
    models.write_text("rtl-buddy-filetype: model_config\n")

    model = ModelConfig(
        name="top", filelist=["+incdir+inc", "top.sv"], path=str(models)
    )
    fpv_cfg = FpvConfig(
        name="demo",
        desc="t",
        model=model,
        tool="sby",
        top="top",
        properties=[],
        constraints=None,
        mode="bmc",
        depth=20,
        engines=["smtbmc yices"],
        _reglvl=None,
        tool_overrides=None,
    )
    sby = SbyFpv(
        name="t/sby", fpv_cfg=fpv_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )

    sources, incdirs, _defines = sby._parse_filelist(sby._write_filelist())

    # the include dir is captured as an incdir, NOT a (missing) source
    assert any(Path(d).resolve() == inc.resolve() for d in incdirs)
    assert all(Path(s).resolve() != inc.resolve() for s in sources)
    assert any(Path(s).name == "top.sv" for s in sources)

    # and it reaches the rendered sby script as a yosys include directive
    content = Path(sby._write_sby_file(sources, incdirs)).read_text()
    assert "verilog_defaults -add -I " in content
    assert any(str(Path(d)) in content for d in incdirs)


# ---------------------------------------------------------------------------
# Vacuous-PASS guard (#260): a bind-based property file that the verilog
# frontend silently drops elaborates zero formal cells. The generated
# script must assert at least one formal cell exists after `prep` so sby
# errors loud instead of reporting a vacuous PASS.
# ---------------------------------------------------------------------------


def _sby_for(tmp_path, *, properties, frontend="verilog"):
    """Build an SbyFpv whose filelist points at a single design source."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("module design(); endmodule\n")
    fl = tmp_path / "artefacts" / "demo" / "fpv.f"
    fl.parent.mkdir(parents=True)
    fl.write_text(f"{src}\n")

    fpv_cfg = _make_fpv_cfg(name="demo", top="design", properties=properties)
    fpv_cfg.frontend = frontend
    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=fpv_cfg,
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    sources, incdirs, _defines = sby._parse_filelist(str(fl))
    return sby, sources, incdirs


def test_sby_fpv_emits_formal_cell_guard_when_properties_listed(tmp_path):
    """A suite with `properties:` must guard against zero formal cells:
    a `select -assert-min 1 ...` after `prep`, then `select -clear` to
    restore the full selection for sby's engine passes."""
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    sby, sources, incdirs = _sby_for(tmp_path, properties=[str(props)])
    content = Path(sby._write_sby_file(sources, incdirs)).read_text()

    script = content.split("[script]")[1].split("[files]")[0]
    prep_pos = script.index("prep -top design")
    guard_pos = script.index("select -assert-min 1 ")
    clear_pos = script.index("select -clear")
    # Guard lands after prep and is followed by the selection restore.
    assert prep_pos < guard_pos < clear_pos
    # Covers both the unified `$check` cell and the legacy dedicated
    # formal-cell types so the guard works across yosys generations.
    for cell in ("t:$assert", "t:$assume", "t:$cover", "t:$live", "t:$check"):
        assert cell in script


def test_sby_fpv_no_guard_for_inline_assertion_suite(tmp_path):
    """`properties: []` (inline-assertion DUTs) are not bind-based, so
    the guard is not emitted — it would false-positive on suites whose
    asserts live in the design source."""
    sby, sources, incdirs = _sby_for(tmp_path, properties=[])
    content = Path(sby._write_sby_file(sources, incdirs)).read_text()
    assert "select -assert-min" not in content


def test_vacuous_guard_hint_flags_dropped_bind(tmp_path):
    """When the guard trips, the ERROR description must explain that zero
    formal cells elaborated and point at `frontend: slang` for the
    verilog-frontend bind case."""
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    sby, _, _ = _sby_for(tmp_path, properties=[str(props)], frontend="verilog")

    log_path = tmp_path / "fpv.log"
    log_path.write_text(
        "SBY [wd] base: ERROR: Assertion failed: selection contains 0 "
        "elements, less than the minimum number 1: t:$assert\n"
    )
    hint = sby._vacuous_guard_hint(str(log_path), str(tmp_path / "missing_wd"))
    assert "zero formal cells" in hint
    assert "frontend: slang" in hint
    assert "verilog" in hint


def test_vacuous_guard_hint_slang_omits_frontend_advice(tmp_path):
    """On the slang frontend the bind already resolves, so a zero-cell
    error is something else — don't suggest switching frontend."""
    props = tmp_path / "props.sv"
    props.write_text("// SVA properties\n")
    sby, _, _ = _sby_for(tmp_path, properties=[str(props)], frontend="slang")

    log_path = tmp_path / "fpv.log"
    log_path.write_text("ERROR: ... selection contains 0 elements ...\n")
    hint = sby._vacuous_guard_hint(str(log_path), str(tmp_path / "missing_wd"))
    assert "zero formal cells" in hint
    assert "frontend: slang" not in hint


def test_vacuous_guard_hint_silent_on_unrelated_error(tmp_path):
    """An ERROR without the empty-selection signature keeps its plain
    description — the hint must not fire on every failure."""
    sby, _, _ = _sby_for(tmp_path, properties=["/x/props.sv"])
    log_path = tmp_path / "fpv.log"
    log_path.write_text("ERROR: syntax error near token 'always'\n")
    assert sby._vacuous_guard_hint(str(log_path), str(tmp_path / "missing_wd")) == ""


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
    sources, incdirs, _defines = sby._parse_filelist(str(fl))
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


# ---------------------------------------------------------------------------
# fpv_log_parse — per-engine summary extraction from sby logfile.txt
# ---------------------------------------------------------------------------


_REAL_LOG_PASS = dedent("""\
    SBY 11:10:18 [wd] Removing directory 'wd'.
    SBY 11:10:18 [wd] engine_0: smtbmc yices
    SBY 11:10:18 [wd] engine_0: starting process "..."
    SBY 11:10:18 [wd] engine_0: ##   0:00:00  Status: passed
    SBY 11:10:18 [wd] engine_0: finished (returncode=0)
    SBY 11:10:18 [wd] summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:00 (0)
    SBY 11:10:18 [wd] summary: Elapsed process time [H:MM:SS (secs)]: 0:00:00 (0)
    SBY 11:10:18 [wd] summary: engine_0 (smtbmc yices) returned pass
    SBY 11:10:18 [wd] summary: engine_0 did not produce any traces
    SBY 11:10:18 [wd] DONE (PASS, rc=0)
""")


def test_parse_engine_summary_single_engine_pass():
    from rtl_buddy.tools.fpv_log_parse import parse_engine_summary

    engines = parse_engine_summary(_REAL_LOG_PASS)
    assert engines == [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0}
    ]


def test_parse_engine_summary_multi_engine_mixed():
    log = dedent("""\
        SBY summary: engine_0 (smtbmc yices) returned pass
        SBY summary: engine_0 did not produce any traces
        SBY summary: engine_1 (smtbmc z3) returned fail
        SBY summary: engine_1 produced 1 trace
        SBY summary: engine_2 (abc pdr) returned pass
        SBY summary: engine_2 did not produce any traces
    """)
    from rtl_buddy.tools.fpv_log_parse import parse_engine_summary

    engines = parse_engine_summary(log)
    assert engines == [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0},
        {"idx": 1, "spec": "smtbmc z3", "verdict": "fail", "trace_count": 1},
        {"idx": 2, "spec": "abc pdr", "verdict": "pass", "trace_count": 0},
    ]


def test_parse_engine_summary_unsorted_lines_sort_by_index():
    log = dedent("""\
        SBY summary: engine_2 (abc pdr) returned pass
        SBY summary: engine_0 (smtbmc yices) returned pass
        SBY summary: engine_1 (smtbmc z3) returned pass
    """)
    from rtl_buddy.tools.fpv_log_parse import parse_engine_summary

    engines = parse_engine_summary(log)
    assert [e["idx"] for e in engines] == [0, 1, 2]


def test_parse_engine_summary_multiple_traces():
    log = "SBY summary: engine_0 produced 3 traces\n"
    from rtl_buddy.tools.fpv_log_parse import parse_engine_summary

    engines = parse_engine_summary(log)
    assert engines == [{"idx": 0, "spec": None, "verdict": None, "trace_count": 3}]


def test_parse_engine_summary_empty_log_returns_empty():
    from rtl_buddy.tools.fpv_log_parse import parse_engine_summary

    assert parse_engine_summary("") == []
    assert parse_engine_summary("no summary lines here\n") == []


def test_parse_elapsed_seconds_extracts_secs():
    from rtl_buddy.tools.fpv_log_parse import parse_elapsed_seconds

    assert parse_elapsed_seconds(_REAL_LOG_PASS) == 0
    assert (
        parse_elapsed_seconds(
            "SBY summary: Elapsed clock time [H:MM:SS (secs)]: 0:01:23 (83)\n"
        )
        == 83
    )


def test_parse_elapsed_seconds_missing_returns_none():
    from rtl_buddy.tools.fpv_log_parse import parse_elapsed_seconds

    assert parse_elapsed_seconds("no elapsed line\n") is None


def test_read_workdir_log_missing_file_returns_none(tmp_path):
    from rtl_buddy.tools.fpv_log_parse import read_workdir_log

    assert read_workdir_log(str(tmp_path)) is None


def test_read_workdir_log_returns_file_contents(tmp_path):
    from rtl_buddy.tools.fpv_log_parse import read_workdir_log

    (tmp_path / "logfile.txt").write_text("hello\n")
    assert read_workdir_log(str(tmp_path)) == "hello\n"


def test_summarize_engines_no_data():
    from rtl_buddy.tools.fpv_log_parse import summarize_engines

    assert summarize_engines([]) == "no engine data"


def test_summarize_engines_single_engine_named():
    from rtl_buddy.tools.fpv_log_parse import summarize_engines

    assert (
        summarize_engines(
            [{"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0}]
        )
        == "1/1 pass (smtbmc yices)"
    )


def test_summarize_engines_all_pass_multi():
    from rtl_buddy.tools.fpv_log_parse import summarize_engines

    engines = [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0},
        {"idx": 1, "spec": "smtbmc z3", "verdict": "pass", "trace_count": 0},
    ]
    assert summarize_engines(engines) == "2/2 pass"


def test_summarize_engines_partial_pass_names_winner():
    from rtl_buddy.tools.fpv_log_parse import summarize_engines

    engines = [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0},
        {"idx": 1, "spec": "smtbmc z3", "verdict": "fail", "trace_count": 1},
    ]
    assert summarize_engines(engines) == "1/2 pass (smtbmc yices won)"


def test_summarize_engines_all_fail():
    from rtl_buddy.tools.fpv_log_parse import summarize_engines

    engines = [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "fail", "trace_count": 1},
        {"idx": 1, "spec": "smtbmc z3", "verdict": "fail", "trace_count": 1},
    ]
    assert summarize_engines(engines) == "0/2 pass"


# ---------------------------------------------------------------------------
# SbyFpv._read_per_engine — integration with the workdir
# ---------------------------------------------------------------------------


def test_sby_fpv_read_per_engine_parses_real_log(tmp_path):
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    workdir.mkdir()
    (workdir / "logfile.txt").write_text(_REAL_LOG_PASS)
    engines = SbyFpv._read_per_engine(str(workdir))
    assert engines == [
        {"idx": 0, "spec": "smtbmc yices", "verdict": "pass", "trace_count": 0}
    ]


def test_sby_fpv_read_per_engine_no_logfile_returns_empty(tmp_path):
    from rtl_buddy.tools.sby_fpv import SbyFpv

    workdir = tmp_path / "sby_workdir"
    workdir.mkdir()
    assert SbyFpv._read_per_engine(str(workdir)) == []


# ---------------------------------------------------------------------------
# xfail (expected-fail) — schema + result re-interpretation
# ---------------------------------------------------------------------------

_XFAIL_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: fpv_config

    verifications:
      - name: "fpv_xfail"
        desc: "Expected-fail, non-strict"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        mode: "prove"
        depth: 20
        xfail: true
      - name: "fpv_xfail_strict"
        desc: "Expected-fail, strict"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        mode: "prove"
        depth: 20
        xfail_strict: true
      - name: "fpv_normal"
        desc: "A normal verification"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        mode: "bmc"
        depth: 20
""")


def test_fpv_config_xfail_flags_default_false():
    cfg = _make_fpv_cfg()
    assert cfg.get_xfail() is False
    assert cfg.get_xfail_strict() is False
    assert cfg.is_xfail() is False


def test_fpv_suite_config_loads_xfail_flags(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "fpv.yaml"
    suite_yaml.write_text(_XFAIL_SUITE_YAML)
    cfg = FpvSuiteConfig(str(suite_yaml))

    nonstrict = cfg.get_verifications("fpv_xfail")[0]
    assert nonstrict.is_xfail() is True
    assert nonstrict.get_xfail_strict() is False

    strict = cfg.get_verifications("fpv_xfail_strict")[0]
    assert strict.is_xfail() is True  # either flag enables xfail
    assert strict.get_xfail_strict() is True

    normal = cfg.get_verifications("fpv_normal")[0]
    assert normal.is_xfail() is False


def test_apply_xfail_fail_becomes_xfail_and_passes():
    from rtl_buddy.runner.fpv_results import FpvFailResults
    from rtl_buddy.runner.xfail import apply_xfail

    for strict in (False, True):
        res = FpvFailResults(name="t", mode="prove", depth=20)
        assert res.is_pass() is False
        apply_xfail(res, strict=strict)
        assert res.results["result"] == "XFAIL"
        assert res.is_pass() is True  # XFAIL passes regardless of strictness
        assert res.results["desc"].startswith("xfail (expected fail): ")


def test_apply_xfail_nonstrict_xpass_still_passes():
    from rtl_buddy.runner.fpv_results import FpvPassResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = FpvPassResults(name="t", mode="prove", depth=20)
    apply_xfail(res, strict=False)
    assert res.results["result"] == "XPASS"
    assert res.is_pass() is True  # non-strict: an XPASS does not fail the run
    assert res.results["desc"].startswith("XPASS (expected fail but passed): ")


def test_apply_xfail_strict_xpass_fails():
    from rtl_buddy.runner.fpv_results import FpvPassResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = FpvPassResults(name="t", mode="prove", depth=20)
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert res.is_pass() is False  # strict: a stale xfail surfaces loudly
    assert res.results["desc"].startswith(
        "XPASS (expected fail but passed — strict, failing): "
    )


def test_apply_xfail_skip_passes_through_unchanged():
    from rtl_buddy.runner.fpv_results import FpvSkipResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = FpvSkipResults(name="t", desc="below reg level")
    apply_xfail(res, strict=True)
    assert res.results["result"] == "SKIP"
    assert res.is_pass() is True


# ---------------------------------------------------------------------------
# `+define+` in a model filelist (#305): a preprocessor define is an option,
# not a source path. It must survive the VlogFilelist round-trip unresolved
# and reach the generated script as a `-D` flag on whichever frontend runs.
# ---------------------------------------------------------------------------


def test_sby_fpv_parse_filelist_extracts_defines(tmp_path):
    """`+define+NAME` / `+define+NAME=VALUE` / the multi `+define+A+B=C` form
    become defines, never (missing) source files."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+define+VERILATOR\n+define+WIDTH=8\n+define+A+B=3\n{src.name}\n")

    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=_make_fpv_cfg(),
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    sources, incdirs, defines = sby._parse_filelist(str(fl))

    assert defines == ["VERILATOR", "WIDTH=8", "A", "B=3"]
    assert incdirs == []
    # the design source is the only thing treated as a path
    assert [Path(s).name for s in sources] == ["design.sv"]


def test_sby_fpv_parse_filelist_drops_reserved_formal_define(tmp_path, caplog):
    """rtl-buddy owns FORMAL: a filelist define for it is dropped with a
    warning rather than silently changing what `ifdef FORMAL elaborates.
    The two frontends do not even agree on which duplicate `-D` wins
    (verilog keeps the last, yosys-slang the first), so the only safe
    answer is to refuse the override."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+define+FORMAL=0\n+define+KEEP=1\n{src.name}\n")

    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=_make_fpv_cfg(),
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    with caplog.at_level("WARNING"):
        _sources, _incdirs, defines = sby._parse_filelist(str(fl))

    assert defines == ["KEEP=1"]
    assert "FORMAL" in caplog.text and "cannot be overridden" in caplog.text


def test_sby_fpv_parse_filelist_drops_a_define_it_cannot_express(tmp_path, caplog):
    """A define value carrying whitespace has no honourable rendering.

    Both renderers splice the token into a yosys *script* line, which yosys
    tokenises on whitespace, so `+define+MSG=hello world` becomes
    `-DMSG=hello` plus a stray `world` argument and the failure surfaces as
    an unrelated read_slang error deep in `fpv.log`. Drop it where the
    message can still name the entry — the same shape as the reserved-name
    rule, and the outcome the FORMAL handling exists to avoid."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+define+MSG=hello world\n+define+KEEP=1\n{src.name}\n")

    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=_make_fpv_cfg(),
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    with caplog.at_level("WARNING"):
        _sources, _incdirs, defines = sby._parse_filelist(str(fl))

    assert defines == ["KEEP=1"]
    assert "MSG=hello world" in caplog.text
    assert "whitespace" in caplog.text


def test_sby_fpv_parse_filelist_collapses_a_redefined_name_to_the_last(
    tmp_path, caplog
):
    """Two definitions of one name is the reserved-name failure in a new
    costume: yosys's verilog frontend keeps the LAST `-D` and yosys-slang
    keeps the FIRST, so passing both through proves `WIDTH=16` on one
    frontend and `WIDTH=8` on the other, silently. Easy to reach through a
    `-F` chain pulling in two vendor filelists. Last wins — filelist
    convention, and what the verilog frontend would have done anyway."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+define+WIDTH=8\n+define+KEEP=1\n+define+WIDTH=16\n{src.name}\n")

    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=_make_fpv_cfg(),
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    with caplog.at_level("WARNING"):
        _sources, _incdirs, defines = sby._parse_filelist(str(fl))

    # One WIDTH, the last one, and it keeps the position of the first
    # appearance so a duplicate-free filelist is byte-identical.
    assert defines == ["WIDTH=16", "KEEP=1"]
    assert "WIDTH" in caplog.text and "dropping" in caplog.text


def test_sby_fpv_parse_filelist_is_quiet_about_an_identical_repeat(tmp_path, caplog):
    """The same define twice with the same value changes nothing, so it is
    deduped without a warning — a `-F` chain that includes one common
    filelist twice is ordinary, not a mistake."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("// design")
    fl = tmp_path / "fpv.f"
    fl.write_text(f"+define+WIDTH=8\n+define+WIDTH=8\n{src.name}\n")

    sby = SbyFpv(
        name="t/sby",
        fpv_cfg=_make_fpv_cfg(),
        tool_cfg=_tool_cfg(),
        suite_dir=str(tmp_path),
    )
    with caplog.at_level("WARNING"):
        _sources, _incdirs, defines = sby._parse_filelist(str(fl))

    assert defines == ["WIDTH=8"]
    assert "dropping" not in caplog.text


def test_sby_fpv_define_in_model_filelist_survives_write_round_trip(tmp_path):
    """Regression for #305: `+define+FOO` in models.yaml used to be resolved
    as a file path by VlogFilelist and fail with "filelist source missing"
    before _parse_filelist ever saw it."""
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "top.sv"
    src.write_text("module top(); endmodule\n")
    models = tmp_path / "models.yaml"
    models.write_text("rtl-buddy-filetype: model_config\n")

    model = ModelConfig(
        name="top",
        filelist=["+define+VERILATOR", "+define+WIDTH=8", "top.sv"],
        path=str(models),
    )
    fpv_cfg = FpvConfig(
        name="demo",
        desc="t",
        model=model,
        tool="sby",
        top="top",
        properties=[],
        constraints=None,
        mode="bmc",
        depth=20,
        engines=["smtbmc yices"],
        _reglvl=None,
        tool_overrides=None,
    )
    sby = SbyFpv(
        name="t/sby", fpv_cfg=fpv_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )

    sources, incdirs, defines = sby._parse_filelist(sby._write_filelist())

    assert defines == ["VERILATOR", "WIDTH=8"]
    assert [Path(s).name for s in sources] == ["top.sv"]
    assert incdirs == []

    # ... and reaches the verilog frontend as a yosys define directive.
    content = Path(sby._write_sby_file(sources, incdirs, defines)).read_text()
    assert "verilog_defaults -add -DVERILATOR" in content
    assert "verilog_defaults -add -DWIDTH=8" in content


# ---------------------------------------------------------------------------
# `params:` — reduced-configuration proofs (#359)
# ---------------------------------------------------------------------------


_PARAMS_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: fpv_config

    verifications:
      - name: "mod_a_k8"
        desc: "Reduced-configuration proof at K=8"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "sby"
        top: "mod_a"
        mode: "bmc"
        depth: 24
        params:
          K: 8
          WIDTH: "8'h20"
          ENABLE: true
""")


def _write_params_suite(tmp_path, body=None):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "fpv.yaml"
    suite_yaml.write_text(body or _PARAMS_SUITE_YAML)
    return suite_yaml


def test_fpv_params_default_is_empty():
    assert _make_fpv_cfg().get_params() == {}
    assert _make_fpv_cfg().get_param_tokens() == []


def test_fpv_suite_config_loads_params(tmp_path):
    cfg = FpvSuiteConfig(str(_write_params_suite(tmp_path)))
    v = cfg.get_verifications("mod_a_k8")[0]
    assert v.get_params() == {"K": 8, "WIDTH": "8'h20", "ENABLE": True}


def test_fpv_param_tokens_render_scalars_for_yosys(tmp_path):
    """int -> decimal, bool -> 1/0 (SystemVerilog has no bare `true`),
    string -> verbatim expression text so a sized literal survives. Order
    is declaration order so the generated script is stable."""
    cfg = FpvSuiteConfig(str(_write_params_suite(tmp_path)))
    v = cfg.get_verifications("mod_a_k8")[0]
    assert v.get_param_tokens() == [
        ("K", "8"),
        ("WIDTH", "8'h20"),
        ("ENABLE", "1"),
    ]


@pytest.mark.parametrize(
    "params_block,match",
    [
        # non-scalar values: a parameter override is one token
        ("params:\n      K: [1, 2]\n", "must be an integer"),
        ("params:\n      K: {a: 1}\n", "must be an integer"),
        ("params:\n      K: 1.5\n", "must be an integer"),
        ("params:\n      K: null\n", "must be an integer"),
        # not a map at all — pyserde rejects the shape before the
        # validator sees it, so the suite load is what fails
        ("params: 8\n", "failed to load"),
        ("params:\n      - K\n", "failed to load"),
        # a value yosys could not tokenise: it splits script lines on
        # whitespace, and quotes inside a token are not grouping characters
        ('params:\n      K: "8 + 1"\n', "may not contain"),
        ('params:\n      K: ""\n', "empty"),
        # `#` starts a yosys comment for the rest of the LINE, mid-line
        # included, so a value carrying one silently swallows the source
        # files that follow it on the read line.
        ('params:\n      K: "4#"\n', "may not contain"),
        # `;` does not separate commands in a script file — it reaches the
        # frontend and dies as a syntax error inside the design source,
        # with nothing pointing back at fpv.yaml.
        ('params:\n      K: "4;stat"\n', "may not contain"),
        # PyYAML is YAML 1.1: a bare `on:` key parses as the boolean True,
        # which is not an identifier — caught here rather than emitted as
        # `chparam -set True ...`
        ("params:\n      on: 1\n", "identifier"),
        ("params:\n      2FOO: 1\n", "identifier"),
    ],
)
def test_fpv_params_rejected_shapes(tmp_path, params_block, match):
    body = (
        dedent("""\
        rtl-buddy-filetype: fpv_config

        verifications:
          - name: "v"
            desc: "d"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "sby"
            top: "mod_a"
            %s
    """)
        % params_block.replace("\n", "\n        ").rstrip()
    )
    with pytest.raises(FatalRtlBuddyError, match=match):
        FpvSuiteConfig(str(_write_params_suite(tmp_path, body)))


def test_fpv_validate_params_rejects_non_mapping():
    """Reachable only from a hand-built FpvConfigFile — pyserde catches the
    shape first when the value comes from YAML."""
    from rtl_buddy.config.fpv import validate_params

    with pytest.raises(FatalRtlBuddyError, match="must be a map"):
        validate_params("v", ["K"])


# ---------------------------------------------------------------------------
# End-to-end: the generated script really elaborates the reduced
# configuration. Runs yosys itself (no sby / no solver needed — the
# question is whether the override reached elaboration), and is skipped
# when yosys is not installed. The slang half additionally needs
# RTL_BUDDY_SLANG_PLUGIN, the same env var the runner honours.
# ---------------------------------------------------------------------------


_PARAM_DUT = dedent("""\
    module ctr #(parameter int K = 16) (
      input  logic clk,
      input  logic rst_n,
      output logic [K-1:0] cnt
    );
      always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) cnt <= '0;
        else if (cnt != {K{1'b1}}) cnt <= cnt + 1'b1;
      `ifdef FORMAL
      always @(posedge clk) assert property (cnt <= {K{1'b1}});
      `endif
    endmodule
""")


def _yosys_port_bits(sby_path, work_dir) -> int:
    """Run the `[script]` section of a generated .sby through yosys and
    return the top module's port-bit count."""
    import re
    import shutil
    import subprocess

    text = Path(sby_path).read_text()
    script = text.split("[script]", 1)[1].split("[files]", 1)[0].strip()
    # The sby script names sources by basename (sby stages them in its
    # workdir); stage them the same way here.
    for src in text.split("[files]", 1)[1].strip().splitlines():
        if src.strip():
            shutil.copy(src.strip(), Path(work_dir) / Path(src.strip()).name)
    ys = Path(work_dir) / "run.ys"
    ys.write_text(script + "\nstat\n")
    res = subprocess.run(
        ["yosys", "-s", str(ys)],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    m = re.search(r"^\s*(\d+)\s+port bits\s*$", res.stdout, re.MULTILINE)
    assert m, res.stdout
    return int(m.group(1))


def _render_param_proof(tmp_path, frontend, params, plugin_path=None):
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.tools.sby_fpv import SbyFpv

    (tmp_path / "ctr.sv").write_text(_PARAM_DUT)
    (tmp_path / "models.yaml").write_text("rtl-buddy-filetype: model_config\n")
    model = ModelConfig(
        name="ctr", filelist=["ctr.sv"], path=str(tmp_path / "models.yaml")
    )
    fpv_cfg = FpvConfig(
        name="ctr_proof",
        desc="t",
        model=model,
        tool="sby",
        top="ctr",
        properties=[],
        constraints=None,
        mode="bmc",
        depth=8,
        engines=["smtbmc yices"],
        _reglvl=None,
        tool_overrides=None,
        frontend=frontend,
        params=dict(params),
    )
    tool_cfg = FpvToolConfig(
        FpvToolConfigFile(
            name="sby",
            tool="sby",
            opts=FpvToolOptsFile(plugin_path=plugin_path),
        )
    )
    suite_dir = tmp_path / frontend
    suite_dir.mkdir()
    sby = SbyFpv(
        name="t/sby", fpv_cfg=fpv_cfg, tool_cfg=tool_cfg, suite_dir=str(suite_dir)
    )
    sources, incdirs, defines = sby._parse_filelist(sby._write_filelist())
    return sby._write_sby_file(sources, incdirs, defines)


def _yosys_missing():
    import shutil

    return shutil.which("yosys") is None


@pytest.mark.skipif(_yosys_missing(), reason="yosys not installed")
def test_params_reduce_elaboration_verilog_frontend(tmp_path):
    """`chparam` in the generated script must actually shrink the design:
    clk + rst_n + cnt[K-1:0] is 18 port bits at the default K=16 and 6 at
    K=4."""
    sby_path = _render_param_proof(tmp_path, "verilog", {"K": 4})
    work = tmp_path / "run_verilog"
    work.mkdir()
    assert _yosys_port_bits(sby_path, work) == 6


@pytest.mark.skipif(_yosys_missing(), reason="yosys not installed")
def test_no_params_keeps_default_elaboration_verilog_frontend(tmp_path):
    sby_path = _render_param_proof(tmp_path, "verilog", {})
    work = tmp_path / "run_verilog"
    work.mkdir()
    assert _yosys_port_bits(sby_path, work) == 18


@pytest.mark.skipif(
    _yosys_missing() or not os.environ.get("RTL_BUDDY_SLANG_PLUGIN"),
    reason="yosys + RTL_BUDDY_SLANG_PLUGIN required",
)
def test_params_reduce_elaboration_slang_frontend(tmp_path):
    """`read_slang -G` is the slang-side mechanism — `chparam` cannot work
    there, since slang has already elaborated the module by then."""
    sby_path = _render_param_proof(
        tmp_path,
        "slang",
        {"K": 4},
        plugin_path=os.environ["RTL_BUDDY_SLANG_PLUGIN"],
    )
    work = tmp_path / "run_slang"
    work.mkdir()
    assert _yosys_port_bits(sby_path, work) == 6


def test_fpv_get_params_hands_back_a_copy(tmp_path):
    """The returned dict is stamped onto a graph node and serialized, so
    config state must not share an object with the payload."""
    cfg = FpvSuiteConfig(str(_write_params_suite(tmp_path)))
    v = cfg.get_verifications("mod_a_k8")[0]
    before = v.get_params()

    mutated = v.get_params()
    mutated["K"] = 999
    mutated["INJECTED"] = 1

    assert v.get_params() == before
