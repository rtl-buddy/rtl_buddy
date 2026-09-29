"""Tests for `frontend: slang` and `plugin-path` plumbing in `rb fpv`.

Covers the per-verification schema field, the tool-config plugin-path field, the
`_render_sby` slang script and the `fpv_coi.build_yosys_script` slang script. Only
generated script content is checked; end-to-end execution runs in the project
template demo.
"""

from __future__ import annotations

import pytest

from rtl_buddy.config.fpv import (
    FpvConfig,
    FpvConfigFile,
    FpvSuiteConfig,
    FpvToolConfig,
    FpvToolConfigFile,
    FpvToolOptsFile,
)
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.fpv_coi import build_yosys_script
from rtl_buddy.tools.sby_fpv import SbyFpv


_STUB_MODELS_YAML = """\
rtl-buddy-filetype: model_config
models:
  - name: m
    filelist: ["-v m.sv"]
"""


def _config_file(**overrides) -> FpvConfigFile:
    base = dict(
        name="v",
        desc="d",
        model="m",
        model_path="models.yaml",
        tool="sby",
        top="m",
        properties=[],
        mode="bmc",
        depth=10,
        engines=["smtbmc yices"],
        reglvl=None,
        tool_overrides=None,
    )
    base.update(overrides)
    return FpvConfigFile(**base)


def _seeded_dir(tmp_path):
    (tmp_path / "models.yaml").write_text(_STUB_MODELS_YAML)
    (tmp_path / "m.sv").write_text("module m; endmodule\n")
    return tmp_path


def test_fpv_config_default_frontend_is_verilog(tmp_path):
    d = _seeded_dir(tmp_path)
    cfg = _config_file().initialise(str(d))
    assert cfg.get_frontend() == "verilog"


def test_fpv_config_accepts_slang_frontend(tmp_path):
    d = _seeded_dir(tmp_path)
    cfg = _config_file(frontend="slang").initialise(str(d))
    assert cfg.get_frontend() == "slang"


def test_fpv_config_rejects_unknown_frontend(tmp_path):
    d = _seeded_dir(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="frontend"):
        _config_file(frontend="bogus").initialise(str(d))


def test_tool_config_default_plugin_path_is_none():
    cfg = FpvToolConfig(
        FpvToolConfigFile(name="sby", tool="sby", opts=FpvToolOptsFile())
    )
    assert cfg.get_opts().plugin_path is None


def test_tool_config_plugin_path_round_trips():
    cfg = FpvToolConfig(
        FpvToolConfigFile(
            name="sby",
            tool="sby",
            opts=FpvToolOptsFile(plugin_path="/opt/slang.so"),
        )
    )
    assert cfg.get_opts().plugin_path == "/opt/slang.so"


def test_tool_config_plugin_path_override_wins():
    cfg = FpvToolConfig(
        FpvToolConfigFile(
            name="sby",
            tool="sby",
            opts=FpvToolOptsFile(plugin_path="/opt/slang.so"),
        )
    )
    opts = cfg.get_opts({"plugin_path": "/etc/slang_custom.so"})
    assert opts.plugin_path == "/etc/slang_custom.so"


def _sby_with_frontend(
    tmp_path,
    *,
    frontend="verilog",
    plugin_path=None,
    properties=None,
) -> SbyFpv:
    model = ModelConfig(name="dut", filelist=[], path=str(tmp_path / "models.yaml"))
    fpv_cfg = FpvConfig(
        name="v",
        desc="d",
        model=model,
        tool="sby",
        top="dut",
        properties=list(properties or []),
        mode="bmc",
        depth=10,
        engines=["smtbmc yices"],
        _reglvl=None,
        constraints=None,
        tool_overrides=None,
        vacuity=None,
        coi=None,
        frontend=frontend,
    )
    tool_cfg = FpvToolConfig(
        FpvToolConfigFile(
            name="sby",
            tool="sby",
            opts=FpvToolOptsFile(plugin_path=plugin_path),
        )
    )
    return SbyFpv(name="t", fpv_cfg=fpv_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path))


def test_render_sby_verilog_emits_read_sv_formal(tmp_path):
    sby = _sby_with_frontend(tmp_path, frontend="verilog")
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "read -sv -formal dut.sv" in text
    assert "plugin -i" not in text
    assert "read_slang" not in text


def test_render_sby_slang_emits_plugin_and_read_slang(tmp_path):
    sby = _sby_with_frontend(
        tmp_path,
        frontend="slang",
        plugin_path="/path/to/slang.so",
    )
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "plugin -i /path/to/slang.so" in text
    # Single `read_slang --top <top> <files...>` so `bind` directives at
    # compilation-unit scope see every declared module; `--top` is required to pull in
    # bound submodules. `--no-synthesis-define -DFORMAL=1` mirrors `read -formal` so
    # in-RTL `ifdef FORMAL asserts survive preprocessing.
    assert (
        "read_slang --top dut --single-unit --no-synthesis-define -DFORMAL=1 dut.sv"
        in text
    )
    # The verilog-frontend command must not appear with slang, or yosys parses the
    # file through two frontends and duplicates $check cells.
    assert "read -sv -formal" not in text


def test_render_sby_slang_passes_single_unit(tmp_path):
    """The slang read uses --single-unit so the filelist is one compilation unit:
    macros carry across files and a compilation-unit-scope `bind` sees modules from
    other files.
    """
    sby = _sby_with_frontend(
        tmp_path,
        frontend="slang",
        plugin_path="/path/to/slang.so",
    )
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/a.sv", "/abs/b.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    read_line = next(ln for ln in text.splitlines() if ln.startswith("read_slang"))
    assert "--single-unit" in read_line
    # One read_slang invocation for the whole filelist.
    assert text.count("read_slang") == 1


def test_render_sby_slang_without_plugin_path_errors(tmp_path, monkeypatch):
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    monkeypatch.delenv(SLANG_PLUGIN_ENV, raising=False)
    sby = _sby_with_frontend(tmp_path, frontend="slang", plugin_path=None)
    out_path = str(tmp_path / "fpv.sby")
    # The error names both configuration channels.
    with pytest.raises(FatalRtlBuddyError, match="plugin-path"):
        sby._render_sby(
            output_path=out_path,
            sources=["/abs/dut.sv"],
            incdirs=[],
            mode="bmc",
            extra_property_files=[],
        )
    with pytest.raises(FatalRtlBuddyError, match=SLANG_PLUGIN_ENV):
        sby._render_sby(
            output_path=out_path,
            sources=["/abs/dut.sv"],
            incdirs=[],
            mode="bmc",
            extra_property_files=[],
        )


def test_render_sby_slang_env_fallback(tmp_path, monkeypatch):
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    monkeypatch.setenv(SLANG_PLUGIN_ENV, "/env/slang.so")
    sby = _sby_with_frontend(tmp_path, frontend="slang", plugin_path=None)
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "plugin -i /env/slang.so" in text


def test_render_sby_slang_config_wins_over_env(tmp_path, monkeypatch):
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    monkeypatch.setenv(SLANG_PLUGIN_ENV, "/env/slang.so")
    sby = _sby_with_frontend(
        tmp_path, frontend="slang", plugin_path="/configured/slang.so"
    )
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "plugin -i /configured/slang.so" in text
    assert "/env/slang.so" not in text


def test_build_yosys_script_verilog_default():
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=[],
        properties=[],
        constraints=None,
        top="dut",
    )
    assert "read -sv -formal dut.sv" in script
    assert "plugin -i" not in script
    assert "read_slang" not in script


def test_build_yosys_script_slang():
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=[],
        properties=["props.sv"],
        constraints=None,
        top="dut",
        frontend="slang",
        plugin_path="/p/slang.so",
    )
    assert "plugin -i /p/slang.so" in script
    # Single `read_slang --top <top> ... dut.sv props.sv` with the same `read -formal`
    # defines as the sby renderer; without them in-RTL `ifdef FORMAL asserts vanish
    # and COI reports 0%.
    assert (
        "read_slang --top dut --single-unit --no-synthesis-define -DFORMAL=1 dut.sv props.sv"
        in script
    )
    assert "read -sv -formal" not in script


def test_build_yosys_script_slang_passes_incdirs_on_read_slang():
    """For slang, include dirs are appended to the read_slang command as `-I <dir>`,
    because read_slang ignores `verilog_defaults -add -I`.
    """
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=["/inc/foo", "/inc/bar"],
        properties=[],
        constraints=None,
        top="dut",
        frontend="slang",
        plugin_path="/p/slang.so",
    )
    read_line = next(ln for ln in script.splitlines() if ln.startswith("read_slang"))
    assert "-I /inc/foo" in read_line
    assert "-I /inc/bar" in read_line
    # Not emitted as a (no-op for slang) verilog_defaults directive.
    assert "verilog_defaults" not in script


def test_render_sby_slang_passes_incdirs_on_read_slang(tmp_path):
    """The SbyFpv slang renderer puts incdirs on the read_slang command line, never in
    verilog_defaults.
    """
    sby = _sby_with_frontend(tmp_path, frontend="slang", plugin_path="/p/slang.so")
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=["/inc/foo"],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    read_line = next(ln for ln in text.splitlines() if ln.startswith("read_slang"))
    assert "-I /inc/foo" in read_line
    assert "verilog_defaults" not in text


def test_render_slang_read_shell_quotes_paths_with_spaces():
    """Paths with spaces on the read_slang line are shell-quoted because yosys
    tokenises script lines shell-style; simple names stay bare.
    """
    from rtl_buddy.tools.fpv_coi import render_slang_read

    line = render_slang_read("dut", ["/a b/inc"], ["/x y/dut.sv"])
    assert "-I '/a b/inc'" in line
    assert "'/x y/dut.sv'" in line

    bare = render_slang_read("dut", ["/inc"], ["dut.sv"])
    assert "-I /inc " in bare
    assert bare.endswith(" dut.sv")
    assert "'" not in bare


def test_build_yosys_script_slang_requires_plugin_path():
    with pytest.raises(ValueError, match="plugin_path"):
        build_yosys_script(
            sources=["dut.sv"],
            incdirs=[],
            properties=[],
            constraints=None,
            top="dut",
            frontend="slang",
            plugin_path=None,
        )


_FPV_YAML_SLANG = """\
rtl-buddy-filetype: fpv_config

verifications:
  - name: slang_proof
    desc: slang-fronted proof
    tool: sby
    model: dut
    model_path: models.yaml
    top: dut
    properties: []
    mode: bmc
    depth: 16
    frontend: slang
"""

_MODELS_YAML = """\
rtl-buddy-filetype: model_config
models:
  - name: dut
    filelist: ["-v dut.sv"]
"""


def test_yaml_round_trip_with_slang_frontend(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    fpv_yaml = tmp_path / "fpv.yaml"
    fpv_yaml.write_text(_FPV_YAML_SLANG)
    suite = FpvSuiteConfig(str(fpv_yaml))
    v = suite.get_verifications("slang_proof")[0]
    assert v.get_frontend() == "slang"


def test_render_sby_slang_emits_filelist_defines(tmp_path):
    """`+define+` entries reach the read_slang line as `-D` flags after the
    rtl-buddy-owned `-DFORMAL=1`, because yosys-slang keeps the first definition of
    a macro.
    """
    sby = _sby_with_frontend(
        tmp_path,
        frontend="slang",
        plugin_path="/path/to/slang.so",
    )
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        defines=["VERILATOR", "WIDTH=8"],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert (
        "read_slang --top dut --single-unit --no-synthesis-define -DFORMAL=1 "
        "-DVERILATOR -DWIDTH=8 dut.sv" in text
    )
    read_line = next(ln for ln in text.splitlines() if ln.startswith("read_slang"))
    assert read_line.index("-DFORMAL=1") < read_line.index("-DVERILATOR")


def test_render_sby_verilog_emits_filelist_defines(tmp_path):
    """The verilog frontend takes defines through `verilog_defaults`, like its include
    dirs; `read -formal` supplies FORMAL.
    """
    sby = _sby_with_frontend(tmp_path, frontend="verilog")
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        defines=["VERILATOR", "WIDTH=8"],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "verilog_defaults -add -DVERILATOR" in text
    assert "verilog_defaults -add -DWIDTH=8" in text
    # The define directives precede the reads they configure.
    assert text.index("verilog_defaults -add -DVERILATOR") < text.index(
        "read -sv -formal dut.sv"
    )
    assert "read_slang" not in text


def test_coi_script_carries_filelist_defines_slang(tmp_path):
    """The COI walk parses the design with the same defines as the proof."""
    script = build_yosys_script(
        sources=["/abs/dut.sv"],
        incdirs=[],
        properties=[],
        constraints=None,
        top="dut",
        frontend="slang",
        plugin_path="/path/to/slang.so",
        defines=["VERILATOR"],
    )
    assert "-DFORMAL=1 -DVERILATOR" in script


def test_coi_script_carries_filelist_defines_verilog(tmp_path):
    script = build_yosys_script(
        sources=["/abs/dut.sv"],
        incdirs=[],
        properties=[],
        constraints=None,
        top="dut",
        frontend="verilog",
        defines=["VERILATOR"],
    )
    assert "verilog_defaults -add -DVERILATOR" in script


# `params:` reduced-configuration proofs.
#
# yosys-slang elaborates during `read_slang`, so `chparam` afterwards fails in `prep`
# with "Module `X' is used with parameters but is not parametric!" (yosys 0.64 +
# yosys-slang). slang frontends therefore use slang's `-G` override, and the native
# verilog frontend uses `chparam` before `prep`.


def _sby_with_params(tmp_path, *, frontend, params, plugin_path=None) -> SbyFpv:
    sby = _sby_with_frontend(tmp_path, frontend=frontend, plugin_path=plugin_path)
    sby.fpv_cfg.params = dict(params)
    return sby


def test_render_sby_slang_emits_param_overrides_as_dash_g(tmp_path):
    sby = _sby_with_params(
        tmp_path,
        frontend="slang",
        params={"K": 8, "WIDTH": "8'h20"},
        plugin_path="/path/to/slang.so",
    )
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "-G K=8 -G WIDTH=8'h20" in text
    # chparam would abort the run on this frontend.
    assert "chparam" not in text


def test_render_sby_verilog_emits_chparam_after_read_before_prep(tmp_path):
    sby = _sby_with_params(tmp_path, frontend="verilog", params={"K": 8})
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    lines = [ln for ln in open(out_path).read().splitlines()]
    assert "chparam -set K 8 dut" in lines
    assert (
        lines.index("read -sv -formal dut.sv")
        < lines.index("chparam -set K 8 dut")
        < lines.index("prep -top dut")
    )


def test_render_sby_without_params_emits_no_override(tmp_path):
    sby = _sby_with_frontend(tmp_path, frontend="verilog")
    out_path = str(tmp_path / "fpv.sby")
    sby._render_sby(
        output_path=out_path,
        sources=["/abs/dut.sv"],
        incdirs=[],
        mode="bmc",
        extra_property_files=[],
    )
    text = open(out_path).read()
    assert "chparam" not in text
    assert "-G " not in text


def test_coi_script_carries_params_slang(tmp_path):
    """The COI walk measures the same reduced elaboration the proof ran."""
    script = build_yosys_script(
        sources=["/abs/dut.sv"],
        incdirs=[],
        properties=[],
        constraints=None,
        top="dut",
        frontend="slang",
        plugin_path="/path/to/slang.so",
        params=[("K", "8")],
    )
    assert "-G K=8" in script
    assert "chparam" not in script


def test_coi_script_carries_params_verilog(tmp_path):
    script = build_yosys_script(
        sources=["/abs/dut.sv"],
        incdirs=[],
        properties=[],
        constraints=None,
        top="dut",
        frontend="verilog",
        params=[("K", "8")],
    )
    lines = script.splitlines()
    assert "chparam -set K 8 dut" in lines
    assert lines.index("chparam -set K 8 dut") < lines.index("prep -flatten -top dut")


def test_vacuity_pass_carries_params(tmp_path, monkeypatch):
    """The vacuity cover pass re-renders the design with the same params as the proof."""
    props = tmp_path / "props.sv"
    props.write_text("assert property (@(posedge clk) a |-> b);\n")
    sby = _sby_with_params(
        tmp_path, frontend="verilog", params={"K": 8}, plugin_path=None
    )
    sby.fpv_cfg.properties = [str(props)]

    monkeypatch.setattr(SbyFpv, "_run", lambda self, cmd, log: None)
    sby._run_vacuity("sby", sources=["/abs/dut.sv"], incdirs=[], defines=[])

    text = open(sby._vacuity_sby_path()).read()
    assert "chparam -set K 8 dut" in text
