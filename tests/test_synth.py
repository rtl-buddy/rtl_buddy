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
    libraries=None,
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
        libraries=libraries,
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
# YosysSynth — run() pass/fail detection
# ---------------------------------------------------------------------------


def _fake_subprocess_run(returncode=0, write_log=None):

    def _run(cmd, stdout, stderr, check):
        if write_log:
            stdout.write(write_log)
        return type("R", (), {"returncode": returncode})()

    return _run


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
        libraries=None,
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
        synth_yosys_module.subprocess, "run", _fake_subprocess_run(returncode=0)
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
        libraries=None,
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
        synth_yosys_module.subprocess, "run", _fake_subprocess_run(returncode=1)
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
        libraries=None,
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
        synth_yosys_module.subprocess,
        "run",
        _fake_subprocess_run(returncode=0, write_log="ERROR: something went wrong\n"),
    )
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "ERROR" in result.results["desc"]


# ---------------------------------------------------------------------------
# YosysSynth — library-mapped flow
# ---------------------------------------------------------------------------


class _FakeLibCfg:
    def __init__(self, path):
        self._path = path

    def get_path(self):
        return self._path


class _FakeRootCfg:
    def __init__(self, lib_map):
        self._lib_map = lib_map

    def get_synth_lib_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        if name not in self._lib_map:
            raise FatalRtlBuddyError(f"synthesis library '{name}' not found")
        return _FakeLibCfg(self._lib_map[name])


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
        synth_cfg=_make_synth_cfg(libraries=["mylib"]),
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
        synth_cfg=_make_synth_cfg(libraries=["mylib"]),
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

    ys = _make_yosys(tmp_path, synth_cfg=_make_synth_cfg(libraries=None))
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
        synth_cfg=_make_synth_cfg(libraries=["unknown_lib"]),
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
        synth_cfg=_make_synth_cfg(libraries=["mylib"], constraints=str(sdc)),
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
