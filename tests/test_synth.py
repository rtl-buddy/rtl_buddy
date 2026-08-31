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
from rtl_buddy.errors import FilelistError
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


def test_synth_tool_config_single_unit_defaults_to_false():
    assert _tool_cfg().get_opts().single_unit is False
    assert _tool_cfg().get_opts({}).single_unit is False


def test_synth_tool_config_single_unit_from_opts_file():
    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="yosys",
            tool="yosys",
            opts=SynthToolOptsFile(frontend="slang", single_unit=True),
        )
    )
    assert cfg.get_opts().single_unit is True


def test_synth_tool_config_single_unit_override_sets_true():
    cfg = _tool_cfg()
    assert cfg.get_opts({"single_unit": True}).single_unit is True


def test_synth_tool_config_single_unit_override_can_disable_base():
    from rtl_buddy.config.synth import SynthToolOptsFile

    cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="yosys", tool="yosys", opts=SynthToolOptsFile(single_unit=True)
        )
    )
    assert cfg.get_opts({"single_unit": False}).single_unit is False


def test_synth_tool_config_kebab_override_key_warns(caplog):
    """`single-unit` under tool_overrides is the kebab-vs-snake trap: it
    used to be silently ignored. It is still ignored — rejecting it would
    be a breaking change on a minor bump — but it now says so."""
    cfg = _tool_cfg()
    with caplog.at_level("WARNING"):
        opts = cfg.get_opts({"single-unit": True})
    assert "'single-unit'" in caplog.text
    assert "'single-unit' -> 'single_unit'" in caplog.text
    assert "tool_overrides.yosys" in caplog.text
    # Warned about, not applied: the base value stands.
    assert opts.single_unit is False


def test_synth_tool_config_misspelled_override_key_warns(caplog):
    cfg = _tool_cfg()
    with caplog.at_level("WARNING"):
        cfg.get_opts({"singel_unit": True})
    assert "singel_unit" in caplog.text


def test_synth_tool_config_unknown_override_keeps_known_keys(caplog):
    """One bad key must not discard the good ones in the same block."""
    cfg = _tool_cfg(synth_args="-flatten")
    with caplog.at_level("WARNING"):
        opts = cfg.get_opts({"synth_args": "-nordff", "nonsense": 1})
    assert opts.synth_args == "-nordff"
    assert "nonsense" in caplog.text


def test_synth_tool_config_unknown_override_lists_accepted_keys(caplog):
    from rtl_buddy.config.synth import SYNTH_TOOL_OVERRIDE_KEYS

    cfg = _tool_cfg()
    with caplog.at_level("WARNING"):
        cfg.get_opts({"nonsense": 1})
    for key in SYNTH_TOOL_OVERRIDE_KEYS:
        assert key in caplog.text


def test_synth_tool_config_known_override_is_quiet(caplog):
    cfg = _tool_cfg()
    with caplog.at_level("WARNING"):
        cfg.get_opts({"single_unit": True, "frontend": "slang"})
    assert "unknown key" not in caplog.text


def test_synth_tool_config_openroad_strategy_override_still_accepted():
    """SynthToolConfig is shared by the yosys and openroad entries, so the
    accepted-key set must cover both tools' knobs."""
    cfg = _tool_cfg(name="openroad", exe="openroad")
    assert cfg.get_opts({"strategy": "TIMING"}).strategy == "TIMING"


def test_synth_tool_config_single_unit_non_bool_raises():
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg = _tool_cfg()
    with pytest.raises(FatalRtlBuddyError, match="must be a bool"):
        cfg.get_opts({"single_unit": "true"})


def test_synth_tool_config_single_unit_null_raises():
    """`single_unit:` with an empty YAML value deserialises to None —
    fatal rather than falling through to the base value, because the
    author plainly meant to set something."""
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg = _tool_cfg()
    with pytest.raises(FatalRtlBuddyError) as exc:
        cfg.get_opts({"single_unit": None})
    assert "must be a bool" in str(exc.value)
    assert "NoneType" in str(exc.value)


def test_synth_tool_config_non_mapping_override_block_raises():
    """`tool_overrides: {yosys: "slang"}` used to die on an AttributeError
    from `overrides.get`; name the file and the shape instead."""
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg = _tool_cfg()
    with pytest.raises(FatalRtlBuddyError, match="must be a mapping, got str"):
        cfg.get_opts("slang")


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


def test_synth_suite_config_duplicate_synthesis_raises(tmp_path):
    """Two syntheses with the same name in one synth.yaml is a hard
    error — the dict-comprehension in SynthSuiteConfig.__init__
    would silently overwrite the first one otherwise."""
    from rtl_buddy.errors import FatalRtlBuddyError

    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text(_MODELS_YAML)
    body = dedent("""\
        rtl-buddy-filetype: synth_config

        syntheses:
          - name: "dup"
            desc: "first"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "yosys"
            reglvl: 0
          - name: "dup"
            desc: "second"
            model: "mod_b"
            model_path: "models.yaml"
            tool: "yosys"
            reglvl: 0
    """)
    path = tmp_path / "synth.yaml"
    path.write_text(body)
    with pytest.raises(FatalRtlBuddyError, match="duplicate synthesis name 'dup'"):
        SynthSuiteConfig(str(path))


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


_SINGLE_UNIT_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: synth_config

    syntheses:
      - name: "synth_su"
        desc: "single-unit slang synth"
        model: "mod_a"
        model_path: "models.yaml"
        tool: "yosys"
        reglvl: 0
        tool_overrides:
          yosys:
            single_unit: true
""")


def _write_single_unit_suite(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "synth.yaml"
    suite_yaml.write_text(_SINGLE_UNIT_SUITE_YAML)
    return suite_yaml


def test_synth_suite_config_preserves_single_unit_override(tmp_path):
    cfg = SynthSuiteConfig(str(_write_single_unit_suite(tmp_path)))
    overrides = cfg.get_syntheses("synth_su")[0].get_tool_overrides_for("yosys")
    assert overrides == {"single_unit": True}
    # YAML `true` must survive as a bool, not the string "true".
    assert overrides["single_unit"] is True


def test_synth_suite_config_single_unit_reaches_tool_opts(tmp_path):
    cfg = SynthSuiteConfig(str(_write_single_unit_suite(tmp_path)))
    synth_cfg = cfg.get_syntheses("synth_su")[0]
    tool_cfg = _tool_cfg()
    opts = tool_cfg.get_opts(synth_cfg.get_tool_overrides_for("yosys"))
    assert opts.single_unit is True


def test_synth_reg_config_preserves_single_unit_override(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_single_unit_suite(sandbox)
    reg_yaml = tmp_path / "synth_regression.yaml"
    reg_yaml.write_text(_REG_YAML)

    reg_cfg = SynthRegConfig(name="reg", path=str(reg_yaml))
    synth_cfg = reg_cfg.get_suite_configs()[0].get_syntheses("synth_su")[0]
    assert synth_cfg.get_tool_overrides_for("yosys")["single_unit"] is True


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


def test_filelist_with_incdir_does_not_leak_directory(tmp_path):
    # Regression: rtl_buddy#69 — `+incdir+<path>` in the model filelist
    # leaked into the generated synth.f as a bare directory path because
    # `strip=True` removed the option prefix, then
    # `_source_files_from_filelist` couldn't tell it from a source file.
    sv = tmp_path / "top.sv"
    sv.write_text("")
    sub_f = tmp_path / "src.f"
    sub_f.write_text("+incdir+.\ntop.sv\n")
    from rtl_buddy.config.model import ModelConfig

    model = ModelConfig(
        name="m", filelist=[f"-F {sub_f}"], path=str(tmp_path / "models.yaml")
    )
    out = tmp_path / "synth.f"
    VlogFilelist(name="t", model_cfg=model, output_path=str(out)).write_output(
        output_filepath=str(out), unroll=True, strip=False, deduplicate=True
    )
    ys = _make_yosys(tmp_path)
    paths = ys._source_files_from_filelist(str(out))
    assert paths == [str(sv)], (
        f"+incdir+ leaked into source list: {paths!r}; synth.f was:\n{out.read_text()}"
    )


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


def test_write_script_strips_formal_cells_after_synth(tmp_path):
    """Formal cells ($assert/$assume/$cover) from unguarded immediate
    assertions must be stripped after synth and before the netlist is
    written — structural Verilog readers (OpenROAD/OpenSTA pnr/power
    `read_verilog`) reject netlists that carry them."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    ys = _make_yosys(tmp_path, synth_cfg=_make_synth_cfg(model_name="my_top"))
    script = Path(ys._write_script(str(fl))).read_text()

    assert "chformal -remove" in script
    assert script.index("chformal -remove") > script.index("synth -top my_top")
    assert script.index("chformal -remove") < script.index("write_rtlil")


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


def test_write_script_frontend_slang_missing_plugin_path_raises(tmp_path, monkeypatch):
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    monkeypatch.delenv(SLANG_PLUGIN_ENV, raising=False)
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
    # The error must name both configuration channels.
    with pytest.raises(FatalRtlBuddyError, match="plugin-path"):
        ys._write_script(str(fl))
    with pytest.raises(FatalRtlBuddyError, match=SLANG_PLUGIN_ENV):
        ys._write_script(str(fl))


def test_write_script_frontend_slang_env_fallback(tmp_path, monkeypatch):
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    monkeypatch.setenv(SLANG_PLUGIN_ENV, str(plugin))
    ys = _make_yosys(tmp_path, tool_cfg=_slang_tool_cfg(""))
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"plugin -i {plugin}" in script


def test_write_script_frontend_slang_relative_env_rejected(tmp_path, monkeypatch):
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    monkeypatch.setenv(SLANG_PLUGIN_ENV, "build/slang.so")
    ys = _make_yosys(tmp_path, tool_cfg=_slang_tool_cfg(""))
    with pytest.raises(FatalRtlBuddyError, match="absolute"):
        ys._write_script(str(fl))


def test_write_script_frontend_slang_config_wins_over_env(tmp_path, monkeypatch):
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    configured = tmp_path / "configured.so"
    configured.write_text("")

    monkeypatch.setenv(SLANG_PLUGIN_ENV, str(tmp_path / "env.so"))
    ys = _make_yosys(tmp_path, tool_cfg=_slang_tool_cfg(str(configured)))
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"plugin -i {configured}" in script
    assert "env.so" not in script


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


def _slang_single_unit_tool_cfg(plugin_path: str):
    from rtl_buddy.config.synth import SynthToolOptsFile

    return SynthToolConfig(
        SynthToolConfigFile(
            name="yosys",
            tool="yosys",
            opts=SynthToolOptsFile(
                frontend="slang", plugin_path=plugin_path, single_unit=True
            ),
        )
    )


def test_write_script_slang_single_unit_from_tool_opts(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(model_name="my_top"),
        tool_cfg=_slang_single_unit_tool_cfg(str(plugin)),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"read_slang --std 1800-2017 --top my_top --single-unit {sv}" in script


def test_write_script_slang_single_unit_precedes_define_and_param_flags(tmp_path):
    """Flag order is part of the emitted command's contract: --single-unit
    sits between --top and the -D/-G flags."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="my_top", params={"WIDTH": 8}, defines={"TARGET_SYNTH": 1}
        ),
        tool_cfg=_slang_single_unit_tool_cfg(str(plugin)),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert (
        "read_slang --std 1800-2017 --top my_top --single-unit "
        f"-DTARGET_SYNTH=1 -GWIDTH=8 {sv}" in script
    )


def test_write_script_slang_single_unit_via_tool_overrides(tmp_path):
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    ys = _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="my_top",
            tool_overrides={
                "yosys": {
                    "frontend": "slang",
                    "plugin_path": str(plugin),
                    "single_unit": True,
                }
            },
        ),
    )
    script = Path(ys._write_script(str(fl))).read_text()
    assert f"read_slang --std 1800-2017 --top my_top --single-unit {sv}" in script


def test_write_script_slang_omits_single_unit_by_default(tmp_path):
    """Default behaviour is byte-identical to before the option existed."""
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
    assert "--single-unit" not in script
    assert f"read_slang --std 1800-2017 --top my_top {sv}" in script


def test_write_script_single_unit_with_verilog_frontend_warns_and_is_ignored(
    tmp_path, caplog
):
    from rtl_buddy.config.synth import SynthToolOptsFile

    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")

    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="yosys",
            tool="yosys",
            opts=SynthToolOptsFile(frontend="verilog", single_unit=True),
        )
    )
    ys = _make_yosys(tmp_path, tool_cfg=tool_cfg)
    with caplog.at_level("WARNING"):
        script = Path(ys._write_script(str(fl))).read_text()

    assert "--single-unit" not in script
    assert "read_verilog -sv -defer" in script
    assert "single_unit" in caplog.text and "slang" in caplog.text


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


def test_openroad_yosys_script_strips_formal_cells_after_synth(tmp_path):
    """Mirror of the YosysSynth test: the OpenROAD backend's stage-1 yosys
    script must strip formal cells after synth and before the netlist is
    written — OpenROAD's structural `read_verilog` (stage 2, and pnr/power
    downstream) rejects netlists that carry them."""
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

    assert "chformal -remove" in script
    assert script.index("chformal -remove") > script.index("synth -top top")
    assert script.index("chformal -remove") < script.index("write_verilog")


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


def _bb_src(tmp_path, module="mymacro", extra_module=None):
    """A source file with one (* blackbox *) module, and optionally a real one."""
    body = (
        "(* blackbox *)\n"
        f"module {module} (\n"
        "  input  wire        clk,\n"
        "  input  wire [7:0]  addr,\n"
        "  output wire [31:0] q\n"
        ");\n"
        "  reg [31:0] mem [0:255];\n"
        "  always @(posedge clk) q <= mem[addr];\n"
        "endmodule\n"
    )
    if extra_module:
        body += f"module {extra_module} (input wire a, output wire z);\n"
        body += "  assign z = ~a;\n"
        body += "endmodule\n"
    src = tmp_path / "macro_bb.v"
    src.write_text(body)
    return src


def _write_filelist(or_synth, src):
    """The stub writer reads the filelist the Yosys stage generated, which lives
    in the artefact directory."""
    fl = Path(or_synth._filelist_path())
    fl.parent.mkdir(parents=True, exist_ok=True)
    fl.write_text(f"-v {src}\n")
    return fl


def test_masters_from_lef_and_liberty_reads_both(tmp_path):
    lef = tmp_path / "macro.lef"
    lef.write_text(
        "VERSION 5.7 ;\nMACRO mymacro\n  CLASS BLOCK ;\n  SIZE 10 BY 10 ;\nEND mymacro\n"
    )
    lib = tmp_path / "macro.lib"
    lib.write_text('library (l) {\n  cell ("othermacro") {\n    area : 1 ;\n  }\n}\n')

    or_synth = _make_openroad(tmp_path)
    masters = or_synth._masters_from_lef_and_liberty([str(lef)], [str(lib)])

    assert masters == {"mymacro", "othermacro"}


def test_masters_from_lef_and_liberty_reads_a_split_cell_declaration(tmp_path):
    """Some generated Liberty puts the cell name on the line after `cell`."""
    lib = tmp_path / "split.lib"
    lib.write_text(
        'library (l) {\n  cell\n  ("splitmacro") {\n    area : 1 ;\n  }\n}\n'
    )

    or_synth = _make_openroad(tmp_path)
    assert or_synth._masters_from_lef_and_liberty([], [str(lib)]) == {"splitmacro"}


def test_masters_from_lef_and_liberty_ignores_lef_cells_in_liberty_position(tmp_path):
    """A LEF is scanned for MACRO only, so a stray `cell (...)` line in one does
    not add a name, and vice versa for MACRO lines in a Liberty."""
    lef = tmp_path / "odd.lef"
    lef.write_text("MACRO realmacro\n  CLASS BLOCK ;\nEND realmacro\n")
    lib = tmp_path / "odd.lib"
    lib.write_text("library (l) {\n  cell (realcell) { area : 1 ; }\n}\n")

    or_synth = _make_openroad(tmp_path)
    assert or_synth._masters_from_lef_and_liberty([str(lef)], [str(lib)]) == {
        "realmacro",
        "realcell",
    }


def test_masters_from_lef_and_liberty_tolerates_missing_files(tmp_path):
    or_synth = _make_openroad(tmp_path)
    assert or_synth._masters_from_lef_and_liberty(["/nope.lef"], ["/nope.lib"]) == set()


def test_blackbox_stub_written_when_no_lef_or_liberty_master(tmp_path):
    """The original behaviour: without a master, link_design needs the stub."""
    src = _bb_src(tmp_path)
    or_synth = _make_openroad(
        tmp_path, synth_cfg=_make_synth_cfg(name="test_synth", model_name="top")
    )
    _write_filelist(or_synth, src)
    stubs = or_synth._write_or_blackbox_stubs(set())

    assert len(stubs) == 1
    stub = Path(stubs[0]).read_text()
    assert "module mymacro" in stub
    # body stripped: OpenSTA's reader does not accept reg arrays or always blocks
    assert "always" not in stub
    assert "reg [31:0] mem" not in stub


def test_blackbox_stub_dropped_when_lef_supplies_the_master(tmp_path):
    """A macro whose LEF this script reads must not also be declared in Verilog:
    the Verilog module shadows the LEF master and the instances vanish (#470)."""
    src = _bb_src(tmp_path)
    or_synth = _make_openroad(
        tmp_path, synth_cfg=_make_synth_cfg(name="test_synth", model_name="top")
    )
    _write_filelist(or_synth, src)
    stubs = or_synth._write_or_blackbox_stubs({"mymacro"})

    assert stubs == []


def test_blackbox_stub_keeps_unmastered_modules_in_a_mixed_file(tmp_path):
    """One file, one mastered blackbox and one real module: drop the first,
    keep the file for the second."""
    src = _bb_src(tmp_path, extra_module="glue")
    or_synth = _make_openroad(
        tmp_path, synth_cfg=_make_synth_cfg(name="test_synth", model_name="top")
    )
    _write_filelist(or_synth, src)
    stubs = or_synth._write_or_blackbox_stubs({"mymacro"})

    assert len(stubs) == 1
    stub = Path(stubs[0]).read_text()
    assert "module mymacro" not in stub
    assert "module glue" in stub


def test_or_script_omits_stub_read_for_a_lef_backed_macro(tmp_path):
    """End to end through the script writer: the read_verilog of the stub is
    what dropped the macros, so it must not be emitted."""
    src = _bb_src(tmp_path)
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "macro.lef"
    lef.write_text("MACRO mymacro\n  CLASS BLOCK ;\nEND mymacro\n")

    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            name="test_synth", model_name="top", platform="mylib"
        ),
        root_cfg=root_cfg,
    )
    _write_filelist(or_synth, src)
    script = Path(or_synth._write_or_script([str(lef)], [str(lib)])).read_text()

    assert f"read_lef {lef}" in script
    assert "or_macro_bb.v" not in script
    assert "link_design top" in script


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


def test_openroad_yosys_stage_forwards_single_unit(tmp_path):
    """The OpenROAD backend's elaboration stage shares the Yosys emitter, so
    a single_unit override on the yosys block must reach its read_slang too."""
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")

    from rtl_buddy.config.synth import SynthToolOptsFile

    root_cfg = _FakeRootCfgORWithYosys(
        lib_map={"mylib": str(lib)},
        yosys_opts=SynthToolOptsFile(frontend="slang", plugin_path=str(plugin)),
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=_make_synth_cfg(
            model_name="top",
            platform="mylib",
            tool_overrides={"yosys": {"single_unit": True}},
        ),
        root_cfg=root_cfg,
    )
    script = Path(or_synth._write_yosys_script(str(fl))).read_text()
    assert f"read_slang --std 1800-2017 --top top --single-unit {sv}" in script


def test_openroad_yosys_stage_surfaces_bad_single_unit_type(tmp_path):
    """A config error raised while resolving the yosys opts must NOT be
    swallowed by the `no yosys tool entry` fallback — that guard covers the
    lookup only. Swallowing it would silently downgrade frontend: slang to
    read_verilog and synthesise the wrong thing."""
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError

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
        synth_cfg=_make_synth_cfg(
            model_name="top",
            platform="mylib",
            tool_overrides={"yosys": {"single_unit": "yes"}},
        ),
        root_cfg=root_cfg,
    )
    with pytest.raises(FatalRtlBuddyError, match="must be a bool"):
        or_synth._write_yosys_script(str(fl))


def test_openroad_yosys_stage_warns_on_unknown_yosys_override(tmp_path, caplog):
    """The non-fatal half of the same path: an unknown key warns, and the
    yosys opts still win over the openroad tool's opts."""
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
        synth_cfg=_make_synth_cfg(
            model_name="top",
            platform="mylib",
            tool_overrides={"yosys": {"single-unit": True}},
        ),
        root_cfg=root_cfg,
    )
    with caplog.at_level("WARNING"):
        script = Path(or_synth._write_yosys_script(str(fl))).read_text()

    assert "'single-unit' -> 'single_unit'" in caplog.text
    assert "read_slang" in script
    assert "--single-unit" not in script


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
    assert "liberty" in result.results["desc"].lower()


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


def test_synth_suite_config_loads_xfail_flags(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    (tmp_path / "synth.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: synth_config

        syntheses:
          - name: "synth_xfail"
            desc: "expected-fail synth, non-strict"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "yosys"
            xfail: true
          - name: "synth_xfail_strict"
            desc: "expected-fail synth, strict"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "yosys"
            xfail_strict: true
          - name: "synth_normal"
            desc: "normal"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "yosys"
    """)
    )
    cfg = SynthSuiteConfig(str(tmp_path / "synth.yaml"))
    assert cfg.get_syntheses("synth_xfail")[0].is_xfail() is True
    assert cfg.get_syntheses("synth_xfail")[0].get_xfail_strict() is False
    assert cfg.get_syntheses("synth_xfail_strict")[0].is_xfail() is True
    assert cfg.get_syntheses("synth_xfail_strict")[0].get_xfail_strict() is True
    assert cfg.get_syntheses("synth_normal")[0].is_xfail() is False


# ---------------------------------------------------------------------------
# Log events — human messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event,fields,expected_substrings",
    [
        (
            "synth.single_unit_ignored",
            {"frontend": "verilog", "top": "my_top"},
            ["single_unit", "verilog", "slang"],
        ),
        (
            "synth_tool_config.unknown_override",
            {
                "tool": "yosys",
                "unknown": ["single-unit"],
                "accepted": ["frontend", "single_unit"],
                "hints": ["'single-unit' -> 'single_unit'"],
            },
            [
                "tool_overrides.yosys",
                "single-unit",
                "ignored",
                "single_unit",
                "did you mean",
                "snake_case",
            ],
        ),
        (
            "synth_tool_config.override_type",
            {"tool": "yosys", "key": "single_unit", "expected": "bool", "got": "str"},
            ["tool_overrides.yosys.single_unit", "bool", "str"],
        ),
        (
            "synth_tool_config.override_not_mapping",
            {"tool": "yosys", "got": "str"},
            ["tool_overrides.yosys", "mapping", "str"],
        ),
    ],
)
def test_synth_override_human_messages_are_specific(event, fields, expected_substrings):
    """Every new WARNING/ERROR event needs a case — `rtl_buddy.log` reads the
    human message, and without one it renders through the lossy dotted-event
    fallback and says less than the console does."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(event, fields)
    assert msg != event.replace(".", " ")
    for sub in expected_substrings:
        assert sub in msg, f"{event}: {sub!r} not in {msg!r}"


# ---------------------------------------------------------------------------
# Synthesis netlists are cleared before each run (#469)
# ---------------------------------------------------------------------------


def test_yosys_failed_rerun_leaves_no_stale_netlist(tmp_path, monkeypatch):
    """`synth_netlist.v` / `synth.rtlil` are the fixed-path INPUTS `rb pnr`
    and `rb power` resolve, guarded by `isfile` alone. A failed rerun must
    not leave the last successful run's netlist for them to consume (#469)."""
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

    mapped = Path(ys.artefact_dir) / "synth_netlist.v"
    rtlil = Path(ys.artefact_dir) / "synth.rtlil"
    mapped.write_text("module stale_top(); endmodule\n")
    rtlil.write_text("# stale rtlil\n")

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _fake_managed_process(returncode=1)
    )

    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert not mapped.exists()
    assert not rtlil.exists()


def test_openroad_failed_yosys_stage_leaves_no_stale_netlist(tmp_path, monkeypatch):
    """Stage 2 reads stage 1's netlist off a fixed path having judged stage 1
    by exit code alone, and `rb pnr` / `rb power` read the same file. A failed
    stage 1 must leave neither behind (#469)."""
    from rtl_buddy.tools import synth_openroad as synth_openroad_module

    model = _setup_run(tmp_path)
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="openroad",
        constraints=None,
        params=None,
        defines=None,
        platform="mylib",
        _reglvl=None,
        tool_overrides=None,
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=synth_cfg,
        root_cfg=_FakeRootCfgOR(
            lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
        ),
    )

    mapped = Path(or_synth.artefact_dir) / "synth_netlist.v"
    rtlil = Path(or_synth.artefact_dir) / "synth.rtlil"
    mapped.write_text("module stale_top(); endmodule\n")
    rtlil.write_text("# stale rtlil\n")

    monkeypatch.setattr(
        synth_openroad_module, "task_status", lambda *a, **kw: nullcontext()
    )

    def _yosys_dies(cmd, **kwargs):
        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(synth_openroad_module.subprocess, "run", _yosys_dies)

    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert "Yosys stage failed" in result.results["desc"]
    assert not mapped.exists()
    assert not rtlil.exists()


def test_yosys_filelist_failure_still_clears_the_netlist(tmp_path, monkeypatch):
    """The clear is the FIRST thing run() does, so a rerun that dies before
    yosys — here a filelist error — still leaves nothing for `rb pnr` /
    `rb power` to resolve (#469)."""
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

    mapped = Path(ys.artefact_dir) / "synth_netlist.v"
    rtlil = Path(ys.artefact_dir) / "synth.rtlil"
    mapped.write_text("module stale_top(); endmodule\n")
    rtlil.write_text("# stale rtlil\n")

    def _boom(*a, **kw):
        raise FilelistError("source file vanished")

    monkeypatch.setattr(YosysSynth, "_write_filelist", _boom)

    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert "Filelist error" in result.results["desc"]
    assert not mapped.exists()
    assert not rtlil.exists()


def test_openroad_pre_yosys_gate_still_clears_the_netlist(tmp_path):
    """`run()` clears before every gate, so a return that never reaches yosys
    — here the "requires Liberty" gate, which is also where #472's new
    non-automatic-function gate lands — leaves no netlist behind (#469)."""
    model = _setup_run(tmp_path)
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="openroad",
        constraints=None,
        params=None,
        defines=None,
        platform=None,
        _reglvl=None,
        tool_overrides=None,
    )
    or_synth = _make_openroad(tmp_path, synth_cfg=synth_cfg)

    mapped = Path(or_synth.artefact_dir) / "synth_netlist.v"
    rtlil = Path(or_synth.artefact_dir) / "synth.rtlil"
    mapped.write_text("module stale_top(); endmodule\n")
    rtlil.write_text("# stale rtlil\n")

    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert "requires Liberty" in result.results["desc"]
    assert not mapped.exists()
    assert not rtlil.exists()


def test_openroad_failed_sta_stage_removes_the_netlist(tmp_path, monkeypatch):
    """Stage 1 can succeed and write a netlist before stage 2 fails on
    `link_design` or the SDC. `rb synth` reports FAIL, so it must not leave
    that netlist at the path `rb pnr` / `rb power` resolve (#469)."""
    from rtl_buddy.tools import synth_openroad as synth_openroad_module

    model = _setup_run(tmp_path)
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="openroad",
        constraints=None,
        params=None,
        defines=None,
        platform="mylib",
        _reglvl=None,
        tool_overrides=None,
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=synth_cfg,
        root_cfg=_FakeRootCfgOR(
            lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
        ),
    )
    netlist = Path(or_synth.artefact_dir) / "synth_netlist.v"

    monkeypatch.setattr(
        synth_openroad_module, "task_status", lambda *a, **kw: nullcontext()
    )

    def _stages(cmd, **kwargs):
        class _R:
            returncode = 0

        exe = str(cmd[0])
        if "yosys" in exe:
            # Stage 1 succeeds and publishes the netlist.
            netlist.write_text("module demo_top(); endmodule\n")
            return _R()
        # Stage 2 (OpenROAD STA) dies.
        _R.returncode = 1
        return _R()

    monkeypatch.setattr(synth_openroad_module.subprocess, "run", _stages)

    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert netlist.exists() is False, "a failed synthesis must publish no netlist"


def test_yosys_nonzero_exit_after_writing_removes_the_netlist(tmp_path, monkeypatch):
    """Yosys writes the netlist partway through its script and only then runs
    the trailing `stat`, so it can crash with the netlist already on disk. A
    FAIL must publish nothing (#469)."""
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
    netlist = Path(ys.artefact_dir) / "synth_netlist.v"

    def _writes_then_dies(cmd, stdout, stderr, **kwargs):
        netlist.write_text("module my_module(); endmodule\n")
        return ManagedProcessResult(returncode=1)

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(synth_yosys_module, "run_managed_process", _writes_then_dies)

    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert "code 1" in result.results["desc"]
    assert not netlist.exists()


def test_yosys_error_line_after_writing_removes_the_netlist(tmp_path, monkeypatch):
    """Same for the other post-run gate: an `ERROR:` line in the log fails the
    run, so the netlist Yosys had already written must go (#469)."""
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
    netlist = Path(ys.artefact_dir) / "synth_netlist.v"

    def _writes_then_errors(cmd, stdout, stderr, **kwargs):
        netlist.write_text("module my_module(); endmodule\n")
        stdout.write("ERROR: cell type not found in liberty\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(synth_yosys_module, "run_managed_process", _writes_then_errors)

    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert "ERROR(s) in synthesis log" in result.results["desc"]
    assert not netlist.exists()


def test_openroad_yosys_stage_writes_then_fails_removes_the_netlist(
    tmp_path, monkeypatch
):
    """Stage 1's script writes the netlist before its trailing `stat`, so a
    stage-1 crash also leaves one behind — the `not yosys_ok` return has to
    clear it too (#469)."""
    from rtl_buddy.tools import synth_openroad as synth_openroad_module

    model = _setup_run(tmp_path)
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "cells.lef"
    lef.write_text("")
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="openroad",
        constraints=None,
        params=None,
        defines=None,
        platform="mylib",
        _reglvl=None,
        tool_overrides=None,
    )
    or_synth = _make_openroad(
        tmp_path,
        synth_cfg=synth_cfg,
        root_cfg=_FakeRootCfgOR(
            lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
        ),
    )
    netlist = Path(or_synth.artefact_dir) / "synth_netlist.v"

    monkeypatch.setattr(
        synth_openroad_module, "task_status", lambda *a, **kw: nullcontext()
    )

    def _stage1_writes_then_dies(cmd, **kwargs):
        netlist.write_text("module demo_top(); endmodule\n")

        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(
        synth_openroad_module.subprocess, "run", _stage1_writes_then_dies
    )

    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert "Yosys stage failed" in result.results["desc"]
    assert not netlist.exists()


# ---------------------------------------------------------------------------
# Static-lifetime gate and conflicting-driver gate (#472)
# ---------------------------------------------------------------------------

# The issue's repro, trimmed to the two declarations that matter. `inc` is on
# line 3 and `same` on line 4.
_STATIC_FN_SRC = dedent("""\
    module my_module;
      typedef logic [4:0] ptr_t;
      function ptr_t inc(input ptr_t p);     return p + 1; endfunction
      function bit   same(input ptr_t a, b); return (a == b); endfunction
    endmodule
""")

_AUTOMATIC_FN_SRC = _STATIC_FN_SRC.replace(
    "function ptr_t", "function automatic ptr_t"
).replace("function bit   same", "function automatic bit same")


def _setup_run_with_source(tmp_path, text):
    """A one-source model whose RTL is `text`."""
    sv = tmp_path / "top.sv"
    sv.write_text(text)
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

    return ModelConfig(
        name="my_module", filelist=[f"-v {sv}"], path=str(models_yaml)
    ), sv


def _gate_yosys(
    tmp_path, text, *, opts_overrides=None, tool_overrides=None, defines=None
):
    from rtl_buddy.config.synth import SynthToolOptsFile

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    model, sv = _setup_run_with_source(tmp_path, text)
    opts = SynthToolOptsFile(**(opts_overrides or {}))
    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(name="yosys", tool="yosys", opts=opts)
    )
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="yosys",
        constraints=None,
        params=None,
        defines=defines,
        platform=None,
        _reglvl=None,
        tool_overrides=tool_overrides,
    )
    ys = YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path)
    )
    return ys, sv


def _patch_yosys(monkeypatch, *, returncode=0, write_log=None, calls=None):
    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module,
        "run_managed_process",
        _fake_managed_process(returncode=returncode, write_log=write_log, calls=calls),
    )


def test_static_functions_error_fails_before_yosys(tmp_path, monkeypatch):
    calls = []
    ys, sv = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    _patch_yosys(monkeypatch, calls=calls)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    desc = result.results["desc"]
    assert f"{sv}:3: function inc" in desc
    assert f"{sv}:4: function same" in desc
    # The gate is a *pre*-synthesis check: yosys must not have been started.
    assert calls == []


def test_static_functions_error_logs_an_error_event(tmp_path, monkeypatch, caplog):
    import logging

    ys, _ = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        ys.run()
    # caplog renders the human message, not the dotted event name.
    assert "without an explicit automatic lifetime" in caplog.text


def test_static_functions_warn_passes_and_warns_per_finding(
    tmp_path, monkeypatch, caplog
):
    import logging

    ys, _ = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "warn"}
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        result = ys.run()
    assert isinstance(result, SynthPassResults)
    assert caplog.text.count("has static lifetime") == 2
    # The finding survives into the machine-readable envelope.
    assert result.results["static_function_findings"] == 2


def test_static_functions_allow_is_silent(tmp_path, monkeypatch, caplog):
    import logging

    ys, _ = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        result = ys.run()
    assert isinstance(result, SynthPassResults)
    assert "static lifetime" not in caplog.text
    assert "static_function_findings" not in result.results


def test_static_functions_default_verilog_frontend_warns(tmp_path, monkeypatch, caplog):
    import logging

    ys, _ = _gate_yosys(tmp_path, _STATIC_FN_SRC)
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        result = ys.run()
    assert isinstance(result, SynthPassResults)
    assert "has static lifetime" in caplog.text


def test_static_functions_gate_is_quiet_on_automatic_sources(
    tmp_path, monkeypatch, caplog
):
    import logging

    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        result = ys.run()
    assert isinstance(result, SynthPassResults)
    assert "static lifetime" not in caplog.text


def test_static_functions_mode_settable_per_run_via_tool_overrides(
    tmp_path, monkeypatch
):
    ys, _ = _gate_yosys(
        tmp_path,
        _STATIC_FN_SRC,
        tool_overrides={"yosys": {"static_functions": "error"}},
    )
    _patch_yosys(monkeypatch)
    assert isinstance(ys.run(), SynthFailResults)


def test_static_functions_invalid_mode_is_fatal(tmp_path, monkeypatch):
    from rtl_buddy.errors import FatalRtlBuddyError

    ys, _ = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "loud"}
    )
    _patch_yosys(monkeypatch)
    with pytest.raises(FatalRtlBuddyError, match="static-functions"):
        ys.run()


def test_static_functions_default_is_error_for_slang_and_warn_for_verilog():
    from rtl_buddy.config.synth import SynthToolOpts, resolve_static_functions_mode

    assert resolve_static_functions_mode(SynthToolOpts(frontend="slang")) == "error"
    assert resolve_static_functions_mode(SynthToolOpts(frontend="verilog")) == "warn"
    # An explicit setting always wins over the frontend-derived default.
    assert (
        resolve_static_functions_mode(
            SynthToolOpts(frontend="slang", static_functions="warn")
        )
        == "warn"
    )


def test_conflicting_drivers_default_error_fails_the_run(tmp_path, monkeypatch):
    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_yosys(
        monkeypatch,
        write_log=(
            "Warning: multiple conflicting drivers for bad.\\inc.p [4]:\n"
            "    port A[4] of cell $add\n"
            "Warning: multiple conflicting drivers for bad.\\inc.p [3]:\n"
        ),
    )
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "2 'multiple conflicting drivers'" in result.results["desc"]
    assert "synth.log" in result.results["desc"]


def test_conflicting_drivers_allow_keeps_the_run_passing(tmp_path, monkeypatch):
    ys, _ = _gate_yosys(
        tmp_path,
        _AUTOMATIC_FN_SRC,
        opts_overrides={"static_functions": "allow", "conflicting_drivers": "allow"},
    )
    _patch_yosys(
        monkeypatch,
        write_log="Warning: multiple conflicting drivers for bad.\\inc.p [4]:\n",
    )
    assert isinstance(ys.run(), SynthPassResults)


def test_conflicting_drivers_invalid_mode_is_fatal(tmp_path, monkeypatch):
    from rtl_buddy.errors import FatalRtlBuddyError

    ys, _ = _gate_yosys(
        tmp_path,
        _AUTOMATIC_FN_SRC,
        opts_overrides={"static_functions": "allow", "conflicting_drivers": "warn"},
    )
    _patch_yosys(monkeypatch, write_log="")
    with pytest.raises(FatalRtlBuddyError, match="conflicting-drivers"):
        ys.run()


@pytest.mark.parametrize(
    "line, expected",
    [
        # The real yosys `check` warning, verbatim.
        ("Warning: multiple conflicting drivers for bad.\\inc.p [4]:", True),
        # Same message reported against a source location.
        ("bad.sv:9: Warning: multiple conflicting drivers for bad.\\p:", True),
        # `check -h` help text, echoed into the log by a `help check`.
        ("  - two or more conflicting drivers for one wire", False),
        # A command echo or a comment that merely names the phrase.
        ("yosys> echo multiple conflicting drivers", False),
        ("# multiple conflicting drivers", False),
    ],
)
def test_conflicting_driver_regex_is_anchored_on_the_warning(line, expected):
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    assert bool(find_conflicting_driver_warnings(line + "\n")) is expected


@pytest.mark.parametrize(
    "event, fields, expected_substrings",
    [
        (
            "synth.static_functions",
            {
                "synth": "block",
                "frontend": "slang",
                "count": 2,
                "findings": ["bad.sv:9: function inc", "bad.sv:10: function same"],
            },
            [
                "block",
                "bad.sv:9: function inc",
                "bad.sv:10: function same",
                "slang",
                "static-functions",
            ],
        ),
        (
            "synth.static_function",
            {
                "synth": "block",
                "frontend": "verilog",
                "path": "bad.sv",
                "line": 9,
                "kind": "function",
                "subroutine": "inc",
            },
            ["bad.sv:9", "function inc", "static lifetime"],
        ),
        (
            "synth.conflicting_drivers",
            {"synth": "block", "count": 5, "log": "artefacts/block/synth.log"},
            ["block", "5", "artefacts/block/synth.log", "conflicting-drivers"],
        ),
    ],
)
def test_gate_human_messages_are_specific(event, fields, expected_substrings):
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(event, fields)
    assert msg != event.replace(".", " ")
    for sub in expected_substrings:
        assert sub in msg, f"{event}: {sub!r} not in {msg!r}"


# ---------------------------------------------------------------------------
# Tristate buses are not conflicting drivers (review item 1)
# ---------------------------------------------------------------------------

# Verbatim shape of the yosys `check` output for two `assign bus = en ? d : 'z;`
# on an `inout wire [7:0] bus`. Yosys renders an inout port as "module input".
_TRISTATE_WARNING = (
    "Warning: multiple conflicting drivers for tri_top.\\bus [7]:\n"
    "    port Y[7] of cell $2 ($tribuf)\n"
    "    port Y[7] of cell $0 ($tribuf)\n"
    "    module input bus[7]\n"
)

# The #472 corruption shape: one shared formal driven by two flops.
_SHARED_FORMAL_WARNING = (
    "Warning: multiple conflicting drivers for bad.\\inc.p [4]:\n"
    "    port Q[4] of cell $driver$inc.p ($dff)\n"
    "    port Q[4] of cell $driver$rptr ($dff)\n"
)


def test_tristate_only_warnings_are_not_counted():
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    assert find_conflicting_driver_warnings(_TRISTATE_WARNING * 3) == []


def test_shared_formal_warnings_are_counted():
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    assert len(find_conflicting_driver_warnings(_SHARED_FORMAL_WARNING * 2)) == 2


def test_a_mixed_log_counts_only_the_real_conflicts():
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    log = (
        "Executing CHECK pass.\n"
        + _TRISTATE_WARNING
        + _SHARED_FORMAL_WARNING
        + _TRISTATE_WARNING
        + "Warnings: 3 unique messages, 3 total\n"
    )
    hits = find_conflicting_driver_warnings(log)
    assert len(hits) == 1
    assert "inc.p" in hits[0]


def test_a_tribuf_mixed_with_a_flop_driver_is_still_counted():
    """One non-tristate driver means the bus is not a clean tristate bus."""
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    log = (
        "Warning: multiple conflicting drivers for m.\\w [0]:\n"
        "    port Y[0] of cell $1 ($tribuf)\n"
        "    port Q[0] of cell $2 ($dff)\n"
    )
    assert len(find_conflicting_driver_warnings(log)) == 1


def test_lower_case_tbuf_cell_type_is_recognised():
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    log = (
        "Warning: multiple conflicting drivers for m.\\w [0]:\n"
        "    port Y[0] of cell $1 ($_TBUF_)\n"
        "    port Y[0] of cell $2 ($_TBUF_)\n"
        "    module inout w[0]\n"
    )
    assert find_conflicting_driver_warnings(log) == []


def test_a_warning_with_no_driver_lines_is_counted():
    """Conservative: an unparsed warning is a real one until proven benign."""
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    log = "Warning: multiple conflicting drivers for m.\\w [0]:\nEnd of script.\n"
    assert len(find_conflicting_driver_warnings(log)) == 1


def test_process_action_drivers_are_counted():
    from rtl_buddy.tools.synth_yosys import find_conflicting_driver_warnings

    log = (
        "Warning: multiple conflicting drivers for m.\\w [0]:\n"
        "    action \\w <= \\a (case rule) in process $proc$m.sv:3$1\n"
        "    action \\w <= \\b (sync rule) in process $proc$m.sv:9$2\n"
    )
    assert len(find_conflicting_driver_warnings(log)) == 1


def test_tristate_run_passes_end_to_end(tmp_path, monkeypatch):
    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_yosys(monkeypatch, write_log=_TRISTATE_WARNING * 16)
    assert isinstance(ys.run(), SynthPassResults)


# ---------------------------------------------------------------------------
# Filelist incdirs and defines reach the scan (review items 2 and 3)
# ---------------------------------------------------------------------------


def test_filelist_scan_context_collects_incdirs_and_defines(tmp_path):
    from rtl_buddy.tools.synth_yosys import filelist_scan_context

    fl = tmp_path / "synth.f"
    fl.write_text(
        dedent("""\
            // rtl-buddy generated model filelist
            +incdir+inc
            +incdir+a+b
            +define+SYNTHESIS=1
            +define+DEBUG
            -v top.sv
        """)
    )
    incdirs, defines = filelist_scan_context(str(fl))
    assert incdirs == [
        str(tmp_path / "inc"),
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]
    assert defines == {"SYNTHESIS": "1", "DEBUG": ""}


def test_filelist_scan_context_on_a_missing_file_is_empty(tmp_path):
    from rtl_buddy.tools.synth_yosys import filelist_scan_context

    assert filelist_scan_context(str(tmp_path / "nope.f")) == ([], {})


def test_included_header_is_scanned_by_the_synth_gate(tmp_path, monkeypatch):
    """The reviewer's repro: the declarations moved into an `include`d header
    used to pass with a corrupted netlist."""
    (tmp_path / "fns.svh").write_text(
        "function ptr_t inc(input ptr_t p);     return p + 1; endfunction\n"
    )
    src = dedent("""\
        module my_module;
          typedef logic [4:0] ptr_t;
        `include "fns.svh"
        endmodule
    """)
    ys, _ = _gate_yosys(tmp_path, src, opts_overrides={"static_functions": "error"})
    _patch_yosys(monkeypatch)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "fns.svh:1: function inc" in result.results["desc"]


_IFNDEF_SRC = dedent("""\
    module my_module;
    `ifndef {macro}
      function bit dbg(input bit x); return x; endfunction
    `endif
    endmodule
""")


def test_run_defines_suppress_an_excluded_ifdef_region(tmp_path, monkeypatch):
    """`defines:` on the synth.yaml entry seeds the scan's preprocessor."""
    src = _IFNDEF_SRC.format(macro="FAST_SIM_ONLY")
    ys, _ = _gate_yosys(
        tmp_path,
        src,
        opts_overrides={"static_functions": "error"},
        defines={"FAST_SIM_ONLY": 1},
    )
    _patch_yosys(monkeypatch)
    assert isinstance(ys.run(), SynthPassResults)

    ys_no_define, _ = _gate_yosys(
        tmp_path / "nodef", src, opts_overrides={"static_functions": "error"}
    )
    _patch_yosys(monkeypatch)
    assert isinstance(ys_no_define.run(), SynthFailResults)


# ---------------------------------------------------------------------------
# Machine output (review item 4)
# ---------------------------------------------------------------------------


def test_static_function_findings_reaches_the_machine_payload():
    from rtl_buddy.rtl_buddy import RtlBuddy

    results = SynthPassResults(
        name="s/results", gate_count=46, static_function_findings=2
    )
    row = RtlBuddy._synth_result_row(
        object(), {"synth_name": "block", "results": results}
    )
    assert row["static_function_findings"] == 2
    assert row["gate_count"] == 46


def test_machine_payload_omits_the_field_when_the_gate_found_nothing():
    from rtl_buddy.rtl_buddy import RtlBuddy

    results = SynthPassResults(name="s/results", gate_count=46)
    row = RtlBuddy._synth_result_row(
        object(), {"synth_name": "block", "results": results}
    )
    assert "static_function_findings" not in row


# ---------------------------------------------------------------------------
# Gate-mode validation happens before yosys runs (review item 5)
# ---------------------------------------------------------------------------


def test_conflicting_drivers_invalid_mode_is_fatal_before_yosys(tmp_path, monkeypatch):
    """The mode is resolved at the top of run(), so a misspelling is fatal even
    on a run whose log would never have tripped the gate."""
    from rtl_buddy.errors import FatalRtlBuddyError

    calls = []
    ys, _ = _gate_yosys(
        tmp_path,
        _AUTOMATIC_FN_SRC,
        opts_overrides={"conflicting_drivers": "warn"},
    )
    _patch_yosys(monkeypatch, write_log="", calls=calls)
    with pytest.raises(FatalRtlBuddyError, match="conflicting-drivers"):
        ys.run()
    assert calls == []


# ---------------------------------------------------------------------------
# Frontend-aware error message (review item 11)
# ---------------------------------------------------------------------------


def test_static_functions_message_explains_corruption_for_slang():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "synth.static_functions",
        {
            "synth": "block",
            "frontend": "slang",
            "count": 1,
            "findings": ["bad.sv:9: function inc"],
        },
    )
    assert "silently merges registers" in msg
    assert "not portable" not in msg


def test_static_functions_message_explains_portability_for_verilog():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "synth.static_functions",
        {
            "synth": "block",
            "frontend": "verilog",
            "count": 1,
            "findings": ["bad.sv:9: function inc"],
        },
    )
    assert "inlines each call site" in msg
    assert "not portable" in msg


def test_static_functions_message_reports_the_truncated_remainder():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "synth.static_functions",
        {
            "synth": "block",
            "frontend": "slang",
            "count": 30,
            "findings": ["bad.sv:1: function f1"],
            "truncated": 29,
        },
    )
    assert "and 29 more" in msg


def test_static_functions_event_caps_the_findings_list(tmp_path, monkeypatch, caplog):
    """A machine log line must not carry an unbounded list; the count and the
    dropped total travel with it."""
    import logging

    from rtl_buddy.tools.synth_yosys import MAX_EVENT_FINDINGS

    body = "\n".join(
        f"  function int f{i}; return 1; endfunction"
        for i in range(MAX_EVENT_FINDINGS + 5)
    )
    ys, _ = _gate_yosys(
        tmp_path,
        f"module my_module;\n{body}\nendmodule\n",
        opts_overrides={"static_functions": "error"},
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.ERROR):
        ys.run()
    events = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", "") == "synth.static_functions"
    ]
    assert len(events) == 1
    assert len(events[0].rtl_fields["findings"]) == MAX_EVENT_FINDINGS
    assert events[0].rtl_fields["count"] == MAX_EVENT_FINDINGS + 5
    assert events[0].rtl_fields["truncated"] == 5


# ---------------------------------------------------------------------------
# The OpenROAD backend gets the same gates (review item 8)
# ---------------------------------------------------------------------------


def _gate_openroad(tmp_path, text, *, opts_overrides=None):
    """An OpenROAD backend with Liberty and LEF, over a one-source model.

    `_FakeRootCfgOR.get_synth_tool_cfg` raises, so stage 1 falls back to this
    backend's own opts — which is where the gate settings go.
    """
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.tools.synth_openroad import OpenRoadSynth

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    model, sv = _setup_run_with_source(tmp_path, text)
    lib = tmp_path / "cells.lib"
    lib.write_text("")
    lef = tmp_path / "tech.lef"
    lef.write_text("")
    root_cfg = _FakeRootCfgOR(
        lib_map={"mylib": str(lib)}, lef_map={"mylib": [str(lef)]}
    )
    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="openroad",
            tool="openroad",
            opts=SynthToolOptsFile(**(opts_overrides or {})),
        )
    )
    synth_cfg = SynthConfig(
        name="s",
        desc="",
        model=model,
        tool="openroad",
        constraints=None,
        params=None,
        defines=None,
        platform="mylib",
        _reglvl=None,
        tool_overrides=None,
    )
    or_synth = OpenRoadSynth(
        name="t",
        synth_cfg=synth_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        yosys_executable="yosys",
    )
    fl = Path(or_synth._filelist_path())
    fl.parent.mkdir(parents=True, exist_ok=True)
    fl.write_text(f"-v {sv}\n")
    return or_synth, str(fl), sv


def _patch_openroad_yosys(monkeypatch, *, returncode=0, write_log="", calls=None):
    from rtl_buddy.tools import synth_openroad as or_module

    calls = calls if calls is not None else []

    class _Result:
        def __init__(self, rc):
            self.returncode = rc

    def _run(cmd, stdout=None, stderr=None, **kwargs):
        calls.append(cmd)
        if stdout is not None and write_log:
            stdout.write(write_log)
        return _Result(returncode)

    monkeypatch.setattr(or_module, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(or_module.subprocess, "run", _run)
    return calls


def test_openroad_stage1_fails_on_static_lifetime_functions(tmp_path, monkeypatch):
    or_synth, fl, sv = _gate_openroad(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    calls = _patch_openroad_yosys(monkeypatch)
    gate_count, ok, desc = or_synth._run_yosys_stage(fl)
    assert (gate_count, ok) == (None, False)
    assert f"{sv}:3: function inc" in desc
    # The gate runs before elaboration, so yosys was never started.
    assert calls == []


def test_openroad_run_surfaces_the_gate_description(tmp_path, monkeypatch):
    """`run()` must report the finding, not the generic stage-failure text."""
    or_synth, _, sv = _gate_openroad(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    _patch_openroad_yosys(monkeypatch)
    result = or_synth.run()
    assert isinstance(result, SynthFailResults)
    assert "explicit automatic lifetime" in result.results["desc"]
    assert f"{sv}:3: function inc" in result.results["desc"]


def test_openroad_stage1_warn_mode_records_the_findings(tmp_path, monkeypatch, caplog):
    import logging

    or_synth, fl, _ = _gate_openroad(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "warn"}
    )
    _patch_openroad_yosys(monkeypatch, write_log="Chip area for module: 1.0\n")
    with caplog.at_level(logging.WARNING):
        _, ok, desc = or_synth._run_yosys_stage(fl)
    assert (ok, desc) == (True, None)
    assert caplog.text.count("has static lifetime") == 2
    assert or_synth.static_function_findings == 2


def test_openroad_stage1_allow_mode_is_quiet(tmp_path, monkeypatch, caplog):
    import logging

    or_synth, fl, _ = _gate_openroad(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_openroad_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _, ok, _desc = or_synth._run_yosys_stage(fl)
    assert ok is True
    assert "static lifetime" not in caplog.text


def test_openroad_stage1_fails_on_conflicting_drivers(tmp_path, monkeypatch):
    or_synth, fl, _ = _gate_openroad(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_openroad_yosys(monkeypatch, write_log=_SHARED_FORMAL_WARNING * 3)
    gate_count, ok, desc = or_synth._run_yosys_stage(fl)
    assert (gate_count, ok) == (None, False)
    assert "3 'multiple conflicting drivers'" in desc
    assert "synth_yosys.log" in desc


def test_openroad_stage1_ignores_a_tristate_bus(tmp_path, monkeypatch):
    or_synth, fl, _ = _gate_openroad(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    _patch_openroad_yosys(monkeypatch, write_log=_TRISTATE_WARNING * 8)
    _, ok, desc = or_synth._run_yosys_stage(fl)
    assert (ok, desc) == (True, None)


def test_openroad_stage1_conflicting_drivers_allow(tmp_path, monkeypatch):
    or_synth, fl, _ = _gate_openroad(
        tmp_path,
        _AUTOMATIC_FN_SRC,
        opts_overrides={
            "static_functions": "allow",
            "conflicting_drivers": "allow",
        },
    )
    _patch_openroad_yosys(monkeypatch, write_log=_SHARED_FORMAL_WARNING)
    _, ok, desc = or_synth._run_yosys_stage(fl)
    assert (ok, desc) == (True, None)


# ---------------------------------------------------------------------------
# The gates must not outrun the stale-netlist cleanup (review round 3, item 1)
# ---------------------------------------------------------------------------


def test_yosys_gate_failure_on_rerun_leaves_no_stale_netlist(tmp_path, monkeypatch):
    """A rerun that fails the static-functions gate returns before yosys runs.

    The cleanup is the first action of `run()` precisely so that early return
    still clears the previous run's product: `rb pnr` / `rb power` resolve
    `synth_netlist.v` by `isfile` alone and would otherwise consume a netlist
    from a run whose RTL no longer exists (#469 + #472).
    """
    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    mapped = Path(ys.artefact_dir) / "synth_netlist.v"
    rtlil = Path(ys.artefact_dir) / "synth.rtlil"

    def _yosys_writes_a_netlist(cmd, stdout, stderr, **kwargs):
        mapped.write_text("module my_module(); endmodule\n")
        rtlil.write_text("# rtlil\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _yosys_writes_a_netlist
    )
    assert isinstance(ys.run(), SynthPassResults)
    assert mapped.exists() and rtlil.exists()

    # Rerun the same synthesis after the RTL grew a static-lifetime function.
    rerun, _ = _gate_yosys(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    assert rerun.artefact_dir == ys.artefact_dir
    calls = []
    _patch_yosys(monkeypatch, calls=calls)
    result = rerun.run()

    assert isinstance(result, SynthFailResults)
    assert "explicit automatic lifetime" in result.results["desc"]
    assert calls == []
    assert not mapped.exists()
    assert not rtlil.exists()


def test_yosys_conflicting_drivers_failure_leaves_no_stale_netlist(
    tmp_path, monkeypatch
):
    """The conflicting-driver gate returns after yosys wrote a netlist from a
    design whose shared net folded to `x`. That netlist must not survive."""
    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    mapped = Path(ys.artefact_dir) / "synth_netlist.v"
    mapped.write_text("module stale(); endmodule\n")

    _patch_yosys(monkeypatch, write_log=_SHARED_FORMAL_WARNING * 4)
    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert "multiple conflicting drivers" in result.results["desc"]
    assert not mapped.exists()


def test_openroad_gate_failure_on_rerun_leaves_no_stale_netlist(tmp_path, monkeypatch):
    """Same ordering requirement for the OpenROAD backend's stage-1 gates."""
    from rtl_buddy.tools import synth_openroad as or_module

    or_synth, _, _ = _gate_openroad(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    mapped = Path(or_synth.artefact_dir) / "synth_netlist.v"
    rtlil = Path(or_synth.artefact_dir) / "synth.rtlil"

    class _Ok:
        returncode = 0

    def _tools_succeed(cmd, stdout=None, stderr=None, **kwargs):
        if "yosys" in cmd[0]:
            mapped.write_text("module my_module(); endmodule\n")
            rtlil.write_text("# rtlil\n")
        return _Ok()

    monkeypatch.setattr(or_module, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(or_module.subprocess, "run", _tools_succeed)
    assert isinstance(or_synth.run(), SynthPassResults)
    assert mapped.exists() and rtlil.exists()

    rerun, _, _ = _gate_openroad(
        tmp_path, _STATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    assert rerun.artefact_dir == or_synth.artefact_dir
    calls = _patch_openroad_yosys(monkeypatch)
    result = rerun.run()

    assert isinstance(result, SynthFailResults)
    assert "explicit automatic lifetime" in result.results["desc"]
    assert calls == []
    assert not mapped.exists()
    assert not rtlil.exists()


def test_openroad_conflicting_drivers_failure_leaves_no_stale_netlist(
    tmp_path, monkeypatch
):
    or_synth, _, _ = _gate_openroad(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    mapped = Path(or_synth.artefact_dir) / "synth_netlist.v"
    mapped.write_text("module stale(); endmodule\n")

    _patch_openroad_yosys(monkeypatch, write_log=_SHARED_FORMAL_WARNING * 2)
    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert "multiple conflicting drivers" in result.results["desc"]
    assert not mapped.exists()


# ---------------------------------------------------------------------------
# The scan follows the frontend's compilation-unit boundary (round 3, item 2)
# ---------------------------------------------------------------------------


def _two_source_ifndef_model(tmp_path):
    """Two sources: the first defines a macro the second guards on."""
    a = tmp_path / "a.sv"
    b = tmp_path / "b.sv"
    a.write_text("`define SHARED 1\nmodule a; endmodule\n")
    b.write_text(
        "module my_module;\n`ifndef SHARED\n"
        "  function bit dbg(input bit x); return x; endfunction\n"
        "`endif\nendmodule\n"
    )
    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text(
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: "my_module"
            filelist: ["-v {a}", "-v {b}"]
        """)
    )
    from rtl_buddy.config.model import ModelConfig

    return ModelConfig(
        name="my_module", filelist=[f"-v {a}", f"-v {b}"], path=str(models_yaml)
    )


def _yosys_for_model(tmp_path, model, opts_overrides):
    from rtl_buddy.config.synth import SynthToolOptsFile

    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="yosys", tool="yosys", opts=SynthToolOptsFile(**opts_overrides)
        )
    )
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
    return YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path)
    )


def test_gate_does_not_carry_a_define_between_sources_without_single_unit(
    tmp_path, monkeypatch
):
    """slang compiles each file separately by default, so `SHARED` is not
    defined while b.sv is read and the guarded function IS compiled."""
    model = _two_source_ifndef_model(tmp_path)
    ys = _yosys_for_model(
        tmp_path,
        model,
        {
            "frontend": "slang",
            "plugin_path": "/x/slang.so",
            "static_functions": "error",
        },
    )
    _patch_yosys(monkeypatch)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "function dbg" in result.results["desc"]


def test_gate_carries_a_define_between_sources_under_single_unit(tmp_path, monkeypatch):
    model = _two_source_ifndef_model(tmp_path)
    ys = _yosys_for_model(
        tmp_path,
        model,
        {
            "frontend": "slang",
            "plugin_path": "/x/slang.so",
            "single_unit": True,
            "static_functions": "error",
        },
    )
    _patch_yosys(monkeypatch)
    assert isinstance(ys.run(), SynthPassResults)


def test_verilog_frontend_ignores_single_unit_for_the_scan_too(tmp_path, monkeypatch):
    """`single-unit` is slang-only; with the verilog frontend it is ignored
    (with a warning), so the scan must not honour it either."""
    model = _two_source_ifndef_model(tmp_path)
    ys = _yosys_for_model(
        tmp_path,
        model,
        {"frontend": "verilog", "single_unit": True, "static_functions": "error"},
    )
    _patch_yosys(monkeypatch)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "function dbg" in result.results["desc"]


# ---------------------------------------------------------------------------
# The scan models the Yosys invocation exactly (review round 4, item 1)
# ---------------------------------------------------------------------------


def _gate_yosys_with_filelist_define(tmp_path, src, macro, *, opts_overrides=None):
    """A model whose filelist carries `+define+<macro>` — which the synth flow
    drops, so Yosys never sees it."""
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.config.synth import SynthToolOptsFile

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    sv = tmp_path / "top.sv"
    sv.write_text(src)
    models_yaml = tmp_path / "models.yaml"
    entries = [f"+define+{macro}=1", f"-v {sv}"]
    models_yaml.write_text(
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: "my_module"
            filelist: {entries!r}
        """)
    )
    model = ModelConfig(name="my_module", filelist=entries, path=str(models_yaml))
    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="yosys",
            tool="yosys",
            opts=SynthToolOptsFile(**(opts_overrides or {})),
        )
    )
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
    return YosysSynth(
        "t", synth_cfg=synth_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path)
    )


def test_filelist_define_does_not_suppress_a_finding(tmp_path, monkeypatch):
    """`_write_script()` passes only the run's `defines:` to Yosys, so a
    filelist `+define+` must not make the scan skip a region Yosys elaborates.
    """
    ys = _gate_yosys_with_filelist_define(
        tmp_path,
        _IFNDEF_SRC.format(macro="FAST_SIM_ONLY"),
        "FAST_SIM_ONLY",
        opts_overrides={"static_functions": "error"},
    )
    _patch_yosys(monkeypatch)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "function dbg" in result.results["desc"]


def test_filelist_defines_the_flow_drops_are_warned_about(
    tmp_path, monkeypatch, caplog
):
    import logging

    ys = _gate_yosys_with_filelist_define(
        tmp_path,
        "module my_module; endmodule\n",
        "FAST_SIM_ONLY",
        opts_overrides={"static_functions": "error"},
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ys.run()
    events = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", "") == "synth.filelist_defines_ignored"
    ]
    assert len(events) == 1
    assert events[0].rtl_fields["defines"] == ["FAST_SIM_ONLY"]
    assert events[0].levelno == logging.WARNING


def test_filelist_defines_warning_fires_even_when_the_gate_is_off(
    tmp_path, monkeypatch, caplog
):
    """The divergence exists whatever the gate is set to."""
    import logging

    ys = _gate_yosys_with_filelist_define(
        tmp_path,
        "module my_module; endmodule\n",
        "FAST_SIM_ONLY",
        opts_overrides={"static_functions": "allow"},
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ys.run()
    assert any(
        getattr(r, "rtl_event", "") == "synth.filelist_defines_ignored"
        for r in caplog.records
    )


def test_a_filelist_define_the_run_also_sets_is_not_warned_about(
    tmp_path, monkeypatch, caplog
):
    import logging

    ys = _gate_yosys_with_filelist_define(
        tmp_path,
        "module my_module; endmodule\n",
        "FAST_SIM_ONLY",
        opts_overrides={"static_functions": "error"},
    )
    ys.synth_cfg.defines = {"FAST_SIM_ONLY": 1}
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ys.run()
    assert not any(
        getattr(r, "rtl_event", "") == "synth.filelist_defines_ignored"
        for r in caplog.records
    )


def test_a_filelist_with_no_defines_is_quiet(tmp_path, monkeypatch, caplog):
    import logging

    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "error"}
    )
    _patch_yosys(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ys.run()
    assert not any(
        getattr(r, "rtl_event", "") == "synth.filelist_defines_ignored"
        for r in caplog.records
    )


def test_synthesis_macro_is_implicitly_defined(tmp_path, monkeypatch):
    """Both Yosys frontends define `SYNTHESIS` themselves — `read_verilog` in
    verilog_frontend.cc and yosys-slang unless `--no-synthesis-define`, which
    rtl_buddy never passes. A `` `ifndef SYNTHESIS `` helper is therefore never
    compiled by synthesis and must not be reported."""
    ys, _ = _gate_yosys(
        tmp_path,
        _IFNDEF_SRC.format(macro="SYNTHESIS"),
        opts_overrides={"static_functions": "error"},
    )
    _patch_yosys(monkeypatch)
    assert isinstance(ys.run(), SynthPassResults)


def test_ifdef_synthesis_region_is_still_scanned(tmp_path, monkeypatch):
    """The other side of the implicit define: an `` `ifdef SYNTHESIS `` region
    IS compiled, so a static-lifetime declaration inside it is a finding."""
    src = dedent("""\
        module my_module;
        `ifdef SYNTHESIS
          function bit synth_only(input bit x); return x; endfunction
        `endif
        endmodule
    """)
    ys, _ = _gate_yosys(tmp_path, src, opts_overrides={"static_functions": "error"})
    _patch_yosys(monkeypatch)
    result = ys.run()
    assert isinstance(result, SynthFailResults)
    assert "function synth_only" in result.results["desc"]


def test_lifetime_scan_inputs_seeds_the_implicit_defines(tmp_path):
    from rtl_buddy.tools.synth_yosys import (
        YOSYS_IMPLICIT_DEFINES,
        lifetime_scan_inputs,
    )

    fl = tmp_path / "synth.f"
    fl.write_text("+incdir+inc\n+define+FROM_FILELIST=1\n-v top.sv\n")
    incdirs, defines = lifetime_scan_inputs(str(fl), "s", {"FROM_RUN": 2})
    assert incdirs == [str(tmp_path / "inc")]
    # Filelist macros are reported, never applied.
    assert "FROM_FILELIST" not in defines
    assert defines["FROM_RUN"] == "2"
    assert defines["SYNTHESIS"] == YOSYS_IMPLICIT_DEFINES["SYNTHESIS"]


# ---------------------------------------------------------------------------
# The conflicting-driver gate deletes the netlist it just made (round 4, item 2)
# ---------------------------------------------------------------------------


def test_conflicting_drivers_failure_removes_the_new_netlist(tmp_path, monkeypatch):
    """Yosys already ran `write_verilog`/`write_rtlil` by the time the gate
    fires, so the netlist at the fixed path is THIS run's product. The
    start-of-run cleanup cannot have removed it."""
    ys, _ = _gate_yosys(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    mapped = Path(ys.artefact_dir) / "synth_netlist.v"
    rtlil = Path(ys.artefact_dir) / "synth.rtlil"

    def _yosys_writes_then_warns(cmd, stdout, stderr, **kwargs):
        # The netlist is created by this run, after the start-of-run cleanup.
        mapped.write_text("module corrupted(); endmodule\n")
        rtlil.write_text("# corrupted rtlil\n")
        stdout.write(_SHARED_FORMAL_WARNING * 5)
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _yosys_writes_then_warns
    )
    result = ys.run()

    assert isinstance(result, SynthFailResults)
    assert "multiple conflicting drivers" in result.results["desc"]
    assert not mapped.exists()
    assert not rtlil.exists()


def test_conflicting_drivers_allow_keeps_the_new_netlist(tmp_path, monkeypatch):
    """`allow` means the warnings are accepted, so the netlist must survive."""
    ys, _ = _gate_yosys(
        tmp_path,
        _AUTOMATIC_FN_SRC,
        opts_overrides={
            "static_functions": "allow",
            "conflicting_drivers": "allow",
        },
    )
    mapped = Path(ys.artefact_dir) / "synth_netlist.v"

    def _yosys_writes_then_warns(cmd, stdout, stderr, **kwargs):
        mapped.write_text("module accepted(); endmodule\n")
        stdout.write(_SHARED_FORMAL_WARNING)
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        synth_yosys_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(
        synth_yosys_module, "run_managed_process", _yosys_writes_then_warns
    )
    assert isinstance(ys.run(), SynthPassResults)
    assert mapped.exists()


def test_openroad_conflicting_drivers_removes_the_new_netlist(tmp_path, monkeypatch):
    from rtl_buddy.tools import synth_openroad as or_module

    or_synth, _, _ = _gate_openroad(
        tmp_path, _AUTOMATIC_FN_SRC, opts_overrides={"static_functions": "allow"}
    )
    mapped = Path(or_synth.artefact_dir) / "synth_netlist.v"
    rtlil = Path(or_synth.artefact_dir) / "synth.rtlil"

    class _Ok:
        returncode = 0

    def _yosys_writes_then_warns(cmd, stdout=None, stderr=None, **kwargs):
        mapped.write_text("module corrupted(); endmodule\n")
        rtlil.write_text("# corrupted rtlil\n")
        if stdout is not None:
            stdout.write(_SHARED_FORMAL_WARNING * 3)
        return _Ok()

    monkeypatch.setattr(or_module, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(or_module.subprocess, "run", _yosys_writes_then_warns)
    result = or_synth.run()

    assert isinstance(result, SynthFailResults)
    assert "multiple conflicting drivers" in result.results["desc"]
    assert not mapped.exists()
    assert not rtlil.exists()


# ---------------------------------------------------------------------------
# OpenROAD resolves the gate modes before anything else (round 4, item 4)
# ---------------------------------------------------------------------------


def test_openroad_invalid_gate_mode_is_fatal_before_the_liberty_check(tmp_path):
    """Resolved at the top of run(), so a misspelled mode is fatal even on a
    run that would have returned early for a missing Liberty."""
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.synth_openroad import OpenRoadSynth

    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="openroad",
            tool="openroad",
            opts=SynthToolOptsFile(conflicting_drivers="warn"),
        )
    )
    or_synth = OpenRoadSynth(
        name="t",
        synth_cfg=_make_synth_cfg(platform=None),
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=None,
        yosys_executable="yosys",
    )
    with pytest.raises(FatalRtlBuddyError, match="conflicting-drivers"):
        or_synth.run()


def test_openroad_invalid_static_functions_mode_is_fatal_before_lef(tmp_path):
    from rtl_buddy.config.synth import SynthToolOptsFile
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.synth_openroad import OpenRoadSynth

    lib = tmp_path / "cells.lib"
    lib.write_text("")
    tool_cfg = SynthToolConfig(
        SynthToolConfigFile(
            name="openroad",
            tool="openroad",
            opts=SynthToolOptsFile(static_functions="loud"),
        )
    )
    or_synth = OpenRoadSynth(
        name="t",
        synth_cfg=_make_synth_cfg(platform="mylib"),
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=_FakeRootCfgOR(lib_map={"mylib": str(lib)}, lef_map={}),
        yosys_executable="yosys",
    )
    with pytest.raises(FatalRtlBuddyError, match="static-functions"):
        or_synth.run()


def test_filelist_defines_ignored_has_a_dedicated_human_message():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "synth.filelist_defines_ignored",
        {"synth": "block", "defines": ["A", "B"], "count": 2, "filelist": "synth.f"},
    )
    assert msg != "synth filelist_defines_ignored"
    for sub in ("block", "A, B", "defines:"):
        assert sub in msg
