"""Tests for `frontend: slang` + `plugin-path` plumbing in `rb fpv`.

Covers both the per-verification schema field, the tool-config
plugin-path field, the `_render_sby` slang script, and the
`fpv_coi.build_yosys_script` slang script. End-to-end execution is
exercised against the template demo in
rtl-buddy-project-template — these tests only verify the generated
script content.
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


# ---------------------------------------------------------------------------
# Schema — FpvConfig.frontend + FpvToolOpts.plugin_path
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SbyFpv._render_sby — slang vs verilog
# ---------------------------------------------------------------------------


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
    # Single `read_slang --top <top> <files...>` so `bind` directives
    # at compilation-unit scope see every declared module. `--top` is
    # required for slang to pull in bound submodules.
    # `--no-synthesis-define -DFORMAL=1` mirrors `read -formal`
    # semantics (FORMAL=1 replaces the implicit SYNTHESIS=1) so in-RTL
    # `ifdef FORMAL asserts survive preprocessing (#246).
    assert "read_slang --top dut --no-synthesis-define -DFORMAL=1 dut.sv" in text
    # The verilog-frontend command must NOT appear when slang is on —
    # otherwise yosys re-parses the same file through two frontends
    # and produces duplicated $check cells.
    assert "read -sv -formal" not in text


def test_render_sby_slang_without_plugin_path_errors(tmp_path):
    sby = _sby_with_frontend(tmp_path, frontend="slang", plugin_path=None)
    out_path = str(tmp_path / "fpv.sby")
    with pytest.raises(FatalRtlBuddyError, match="plugin-path"):
        sby._render_sby(
            output_path=out_path,
            sources=["/abs/dut.sv"],
            incdirs=[],
            mode="bmc",
            extra_property_files=[],
        )


# ---------------------------------------------------------------------------
# fpv_coi.build_yosys_script — slang vs verilog
# ---------------------------------------------------------------------------


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
    # Single `read_slang --top <top> ... dut.sv props.sv` with the same
    # `read -formal`-parity defines as the sby renderer — without them
    # in-RTL `ifdef FORMAL asserts vanish and COI reports 0% (#246).
    assert (
        "read_slang --top dut --no-synthesis-define -DFORMAL=1 dut.sv props.sv"
        in script
    )
    assert "read -sv -formal" not in script


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


# ---------------------------------------------------------------------------
# YAML loader — `frontend:` + `plugin-path:` end-to-end
# ---------------------------------------------------------------------------


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
