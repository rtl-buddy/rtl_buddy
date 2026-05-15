"""Tests for synthesis flow: config, Yosys backend, and filelist strip fix."""

from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config.synth import (
    SynthConfig,
    SynthRegConfig,
    SynthSuiteConfig,
    SynthToolConfig,
    SynthToolConfigFile,
)
from rtl_buddy.runner.synth_results import (
    SynthFailResults,
    SynthPassResults,
    SynthSkipResults,
)
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.tools import synth_yosys as synth_yosys_module
from rtl_buddy.tools.synth_yosys import YosysSynth
from rtl_buddy.tools.vlog_filelist import VlogFilelist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_cfg(name="yosys", exe="yosys", synth_args="", abc_args=""):
    cfg_file = SynthToolConfigFile(
        name=name,
        tool=exe,
        opts=_make_opts_file(synth_args, abc_args),
    )
    return SynthToolConfig(cfg_file)


def _make_opts_file(synth_args="", abc_args=""):
    from rtl_buddy.config.synth import SynthToolOptsFile

    return SynthToolOptsFile(synth_args=synth_args, abc_args=abc_args)


def _make_synth_cfg(
    *,
    name="test_synth",
    model_name="my_module",
    model_path="/fake/models.yaml",
    tool="yosys",
    constraints=None,
    params=None,
    defines=None,
    platform=None,
    reglvl=None,
    tool_overrides=None,
):
    from rtl_buddy.config.model import ModelConfig

    model = ModelConfig(name=model_name, filelist=[], path=model_path)
    return SynthConfig(
        name=name,
        desc="test synth",
        model=model,
        tool=tool,
        constraints=constraints,
        params=params,
        defines=defines,
        platform=platform,
        _reglvl=reglvl,
        tool_overrides=tool_overrides,
    )


def _make_yosys(tmp_path, synth_cfg=None, tool_cfg=None, root_cfg=None):
    synth_cfg = synth_cfg or _make_synth_cfg()
    tool_cfg = tool_cfg or _tool_cfg()
    return YosysSynth(
        name="test/yosys",
        synth_cfg=synth_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )


# ---------------------------------------------------------------------------
# SynthToolConfig
# ---------------------------------------------------------------------------


def test_synth_tool_config_returns_base_opts():
    cfg = _tool_cfg(synth_args="-flatten", abc_args="-fast")
    opts = cfg.get_opts()
    assert opts.synth_args == "-flatten"
    assert opts.abc_args == "-fast"


def test_synth_tool_config_overrides_merge_over_base():
    cfg = _tool_cfg(synth_args="-flatten", abc_args="")
    opts = cfg.get_opts({"synth_args": "-flatten -nordff", "abc_args": "-fast"})
    assert opts.synth_args == "-flatten -nordff"
    assert opts.abc_args == "-fast"


def test_synth_tool_config_partial_override_keeps_unset_base():
    cfg = _tool_cfg(synth_args="-flatten", abc_args="-O2")
    opts = cfg.get_opts({"synth_args": "-nordff"})
    assert opts.synth_args == "-nordff"
    assert opts.abc_args == "-O2"  # unchanged


def test_synth_tool_config_none_override_returns_base():
    cfg = _tool_cfg(synth_args="-flatten")
    assert cfg.get_opts(None).synth_args == "-flatten"
    assert cfg.get_opts({}).synth_args == "-flatten"


# ---------------------------------------------------------------------------
# SynthConfig
# ---------------------------------------------------------------------------


def test_synth_config_top_is_model_name():
    cfg = _make_synth_cfg(model_name="my_top")
    assert cfg.get_top() == "my_top"


def test_synth_config_reglvl_int():
    cfg = _make_synth_cfg(reglvl=500)
    assert cfg.get_reglvl("yosys") == 500


def test_synth_config_reglvl_none_defaults_to_zero():
    cfg = _make_synth_cfg(reglvl=None)
    assert cfg.get_reglvl("yosys") == 0


def test_synth_config_reglvl_dict_tool_specific():
    cfg = _make_synth_cfg(reglvl={"yosys": 100, "dc": 200, "default": 50})
    assert cfg.get_reglvl("yosys") == 100
    assert cfg.get_reglvl("dc") == 200
    assert cfg.get_reglvl("quartus") == 50  # falls back to default


def test_synth_config_tool_overrides_for_matching_tool():
    cfg = _make_synth_cfg(tool_overrides={"yosys": {"abc_args": "-fast"}})
    assert cfg.get_tool_overrides_for("yosys") == {"abc_args": "-fast"}


def test_synth_config_tool_overrides_for_non_matching_tool():
    cfg = _make_synth_cfg(tool_overrides={"yosys": {"abc_args": "-fast"}})
    assert cfg.get_tool_overrides_for("dc") is None


def test_synth_config_tool_overrides_none():
    cfg = _make_synth_cfg(tool_overrides=None)
    assert cfg.get_tool_overrides_for("yosys") is None


# ---------------------------------------------------------------------------
# SynthSuiteConfig — YAML loading
# ---------------------------------------------------------------------------

_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: synth_config

    syntheses:
      - name: "synth_a"
        desc: "First synth"
        model: "mod_a"
        model_path: "{models_path}"
        tool: "yosys"
        reglvl: 0
      - name: "synth_b"
        desc: "Second synth"
        model: "mod_b"
        model_path: "{models_path}"
        tool: "yosys"
        reglvl: 1000
        params:
          WIDTH: 8
        defines:
          TARGET_SYNTH: 1
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
    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "synth.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))
    return suite_yaml


def test_synth_suite_config_loads_all_syntheses(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = SynthSuiteConfig(str(suite_yaml))
    assert cfg.get_synth_names() == ["synth_a", "synth_b"]


def test_synth_suite_config_get_by_name(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = SynthSuiteConfig(str(suite_yaml))
    results = cfg.get_syntheses("synth_a")
    assert len(results) == 1
    assert results[0].get_name() == "synth_a"
    assert results[0].get_top() == "mod_a"


def test_synth_suite_config_params_and_defines_loaded(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = SynthSuiteConfig(str(suite_yaml))
    synth_b = cfg.get_syntheses("synth_b")[0]
    assert synth_b.get_params() == {"WIDTH": 8}
    assert synth_b.get_defines() == {"TARGET_SYNTH": 1}


def test_synth_suite_config_missing_name_raises(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError

    suite_yaml = _write_suite(tmp_path)
    cfg = SynthSuiteConfig(str(suite_yaml))
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        cfg.get_syntheses("nonexistent")


# ---------------------------------------------------------------------------
# SynthRegConfig — YAML loading
# ---------------------------------------------------------------------------

_REG_YAML = dedent("""\
    rtl-buddy-filetype: synth_reg_config

    synth-configs:
      - "sandbox/synth.yaml"
""")


def test_synth_reg_config_loads_suite_paths(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    models_yaml = sandbox / "models.yaml"
    models_yaml.write_text(_MODELS_YAML)
    suite_yaml = sandbox / "synth.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))

    reg_yaml = tmp_path / "synth_regression.yaml"
    reg_yaml.write_text(_REG_YAML)

    reg_cfg = SynthRegConfig(name="reg", path=str(reg_yaml))
    suites = reg_cfg.get_suite_configs()
    assert len(suites) == 1
    assert suites[0].get_synth_names() == ["synth_a", "synth_b"]


# ---------------------------------------------------------------------------
# SynthResults
# ---------------------------------------------------------------------------


def test_synth_pass_results_is_pass():
    assert SynthPassResults("r").is_pass()
    assert SynthPassResults("r").results["result"] == "PASS"


def test_synth_fail_results_is_not_pass():
    r = SynthFailResults("r", desc="Tool exited with code 1")
    assert not r.is_pass()
    assert r.results["result"] == "FAIL"
    assert "code 1" in r.results["desc"]


def test_synth_skip_results_is_pass():
    assert SynthSkipResults("r", desc="skipped").is_pass()


# ---------------------------------------------------------------------------
# YosysSynth — artefact paths
# ---------------------------------------------------------------------------


def test_yosys_synth_artefact_dir_created(tmp_path):
    ys = _make_yosys(tmp_path)
    assert Path(ys.artefact_dir).is_dir()
    assert Path(ys.artefact_dir).name == "test_synth"


# ---------------------------------------------------------------------------
# YosysSynth — _source_files_from_filelist
# ---------------------------------------------------------------------------


def test_source_files_strips_v_prefix(tmp_path):
    fl = tmp_path / "synth.f"
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl.write_text(f"-v {sv}\n")
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(fl))
    assert paths == [str(sv)]


def test_source_files_plain_path(tmp_path):
    fl = tmp_path / "synth.f"
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl.write_text(f"{sv}\n")
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(fl))
    assert paths == [str(sv)]


def test_source_files_skips_incdir(tmp_path):
    fl = tmp_path / "synth.f"
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl.write_text(f"+incdir+../inc\n-v {sv}\n")
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(fl))
    assert paths == [str(sv)]


def test_source_files_skips_comments_and_blank_lines(tmp_path):
    fl = tmp_path / "synth.f"
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl.write_text(f"// generated\n\n-v {sv}\n")
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(fl))
    assert paths == [str(sv)]


def test_source_files_resolves_relative_paths(tmp_path):
    fl = tmp_path / "synth.f"
    sv = tmp_path / "src" / "top.sv"
    sv.parent.mkdir()
    sv.write_text("")
    fl.write_text("-v src/top.sv\n")
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(fl))
    assert paths == [str(sv)]


# ---------------------------------------------------------------------------
# YosysSynth — _write_script
# ---------------------------------------------------------------------------


def test_write_script_basic(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path, synth_cfg=_make_synth_cfg(model_name="my_top"))
    script_path = ys._write_script(str(fl))
    script = Path(script_path).read_text()

    assert f"read_verilog -sv -defer {sv}" in script
    assert "synth -top my_top" in script
    assert "write_rtlil" in script


def test_write_script_includes_synth_args(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(
        tmp_path,
        tool_cfg=_tool_cfg(synth_args="-flatten"),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "synth -top my_module -flatten" in script


def test_write_script_includes_abc_args(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path, tool_cfg=_tool_cfg(abc_args="-fast"))
    script = Path(ys._write_script(str(fl))).read_text()
    assert "abc -fast" in script


def test_write_script_no_abc_when_empty(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path, tool_cfg=_tool_cfg(abc_args=""))
    script = Path(ys._write_script(str(fl))).read_text()
    assert "\nabc " not in script  # abc as a standalone command line


def test_write_script_params(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", params={"WIDTH": 8, "DEPTH": 16}),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "chparam -set WIDTH 8 top" in script
    assert "chparam -set DEPTH 16 top" in script


def test_write_script_defines(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(defines={"TARGET_SYNTH": 1, "WIDTH": 8}),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "-D TARGET_SYNTH=1" in script
    assert "-D WIDTH=8" in script


def test_write_script_tool_overrides_applied(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            tool_overrides={"yosys": {"synth_args": "-nordff", "abc_args": "-fast"}}
        ),
        tool_cfg=_tool_cfg(synth_args="-flatten", abc_args=""),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "synth -top my_module -nordff" in script
    assert "abc -fast" in script


# ---------------------------------------------------------------------------
# YosysSynth — frontend: slang
# ---------------------------------------------------------------------------


def _slang_tool_cfg(plugin_path: str):
    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="slang", plugin_path=plugin_path),
    )
    return SynthToolConfig(cfg_file)


class _FakeRoot:
    """Minimal stand-in for RootConfig.get_project_rootdir() in tests."""

    def __init__(self, rootdir: str):
        self._rootdir = rootdir

    def get_project_rootdir(self) -> str:
        return self._rootdir


def test_write_script_frontend_slang_emits_plugin_and_read_slang(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="my_top"),
        tool_cfg=_slang_tool_cfg(str(plugin)),
    )
    script = Path(ys._write_script(str(fl))).read_text()

    assert f"plugin -i {plugin}" in script
    assert "read_slang --std 1800-2017 --top my_top" in script
    assert f"{sv}" in script
    # Legacy verilog frontend must not be emitted.
    assert "read_verilog -sv -defer" not in script


def test_write_script_frontend_slang_resolves_relative_plugin_path(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin_rel = "tools/slang.so"
    (tmp_path / "tools").mkdir()
    plugin_abs = tmp_path / plugin_rel
    plugin_abs.write_text("")

    ys = _make_yosys(
        tmp_path,
        tool_cfg=_slang_tool_cfg(plugin_rel),
        root_cfg=_FakeRoot(str(tmp_path)),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"plugin -i {plugin_abs.resolve()}" in script


def test_write_script_frontend_slang_folds_params_into_G(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", params={"WIDTH": 8, "DEPTH": 16}),
        tool_cfg=_slang_tool_cfg(str(plugin)),
    )
    script = Path(ys._write_script(str(fl))).read_text()

    assert "-GWIDTH=8" in script
    assert "-GDEPTH=16" in script
    # Slang elaborates eagerly; a later chparam would arrive too late.
    assert "chparam" not in script


def test_write_script_frontend_slang_folds_defines_into_D(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(defines={"SYNTH": 1, "FOO": "bar"}),
        tool_cfg=_slang_tool_cfg(str(plugin)),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "-DSYNTH=1" in script
    assert "-DFOO=bar" in script


def test_write_script_frontend_slang_missing_plugin_path_raises(tmp_path):
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="slang", plugin_path=""),
    )
    ys = _make_yosys(tmp_path, tool_cfg=SynthToolConfig(cfg_file))
    with pytest.raises(FatalRtlBuddyError, match="plugin-path"):
        ys._write_script(str(fl))


def test_write_script_frontend_unknown_raises(tmp_path):
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="vhdl"),
    )
    ys = _make_yosys(tmp_path, tool_cfg=SynthToolConfig(cfg_file))
    with pytest.raises(FatalRtlBuddyError, match="unknown synth frontend"):
        ys._write_script(str(fl))


def test_write_script_default_frontend_is_verilog(tmp_path):
    """Regression guard: existing root_config.yaml without a frontend
    field continues to use read_verilog -sv -defer."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path)
    script = Path(ys._write_script(str(fl))).read_text()
    assert "read_verilog -sv -defer" in script
    assert "plugin -i" not in script
    assert "read_slang" not in script


def test_write_script_explicit_frontend_verilog(tmp_path):
    """``frontend: "verilog"`` explicitly set must produce the same
    output as the default. Guards against future default flips that
    would silently change behavior for projects that pinned to the
    explicit value."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg_file = SynthToolConfigFile(
        name="yosys", tool="yosys", opts=SynthToolOptsFile(frontend="verilog")
    )
    ys = _make_yosys(tmp_path, tool_cfg=SynthToolConfig(cfg_file))
    script = Path(ys._write_script(str(fl))).read_text()
    assert "read_verilog -sv -defer" in script
    assert "plugin -i" not in script
    assert "read_slang" not in script


def test_write_script_frontend_slang_quotes_path_with_spaces(tmp_path):
    """Source paths containing spaces must be shell-quoted on the
    read_slang line, otherwise the whole elaboration corrupts (one
    line per source on the verilog path; one line for ALL sources
    on the slang path → unquoted space breaks slang elaboration
    entirely). Plugin path also quoted."""
    spacey_dir = tmp_path / "dir with spaces"
    spacey_dir.mkdir()
    sv = spacey_dir / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = spacey_dir / "slang.so"
    plugin.write_text("")

    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="slang", plugin_path=str(plugin)),
    )
    ys = _make_yosys(tmp_path, tool_cfg=SynthToolConfig(cfg_file))
    script = Path(ys._write_script(str(fl))).read_text()
    # The literal unquoted path must NOT appear (would tokenise).
    assert f"read_slang --std 1800-2017 --top my_module {sv}" not in script
    # Both source and plugin path must be present in *quoted* form
    # — shlex.quote uses single quotes for paths with spaces.
    assert f"'{sv}'" in script
    assert f"'{plugin}'" in script


def test_write_script_frontend_slang_quotes_define_value_with_spaces(tmp_path):
    """Define values containing spaces (uncommon but possible — e.g.
    a multi-token macro expansion) must be quoted on the read_slang
    line. Same correctness invariant as path quoting; missed during
    the original implementation."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="slang", plugin_path=str(plugin)),
    )
    ys = _make_yosys(
        tmp_path,
        tool_cfg=SynthToolConfig(cfg_file),
        synth_cfg=_make_synth_cfg(defines={"MULTI": "a b c"}),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    # Quoted form: -DMULTI='a b c' (shlex.quote single-quotes anything
    # that needs escaping). Unquoted -DMULTI=a b c would be parsed as
    # three tokens by Yosys.
    assert "-DMULTI='a b c'" in script


def test_write_script_frontend_slang_whitespace_only_plugin_path_raises(tmp_path):
    """Whitespace-only plugin-path must raise the same FatalRtlBuddyError
    as empty string — otherwise we'd build a `plugin -i '   '` line
    that fails inscrutably inside Yosys."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg_file = SynthToolConfigFile(
        name="yosys",
        tool="yosys",
        opts=SynthToolOptsFile(frontend="slang", plugin_path="   "),
    )
    ys = _make_yosys(tmp_path, tool_cfg=SynthToolConfig(cfg_file))
    with pytest.raises(FatalRtlBuddyError, match="plugin-path"):
        ys._write_script(str(fl))


def test_tool_overrides_can_flip_frontend_to_slang(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    # Tool config defaults to verilog; per-block override flips to slang.
    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            tool_overrides={"yosys": {"frontend": "slang", "plugin_path": str(plugin)}}
        ),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "read_slang" in script
    assert "read_verilog -sv -defer" not in script


# ---------------------------------------------------------------------------
# YosysSynth — run() pass/fail detection
# ---------------------------------------------------------------------------


def _fake_managed_process(returncode=0, write_log=None, calls=None):
    calls = calls if calls is not None else []

    def _run_managed_process(cmd, stdout, stderr, **kwargs):
        calls.append({"cmd": cmd, "stdout": stdout, "stderr": stderr, **kwargs})
        if write_log:
            stdout.write(write_log)
        return ManagedProcessResult(returncode=returncode)

    return _run_managed_process


def _setup_run(tmp_path):
    """Write a minimal valid filelist so _write_filelist succeeds."""
    sv = tmp_path / "top.sv"
    sv.write_text("module my_module(); endmodule")

    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text(
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: "my_module"
            filelist: ["-v {sv}"]
        """)
    )
    from rtl_buddy.config.model import ModelConfig

    model = ModelConfig(name="my_module", filelist=[f"-v {sv}"], path=str(models_yaml))
    return model


def test_run_returns_pass_on_clean_exit(tmp_path, monkeypatch):
    model = _setup_run(tmp_path)
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="yosys",
        constraints=None,
        params=None,
        defines=None,
        platform=None,
        _reglvl=None,
        tool_overrides=None,
    )
    ys = YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )
    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _fake_managed_process(returncode=0)
    )
    result = ys.run()
    assert isinstance(result, SynthPassResults)


def test_run_returns_fail_on_nonzero_exit(tmp_path, monkeypatch):
    model = _setup_run(tmp_path)
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="yosys",
        constraints=None,
        params=None,
        defines=None,
        platform=None,
        _reglvl=None,
        tool_overrides=None,
    )
    ys = YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )
    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _fake_managed_process(returncode=1)
    )
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "code 1" in result.results["desc"]


def test_run_returns_fail_on_error_in_log(tmp_path, monkeypatch):
    model = _setup_run(tmp_path)
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="yosys",
        constraints=None,
        params=None,
        defines=None,
        platform=None,
        _reglvl=None,
        tool_overrides=None,
    )
    ys = YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )
    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module,
        "run_managed_process",
        _fake_managed_process(returncode=0, write_log="ERROR: something went wrong\n"),
    )
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "ERROR" in result.results["desc"]


def test_run_uses_managed_process_for_yosys(tmp_path, monkeypatch):
    model = _setup_run(tmp_path)
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="yosys",
        constraints=None,
        params=None,
        defines=None,
        platform=None,
        _reglvl=None,
        tool_overrides=None,
    )
    ys = YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )
    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    calls = []
    monkeypatch.setattr(
        synth_yosys_module,
        "run_managed_process",
        _fake_managed_process(returncode=0, calls=calls),
    )

    result = ys.run()

    assert isinstance(result, SynthPassResults)
    assert calls
    assert calls[0]["stderr"] == synth_yosys_module.subprocess.STDOUT


# ---------------------------------------------------------------------------
# YosysSynth — library-mapped flow
# ---------------------------------------------------------------------------


class _FakePlatformCfg:
    def __init__(self, path):
        self._path = path

    def get_path(self):
        return self._path


class _FakeRootCfg:
    def __init__(self, lib_map):
        self._lib_map = lib_map

    def get_synth_platform_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        if name not in self._lib_map:
            raise FatalRtlBuddyError(f"synthesis library '{name}' not found")
        return _FakePlatformCfg(self._lib_map[name])


def test_write_script_lib_flow_emits_read_liberty_and_mapping(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")

    root_cfg = _FakeRootCfg({"mylib": str(lib)})
    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(platform="mylib"),
        root_cfg=root_cfg,
    )
    script = Path(ys._write_script(str(fl))).read_text()

    assert f"read_liberty -lib {lib}" in script
    assert f"dfflibmap -liberty {lib}" in script
    assert f"abc -liberty {lib}" in script
    assert "write_verilog" in script
    assert "write_rtlil" not in script


def test_write_script_lib_flow_no_standalone_abc(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")

    root_cfg = _FakeRootCfg({"mylib": str(lib)})
    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(platform="mylib"),
        tool_cfg=_tool_cfg(abc_args="-fast"),
        root_cfg=root_cfg,
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert "\nabc -fast" not in script


def test_write_script_no_lib_flow_unchanged(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path, synth_cfg=_make_synth_cfg(platform=None))
    script = Path(ys._write_script(str(fl))).read_text()

    assert "read_liberty" not in script
    assert "dfflibmap" not in script
    assert "write_rtlil" in script
    assert "write_verilog" not in script


def test_resolve_lib_paths_unknown_name_raises(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError

    root_cfg = _FakeRootCfg({})
    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(platform="unknown_lib"),
        root_cfg=root_cfg,
    )
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        ys._resolve_lib_paths()


# ---------------------------------------------------------------------------
# SDC clock period parsing
# ---------------------------------------------------------------------------


def test_parse_clock_period_ps_basic(tmp_path):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 10.0 [get_ports clk]\n")
    ys = _make_yosys(tmp_path)
    assert ys._parse_clock_period_ps(str(sdc)) == 10000


def test_parse_clock_period_ps_fractional(tmp_path):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 3.333 [get_ports clk]\n")
    ys = _make_yosys(tmp_path)
    assert ys._parse_clock_period_ps(str(sdc)) == 3333


def test_parse_clock_period_ps_multi_clock_returns_minimum(tmp_path):
    sdc = tmp_path / "c.sdc"
    sdc.write_text(
        "create_clock -period 10.0 [get_ports clk_fast]\n"
        "create_clock -period 40.0 [get_ports clk_slow]\n"
    )
    ys = _make_yosys(tmp_path)
    assert ys._parse_clock_period_ps(str(sdc)) == 10000


def test_parse_clock_period_ps_no_clock_returns_none(tmp_path):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("set_input_delay 2.0 -clock clk [all_inputs]\n")
    ys = _make_yosys(tmp_path)
    assert ys._parse_clock_period_ps(str(sdc)) is None


def test_parse_clock_period_ps_missing_file_returns_none(tmp_path):
    ys = _make_yosys(tmp_path)
    assert ys._parse_clock_period_ps(str(tmp_path / "missing.sdc")) is None


def test_write_script_lib_flow_with_sdc_adds_D_flag(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 5.0 [get_ports clk]\n")

    root_cfg = _FakeRootCfg({"mylib": str(lib)})
    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(platform="mylib", constraints=str(sdc)),
        root_cfg=root_cfg,
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"abc -liberty {lib} -D 5000" in script


# ---------------------------------------------------------------------------
# VlogFilelist strip=True fix
# ---------------------------------------------------------------------------


def _write_models(tmp_path, filelist_entries):
    from rtl_buddy.config.model import ModelConfig

    fl_file = tmp_path / "src.f"
    fl_file.write_text("\n".join(filelist_entries) + "\n")
    return ModelConfig(
        name="m",
        filelist=[f"-F {fl_file}"],
        path=str(tmp_path / "models.yaml"),
    )


def test_vlog_filelist_strip_removes_option_prefix(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    model = _write_models(tmp_path, [f"-v {sv}"])
    out = tmp_path / "out.f"
    fl = VlogFilelist(name="t", model_cfg=model, output_path=str(out))
    fl.write_output(output_filepath=str(out), unroll=True, strip=True)
    lines = [
        ln for ln in out.read_text().splitlines() if ln and not ln.startswith("//")
    ]
    assert all(not ln.startswith("-") for ln in lines), (
        f"Option prefix not stripped: {lines}"
    )


def test_vlog_filelist_strip_false_keeps_option_prefix(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    model = _write_models(tmp_path, [f"-v {sv}"])
    out = tmp_path / "out.f"
    fl = VlogFilelist(name="t", model_cfg=model, output_path=str(out))
    fl.write_output(output_filepath=str(out), unroll=True, strip=False)
    lines = [
        ln for ln in out.read_text().splitlines() if ln and not ln.startswith("//")
    ]
    assert any(ln.startswith("-v ") for ln in lines), f"Expected -v prefix: {lines}"


# ---------------------------------------------------------------------------
# SynthPassResults — tns_ps field
# ---------------------------------------------------------------------------


def test_synth_pass_results_tns_stored():
    r = SynthPassResults("r", tns_ps=-500.0)
    assert r.results["tns_ps"] == -500.0
    assert r.is_pass()


def test_synth_pass_results_tns_absent_when_none():
    r = SynthPassResults("r")
    assert "tns_ps" not in r.results


def test_synth_pass_results_all_fields():
    r = SynthPassResults(
        "r", area_um2=100.0, gate_count=42, wns_ps=200.0, tns_ps=-100.0
    )
    assert r.results["area_um2"] == 100.0
    assert r.results["gate_count"] == 42
    assert r.results["wns_ps"] == 200.0
    assert r.results["tns_ps"] == -100.0


# ---------------------------------------------------------------------------
# SynthToolConfig — strategy opt
# ---------------------------------------------------------------------------


def test_synth_tool_config_strategy_default_empty():
    cfg = _tool_cfg()
    assert cfg.get_opts().strategy == ""


def test_synth_tool_config_strategy_override():
    from rtl_buddy.config.synth import SynthToolConfigFile, SynthToolOptsFile

    opts_file = SynthToolOptsFile(synth_args="", abc_args="", strategy="TIMING")
    cfg_file = SynthToolConfigFile(name="openroad", tool="openroad", opts=opts_file)
    cfg = SynthToolConfig(cfg_file)
    assert cfg.get_opts().strategy == "TIMING"


def test_synth_tool_config_strategy_via_override_dict():
    cfg = _tool_cfg()
    opts = cfg.get_opts({"strategy": "AREA"})
    assert opts.strategy == "AREA"


# ---------------------------------------------------------------------------
# SynthPlatformConfig — pdk + corner + lef paths
# ---------------------------------------------------------------------------


def _make_pdk(name, root_cfg_path, *, tech_lef="", macro_lef="", corners=None):
    from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile

    return PdkConfig(
        PdkConfigFile(
            name=name,
            corners=corners or {"typ": "lib/cells.lib"},
            tech_lef=tech_lef,
            macro_lef=macro_lef,
        ),
        root_cfg_path,
    )


def test_synth_platform_config_lef_paths_empty_when_pdk_has_no_lef(tmp_path):
    from rtl_buddy.config.synth import SynthPlatformConfigFile, SynthPlatformConfig

    root_cfg_path = str(tmp_path / "root_config.yaml")
    pdk = _make_pdk("nangate45", root_cfg_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_typ", pdk="nangate45"),
        lambda _name: pdk,
    )
    assert cfg.get_lef_paths() == []
    assert cfg.get_path() == str(tmp_path / "lib" / "cells.lib")


def test_synth_platform_config_lef_paths_from_pdk(tmp_path):
    from rtl_buddy.config.synth import SynthPlatformConfigFile, SynthPlatformConfig

    root_cfg_path = str(tmp_path / "root_config.yaml")
    pdk = _make_pdk(
        "nangate45",
        root_cfg_path,
        tech_lef="lef/tech.lef",
        macro_lef="lef/cells.lef",
    )
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_typ", pdk="nangate45"),
        lambda _name: pdk,
    )
    assert cfg.get_lef_paths() == [
        str(tmp_path / "lef" / "tech.lef"),
        str(tmp_path / "lef" / "cells.lef"),
    ]


# ---------------------------------------------------------------------------
# OpenRoadSynth — artefact paths and script generation
# ---------------------------------------------------------------------------


class _FakePlatformCfgWithLef:
    def __init__(self, path, lef_paths=None):
        self._path = path
        self._lef_paths = lef_paths or []

    def get_path(self):
        return self._path

    def get_lef_paths(self):
        return self._lef_paths


class _FakeRootCfgOR:
    def __init__(self, lib_map, lef_map=None):
        self._lib_map = lib_map
        self._lef_map = lef_map or {}

    def get_synth_platform_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        if name not in self._lib_map:
            raise FatalRtlBuddyError(f"synthesis library '{name}' not found")
        lef_paths = self._lef_map.get(name, [])
        return _FakePlatformCfgWithLef(self._lib_map[name], lef_paths)

    def get_synth_tool_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        raise FatalRtlBuddyError(f"tool '{name}' not found")


def _make_or_tool_cfg(strategy=""):
    from rtl_buddy.config.synth import SynthToolConfigFile, SynthToolOptsFile

    opts_file = SynthToolOptsFile(synth_args="", abc_args="", strategy=strategy)
    cfg_file = SynthToolConfigFile(name="openroad", tool="openroad", opts=opts_file)
    return SynthToolConfig(cfg_file)


def _make_openroad(tmp_path, synth_cfg=None, tool_cfg=None, root_cfg=None):
    from rtl_buddy.tools.synth_openroad import OpenRoadSynth

    synth_cfg = synth_cfg or _make_synth_cfg()
    tool_cfg = tool_cfg or _make_or_tool_cfg()
    return OpenRoadSynth(
        name="test/openroad",
        synth_cfg=synth_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        yosys_executable="yosys",
    )


def test_openroad_synth_artefact_dir_created(tmp_path):

    or_synth = _make_openroad(tmp_path)
    assert Path(or_synth.artefact_dir).is_dir()
    assert Path(or_synth.artefact_dir).name == "test_synth"


def test_openroad_yosys_script_has_liberty_and_netlist(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")

    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", platform="mylib"),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_yosys_script(str(fl))).read_text()

    assert f"read_liberty -lib {lib}" in script
    assert f"dfflibmap -liberty {lib}" in script
    assert f"abc -liberty {lib}" in script
    assert "write_verilog" in script
    assert "write_rtlil" not in script


def test_openroad_or_script_has_lef_liberty_verilog_sdc(tmp_path):
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 10.0 [get_ports clk]\n")

    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="top", platform="mylib", constraints=str(sdc)
        ),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_or_script([str(lef)], [str(lib)])).read_text()

    assert f"read_lef {lef}" in script
    assert f"read_liberty {lib}" in script
    assert "read_verilog" in script
    assert "link_design top" in script
    assert f"read_sdc {sdc}" in script
    assert "report_design_area" in script
    assert "report_checks -path_delay max" in script
    assert "report_worst_slack -max" in script
    assert "report_tns" in script


def test_openroad_or_script_no_sdc_omits_timing_reports(tmp_path):
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")

    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", platform="mylib", constraints=None),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_or_script([str(lef)], [str(lib)])).read_text()

    assert "read_sdc" not in script
    assert "report_wns" not in script
    assert "report_tns" not in script
    assert "report_design_area" in script


def test_openroad_or_script_timing_strategy_adds_resynth(tmp_path):
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 10.0 [get_ports clk]\n")

    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="top", platform="mylib", constraints=str(sdc)
        ),
        tool_cfg=_make_or_tool_cfg(strategy="TIMING"),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_or_script([str(lef)], [str(lib)])).read_text()
    assert "resynth_annealing" in script


# ---------------------------------------------------------------------------
# OpenRoadSynth — frontend pickup from yosys tool config
# ---------------------------------------------------------------------------


class _FakeRootCfgORWithYosys:
    """Variant of _FakeRootCfgOR that exposes a yosys tool config so the
    elaboration stage can find frontend / plugin-path settings."""

    def __init__(self, lib_map, lef_map=None, yosys_opts=None):
        self._lib_map = lib_map
        self._lef_map = lef_map or {}
        self._yosys_opts = yosys_opts

    def get_synth_platform_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        if name not in self._lib_map:
            raise FatalRtlBuddyError(f"synthesis library '{name}' not found")
        lef_paths = self._lef_map.get(name, [])
        return _FakePlatformCfgWithLef(self._lib_map[name], lef_paths)

    def get_synth_tool_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.config.synth import SynthToolConfigFile

        if name != "yosys" or self._yosys_opts is None:
            raise FatalRtlBuddyError(f"tool '{name}' not found")
        cfg_file = SynthToolConfigFile(
            name="yosys", tool="yosys", opts=self._yosys_opts
        )
        return SynthToolConfig(cfg_file)


def test_openroad_yosys_stage_picks_up_yosys_frontend_from_root_cfg(tmp_path):
    """When `tool: openroad` is selected, the internal Yosys elaboration stage
    should read frontend / plugin-path from the *yosys* tool config (and
    tool_overrides.yosys), not from the openroad tool config."""
    from rtl_buddy.config.synth import SynthToolOptsFile

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    root_cfg = _FakeRootCfgORWithYosys(
        lib_map={"mylib": str(lib)},
        yosys_opts=SynthToolOptsFile(frontend="slang", plugin_path=str(plugin)),
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", platform="mylib"),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_yosys_script(str(fl))).read_text()

    assert f"plugin -i {plugin}" in script
    assert "read_slang --std 1800-2017 --top top" in script
    assert "read_verilog -sv -defer" not in script


def test_openroad_yosys_stage_picks_up_yosys_tool_overrides(tmp_path):
    """A `tool_overrides.yosys` block in synth.yaml should reach the Yosys
    elaboration stage of the OpenROAD backend (not just `tool: yosys` flows)."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    from rtl_buddy.config.synth import SynthToolOptsFile

    # yosys tool defaults to verilog frontend; per-block override flips to slang.
    root_cfg = _FakeRootCfgORWithYosys(
        lib_map={"mylib": str(lib)},
        yosys_opts=SynthToolOptsFile(),  # all defaults — frontend="verilog"
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="top",
            platform="mylib",
            tool_overrides={"yosys": {"frontend": "slang", "plugin_path": str(plugin)}},
        ),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_yosys_script(str(fl))).read_text()

    assert "read_slang" in script
    assert "read_verilog -sv -defer" not in script


def test_openroad_falls_back_to_openroad_opts_when_no_yosys_tool_cfg(tmp_path):
    """Projects that only configure cfg-synth-tools[openroad] keep working —
    the OpenROAD backend falls back to its own opts (default frontend=verilog)
    when no yosys tool entry is configured."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")

    root_cfg = _FakeRootCfgOR(lib_map={"mylib": str(lib)})
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="top", platform="mylib"),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_yosys_script(str(fl))).read_text()

    assert "read_verilog -sv -defer" in script
    assert "read_slang" not in script


# ---------------------------------------------------------------------------
# OpenRoadSynth — output parsing
# ---------------------------------------------------------------------------


def test_openroad_parse_area():

    or_synth = _make_openroad(Path("/tmp"))
    log = "Design area 179 um^2 100% utilization.\n"
    assert or_synth._parse_or_area_um2(log) == 179.0


def test_openroad_parse_wns_met():

    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_wns_ns(
        "            6.754   slack (MET)\n"
    ) == pytest.approx(6.754)


def test_openroad_parse_wns_violated():

    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_wns_ns(
        "           -0.431   slack (VIOLATED)\n"
    ) == pytest.approx(-0.431)


def test_openroad_parse_wns_prefers_report_worst_slack():
    # When `report_worst_slack -max` is present, prefer that authoritative
    # line over the per-group path summaries (which may appear in any order).
    log = (
        "            6.754   slack (MET)\n"
        "           -0.431   slack (VIOLATED)\n"
        "worst slack max -2.150\n"
    )
    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_wns_ns(log) == pytest.approx(-2.150)


def test_openroad_parse_wns_multi_group_fallback_picks_min():
    # Legacy log without `report_worst_slack`. The parser must scan every
    # `slack (...)` line and return the minimum — the historical bug was
    # to take the first match, which on multi-clock designs is whichever
    # path group OpenROAD prints first, not the true WNS.
    log = (
        "            3.054   slack (MET)\n"
        "           -2.000   slack (VIOLATED)\n"
        "          -11.867   slack (VIOLATED)\n"
        "         -556.494   slack (VIOLATED)\n"
        "            5.919   slack (MET)\n"
    )
    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_wns_ns(log) == pytest.approx(-556.494)


def test_openroad_parse_tns_with_corner():

    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_tns_ns("tns max -3.964\n") == pytest.approx(-3.964)


def test_openroad_parse_area_missing_returns_none():

    or_synth = _make_openroad(Path("/tmp"))
    assert or_synth._parse_or_area_um2("no area here\n") is None


# ---------------------------------------------------------------------------
# OpenRoadSynth — run() returns fail when no library / no lef
# ---------------------------------------------------------------------------


def test_openroad_run_fails_without_library(tmp_path, monkeypatch):

    or_synth = _make_openroad(tmp_path, synth_cfg=_make_synth_cfg(platform=None))
    result = or_synth.run()
    assert isinstance(result, SynthFailResults)
    assert "library" in result.results["desc"].lower()


def test_openroad_run_fails_without_lef(tmp_path, monkeypatch):

    lib = tmp_path / "cells.lib"
    lib.write_text("")
    root_cfg = _FakeRootCfgOR(lib_map={"mylib": str(lib)}, lef_map={})
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(platform="mylib"),
        root_cfg=root_cfg,
    )
    result = or_synth.run()
    assert isinstance(result, SynthFailResults)
    assert "lef" in result.results["desc"].lower()
