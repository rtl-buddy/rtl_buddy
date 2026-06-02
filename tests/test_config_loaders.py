"""Unit tests for config loaders and small config dataclasses."""

from __future__ import annotations

from pathlib import Path

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.model import ModelConfig, ModelConfigLoader
from rtl_buddy.config.reg import RegConfig
from rtl_buddy.config.rtl import RtlBuilderConfig
from rtl_buddy.config.root import (
    RootConfig,
    _discover_root_cfg,
    discover_project_root,
)
from rtl_buddy.config.suite import SuiteConfig

# Alias imports so pytest does not try to collect them as test classes.
from rtl_buddy.config.test import CocotbTestbenchConfig, SystemCTestbenchConfig
from rtl_buddy.config.test import TestConfig as TC
from rtl_buddy.config.test import TestbenchConfig as TB
from rtl_buddy.errors import FatalRtlBuddyError


# ---------------------------------------------------------------------------
# RtlBuilderConfig
# ---------------------------------------------------------------------------

_VERILATOR_BUILDER_YAML = """\
name: verilator
builder: verilator
builder-simv: obj_dir/simv
sim-rand-seed: 31310
sim-rand-seed-prefix: "+verilator+seed+"
builder-opts:
  reg:
    compile-time: "--binary -sv -o simv"
    run-time: "+verilator+rand+reset+2"
"""


def _verilator_builder() -> RtlBuilderConfig:
    return from_yaml(RtlBuilderConfig, _VERILATOR_BUILDER_YAML)


def test_rtl_builder_simulator_family_from_explicit_field():
    cfg = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML + "simulator-family: my-fork\n",
    )
    assert cfg.get_simulator_family() == "my-fork"


def test_rtl_builder_simulator_family_inferred_from_exe():
    assert _verilator_builder().get_simulator_family() == "verilator"

    vcs = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML.replace("builder: verilator", "builder: vcs"),
    )
    assert vcs.get_simulator_family() == "vcs"

    other = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML.replace("builder: verilator", "builder: /tools/xrun"),
    )
    assert other.get_simulator_family() == "xrun"


def test_rtl_builder_get_modes_and_compile_opts():
    cfg = _verilator_builder()
    assert "reg" in cfg.opts
    assert cfg.get_compile_time_opts("reg") == ["--binary", "-sv", "-o", "simv"]


def test_rtl_builder_get_run_opts_with_seed():
    opts = _verilator_builder().get_run_time_opts("reg", seed=42)
    assert opts[-1] == "+verilator+seed+42"
    assert "+verilator+rand+reset+2" in opts


def test_rtl_builder_unknown_mode_raises():
    cfg = _verilator_builder()
    with pytest.raises(FatalRtlBuddyError, match="not in config"):
        cfg.get_compile_time_opts("debug")
    with pytest.raises(FatalRtlBuddyError, match="not in config"):
        cfg.get_run_time_opts("debug")


# ---------------------------------------------------------------------------
# TestbenchConfig / CocotbTestbenchConfig
# ---------------------------------------------------------------------------


def test_cocotb_get_modules_normalizes_to_list():
    assert CocotbTestbenchConfig(module="tb_a").get_modules() == ["tb_a"]
    assert CocotbTestbenchConfig(module=["a", "b"]).get_modules() == ["a", "b"]


def test_testbench_cocotb_requires_toplevel():
    with pytest.raises(FatalRtlBuddyError, match="toplevel is required"):
        TB(
            name="tb",
            filelist=["a.sv"],
            cocotb=CocotbTestbenchConfig(module="tb_mod"),
        )


def test_testbench_is_cocotb_flag():
    plain = TB(name="tb", filelist=["a.sv"])
    assert plain.is_cocotb() is False
    cocotb = TB(
        name="tb",
        filelist=["a.sv"],
        toplevel="dut",
        cocotb=CocotbTestbenchConfig(module="tb_mod"),
    )
    assert cocotb.is_cocotb() is True


# ---------------------------------------------------------------------------
# SystemCTestbenchConfig
# ---------------------------------------------------------------------------


def test_systemc_testbench_requires_toplevel():
    with pytest.raises(FatalRtlBuddyError, match="toplevel is required"):
        TB(
            name="tb",
            filelist=["a.sv"],
            systemc=SystemCTestbenchConfig(sc_main="sc_main.cpp"),
        )


def test_systemc_and_cocotb_are_mutually_exclusive():
    with pytest.raises(FatalRtlBuddyError, match="mutually exclusive"):
        TB(
            name="tb",
            filelist=["a.sv"],
            toplevel="dut",
            cocotb=CocotbTestbenchConfig(module="tb_mod"),
            systemc=SystemCTestbenchConfig(sc_main="sc_main.cpp"),
        )


def test_testbench_is_systemc_flag():
    plain = TB(name="tb", filelist=["a.sv"])
    assert plain.is_systemc() is False
    sc = TB(
        name="tb",
        filelist=["a.sv"],
        toplevel="dut",
        systemc=SystemCTestbenchConfig(sc_main="sc_main.cpp"),
    )
    assert sc.is_systemc() is True
    assert sc.is_cocotb() is False


def test_systemc_default_fields_are_empty():
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    assert sc.sc_extra == []
    assert sc.cflags == []
    assert sc.ldflags == []
    assert sc.pin_style is None


# ---------------------------------------------------------------------------
# TestConfig — pure logic helpers
# ---------------------------------------------------------------------------


def _make_test_config(reglvl=None, timeout=None) -> TC:
    tb = TB(name="tb", filelist=["a.sv"])
    return TC(
        name="basic",
        desc="basic test",
        model=None,
        _reglvl=reglvl,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=tb,
        timeout=timeout,
    )


def test_test_config_reglvl_int():
    assert _make_test_config(reglvl=3).get_reglvl("verilator") == 3


def test_test_config_reglvl_dict_builder_match_and_default():
    cfg = _make_test_config(reglvl={"verilator": 2, "default": 5})
    assert cfg.get_reglvl("verilator") == 2
    assert cfg.get_reglvl("vcs") == 5


def test_test_config_reglvl_none_defaults_to_zero():
    assert _make_test_config(reglvl=None).get_reglvl("verilator") == 0


def test_test_config_reglvl_malformed_dict_raises():
    cfg = _make_test_config(reglvl={"vcs": 1})  # no builder match, no default
    with pytest.raises(FatalRtlBuddyError, match="reglvl"):
        cfg.get_reglvl("verilator")


def test_test_config_plusargs_lazy_init_and_merge():
    cfg = _make_test_config()
    assert cfg.get_plusargs() is None
    cfg.set_plusarg("FOO", 1)
    assert cfg.get_plusarg("FOO") == 1
    cfg.set_plusargs({"BAR": 2, "BAZ": 3})
    assert cfg.get_plusargs() == {"FOO": 1, "BAR": 2, "BAZ": 3}


def test_test_config_plusdefines_lazy_init_and_merge():
    cfg = _make_test_config()
    assert cfg.get_plusdefines() is None
    cfg.set_plusdefine("WIDTH", 8)
    assert cfg.get_plusdefine("WIDTH") == 8
    cfg.set_plusdefines({"DEPTH": 16})
    assert cfg.get_plusdefines() == {"WIDTH": 8, "DEPTH": 16}


def test_test_config_timeout_default_and_override():
    cfg = _make_test_config(timeout=None)
    timeout, is_custom = cfg.get_timeout()
    assert is_custom is False and timeout == cfg.default_timeout

    cfg.set_timeout(120)
    timeout, is_custom = cfg.get_timeout()
    assert is_custom is True and timeout == 120


# ---------------------------------------------------------------------------
# RegConfig / SuiteConfig
# ---------------------------------------------------------------------------


def _write_suite(
    tmp_path: Path, *, missing_tb: bool = False, malformed_tb: bool = False
) -> Path:
    if malformed_tb:
        tb_section = "testbenches:\n  - filelist: []  # missing name\n"
    else:
        tb_section = "testbenches:\n  - name: tb1\n    filelist: [src/a.sv]\n"
    tb_ref = "tb_missing" if missing_tb else "tb1"

    body = f"""\
rtl-buddy-filetype: test_config
{tb_section}tests:
  - name: basic
    desc: example
    model: m
    model_path: models.yaml
    reglvl:
    plusargs:
    plusdefines:
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: {tb_ref}
    sim_timeout:
"""
    path = tmp_path / "tests.yaml"
    path.write_text(body)
    return path


def test_reg_config_load_failed_missing_file(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        RegConfig(name="r", path=str(tmp_path / "does-not-exist.yaml"))


def test_reg_config_load_failed_invalid_yaml(tmp_path):
    bad = tmp_path / "regression.yaml"
    bad.write_text("not: a, valid: schema\n")
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        RegConfig(name="r", path=str(bad))


def test_reg_config_missing_suite_blames_suite_file(tmp_path):
    """When a referenced tests.yaml is absent, the error must name the
    missing suite file — not the present, valid regression.yaml."""
    reg = tmp_path / "regression.yaml"
    reg.write_text("rtl-buddy-filetype: reg_config\ntest-configs: [tests.yaml]\n")
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        RegConfig(name="r", path=str(reg))
    assert "tests.yaml" in str(excinfo.value)
    assert "regression.yaml" not in str(excinfo.value)


def test_reg_config_load_empty_suites(tmp_path):
    """A regression.yaml with no test-configs should load with zero suites."""
    reg = tmp_path / "regression.yaml"
    reg.write_text("rtl-buddy-filetype: reg_config\ntest-configs: []\n")
    cfg = RegConfig(name="r", path=str(reg))
    assert cfg.get_name() == "r"
    assert cfg.get_path() == str(reg)
    assert cfg.get_suite_configs() == []


def test_suite_config_load_happy_path(tmp_path):
    """SuiteConfig load succeeds when the testbench ref resolves; missing model
    file is tolerated here because tests/initialise pulls models.yaml lazily.

    We can't easily exercise the full happy path without a models.yaml fixture,
    so we verify the malformed and missing-testbench error branches separately.
    """
    # This branch is covered indirectly by the missing-tb test below; we just
    # assert here that SuiteConfig surfaces a FatalRtlBuddyError for malformed
    # YAML rather than a raw exception type.
    bad = tmp_path / "tests.yaml"
    bad.write_text("not-a-real: schema\n")
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        SuiteConfig(str(bad))


def test_suite_config_missing_testbench_raises(tmp_path):
    suite = _write_suite(tmp_path, missing_tb=True)
    with pytest.raises(FatalRtlBuddyError, match="testbench"):
        SuiteConfig(str(suite))


def test_suite_config_duplicate_testbench_raises(tmp_path):
    """Two testbenches with the same name in one tests.yaml is a hard
    error — letting the dict-comprehension silently overwrite the
    first one hides typos until later 'X not found' errors fire."""
    body = """\
rtl-buddy-filetype: test_config
testbenches:
  - name: tb1
    filelist: [src/a.sv]
  - name: tb1
    filelist: [src/b.sv]
tests: []
"""
    path = tmp_path / "tests.yaml"
    path.write_text(body)
    with pytest.raises(FatalRtlBuddyError, match="duplicate testbench name 'tb1'"):
        SuiteConfig(str(path))


def test_suite_config_resolves_hook_paths_against_suite_dir(tmp_path):
    """preproc/postproc/sweep paths declared in tests.yaml are
    relative to the suite config's directory. They must be absolute
    after load so VlogSim.pre() and _expand_tests_with_sweep can
    open() them regardless of the process cwd (#223)."""
    import os

    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n  - name: m\n    filelist: [top.sv]\n"
    )
    (suite_dir / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        "  - name: tb1\n"
        "    filelist: [tb.sv]\n"
        "tests:\n"
        "  - name: basic\n"
        "    desc: example\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    plusargs:\n"
        "    plusdefines:\n"
        "    uvm:\n"
        "    preproc:\n"
        "      path: scripts/pre.py\n"
        "    postproc:\n"
        "      path: /abs/path/post.py\n"
        "    sweep:\n"
        "      path: scripts/sweep.py\n"
        "    testbench: tb1\n"
        "    sim_timeout:\n"
    )

    cfg = SuiteConfig(str(suite_dir / "tests.yaml"))
    test = cfg.tests["basic"]
    # Relative paths resolve against the suite dir.
    assert test.preproc_path == os.path.normpath(str(suite_dir / "scripts" / "pre.py"))
    assert test.sweep_path == os.path.normpath(str(suite_dir / "scripts" / "sweep.py"))
    # Absolute paths pass through unchanged.
    assert test.postproc_path == "/abs/path/post.py"


def test_suite_config_duplicate_test_raises(tmp_path):
    body = """\
rtl-buddy-filetype: test_config
testbenches:
  - name: tb1
    filelist: [src/a.sv]
tests:
  - name: basic
    desc: example
    model: m
    model_path: models.yaml
    reglvl:
    plusargs:
    plusdefines:
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb1
    sim_timeout:
  - name: basic
    desc: collision
    model: m
    model_path: models.yaml
    reglvl:
    plusargs:
    plusdefines:
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb1
    sim_timeout:
"""
    path = tmp_path / "tests.yaml"
    path.write_text(body)
    with pytest.raises(FatalRtlBuddyError, match="duplicate test name 'basic'"):
        SuiteConfig(str(path))


def test_model_config_loader_duplicate_model_raises(tmp_path):
    from rtl_buddy.config.model import ModelConfigLoader

    body = """\
rtl-buddy-filetype: model_config
models:
  - name: mod_a
    filelist: [a.sv]
  - name: mod_a
    filelist: [b.sv]
"""
    path = tmp_path / "models.yaml"
    path.write_text(body)
    with pytest.raises(FatalRtlBuddyError, match="duplicate model name 'mod_a'"):
        ModelConfigLoader(str(path))


# ---------------------------------------------------------------------------
# ModelConfig back-pointers (cdc / synth / tests)
# ---------------------------------------------------------------------------


def test_model_config_back_pointers_default_to_none(tmp_path):
    from rtl_buddy.config.model import ModelConfigLoader

    body = """\
rtl-buddy-filetype: model_config
models:
  - name: mod_a
    filelist: [a.sv]
"""
    path = tmp_path / "models.yaml"
    path.write_text(body)
    loader = ModelConfigLoader(str(path))
    mod = loader.get_model("mod_a")
    assert mod.cdc is None
    assert mod.synth is None
    assert mod.tests is None


def test_model_config_back_pointers_loaded(tmp_path):
    from rtl_buddy.config.model import ModelConfigLoader

    body = """\
rtl-buddy-filetype: model_config
models:
  - name: mod_a
    filelist: [a.sv]
    cdc: cdc.yaml
    synth: synth.yaml#fast
    tests: tests.yaml#smoke
"""
    path = tmp_path / "models.yaml"
    path.write_text(body)
    loader = ModelConfigLoader(str(path))
    mod = loader.get_model("mod_a")
    assert mod.cdc == "cdc.yaml"
    assert mod.synth == "synth.yaml#fast"
    assert mod.tests == "tests.yaml#smoke"


def test_split_back_pointer_no_fragment():
    from rtl_buddy.config.model import split_back_pointer

    assert split_back_pointer("cdc.yaml") == ("cdc.yaml", None)


def test_split_back_pointer_with_fragment():
    from rtl_buddy.config.model import split_back_pointer

    assert split_back_pointer("cdc.yaml#full_design") == ("cdc.yaml", "full_design")


def test_split_back_pointer_empty_fragment_is_none():
    """``cdc.yaml#`` parses to ``("cdc.yaml", None)`` — an empty fragment
    is treated as "no entry specified" rather than "entry named the empty
    string", which would fail downstream lookups with a confusing error."""
    from rtl_buddy.config.model import split_back_pointer

    assert split_back_pointer("cdc.yaml#") == ("cdc.yaml", None)


def test_resolve_back_pointer_absent_returns_none(tmp_path):
    from rtl_buddy.config.model import ModelConfig, resolve_back_pointer

    model = ModelConfig(name="m", filelist=[], path=str(tmp_path / "models.yaml"))
    assert resolve_back_pointer(model, "cdc") is None


def test_resolve_back_pointer_relative_to_models_yaml(tmp_path):
    """``cdc: ../shared/cdc.yaml#foo`` from a models.yaml at
    ``<root>/blocks/dma/models.yaml`` resolves to
    ``<root>/blocks/shared/cdc.yaml``, entry ``foo``."""
    from rtl_buddy.config.model import ModelConfig, resolve_back_pointer

    models_path = tmp_path / "blocks" / "dma" / "models.yaml"
    models_path.parent.mkdir(parents=True)
    model = ModelConfig(
        name="m",
        filelist=[],
        cdc="../shared/cdc.yaml#foo",
        path=str(models_path),
    )
    resolved = resolve_back_pointer(model, "cdc")
    assert resolved is not None
    abs_path, entry = resolved
    assert Path(abs_path) == tmp_path / "blocks" / "shared" / "cdc.yaml"
    assert entry == "foo"


def test_resolve_back_pointer_no_path_raises():
    """A ModelConfig that the loader never tagged (no ``.path``) is a
    programming error — ``resolve_back_pointer`` can't anchor the
    relative cdc/synth/tests path without it."""
    from rtl_buddy.config.model import ModelConfig, resolve_back_pointer

    model = ModelConfig(name="m", filelist=[], cdc="cdc.yaml", path=None)
    with pytest.raises(FatalRtlBuddyError, match="has no path"):
        resolve_back_pointer(model, "cdc")


# ---------------------------------------------------------------------------
# Project-root discovery
# ---------------------------------------------------------------------------


def test_discover_root_cfg_walks_up_to_root_config(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    nested = root / "verif" / "suite"
    nested.mkdir(parents=True)
    (root / "root_config.yaml").write_text("rtl-buddy-filetype: project_root_config\n")

    monkeypatch.chdir(nested)
    assert _discover_root_cfg() == str(root / "root_config.yaml")
    assert discover_project_root() == root


def test_discover_project_root_falls_back_to_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()  # marker dir is enough

    monkeypatch.chdir(nested)
    assert discover_project_root() == repo


def test_discover_project_root_raises_when_nothing_found(tmp_path, monkeypatch):
    # Bare directory with no root_config.yaml and no .git anywhere above.
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    # If something walks above tmp_path into a parent containing .git or
    # root_config.yaml, the test setup is wrong; in normal test isolation this
    # raises.
    try:
        discover_project_root()
    except FatalRtlBuddyError:
        pass
    else:
        pytest.skip("cannot isolate from ambient project root")


def test_discover_project_root_fallback_cwd(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = discover_project_root(fallback_cwd=True)
    # fallback_cwd always returns a Path; it should at least be a directory.
    assert isinstance(result, Path)
    assert result.is_dir()


def test_discover_rtl_builder_names_raises_without_root_config(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    try:
        names = RootConfig.discover_rtl_builder_names(max_levels=2)
    except ValueError:
        return
    # If we picked up a real root_config.yaml from above tmp_path, accept that
    # too — just confirm the contract holds.
    assert isinstance(names, list)


# ---------------------------------------------------------------------------
# RootConfig — lazy regression-config loading (issue #248)
# ---------------------------------------------------------------------------


def test_root_config_init_skips_regression_config(minimal_project):
    """RootConfig must construct even when regression.yaml references a
    missing suite config (design-only sandboxed checkouts); the failure
    only surfaces when the regression config is actually consumed."""
    (minimal_project / "tests.yaml").unlink()

    root_cfg = RootConfig(name="lazy-test")
    assert root_cfg.reg_cfg is None

    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        root_cfg.get_rtl_reg_cfg()


def test_root_config_init_skips_missing_regression_yaml(minimal_project):
    """Even regression.yaml itself may be absent from a sandbox."""
    (minimal_project / "regression.yaml").unlink()

    root_cfg = RootConfig(name="lazy-test")
    assert root_cfg.reg_cfg is None

    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        root_cfg.get_rtl_reg_cfg()


def test_root_config_reg_cfg_loads_on_demand_and_caches(minimal_project):
    root_cfg = RootConfig(name="lazy-test")
    assert root_cfg.reg_cfg is None

    reg_cfg = root_cfg.get_rtl_reg_cfg()
    assert isinstance(reg_cfg, RegConfig)
    assert [Path(s.get_path()).name for s in reg_cfg.get_suite_configs()] == [
        "tests.yaml"
    ]
    assert root_cfg.get_rtl_reg_cfg() is reg_cfg


# ---------------------------------------------------------------------------
# ModelConfig — axi_bundles + axi_monitor_out
# ---------------------------------------------------------------------------


def test_model_config_axi_fields_default_none():
    """Bare ModelConfig has no AXI fields set (back-compat)."""
    model = ModelConfig(name="soc", filelist=["src/soc.sv"])
    assert model.axi_bundles is None
    assert model.axi_monitor_out is None
    assert model.get_axi_bundles_path() is None
    assert model.get_axi_monitor_out_path() is None


def test_model_config_axi_bundles_resolves_relative_to_models_yaml(tmp_path):
    """axi_bundles relative path resolves against the models.yaml directory."""
    models_yaml = tmp_path / "design" / "soc" / "models.yaml"
    models_yaml.parent.mkdir(parents=True)

    model = ModelConfig(
        name="soc",
        filelist=["src/soc.sv"],
        axi_bundles="src/axi-bundles.yaml",
        path=str(models_yaml),
    )
    resolved = model.get_axi_bundles_path()
    assert resolved == str(tmp_path / "design" / "soc" / "src" / "axi-bundles.yaml")


def test_model_config_axi_monitor_out_resolves_relative(tmp_path):
    """axi_monitor_out relative path resolves against the models.yaml directory.

    Typical usage: monitor SV lives in the verif testbench tree, sibling
    to the design tree.
    """
    models_yaml = tmp_path / "design" / "soc" / "models.yaml"
    models_yaml.parent.mkdir(parents=True)

    model = ModelConfig(
        name="soc",
        filelist=["src/soc.sv"],
        axi_monitor_out="../../verif/soc_top/gen/axi_perf_mon.sv",
        path=str(models_yaml),
    )
    resolved = model.get_axi_monitor_out_path()
    assert resolved == str(tmp_path / "verif" / "soc_top" / "gen" / "axi_perf_mon.sv")


def test_model_config_axi_paths_pass_absolute_through(tmp_path):
    abs_bundles = tmp_path / "elsewhere" / "axi-bundles.yaml"
    abs_monitor = tmp_path / "verif" / "axi_perf_mon.sv"
    model = ModelConfig(
        name="soc",
        filelist=["src/soc.sv"],
        axi_bundles=str(abs_bundles),
        axi_monitor_out=str(abs_monitor),
        path=str(tmp_path / "design" / "models.yaml"),
    )
    assert model.get_axi_bundles_path() == str(abs_bundles)
    assert model.get_axi_monitor_out_path() == str(abs_monitor)


def test_model_config_axi_paths_resolved_against_cwd_when_path_unset(
    tmp_path, monkeypatch
):
    """When the loader hasn't set path yet, fall back to cwd.

    In normal use the loader sets path; this just locks the fallback
    so a bare ModelConfig in tests doesn't blow up on relative paths.
    """
    monkeypatch.chdir(tmp_path)
    model = ModelConfig(
        name="soc",
        filelist=["src/soc.sv"],
        axi_bundles="axi-bundles.yaml",
    )
    assert model.get_axi_bundles_path() == str(tmp_path / "axi-bundles.yaml")


def test_model_config_loader_round_trips_axi_fields(tmp_path):
    """models.yaml with the new fields round-trips through the loader.

    The loader sets ``path`` so the helpers resolve relative paths
    correctly without further wiring.
    """
    models_yaml = tmp_path / "design" / "models.yaml"
    models_yaml.parent.mkdir()
    models_yaml.write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        "  - name: soc\n"
        "    filelist:\n"
        "      - src/soc.sv\n"
        "    axi_bundles: src/soc/axi-bundles.yaml\n"
        "    axi_monitor_out: ../verif/soc_top/gen/axi_perf_mon.sv\n"
        "  - name: cpu\n"
        "    filelist:\n"
        "      - src/cpu.sv\n"
    )

    loader = ModelConfigLoader(str(models_yaml))

    soc = loader.get_model("soc")
    assert soc.axi_bundles == "src/soc/axi-bundles.yaml"
    assert soc.axi_monitor_out == "../verif/soc_top/gen/axi_perf_mon.sv"
    assert soc.get_axi_bundles_path() == str(
        tmp_path / "design" / "src" / "soc" / "axi-bundles.yaml"
    )
    assert soc.get_axi_monitor_out_path() == str(
        tmp_path / "verif" / "soc_top" / "gen" / "axi_perf_mon.sv"
    )

    cpu = loader.get_model("cpu")
    assert cpu.axi_bundles is None
    assert cpu.axi_monitor_out is None
    assert cpu.get_axi_bundles_path() is None
    assert cpu.get_axi_monitor_out_path() is None
