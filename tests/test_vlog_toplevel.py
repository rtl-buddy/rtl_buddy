"""Tests for `toplevel:` in the SystemVerilog builders.

They cover the top flag each builder family gets, its absence without `toplevel:`, no duplicate when the builder opts already pin a top, and its effect on the shared-build compile key.
"""

import logging

import pytest

from rtl_buddy.tools import vlog_sim as vlog_sim_module


def _events(caplog):
    return [getattr(r, "rtl_event", None) for r in caplog.records]


@pytest.fixture(autouse=True)
def _forget_toplevel_conflicts():
    """Reset the once-per-process conflict warning so one test does not silence the next."""
    vlog_sim_module._reset_toplevel_conflicts()
    yield
    vlog_sim_module._reset_toplevel_conflicts()


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
    # A family without a top flag gets none.
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
    # SystemC and cocotb-on-VCS emit their own top flag via _get_extra_compile_flags(); it counts for the duplicate check only, not for conflicts.
    sim = _make_sim(tmp_path, toplevel="my_dut", family="vcs")
    assert sim._get_top_module_flags([], ["-top", "my_dut"]) == []


def test_generated_top_never_shadows_a_configured_one(tmp_path, caplog):
    # A generated top comes later on the command line and would win under Verilator's last-wins rule, so a conflict is judged on the user's opts alone.
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.WARNING):
        assert (
            sim._get_top_module_flags(
                ["--top", "other_top"], ["--top-module", "tb_top"]
            )
            == []
        )
    conflict = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_conflict"
    ]
    assert len(conflict) == 1
    assert conflict[0].rtl_fields["configured"] == "other_top"


def test_backend_generated_top_is_recorded_as_ours(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.DEBUG):
        assert sim._get_top_module_flags([], ["--top-module", "tb_top"]) == []
    pinned = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_already_pinned"
    ]
    assert len(pinned) == 1
    assert pinned[0].rtl_fields["source"] == "backend"


def test_configured_top_is_recorded_as_the_users(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.DEBUG):
        assert sim._get_top_module_flags(["--top-module", "tb_top"], []) == []
    pinned = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_already_pinned"
    ]
    assert len(pinned) == 1
    assert pinned[0].rtl_fields["source"] == "builder-opts"


def test_top_flag_dedup_is_token_level_not_substring(tmp_path):
    # `+define+X-top-Y` contains "-top" only as a substring and must not suppress the real flag.
    sim = _make_sim(tmp_path, toplevel="my_dut", family="vcs")
    assert sim._get_top_module_flags(["+define+X-top-Y"], []) == ["-top", "my_dut"]


# ---------------------------------------------------------------------------
# Every spelling the family accepts counts as "already pinned"
#
# Verilator accepts `--top-module` and `--top` with one or two dashes; iverilog
# accepts the module glued to `-s`. Otherwise rtl_buddy would append a second
# top that wins under last-wins precedence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opts",
    [
        ["--top-module", "spare_top"],
        ["-top-module", "spare_top"],
        ["--top", "spare_top"],
        ["-top", "spare_top"],
    ],
)
def test_verilator_top_aliases_all_count_as_pinned(tmp_path, caplog, opts):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.WARNING):
        assert sim._get_top_module_flags(opts, []) == []
    conflict = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_conflict"
    ]
    assert len(conflict) == 1
    assert conflict[0].rtl_fields["configured"] == "spare_top"
    # The flag is reported as the user wrote it, not as rtl_buddy spells it.
    assert conflict[0].rtl_fields["flag"] == opts[0]


def test_verilator_top_alias_that_agrees_is_debug_not_warning(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.DEBUG):
        assert sim._get_top_module_flags(["--top", "tb_top"], []) == []
    assert "compile.toplevel_already_pinned" in _events(caplog)
    assert "compile.toplevel_conflict" not in _events(caplog)


def test_icarus_glued_top_counts_as_pinned(tmp_path, caplog):
    # `iverilog -stb` is `-s tb`.
    sim = _make_sim(tmp_path, toplevel="tb_top", family="icarus", exe="iverilog")
    with caplog.at_level(logging.WARNING):
        assert sim._get_top_module_flags(["-g2012", "-stb"], []) == []
    conflict = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_conflict"
    ]
    assert len(conflict) == 1
    assert conflict[0].rtl_fields["configured"] == "tb"


def test_icarus_glued_top_that_agrees_is_debug(tmp_path, caplog):
    sim = _make_sim(tmp_path, toplevel="tb_top", family="icarus", exe="iverilog")
    with caplog.at_level(logging.DEBUG):
        assert sim._get_top_module_flags(["-stb_top"], []) == []
    assert "compile.toplevel_already_pinned" in _events(caplog)


def test_bare_dash_s_is_not_read_as_glued():
    # `-s` alone is the separate-token spelling; with no value there is no configured top.
    assert vlog_sim_module._find_configured_top(
        vlog_sim_module.TOP_MODULE_FLAGS["icarus"], ["-s"]
    ) == ("-s", None)


def test_repeated_top_flags_compare_against_the_last(tmp_path, caplog):
    # Verilator is last-wins, so `toplevel:` is compared against the last spelling.
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.DEBUG):
        assert (
            sim._get_top_module_flags(
                ["--top", "spare_top", "--top-module", "tb_top"], []
            )
            == []
        )
    # The last spelling agrees with `toplevel:`, so there is no conflict.
    assert "compile.toplevel_already_pinned" in _events(caplog)
    assert "compile.toplevel_conflict" not in _events(caplog)


def test_trailing_bare_top_flag_is_pinned_with_no_value(tmp_path, caplog):
    # A trailing bare `--top` still counts as pinned; the missing value is not rendered as "None".
    sim = _make_sim(tmp_path, toplevel="tb_top", family="verilator")
    with caplog.at_level(logging.WARNING):
        assert sim._get_top_module_flags(["-sv", "--top"], []) == []
    conflict = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.toplevel_conflict"
    ]
    assert len(conflict) == 1
    assert "configured" not in conflict[0].rtl_fields
    assert "None" not in conflict[0].getMessage()
    assert "with no value" in conflict[0].getMessage()


def test_top_flag_followed_by_another_option_has_no_value():
    assert vlog_sim_module._find_configured_top(
        vlog_sim_module.TOP_MODULE_FLAGS["verilator"], ["--top", "--trace", "-sv"]
    ) == ("--top", None)


def test_conflict_warns_once_per_process(tmp_path, caplog):
    # One builder config shared by N tests warns once.
    opts = ["--top", "spare_top"]
    with caplog.at_level(logging.WARNING):
        for name in ("a", "b", "c"):
            sim = _make_sim(
                tmp_path / name, toplevel="tb_top", family="verilator", test_name=name
            )
            assert sim._get_top_module_flags(opts, []) == []
    assert _events(caplog).count("compile.toplevel_conflict") == 1


def test_a_different_conflict_still_warns(tmp_path, caplog):
    # The warning is once per distinct conflict.
    with caplog.at_level(logging.WARNING):
        first = _make_sim(tmp_path / "a", toplevel="tb_top", family="verilator")
        assert first._get_top_module_flags(["--top", "spare_top"], []) == []
        second = _make_sim(tmp_path / "b", toplevel="tb_other", family="verilator")
        assert second._get_top_module_flags(["--top", "spare_top"], []) == []
    assert _events(caplog).count("compile.toplevel_conflict") == 2


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
# Compile-key participation
# ---------------------------------------------------------------------------


def _key(tmp_path, **kwargs):
    """Return the compile key for a config in ``tmp_path``.

    The key hashes the resolved run.f entries, so compare only configs that share a suite directory.
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


def test_resolved_runtime_seed_does_not_change_compile_fingerprint(tmp_path):
    sim = _make_sim(tmp_path, toplevel="tb_top", share_build=True)
    before = sim._build_compile_plan().fingerprint

    sim.test_cfg.resolved_seed = 410729
    sim.test_cfg.seed_source = "master"
    sim.test_cfg.seed_identity = "verif/blk/tests.yaml::t::single"
    sim.test_cfg.get_plusargs = lambda: {"stimulus_seed": 410729}
    after = sim._build_compile_plan().fingerprint

    assert after == before
    assert vlog_sim_module.VlogSim._compile_config_key(after) == (
        vlog_sim_module.VlogSim._compile_config_key(before)
    )


def _key_without_top_plumbing(tmp_path, monkeypatch, **kwargs):
    """Return the compile key with :meth:`_get_top_module_flags` disabled, i.e. without any top flag."""
    monkeypatch.setattr(
        vlog_sim_module.VlogSim,
        "_get_top_module_flags",
        lambda self, builder_opts, extra_compile_flags: [],
    )
    return _key(tmp_path, **kwargs)


def test_compile_key_unchanged_when_no_toplevel_declared(tmp_path, monkeypatch):
    # A suite without `toplevel:` keeps its shared-build dir.
    before = _key_without_top_plumbing(tmp_path, monkeypatch, toplevel=None)
    monkeypatch.undo()
    after = _key(tmp_path, toplevel=None)
    assert after == before


def test_compile_key_shifts_once_when_toplevel_is_declared(tmp_path, monkeypatch):
    # A testbench with `toplevel:` gets a new dir, because the flag changes the binary.
    before = _key_without_top_plumbing(tmp_path, monkeypatch, toplevel="tb_one")
    monkeypatch.undo()
    after = _key(tmp_path, toplevel="tb_one")
    assert after != before
