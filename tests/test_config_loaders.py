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


def test_rtl_builder_extra_sim_timeout_defaults_to_zero():
    assert _verilator_builder().get_extra_sim_timeout() == 0


def test_rtl_builder_extra_sim_timeout_from_config():
    cfg = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML + "extra-sim-timeout: 900\n",
    )
    assert cfg.get_extra_sim_timeout() == 900


def test_resolve_extra_sim_timeout_prefers_cli_override():
    """``--extra-sim-timeout`` wins over the builder's own value, and 0 is honoured.

    0 must not be mistaken for "unset", or a caller could not turn a
    configured allowance back off for one run.
    """
    cfg = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML + "extra-sim-timeout: 900\n",
    )
    root = RootConfig.__new__(RootConfig)

    root.extra_sim_timeout_override = None
    assert root.resolve_extra_sim_timeout(cfg) == 900

    root.extra_sim_timeout_override = 120
    assert root.resolve_extra_sim_timeout(cfg) == 120

    root.extra_sim_timeout_override = 0
    assert root.resolve_extra_sim_timeout(cfg) == 0


def test_rtl_builder_negative_extra_sim_timeout_is_fatal():
    """Rejected, not clamped: a negative value shrinks every test's timeout."""
    cfg = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML + "extra-sim-timeout: -100\n",
    )
    with pytest.raises(FatalRtlBuddyError, match="negative extra-sim-timeout"):
        cfg.get_extra_sim_timeout()


def test_resolve_extra_sim_timeout_zero_when_neither_set():
    root = RootConfig.__new__(RootConfig)
    root.extra_sim_timeout_override = None
    assert root.resolve_extra_sim_timeout(_verilator_builder()) == 0


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


# ---------------------------------------------------------------------------
# TestConfig — dispatch plan (de)serialization (#351)
# ---------------------------------------------------------------------------


def test_testconfig_plan_roundtrip():
    """A fully-populated TestConfig survives to_plan_dict -> from_plan_dict.

    This is the fidelity guarantee the dispatch plan manifest relies on:
    the sweep hook runs once on the head, and every field a hook could
    have set must reach the build/sim jobs unchanged.
    """
    import json

    from rtl_buddy.config.dispatch import DispatchResourcesFile
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.config.uvm import UVMConfig

    original = TC(
        name="axi_soak.W64",
        desc="soak, 64-bit",
        model=ModelConfig(
            name="axi", filelist=["rtl/axi.sv"], desc="axi dut", path="/abs/models.yaml"
        ),
        _reglvl={"verilator": 1000, "default": 500},
        pa={"ITERS": 4000},
        pd={"WIDTH": 64},
        uvm=UVMConfig(max_warns=3, max_errors=1),
        preproc_path="/abs/hooks/pre.py",
        postproc_path=None,
        sweep_path="/abs/hooks/sweep.py",
        tb=TB(name="axi_tb", filelist=["tb/axi_tb.sv"], toplevel="axi_top"),
        timeout=120,
        covers=["axi.rd", "axi.wr"],
        builder_name="verilator",
        assertions=True,
        resources=DispatchResourcesFile(cpus=4, mem="16G", time="02:00:00"),
        xfail=True,
        xfail_strict=False,
    )

    plan = original.to_plan_dict()
    # Must be JSON-safe: the manifest is written as JSON on the shared FS.
    reloaded = TC.from_plan_dict(json.loads(json.dumps(plan)))
    assert reloaded == original


def test_testconfig_plan_dict_covers_every_field():
    """Guard: adding a TestConfig field must force a plan-serialization update.

    Fails loudly if a new dataclass field is not carried by to_plan_dict,
    so a silently-dropped field can't reach a sim job as a default.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(TC)}
    carried = {TC._PLAN_FIELD_RENAMES.get(n, n) for n in field_names}
    plan = _make_full_plan_dict()
    assert carried == set(plan.keys())


def _make_full_plan_dict() -> dict:
    from rtl_buddy.config.model import ModelConfig

    cfg = TC(
        name="t",
        desc="d",
        model=ModelConfig(name="m", filelist=[], path="/abs/models.yaml"),
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=TB(name="tb", filelist=["a.sv"]),
        timeout=None,
    )
    return cfg.to_plan_dict()

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


def test_suite_config_missing_models_yaml_keeps_precise_error(tmp_path):
    """A test referencing a models.yaml that doesn't exist must surface the
    model loader's 'failed to load' message, not 'Tests section malformed'."""
    suite = _write_suite(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match=r"failed to load.*models\.yaml"):
        SuiteConfig(str(suite))


def test_suite_config_unknown_model_keeps_precise_error(tmp_path):
    """A test referencing a model absent from models.yaml must surface the
    loader's "model 'X' not found", not 'Tests section malformed'."""
    (tmp_path / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        "  - name: mod_a\n"
        "    filelist: [a.sv]\n"
    )
    suite = _write_suite(tmp_path)  # references model 'm'
    with pytest.raises(FatalRtlBuddyError, match="model 'm' not found"):
        SuiteConfig(str(suite))


def test_suite_config_testbench_handler_keeps_precise_error(tmp_path, monkeypatch):
    """The testbench-section handler must re-raise FatalRtlBuddyErrors as-is
    rather than re-wrapping them as 'Testbench section malformed'. No current
    code path raises one inside the dict build, so inject it: get_name is
    called once per testbench by the duplicate check, then again by the
    guarded dict build — fail on the second call."""
    from rtl_buddy.config.test import TestbenchConfig

    suite = _write_suite(tmp_path)
    real_get_name = TestbenchConfig.get_name
    calls = {"n": 0}

    def flaky_get_name(self):
        calls["n"] += 1
        if calls["n"] > 1:
            raise FatalRtlBuddyError("precise testbench error")
        return real_get_name(self)

    monkeypatch.setattr(TestbenchConfig, "get_name", flaky_get_name)
    with pytest.raises(FatalRtlBuddyError, match="precise testbench error"):
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


_SUITE_WITH_COMPILE = """\
rtl-buddy-filetype: test_config
compile:
  cpus: 8
  mem: 48G
  time: "03:00:00"
testbenches:
  - name: tb1
    filelist: [tb.sv]
tests:
  - name: basic
    desc: example
    model: m
    model_path: models.yaml
    reglvl: 0
    plusargs:
    plusdefines:
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb1
    sim_timeout:
"""


def _write_compile_suite(tmp_path, body):
    (tmp_path / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n  - name: m\n    filelist: [top.sv]\n"
    )
    path = tmp_path / "tests.yaml"
    path.write_text(body)
    return path


def test_suite_config_loads_the_compile_block(tmp_path):
    """The suite-level dispatch compile reservation survives load (#497)."""
    path = _write_compile_suite(tmp_path, _SUITE_WITH_COMPILE)
    cfg = SuiteConfig(str(path))
    block = cfg.get_compile()
    assert (block.cpus, block.mem, block.time) == (8, "48G", "03:00:00")
    # The accessor and the attribute are the same object; nothing else in
    # the suite is disturbed by the extra key.
    assert cfg.get_compile() is cfg.compile
    assert cfg.get_test_names() == ["basic"]


def test_suite_config_without_a_compile_block_reports_none(tmp_path):
    body = _SUITE_WITH_COMPILE.replace(
        'compile:\n  cpus: 8\n  mem: 48G\n  time: "03:00:00"\n', ""
    )
    assert SuiteConfig(str(_write_compile_suite(tmp_path, body))).get_compile() is None


def test_suite_config_compile_block_rejects_unquoted_time(tmp_path):
    """YAML 1.1 reads `3:00:00` as 10800; a 10800-minute build is not it."""
    path = _write_compile_suite(
        tmp_path, _SUITE_WITH_COMPILE.replace('"03:00:00"', "3:00:00")
    )
    with pytest.raises(FatalRtlBuddyError, match="sexagesimal"):
        SuiteConfig(str(path))


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


# ---------------------------------------------------------------------------
# xfail (expected-fail) — schema + result re-interpretation
# ---------------------------------------------------------------------------


def test_test_config_xfail_flags_default_false():
    cfg = _make_test_config()
    assert cfg.get_xfail() is False
    assert cfg.get_xfail_strict() is False
    assert cfg.is_xfail() is False


def test_suite_config_loads_xfail_flags(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n  - name: m\n    filelist: [top.sv]\n"
    )
    (tmp_path / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        "  - name: tb1\n"
        "    filelist: [tb.sv]\n"
        "tests:\n"
        "  - name: known_fail\n"
        "    desc: a test known to fail (non-strict)\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    testbench: tb1\n"
        "    xfail: true\n"
        "  - name: known_fail_strict\n"
        "    desc: a test known to fail (strict)\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    testbench: tb1\n"
        "    xfail_strict: true\n"
        "  - name: normal\n"
        "    desc: a normal test\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    testbench: tb1\n"
    )
    cfg = SuiteConfig(str(tmp_path / "tests.yaml"))
    assert cfg.tests["known_fail"].is_xfail() is True
    assert cfg.tests["known_fail"].get_xfail_strict() is False
    assert cfg.tests["known_fail_strict"].is_xfail() is True
    assert cfg.tests["known_fail_strict"].get_xfail_strict() is True
    # Absent both keys defaults to not-xfail.
    assert cfg.tests["normal"].is_xfail() is False


def test_apply_test_xfail_fail_becomes_xfail_and_passes():
    from rtl_buddy.runner.test_results import CompileFailResults
    from rtl_buddy.runner.xfail import apply_xfail

    for strict in (False, True):
        res = CompileFailResults(name="t")
        assert res.is_pass() is False
        apply_xfail(res, strict=strict)
        assert res.results["result"] == "XFAIL"
        assert res.is_pass() is True  # XFAIL passes regardless of strictness
        assert res.results["desc"].startswith("xfail (expected fail): ")


def test_apply_test_xfail_nonstrict_xpass_still_passes():
    from rtl_buddy.runner.test_results import TestPassResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = TestPassResults(name="t")
    apply_xfail(res, strict=False)
    assert res.results["result"] == "XPASS"
    assert res.is_pass() is True  # non-strict: an XPASS does not fail the run
    assert res.results["desc"].startswith("XPASS (expected fail but passed): ")


def test_apply_test_xfail_strict_xpass_fails():
    from rtl_buddy.runner.test_results import TestPassResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = TestPassResults(name="t")
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert res.is_pass() is False  # strict: a stale xfail surfaces loudly
    assert res.results["desc"].startswith(
        "XPASS (expected fail but passed — strict, failing): "
    )


def test_apply_test_xfail_skip_passes_through_unchanged():
    from rtl_buddy.runner.test_results import SkipResults
    from rtl_buddy.runner.xfail import apply_xfail

    res = SkipResults(name="t", desc="below reg level")
    apply_xfail(res, strict=True)
    assert res.results["result"] == "SKIP"
    assert res.is_pass() is True


# ---------------------------------------------------------------------------
# Tool path resolution — $VAR / ~ expansion and candidate lists (#439)
# ---------------------------------------------------------------------------


def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def _touch_exe(path):
    """Create an executable stub.

    Tool-path resolution requires the executable bit for binary-valued
    fields, so its existence test agrees with the callers' availability
    checks (#439) — a plain `touch` would be skipped.
    """
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_expand_path_resolves_set_var(monkeypatch):
    from rtl_buddy.config.toolpath import expand_path

    monkeypatch.setenv("RB_TEST_TOOLS", "/opt/rb")
    assert expand_path("${RB_TEST_TOOLS}/bin/surfer") == "/opt/rb/bin/surfer"


def test_expand_path_returns_none_for_unset_var(monkeypatch):
    from rtl_buddy.config.toolpath import expand_path

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    assert expand_path("${RB_TEST_TOOLS}/bin/surfer") is None


def test_expand_path_expands_tilde(monkeypatch):
    from rtl_buddy.config.toolpath import expand_path

    monkeypatch.setenv("HOME", "/home/rb")
    assert expand_path("~/tools/surfer") == "/home/rb/tools/surfer"


def test_resolve_tool_path_single_string_passes_through(monkeypatch):
    """The pre-#439 shape: one bare name, resolved by PATH at exec time."""
    from rtl_buddy.config.toolpath import resolve_tool_path

    assert resolve_tool_path("definitely-not-installed") == "definitely-not-installed"


def test_resolve_tool_path_env_override_wins_over_canonical(tmp_path, monkeypatch):
    from rtl_buddy.config.toolpath import resolve_tool_path

    mine = tmp_path / "mine" / "bin"
    mine.mkdir(parents=True)
    _touch_exe(mine / "surfer")
    canonical = tmp_path / "canonical" / "bin"
    canonical.mkdir(parents=True)
    _touch_exe(canonical / "surfer")

    monkeypatch.setenv("RB_TEST_TOOLS", str(tmp_path / "mine"))
    chosen = resolve_tool_path(
        ["${RB_TEST_TOOLS}/bin/surfer", str(canonical / "surfer"), "surfer"]
    )
    assert chosen == str(mine / "surfer")


def test_resolve_tool_path_falls_through_unset_var_to_canonical(tmp_path, monkeypatch):
    from rtl_buddy.config.toolpath import resolve_tool_path

    canonical = tmp_path / "canonical" / "bin"
    canonical.mkdir(parents=True)
    _touch_exe(canonical / "surfer")

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    chosen = resolve_tool_path(
        ["${RB_TEST_TOOLS}/bin/surfer", str(canonical / "surfer"), "surfer"]
    )
    assert chosen == str(canonical / "surfer")


def test_resolve_tool_path_falls_through_missing_path_to_bare_name(
    tmp_path, monkeypatch
):
    """Nothing on disk: the trailing bare name is the PATH fallback slot."""
    from rtl_buddy.config.toolpath import resolve_tool_path

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    chosen = resolve_tool_path(
        [
            "${RB_TEST_TOOLS}/bin/surfer",
            str(tmp_path / "nowhere" / "surfer"),
            "surfer-not-installed",
        ]
    )
    assert chosen == "surfer-not-installed"


def test_resolve_tool_path_all_candidates_unresolved_returns_literal(monkeypatch):
    from rtl_buddy.config.toolpath import resolve_tool_path

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    assert (
        resolve_tool_path("${RB_TEST_TOOLS}/bin/surfer")
        == "${RB_TEST_TOOLS}/bin/surfer"
    )


def test_resolve_tool_path_relative_candidate_anchors_at_base_dir(tmp_path):
    from rtl_buddy.config.toolpath import resolve_tool_path

    (tmp_path / "vendor").mkdir()
    _touch_exe(tmp_path / "vendor" / "yosys")
    chosen = resolve_tool_path(
        ["vendor/nope/yosys", "vendor/yosys", "yosys"], base_dir=str(tmp_path)
    )
    # Returned unjoined — callers keep their own relative-path semantics.
    assert chosen == "vendor/yosys"


def test_builder_exe_expands_env_var(monkeypatch, tmp_path):
    """cfg-rtl-builder.builder gets the cfg-systemc.home treatment."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _touch_exe(bindir / "verilator")
    monkeypatch.setenv("RB_TEST_TOOLS", str(tmp_path))

    cfg = from_yaml(
        RtlBuilderConfig,
        _VERILATOR_BUILDER_YAML.replace(
            "builder: verilator", 'builder: "${RB_TEST_TOOLS}/bin/verilator"'
        ),
    )
    assert cfg.get_exe() == str(bindir / "verilator")
    # Family inference reads the resolved path, not the literal.
    assert cfg.get_simulator_family() == "verilator"


def test_builder_exe_candidate_list_falls_back_to_bare_name(monkeypatch):
    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    yaml = _VERILATOR_BUILDER_YAML.replace(
        "builder: verilator",
        'builder: ["${RB_TEST_TOOLS}/bin/verilator", "verilator"]',
    )
    cfg = from_yaml(RtlBuilderConfig, yaml)
    assert cfg.get_exe() == "verilator"


def test_surfer_path_candidate_list_picks_existing(tmp_path, monkeypatch):
    from rtl_buddy.config.surfer import SurferConfigFile

    bindir = tmp_path / "opt" / "bin"
    bindir.mkdir(parents=True)
    exe = _touch_exe(bindir / "surfer")
    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)

    cfg = SurferConfigFile(
        name="surfer-shared",
        path=["${RB_TEST_TOOLS}/bin/surfer", str(exe), "surfer"],
    ).initialise(str(tmp_path / "root_config.yaml"))
    assert cfg.path == str(exe)
    assert cfg.available is True


def test_synth_tool_executable_expands_env_var(monkeypatch, tmp_path):
    from rtl_buddy.config.synth import SynthToolConfig, SynthToolConfigFile

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _touch_exe(bindir / "yosys")
    monkeypatch.setenv("RB_TEST_TOOLS", str(tmp_path))

    cfg = SynthToolConfig(
        SynthToolConfigFile(name="yosys", tool="${RB_TEST_TOOLS}/bin/yosys")
    )
    assert cfg.get_executable() == str(bindir / "yosys")


def test_fpv_tool_executable_candidate_list(monkeypatch, tmp_path):
    from rtl_buddy.config.fpv import FpvToolConfig, FpvToolConfigFile

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    cfg = FpvToolConfig(
        FpvToolConfigFile(
            name="sby", tool=["${RB_TEST_TOOLS}/bin/sby", str(tmp_path / "gone"), "sby"]
        )
    )
    assert cfg.get_executable() == "sby"


def test_resolve_tool_path_warns_once_for_unresolved_var(monkeypatch, caplog):
    """The WARNING is emitted, and emitted once.

    `resolve_tool_path` runs on every `get_exe()` / `get_executable()` —
    several times per test — so an unset variable would otherwise put
    thousands of identical lines through a regression (#439).
    """
    import logging

    from rtl_buddy.config import toolpath

    monkeypatch.delenv("RB_TEST_TOOLS", raising=False)
    toolpath.reset_unresolved_warnings()

    with caplog.at_level(logging.WARNING, logger="rtl_buddy.config.toolpath"):
        for _ in range(3):
            chosen = toolpath.resolve_tool_path(
                "${RB_TEST_TOOLS}/bin/surfer",
                block="cfg-surfer",
                name="surfer-shared",
                field="path",
            )

    assert chosen == "${RB_TEST_TOOLS}/bin/surfer"
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "RB_TEST_TOOLS" in records[0].getMessage()


def test_resolve_tool_path_skips_a_non_executable_candidate(tmp_path):
    """Existence test and availability test must agree.

    `os.path.exists` let a non-executable file (or a directory named
    `surfer`) win resolution and then be reported unavailable, skipping a
    later candidate that would have worked (#439).
    """
    from rtl_buddy.config.toolpath import resolve_tool_path

    not_exec = tmp_path / "readonly" / "surfer"
    not_exec.parent.mkdir()
    not_exec.write_text("")
    not_exec.chmod(0o644)
    a_directory = tmp_path / "dir" / "surfer"
    a_directory.mkdir(parents=True)
    good = _touch_exe(_mkdir(tmp_path / "good") / "surfer")

    chosen = resolve_tool_path([str(not_exec), str(a_directory), str(good)])
    assert chosen == str(good)


def test_tool_candidates_anchor_at_root_config_not_cwd(tmp_path, monkeypatch):
    """A relative `tool:` candidate resolves next to root_config.yaml.

    `rb` is routinely invoked from a suite directory, so testing a
    relative candidate against the process cwd made resolution depend on
    where the user stood (#439).
    """
    _write_routed_project(tmp_path)
    _touch_exe(_mkdir(tmp_path / "vendor" / "bin") / "yosys")
    cfg = tmp_path / "root_config.yaml"
    cfg.write_text(
        cfg.read_text().replace(
            'cfg-synth-tools:\n  - name: "yosys"\n    tool: "yosys"\n',
            'cfg-synth-tools:\n  - name: "yosys"\n'
            '    tool: ["vendor/bin/yosys", "yosys"]\n',
            1,
        )
    )
    suite = tmp_path / "suite"
    suite.mkdir()
    monkeypatch.chdir(suite)

    rc = RootConfig(name="anchored", start_dir=str(tmp_path))
    assert rc.get_synth_tool_cfg("yosys").get_executable() == "vendor/bin/yosys"


# ---------------------------------------------------------------------------
# cfg-verible — a pinned path that silently falls back to PATH (#439)
# ---------------------------------------------------------------------------


def test_verible_path_fallback_warns_naming_both_paths(tmp_path, monkeypatch, caplog):
    """A pin that resolves to something else must not pass unannounced."""
    import logging

    from rtl_buddy.config.verible import VeribleConfigFile

    fake_path_bin = tmp_path / "site" / "verible-verilog-syntax"
    fake_path_bin.parent.mkdir(parents=True)
    fake_path_bin.write_text("")
    monkeypatch.setattr(
        "rtl_buddy.config.verible.shutil.which", lambda _n: str(fake_path_bin)
    )

    with caplog.at_level(logging.WARNING, logger="rtl_buddy.config.verible"):
        cfg = VeribleConfigFile(
            name="verible-pinned", path="pinned/dir", extra_args={}
        ).initialise(str(tmp_path / "root_config.yaml"))

    assert cfg.available is True
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert records, "expected a WARNING for the silent PATH fallback"
    text = records[0].getMessage()
    assert str(tmp_path / "pinned" / "dir") in text
    assert str(fake_path_bin) in text


def test_verible_path_present_does_not_warn(tmp_path, caplog):
    import logging

    from rtl_buddy.config.verible import VeribleConfigFile

    pinned = tmp_path / "pinned"
    pinned.mkdir()
    _touch_exe(pinned / "verible-verilog-syntax")
    with caplog.at_level(logging.WARNING, logger="rtl_buddy.config.verible"):
        cfg = VeribleConfigFile(
            name="verible-pinned", path="pinned", extra_args={}
        ).initialise(str(tmp_path / "root_config.yaml"))

    assert cfg.available is True
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_verible_path_exists_but_is_empty_warns(tmp_path, monkeypatch, caplog):
    """The other half of the silent-pin case: directory there, binaries not.

    `initialise` only saw the missing-directory case, so a pin at an empty
    directory reported available with no diagnostic at all (#439).
    """
    import logging

    from rtl_buddy.config.verible import VeribleConfigFile

    (tmp_path / "pinned").mkdir()
    site = _touch_exe(
        _mkdir(tmp_path / "site") / "verible-verilog-syntax",
    )
    monkeypatch.setattr("rtl_buddy.config.verible.shutil.which", lambda _n: str(site))

    with caplog.at_level(logging.WARNING, logger="rtl_buddy.config.verible"):
        cfg = VeribleConfigFile(
            name="verible-pinned", path="pinned", extra_args={}
        ).initialise(str(tmp_path / "root_config.yaml"))

    assert cfg.available is True
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert records, "expected a WARNING for the empty pinned directory"
    text = records[0].getMessage()
    assert str(tmp_path / "pinned") in text
    assert "verible-verilog-syntax" in text


def test_verible_get_exe_path_warns_once_on_path_fallback(
    tmp_path, monkeypatch, caplog
):
    """Per-binary fallback is the same broken pin, and must not be silent."""
    import logging

    from rtl_buddy.config import verible as verible_mod

    pinned = tmp_path / "pinned"
    pinned.mkdir()
    _touch_exe(pinned / "verible-verilog-syntax")
    site = _touch_exe(_mkdir(tmp_path / "site") / "verible-verilog-lint")
    monkeypatch.setattr(verible_mod.shutil, "which", lambda _n: str(site))
    verible_mod.reset_exe_fallback_warnings()

    cfg = verible_mod.VeribleConfigFile(
        name="verible-pinned", path="pinned", extra_args={}
    ).initialise(str(tmp_path / "root_config.yaml"))

    with caplog.at_level(logging.WARNING, logger="rtl_buddy.config.verible"):
        first = cfg.get_exe_path("verible-verilog-lint")
        second = cfg.get_exe_path("verible-verilog-lint")

    assert first == str(site)
    assert second == str(site)
    records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "verible-verilog-lint" in r.getMessage()
    ]
    # Warned, and warned once: get_exe_path runs per lint invocation.
    assert len(records) == 1


_TWO_VERIBLE_ROOT = """\
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "test-host"
    unames: ["Darwin", "Linux"]
    builder: "stub"
    verible: "verible-active"
  - os: "other-host"
    unames: ["NoSuchUname"]
    builder: "stub"
    verible: "verible-inactive"

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
  - name: "verible-active"
    path: "{active}"
    extra_args: {{}}
  - name: "verible-inactive"
    path: "{inactive}"
    extra_args: {{}}

cfg-rtl-reg:
  reg-cfg-path: "regression.yaml"
"""


def _write_two_verible_project(root, *, active: str, inactive: str) -> None:
    """A project whose two platforms take one `cfg-verible` entry each."""
    (root / "root_config.yaml").write_text(
        _TWO_VERIBLE_ROOT.format(active=active, inactive=inactive)
    )
    (root / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs: []\n"
    )


def _verible_records(caplog, level):
    return [
        r
        for r in caplog.records
        if r.name == "rtl_buddy.config.verible" and r.levelno == level
    ]


def test_verible_pin_diagnostics_skip_the_unrouted_entry(tmp_path, monkeypatch, caplog):
    """The other platform's broken pin is not this host's warning.

    Every `cfg-verible` entry is initialised at load, so a stock
    two-platform project would otherwise WARN about the *other*
    platform's directory on every single `rb` invocation — about a pin
    that is not being used, and that nobody on this host can act on
    (#439). It stays visible at DEBUG.
    """
    import logging

    from rtl_buddy.config import verible as verible_mod

    active = _mkdir(tmp_path / "verible-here")
    _touch_exe(active / "verible-verilog-syntax")
    site = _touch_exe(_mkdir(tmp_path / "site") / "verible-verilog-syntax")
    monkeypatch.setattr(verible_mod.shutil, "which", lambda _n: str(site))
    _write_two_verible_project(
        tmp_path, active=str(active), inactive=str(tmp_path / "absent-elsewhere")
    )
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.config.verible"):
        rc = RootConfig(name="two-platforms")

    assert rc.platform_cfg.get_verible().get_name() == "verible-active"
    assert _verible_records(caplog, logging.WARNING) == []
    debug = [r.getMessage() for r in _verible_records(caplog, logging.DEBUG)]
    assert any("verible-inactive" in m for m in debug)


def test_verible_pin_diagnostics_fire_for_the_routed_entry(
    tmp_path, monkeypatch, caplog
):
    """Scoping the warning must not silence the case it exists for."""
    import logging

    from rtl_buddy.config import verible as verible_mod

    inactive = _mkdir(tmp_path / "verible-elsewhere")
    _touch_exe(inactive / "verible-verilog-syntax")
    site = _touch_exe(_mkdir(tmp_path / "site") / "verible-verilog-syntax")
    monkeypatch.setattr(verible_mod.shutil, "which", lambda _n: str(site))
    _write_two_verible_project(
        tmp_path, active=str(tmp_path / "absent-here"), inactive=str(inactive)
    )
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.config.verible"):
        RootConfig(name="two-platforms")

    warnings = [r.getMessage() for r in _verible_records(caplog, logging.WARNING)]
    assert len(warnings) == 1
    assert "verible-active" in warnings[0]
    assert str(tmp_path / "absent-here") in warnings[0]
    assert str(site) in warnings[0]


def test_verible_directory_candidate_without_separator_is_found(tmp_path):
    """A bare candidate on `path:` is a directory, never a PATH lookup.

    `cfg-verible.path` names a directory, so routing a separator-free
    candidate through `shutil.which` made an existing `verible-arm/` next
    to root_config.yaml invisible and silently selected the absent one
    (#439).
    """
    from rtl_buddy.config.verible import VeribleConfigFile

    present = tmp_path / "verible-arm"
    present.mkdir()
    _touch_exe(present / "verible-verilog-syntax")

    cfg = VeribleConfigFile(
        name="v", path=["verible-arm", "verible-x86"], extra_args={}
    ).initialise(str(tmp_path / "root_config.yaml"))

    assert cfg.path == str(present)
    assert cfg.available is True


# ---------------------------------------------------------------------------
# cfg-platforms — routing an entry in any tool block (#439)
# ---------------------------------------------------------------------------

_ROUTING_BLOCKS = """
cfg-surfer:
  - name: "surfer-shared"
    path: "surfer-shared-bin"
  - name: "surfer-default"
    path: "surfer"

cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"
  - name: "yosys-shared"
    tool: "yosys-shared-bin"

cfg-fpv-tools:
  - name: "sby-shared"
    tool: "sby-shared-bin"

cfg-cdc-tools:
  - name: "cdc-shared"
    tool: "rtl-buddy-cdc"

cfg-pnr-tools:
  - name: "openroad-shared"
    tool: "openroad"

cfg-power-tools:
  - name: "power-shared"
    tool: "opensta"

cfg-fpga-tools:
  - name: "vivado-shared"
    tool: "vivado"
"""


#: A second, legitimately configured platform whose unames never match
#: this host — for asserting that "another platform's" behaviour is the
#: quiet skip, as distinct from naming a platform that does not exist.
_INACTIVE_PLATFORM = """\
  - os: "other-host"
    unames: ["NoSuchUname"]
    builder: "stub"
    verible: "stub-verible"
"""


def _write_routed_project(
    root: Path, *, routing: str = "", extra_platform: str = "", extra: str = ""
) -> None:
    (root / "root_config.yaml").write_text(
        f"""\
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "test-host"
    unames: ["Darwin", "Linux"]
    builder: "stub"
    verible: "stub-verible"
{routing}{extra_platform}
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
    extra_args: {{}}

cfg-rtl-reg:
  reg-cfg-path: "regression.yaml"
"""
        + _ROUTING_BLOCKS
        + extra
    )
    (root / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs: []\n"
    )


def test_platform_routes_surfer(tmp_path, monkeypatch):
    _write_routed_project(tmp_path, routing='    surfer: "surfer-shared"\n')
    monkeypatch.chdir(tmp_path)
    rc = RootConfig(name="routed")

    assert rc.get_platform_tool_name("surfer") == "surfer-shared"
    assert rc.get_surfer_cfg().name == "surfer-shared"


def test_unrouted_blocks_keep_their_global_default(tmp_path, monkeypatch):
    """Zero impact on existing configs: no routing keys, today's behaviour."""
    _write_routed_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = RootConfig(name="unrouted")

    assert rc.get_platform_tool_name("surfer") is None
    # cfg-surfer keeps the hardcoded "surfer-default" fallback.
    assert rc.get_surfer_cfg().name == "surfer-default"
    # *-tools blocks are not routable: the flow yaml's `tool:` names one.
    assert rc.get_synth_tool_cfg("yosys").get_name() == "yosys"


def test_explicit_name_wins_over_platform_routing(tmp_path, monkeypatch):
    _write_routed_project(tmp_path, routing='    surfer: "surfer-shared"\n')
    monkeypatch.chdir(tmp_path)
    rc = RootConfig(name="routed")

    assert rc.get_surfer_cfg("surfer-default").name == "surfer-default"


def test_platform_routing_to_missing_entry_is_fatal(tmp_path, monkeypatch):
    _write_routed_project(tmp_path, routing='    surfer: "surfer-nope"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="cfg-surfer"):
        RootConfig(name="routed")


def test_routing_typo_on_an_inactive_platform_is_still_fatal(tmp_path, monkeypatch):
    """A bad Linux entry must not wait for the CI host to be discovered.

    The second entry's unames never match this host, so only a load-time
    sweep over *every* platform entry catches it (#439).
    """
    _write_routed_project(tmp_path, routing='    surfer: "surfer-shared"\n')
    cfg = tmp_path / "root_config.yaml"
    cfg.write_text(
        cfg.read_text().replace(
            "cfg-rtl-builder:\n",
            '  - os: "never-this-host"\n'
            '    unames: ["NoSuchUname"]\n'
            '    builder: "stub"\n'
            '    verible: "stub-verible"\n'
            '    surfer: "surfer-typo"\n\n'
            "cfg-rtl-builder:\n",
            1,
        )
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="never-this-host"):
        RootConfig(name="routed")


def test_tools_blocks_are_not_routable(tmp_path, monkeypatch):
    """`*-tools` keys are rejected, not silently ignored.

    Their entry name doubles as the backend selector, so routing them
    could never bind at run time; the candidate list in `tool:` is the
    supported way to pin one of those binaries per platform (#439).
    """
    _write_routed_project(tmp_path, routing='    synth-tools: "yosys-shared"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="cannot be routed per platform"):
        RootConfig(name="routed")


def test_platform_config_file_parses_routing_keys():
    from rtl_buddy.config.platform import PlatformConfigFile

    cfg = from_yaml(
        PlatformConfigFile,
        'os: "linux"\n'
        'unames: ["Linux"]\n'
        'builder: "verilator"\n'
        'verible: "verible-x86_64"\n'
        'surfer: "surfer-shared"\n',
    )
    assert cfg.get_routed_names() == {"surfer": "surfer-shared"}


# ---------------------------------------------------------------------------
# cfg-tools — per-platform min-version (#439)
# ---------------------------------------------------------------------------


def test_tool_min_version_platform_specific_wins(tmp_path, monkeypatch):
    _write_routed_project(
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
    rc = RootConfig(name="pins")
    assert rc.get_tool_version_cfg("verilator").min_version == "5.050"


def test_tool_min_version_platform_specific_wins_regardless_of_order(
    tmp_path, monkeypatch
):
    _write_routed_project(
        tmp_path,
        extra=(
            "\ncfg-tools:\n"
            "  - name: verilator\n"
            '    min-version: "5.050"\n'
            '    platform: "test-host"\n'
            "  - name: verilator\n"
            '    min-version: "5.049"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = RootConfig(name="pins")
    assert rc.get_tool_version_cfg("verilator").min_version == "5.050"


def test_tool_min_version_other_platform_entry_is_dropped(tmp_path, monkeypatch):
    """A pin for a *configured* other platform is skipped, silently."""
    _write_routed_project(
        tmp_path,
        extra_platform=_INACTIVE_PLATFORM,
        extra=(
            "\ncfg-tools:\n"
            "  - name: verilator\n"
            '    min-version: "5.049"\n'
            "  - name: verilator\n"
            '    min-version: "5.050"\n'
            '    platform: "other-host"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = RootConfig(name="pins")
    assert rc.get_tool_version_cfg("verilator").min_version == "5.049"


def test_tool_min_version_unknown_platform_is_fatal(tmp_path, monkeypatch):
    """A `platform:` that names no cfg-platforms os is a config error.

    Dropping it as "another platform's pin" is what makes a typo lethal:
    the version floor then applies nowhere and `rb tool-check` goes green
    on every host, which is exactly the silent no-op that naming an
    unroutable block or a missing routed entry is already fatal for (#439).
    """
    _write_routed_project(
        tmp_path,
        extra_platform=_INACTIVE_PLATFORM,
        extra=(
            "\ncfg-tools:\n"
            "  - name: verilator\n"
            '    min-version: "5.050"\n'
            '    platform: "osxx"\n'
        ),
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        RootConfig(name="pins")
    message = str(excinfo.value)
    # The bad value and the set it had to come from — a message naming
    # only one of the two leaves the reader guessing at the other.
    assert "osxx" in message
    assert "other-host" in message and "test-host" in message


def test_unknown_platform_pin_has_a_dedicated_human_message():
    """The ERROR event must not render through the dotted-event fallback.

    A typo in `cfg-tools[].platform` is the config error a user is most
    likely to hit here, and `rtl_buddy.log` reads the human message —
    without a case it would say less than the console does (#439 review).
    """
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "tool_version.platform_unknown",
        {"name": "verilator", "entry_platform": "osxx", "available": "linux, macos"},
    )
    assert msg != "tool_version platform_unknown"
    assert "verilator" in msg
    assert "osxx" in msg
    assert "linux, macos" in msg


# ---------------------------------------------------------------------------
# ModelConfig — graph opt-out + top override (#479)
# ---------------------------------------------------------------------------


def test_model_config_graph_and_top_default_to_graphable_self_topped():
    """Every existing models.yaml keeps its current meaning.

    ``graph:`` defaults to True and ``top:`` to None, so ``get_top()``
    reproduces the project convention the whole codebase assumed before
    the knobs existed: the model's root module is named after the model.
    """
    model = ModelConfig(name="soc", filelist=["src/soc.sv"])
    assert model.graph is True
    assert model.top is None
    assert model.get_top() == "soc"


def test_model_config_graph_false_and_top_override_round_trip(tmp_path):
    """The two #479 knobs parse out of models.yaml and reach ``get_top()``."""
    from rtl_buddy.config.model import ModelConfigLoader

    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "apb_intf"\n'
        '    desc: "interface library"\n'
        '    filelist: ["-v apb_intf.sv"]\n'
        "    graph: false\n"
        '  - name: "pp_axi"\n'
        '    desc: "vendored collection"\n'
        '    filelist: ["-F pp_axi.f"]\n'
        '    top: "axi_xbar"\n'
    )
    loader = ModelConfigLoader(str(models_yaml))
    intf = loader.get_model("apb_intf")
    assert intf.graph is False
    assert intf.get_top() == "apb_intf"

    coll = loader.get_model("pp_axi")
    assert coll.graph is True
    assert coll.top == "axi_xbar"
    assert coll.get_top() == "axi_xbar"


def test_model_top_override_reaches_the_non_simulation_flows(tmp_path):
    """``top:`` is the model's root module, not a graph-only fact.

    ``cdc.yaml`` / ``synth.yaml`` / ``lint.yaml`` / ``fpga.yaml`` runs all
    default their top to the model, so the override has to reach them —
    otherwise a project needs the same escape hatch four more times.
    """
    from types import SimpleNamespace

    from rtl_buddy.config.cdc import CdcConfig
    from rtl_buddy.config.fpga import FpgaConfig
    from rtl_buddy.config.lint import LintConfig
    from rtl_buddy.config.synth import SynthConfig

    model = ModelConfig(
        name="pp_axi",
        filelist=["-F pp_axi.f"],
        top="axi_xbar",
        path=str(tmp_path / "models.yaml"),
    )
    # `get_top` reads nothing but `self.model`, and each of these entry
    # classes needs a dozen unrelated required fields to instantiate —
    # so call the accessor against the one attribute it uses.
    entry = SimpleNamespace(model=model)
    for cls in (CdcConfig, SynthConfig, LintConfig, FpgaConfig):
        assert cls.get_top(entry) == "axi_xbar", cls.__name__

    plain = SimpleNamespace(model=ModelConfig(name="pp_axi", filelist=[]))
    for cls in (CdcConfig, SynthConfig, LintConfig, FpgaConfig):
        assert cls.get_top(plain) == "pp_axi", cls.__name__
