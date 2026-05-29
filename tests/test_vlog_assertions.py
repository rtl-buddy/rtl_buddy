"""Tests for SVA-in-simulation plumbing (`assertions: true` in tests.yaml)."""

from rtl_buddy.tools import vlog_post, vlog_sim as vlog_sim_module


# ---------------------------------------------------------------------------
# count_assertion_failures
# ---------------------------------------------------------------------------


def test_count_assertion_failures_matches_verilator_error_lines(tmp_path):
    log = tmp_path / "test.log"
    log.write_text(
        "running...\n"
        "%Error: dut.sv:42: Assertion failed in top.dut: 'signal == expected'\n"
        "%Error: dut.sv:51: Assertion failed in top.dut: 'a |-> b'\n"
        "%Warning: dut.sv:99: something noisy\n"
        "PASS\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 2


def test_count_assertion_failures_matches_timing_prefixed_lines(tmp_path):
    # Under Verilator's `--timing` flow, assertion-failure lines are prefixed
    # with the sim time, e.g. `[500] %Error: ...`. The follow-up `$stop` line
    # starts with a bare `%Error` but lacks "Assertion failed", so only the
    # firing should be counted.
    log = tmp_path / "test.log"
    log.write_text(
        "[500] %Error: tb_top.sv:32: Assertion failed in tb_top.CNT_MONOTONE: "
        "'assert' failed.\n"
        "%Error: ../../tb_top.sv:32: Verilog $stop\n"
        "Aborting...\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 1


def test_count_assertion_failures_handles_missing_file(tmp_path):
    log = tmp_path / "exists.log"
    log.write_text("nothing fired\n")
    missing = tmp_path / "absent.err"
    assert vlog_post.count_assertion_failures(str(log), str(missing)) == 0


def test_count_assertion_failures_reads_both_log_and_err(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("PASS\n%Error: a.sv:1: Assertion failed in dut: 'cond'\n")
    err = tmp_path / "test.err"
    err.write_text(
        "%Error: b.sv:2: Assertion failed in dut: 'cond2'\n"
        "%Error: b.sv:3: Assertion failed in dut: 'cond3'\n"
    )
    assert vlog_post.count_assertion_failures(str(log), str(err)) == 3


# ---------------------------------------------------------------------------
# VlogPost result-dict assertion annotation
# ---------------------------------------------------------------------------


def test_vlog_post_skips_assertions_when_disabled(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("PASS smoke\n")
    post = vlog_post.VlogPost(name="t", path=str(log))
    results = post.get_results().results
    assert results["result"] == "PASS"
    assert "assertions" not in results


def test_vlog_post_reports_zero_firings_when_enabled_and_pass(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("PASS smoke\n")
    post = vlog_post.VlogPost(
        name="t",
        path=str(log),
        err_path=str(tmp_path / "test.err"),
        assertions_enabled=True,
    )
    results = post.get_results().results
    assert results["result"] == "PASS"
    assert results["assertions"] == {"enabled": True, "fired": 0}


def test_vlog_post_flips_pass_to_fail_when_assertion_fires(tmp_path):
    log = tmp_path / "test.log"
    # Verilator usually aborts before PASS is printed, but if a testbench
    # wrapper swallows the abort and prints PASS, we should still surface
    # the firing as a FAIL.
    log.write_text(
        "PASS smoke\n%Error: dut.sv:7: Assertion failed in top.dut: 'cond'\n"
    )
    post = vlog_post.VlogPost(
        name="t",
        path=str(log),
        err_path=None,
        assertions_enabled=True,
    )
    results = post.get_results().results
    assert results["result"] == "FAIL"
    assert results["assertions"] == {"enabled": True, "fired": 1}
    assert "SVA assertion failure" in results["desc"]


def test_vlog_post_flips_na_to_fail_on_timing_abort(tmp_path):
    # The issue-229 scenario: under `--timing`, a fired SVA aborts the sim
    # before any PASS/FAIL marker is printed. Without the assertion override
    # this would report NA; the timing-prefixed firing must flip it to FAIL.
    log = tmp_path / "test.log"
    log.write_text(
        "[500] %Error: tb_top.sv:32: Assertion failed in tb_top.CNT_MONOTONE: "
        "'assert' failed.\n"
        "%Error: ../../tb_top.sv:32: Verilog $stop\n"
        "Aborting...\n",
    )
    post = vlog_post.VlogPost(
        name="smoke_with_sva",
        path=str(log),
        err_path=None,
        assertions_enabled=True,
    )
    results = post.get_results().results
    assert results["result"] == "FAIL"
    assert results["assertions"] == {"enabled": True, "fired": 1}
    assert "SVA assertion failure" in results["desc"]


# ---------------------------------------------------------------------------
# VlogSim._get_verilator_assertion_flags
# ---------------------------------------------------------------------------


class _DummyBuilder:
    def __init__(self, *, family="verilator", exe="verilator"):
        self.family = family
        self.exe = exe

    def get_simulator_family(self):
        return self.family

    def get_exe(self):
        return self.exe

    def get_compile_time_opts(self, _mode):
        return []

    def get_run_time_opts(self, _mode, seed=None):
        return []

    def get_name(self):
        return "dummy"

    def get_seed(self):
        return 0

    def get_simv(self):
        return "simv"


class _DummyRoot:
    def __init__(self, builder):
        self._builder = builder

    def get_rtl_builder_cfg(self):
        return self._builder

    def get_use_lcov(self, _family):
        return False


class _DummyModel:
    def get_filelist(self):
        return []

    def get_model_path(self):
        return ""


class _DummyTb:
    def get_filelist(self):
        return []


class _DummyTestCfg:
    def __init__(self, *, assertions=False, name="t"):
        self.name = name
        self.assertions = assertions
        self.model = _DummyModel()
        self.tb = _DummyTb()
        self.pd = None
        self.uvm = None

    def get_name(self):
        return self.name

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


def _make_sim(tmp_path, *, assertions=False, family="verilator"):
    builder = _DummyBuilder(family=family, exe=family)
    root = _DummyRoot(builder)
    test_cfg = _DummyTestCfg(assertions=assertions)
    sim = vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=root,
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        suite_dir=str(tmp_path),
    )
    return sim


def test_assertion_flags_skipped_when_disabled(tmp_path):
    sim = _make_sim(tmp_path, assertions=False)
    assert sim._get_verilator_assertion_flags([]) == []


def test_assertion_flags_injected_for_verilator(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="verilator")
    flags = sim._get_verilator_assertion_flags([])
    assert "--assert" in flags
    assert "--coverage-user" in flags


def test_assertion_flags_idempotent(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="verilator")
    # Existing builder opts already include --coverage-user; we should not
    # duplicate it but we still add --assert.
    flags = sim._get_verilator_assertion_flags(["--coverage-line", "--coverage-user"])
    assert "--assert" in flags
    assert "--coverage-user" not in flags


def test_assertion_flags_no_op_for_non_verilator(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="vcs")
    flags = sim._get_verilator_assertion_flags([])
    assert flags == []


def test_coverage_enabled_when_assertions_alone_on_verilator(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="verilator")
    assert sim._coverage_enabled() is True


def test_coverage_disabled_when_neither_set(tmp_path):
    sim = _make_sim(tmp_path, assertions=False, family="verilator")
    assert sim._coverage_enabled() is False
