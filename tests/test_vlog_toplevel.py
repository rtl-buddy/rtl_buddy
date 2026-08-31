"""Tests for `toplevel:` reaching the SystemVerilog builders (#506, #508).

`TestbenchConfig.toplevel` used to be read only by the SystemC path and the
graph/hier tooling, so a plain SystemVerilog testbench had no way to pin the
elaboration root: Verilator and VCS elected a top (and Verilator named the
model) from filelist order. These tests pin the flag each family gets, that
it is absent when no `toplevel:` is declared, that it is never doubled when
the builder's own opts already pin a top, and that it participates in the
shared-build compile key.
"""

import logging

import pytest

from rtl_buddy.tools import vlog_sim as vlog_sim_module


def _events(caplog):
    return [getattr(r, "rtl_event", None) for r in caplog.records]


class _DummyBuilder:
    def __init__(self, *, family="verilator", exe=None, compile_opts=None):
        self.family = family
        self.exe = exe if exe is not None else family
        self.compile_opts = list(compile_opts or [])

    def get_simulator_family(self):
        return self.family

    def get_exe(self):
        return self.exe

    def get_compile_time_opts(self, _mode):
        return list(self.compile_opts)

    def get_run_time_opts(self, _mode, seed=None):
        return []

    def get_name(self):
        return self.family

    def get_seed(self):
        return 0

    def get_simv(self):
        return "simv"


class _DummyRoot:
    def __init__(self, builder):
        self._builder = builder

    def get_rtl_builder_cfg(self):
        return self._builder

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return self._builder

    def get_use_lcov(self, _family):
        return False


class _DummyModel:
    def __init__(self, model_path, filelist):
        self.model_path = str(model_path)
        self.filelist = list(filelist)

    def get_filelist(self):
        return list(self.filelist)

    def get_model_path(self):
        return self.model_path


class _DummyTb:
    def __init__(self, toplevel=None):
        self.toplevel = toplevel

    def get_filelist(self):
        return []


class _DummyTestCfg:
    def __init__(self, *, name="t", toplevel=None, model=None):
        self.name = name
        self.assertions = False
        self.model = model
        self.tb = _DummyTb(toplevel=toplevel)
        self.pd = None
        self.uvm = None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return None

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusargs(self):
        return None

    def get_plusdefines(self):
        return {}

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return None


def _make_sim(
    tmp_path,
    *,
    toplevel=None,
    family="verilator",
    exe=None,
    compile_opts=None,
    test_name="t",
    share_build=False,
    cls=vlog_sim_module.VlogSim,
):
    src = tmp_path / "src" / "top.sv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("module top; endmodule\n")
    builder = _DummyBuilder(family=family, exe=exe, compile_opts=compile_opts)
    model = _DummyModel(tmp_path / "models.yaml", ["src/top.sv"])
    test_cfg = _DummyTestCfg(name=test_name, toplevel=toplevel, model=model)
    return cls(
        name="rtl_buddy/vlog_sim",
        root_cfg=_DummyRoot(builder),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        suite_dir=str(tmp_path),
        share_build=share_build,
    )


# ---------------------------------------------------------------------------
# _get_top_module_flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family,exe,flag",
    [
        ("verilator", "verilator", "--top-module"),
        ("vcs", "vcs", "-top"),
        ("icarus", "iverilog", "-s"),
    ],
)
def test_top_flag_per_family(tmp_path, family, exe, flag):
    sim = _make_sim(tmp_path, toplevel="tb_top", family=family, exe=exe)
    assert sim._get_top_module_flags([], []) == [flag, "tb_top"]


def test_no_top_flag_without_declared_toplevel(tmp_path):
    sim = _make_sim(tmp_path, toplevel=None)
    assert sim._get_top_module_flags([], []) == []


def test_no_top_flag_for_family_without_one(tmp_path):
    # A family rtl_buddy has no top-selection flag for must not get a
    # verilator flag by accident.
    sim = _make_sim(tmp_path, toplevel="tb_top", family="questa", exe="vlog")
    assert sim._get_top_module_flags([], []) == []


def test_top_flag_not_doubled_when_builder_opts_pin_it(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.DEBUG):
        assert sim._get_top_module_flags(["--top-module", "tb_top"], []) == []
    assert "compile.toplevel_already_pinned" in _events(caplog)


def test_top_flag_disagreement_warns_and_config_wins(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.WARNING):
        assert sim._get_top_module_flags(["--top-module", "other_top"], []) == []
    conflict = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_conflict"
    ]
    assert len(conflict) == 1
    fields = conflict[0].rtl_fields
    assert fields["configured"] == "other_top"
    assert fields["toplevel"] == "tb_top"


def test_top_flag_not_doubled_when_extra_compile_flags_pin_it(tmp_path):
    # The SystemC and cocotb-on-VCS subclasses emit their own top flag from
    # _get_extra_compile_flags(); the base must see those too.
    sim = _make_sim(tmp_path, toplevel="my_dut", family="vcs")
    assert sim._get_top_module_flags([], ["-top", "my_dut"]) == []


def test_top_flag_dedup_is_token_level_not_substring(tmp_path):
    # `+define+X_top_Y` contains "-top" nowhere as a token; an opt that only
    # looks like the flag must not suppress the real one.
    sim = _make_sim(tmp_path, toplevel="my_dut", family="vcs")
    assert sim._get_top_module_flags(["+define+X-top-Y"], []) == ["-top", "my_dut"]


# ---------------------------------------------------------------------------
# The plan and the command line it produces
# ---------------------------------------------------------------------------


def test_compile_argv_carries_top_module_for_verilator(tmp_path):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    plan = sim._build_compile_plan()
    assert plan.top_flags == ["--top-module", "tb_top"]
    argv = sim._compile_argv(plan, quiet=True)
    assert argv[argv.index("--top-module") + 1] == "tb_top"


def test_compile_argv_has_no_top_module_without_toplevel(tmp_path):
    sim = _make_sim(tmp_path, toplevel=None, family="verilator")
    plan = sim._build_compile_plan()
    assert plan.top_flags == []
    assert "--top-module" not in sim._compile_argv(plan, quiet=True)


def test_compile_argv_carries_dash_s_for_icarus(tmp_path):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="icarus", exe="iverilog")
    plan = sim._build_compile_plan()
    argv = sim._compile_argv(plan, quiet=True)
    assert argv[argv.index("-s") + 1] == "tb_top"


# ---------------------------------------------------------------------------
# Compile-key participation (#508: the flag changes the binary)
# ---------------------------------------------------------------------------


def _key(tmp_path, **kwargs):
    """Compile key for a config in ``tmp_path`` — one suite dir per call site.

    The key hashes the resolved run.f entries, so two configs may only be
    compared when they share a suite directory; that is also the situation
    the sharing is for (two tests of one suite over one model).
    """
    sim = _make_sim(tmp_path, share_build=True, **kwargs)
    plan = sim._build_compile_plan()
    return vlog_sim_module.VlogSim._compile_config_key(plan.fingerprint)


def test_compile_key_differs_by_toplevel(tmp_path):
    a = _key(tmp_path, toplevel="tb_one", test_name="a")
    b = _key(tmp_path, toplevel="tb_two", test_name="b")
    assert a != b


def test_compile_key_matches_for_same_toplevel(tmp_path):
    a = _key(tmp_path, toplevel="tb_one", test_name="a")
    b = _key(tmp_path, toplevel="tb_one", test_name="b")
    assert a == b


def test_compile_key_unchanged_when_no_toplevel_declared(tmp_path):
    # Backwards compatibility: a suite that never declared `toplevel:` must
    # not be handed a new shared-build dir by this change. Pinned as "the
    # key of a no-toplevel config equals the key of the same config computed
    # with the top flags removed" — the flags contribute nothing.
    sim = _make_sim(tmp_path, share_build=True, toplevel=None)
    plan = sim._build_compile_plan()
    assert plan.top_flags == []
    assert "--top-module" not in plan.key_cmd
