"""Tests for SVA-in-simulation plumbing (`assertions: true` in tests.yaml)."""

from rtl_buddy.tools import vlog_post, vlog_sim as vlog_sim_module


# count_assertion_failures


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
    # Under `--timing`, failure lines carry a sim-time prefix such as `[500] %Error: ...`; the follow-up `$stop` line lacks "Assertion failed" and must not count.
    log = tmp_path / "test.log"
    log.write_text(
        "[500] %Error: tb_top.sv:32: Assertion failed in tb_top.CNT_MONOTONE: "
        "'assert' failed.\n"
        "%Error: ../../tb_top.sv:32: Verilog $stop\n"
        "Aborting...\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 1


# xezim 0.11.3 output for `$error`, an assertion with an `else $error(...)`
# action block, a bare assertion, `$warning` and a concurrent assertion with an
# action block, followed by the testbench's own PASS marker.
XEZIM_ERRORS_LOG = """\
=== xezim 0.11.3 ===
** Error: plain error 7
   Time: 0  Scope: t
** Error: imm assert with action
   Time: 1  Scope: t
** Error: Assertion error.
   Time: 2  Scope: t.lbl
** Warning: a warning
   Time: 2  Scope: t
** Error: [ASSERT FAILED] [t.ap] more_than_1_bit_set: 3
   Time: 32500  Scope: t.ap
PASS done
Simulation finished at time 32600 ($finish called)
"""


def test_count_assertion_failures_matches_xezim_error_reports(tmp_path):
    # Every xezim error-severity report counts, as Verilator's `%Error: ... Assertion failed` does for `$error` and action blocks alike; warnings do not.
    log = tmp_path / "test.log"
    log.write_text(XEZIM_ERRORS_LOG)
    assert vlog_post.count_assertion_failures(str(log)) == 4
    assert vlog_post.first_assertion_failure(str(log)) == "** Error: plain error 7"


def test_count_assertion_failures_needs_xezim_time_scope_line(tmp_path):
    # A `** Error:` line without xezim's `Time: … Scope: …` line under it is testbench output, not a simulator report.
    log = tmp_path / "test.log"
    log.write_text(
        "** Error: printed by the testbench\n"
        "** Error: also printed\n"
        "PASS\n"
        "** Error: last line of the file\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 0
    assert vlog_post.first_assertion_failure(str(log)) is None


def test_count_assertion_failures_xezim_report_after_unpaired_line(tmp_path):
    log = tmp_path / "test.log"
    log.write_text(
        "** Error: printed by the testbench\n"
        "** Error: Assertion error.\n"
        "   Time: 5  Scope: t.ap2\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 1
    assert vlog_post.first_assertion_failure(str(log)) == "** Error: Assertion error."


def test_count_assertion_failures_xezim_bare_error_and_timeformat(tmp_path):
    # `$error;` with no message prints `** Error` with no colon; `$timeformat` puts a fraction and a unit in the time.
    log = tmp_path / "test.log"
    log.write_text(
        "** Error\n"
        "   Time: 1.50 ns  Scope: g\n"
        "** Error: with timeformat\n"
        "   Time: 1.50 ns  Scope: g\n"
        "** Errors are listed below\n"
        "   Time: 2  Scope: g\n"
        "PASS\n",
    )
    assert vlog_post.count_assertion_failures(str(log)) == 2
    assert vlog_post.first_assertion_failure(str(log)) == "** Error"


def test_xezim_timing_check_violation_is_not_an_assertion(tmp_path):
    # xezim's timing-check violations are one-line `** Error:` reports: not counted, but they name a nonzero exit.
    line = "** Error: $setup violation in tb.dut.ff at time 1200 (ff.v:12)"
    log = tmp_path / "test.log"
    log.write_text(f"{line}\nPASS\n")
    assert vlog_post.count_assertion_failures(str(log)) == 0
    assert vlog_post.first_sim_error(str(log)) == line


def test_first_sim_error_names_xezim_fatal(tmp_path):
    # `$fatal` is not counted as an assertion failure (nor is Verilator's `%Fatal`), but it names the nonzero exit.
    log = tmp_path / "test.log"
    log.write_text("PASS\n** Fatal: fatal msg\n   Time: 0  Scope: f\n")
    assert vlog_post.count_assertion_failures(str(log)) == 0
    assert vlog_post.first_sim_error(str(log)) == "** Fatal: fatal msg"


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


# VlogPost result-dict assertion annotation


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
    # A testbench wrapper may swallow the abort and print PASS; the firing must still surface as FAIL.
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


def test_vlog_post_counts_xezim_action_block_reports(tmp_path):
    # rtl-buddy/rtl_buddy#981: assertion macros with `else $error(...)` fired under xezim, the testbench still printed PASS, and the Assertions column stayed empty.
    log = tmp_path / "test.log"
    log.write_text(XEZIM_ERRORS_LOG)
    post = vlog_post.VlogPost(
        name="t", path=str(log), err_path=None, assertions_enabled=True
    )
    results = post.get_results().results
    assert results["result"] == "FAIL"
    assert results["assertions"] == {"enabled": True, "fired": 4}
    assert results["desc"] == "4 SVA assertion failure(s) (was PASS: done)"
    assert results["first_error"] == "** Error: plain error 7"


def test_vlog_post_flips_na_to_fail_on_timing_abort(tmp_path):
    # Under `--timing` a fired SVA aborts before any PASS/FAIL marker; the timing-prefixed firing must flip NA to FAIL.
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


# VlogSim._get_verilator_assertion_flags


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

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
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
    # --coverage-user is already in the builder opts and must not be duplicated; --assert is still added.
    flags = sim._get_verilator_assertion_flags(["--coverage-line", "--coverage-user"])
    assert "--assert" in flags
    assert "--coverage-user" not in flags


def test_assertion_flags_no_op_for_non_verilator(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="vcs")
    flags = sim._get_verilator_assertion_flags([])
    assert flags == []


def test_assertion_flags_quiet_for_xezim(tmp_path, caplog):
    # xezim checks SVA unconditionally, so `assertions: true` needs no flag and no warning.
    import logging

    sim = _make_sim(tmp_path, assertions=True, family="xezim")
    with caplog.at_level(logging.WARNING):
        assert sim._get_verilator_assertion_flags([]) == []
    events = [getattr(r, "rtl_event", None) for r in caplog.records]
    assert "compile.assertions_not_verilator" not in events
    assert sim._coverage_enabled() is False


def test_coverage_enabled_when_assertions_alone_on_verilator(tmp_path):
    sim = _make_sim(tmp_path, assertions=True, family="verilator")
    assert sim._coverage_enabled() is True


def test_coverage_disabled_when_neither_set(tmp_path):
    sim = _make_sim(tmp_path, assertions=False, family="verilator")
    assert sim._coverage_enabled() is False
