"""A simulator that exits nonzero after the transcript's PASS verdict fails the test.

Covers the exit-status rule in ``grade_sim_exit``: the ``final``-block assertion, ``$stop`` and abort shapes Verilator prints after the pass marker, the cases that keep their verdict, and how an xfail marker reads the result.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from rtl_buddy.logging_utils import _human_message, setup_logging
from rtl_buddy.runner.test_results import TestResults as RtlBuddyTestResults
from rtl_buddy.runner.test_results import is_run_failure
from rtl_buddy.runner.xfail import FAIL_STAGE_KEY, FIRST_ERROR_KEY, apply_xfail
from rtl_buddy.tools.cocotb_sim import CocotbSim
from rtl_buddy.tools.vlog_post import UvmVlogPost, VlogPost, grade_sim_exit
from rtl_buddy.tools.vlog_sim import VlogSim

# Verilator 5 output for a `final` immediate assertion that fails after the pass marker (`--binary --timing --assert`).
FINAL_ASSERT_LOG = """\
PASS (nwrn=  0)
- tb.sv:3: Verilog $finish
[10] %Error: tb.sv:4: Assertion failed in tb.empty_chk: not empty at end of simulation
%Error: tb.sv:4: Verilog $stop
Aborting...
"""
FINAL_ASSERT_LINE = (
    "[10] %Error: tb.sv:4: Assertion failed in tb.empty_chk: "
    "not empty at end of simulation"
)

STOP_LOG = """\
PASS (nwrn=  0)
%Error: tb.sv:1: Verilog $stop
Aborting...
"""

UVM_PASS_LOG = """\
--- UVM Report Summary ---

** Report counts by severity
UVM_INFO :    4
UVM_WARNING :    0
UVM_ERROR :    0
UVM_FATAL :    0
"""


def _grade(tmp_path: Path, transcript: str, returncode, *, assertions=False):
    log = tmp_path / "test.log"
    log.write_text(transcript)
    results = (
        VlogPost(name="t", path=str(log), assertions_enabled=assertions)
        .get_results()
        .results
    )
    grade_sim_exit(results, returncode, test="t", transcripts=(str(log),))
    return results


def _events(log_path: Path) -> list[dict]:
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


class TestPassThenError:
    def test_final_block_assertion_after_pass_fails(self, tmp_path: Path):
        results = _grade(tmp_path, FINAL_ASSERT_LOG, 1)
        assert results["result"] == "FAIL"
        assert results["desc"] == (
            f"Sim exited 1 after a PASS verdict: {FINAL_ASSERT_LINE} "
            "(was PASS: (nwrn=  0))"
        )
        assert results[FIRST_ERROR_KEY] == FINAL_ASSERT_LINE
        assert FAIL_STAGE_KEY not in results
        assert is_run_failure(RtlBuddyTestResults("t", results))

    def test_stop_after_pass_names_the_stop(self, tmp_path: Path):
        results = _grade(tmp_path, STOP_LOG, 1)
        assert results["result"] == "FAIL"
        assert results[FIRST_ERROR_KEY] == "%Error: tb.sv:1: Verilog $stop"

    def test_signal_death_after_pass_names_the_signal(self, tmp_path: Path):
        results = _grade(tmp_path, "PASS done\n", -6)
        assert results["result"] == "FAIL"
        assert results["desc"] == (
            "Sim killed by signal 6 after a PASS verdict (was PASS: done)"
        )
        assert FIRST_ERROR_KEY not in results

    def test_error_line_on_stderr_is_found(self, tmp_path: Path):
        log = tmp_path / "test.log"
        log.write_text("PASS done\n")
        err = tmp_path / "test.err"
        err.write_text("%Fatal: tb.sv:9: out of memory\n")
        results = VlogPost(name="t", path=str(log)).get_results().results
        grade_sim_exit(results, 1, test="t", transcripts=(str(log), str(err)))
        assert results[FIRST_ERROR_KEY] == "%Fatal: tb.sv:9: out of memory"

    def test_uvm_pass_with_nonzero_exit_fails(self, tmp_path: Path):
        log = tmp_path / "test.log"
        log.write_text(UVM_PASS_LOG)
        results = (
            UvmVlogPost(name="t", path=str(log), max_warns=0, max_errors=0)
            .get_results()
            .results
        )
        assert results["result"] == "PASS"
        grade_sim_exit(results, 1, test="t", transcripts=(str(log),))
        assert results["result"] == "FAIL"

    def test_event_is_logged_at_error(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        _grade(tmp_path, FINAL_ASSERT_LOG, 1)
        events = [e for e in _events(log_path) if e["event"] == "sim.error_after_pass"]
        assert len(events) == 1
        assert events[0]["returncode"] == 1
        assert events[0]["first_error"] == FINAL_ASSERT_LINE

    def test_event_has_a_human_message(self):
        msg = _human_message(
            "sim.error_after_pass",
            {"test": "smoke", "returncode": -6, "first_error": FINAL_ASSERT_LINE},
        )
        assert "smoke" in msg and "killed by signal 6" in msg and "FAIL" in msg
        assert FINAL_ASSERT_LINE in msg


class TestVerdictsThatStand:
    def test_pass_with_clean_exit_stays_pass(self, tmp_path: Path):
        results = _grade(tmp_path, "PASS (nwrn=  0)\n- tb.sv:1: Verilog $finish\n", 0)
        assert results == {"result": "PASS", "desc": "(nwrn=  0)"}

    def test_no_returncode_grades_nothing(self, tmp_path: Path):
        assert _grade(tmp_path, FINAL_ASSERT_LOG, None)["result"] == "PASS"

    def test_fail_marker_keeps_its_own_reason(self, tmp_path: Path):
        """A ``$fatal`` after a FAIL banner keeps the testbench's reason."""
        transcript = (
            "FAIL\nERR: boom\n[10] %Fatal: tb.sv:1: Assertion failed in tb: boom\n"
            "%Error: tb.sv:1: Verilog $stop\nAborting...\n"
        )
        results = _grade(tmp_path, transcript, 1)
        assert results["result"] == "FAIL"
        assert results["desc"] == "boom"
        assert results[FIRST_ERROR_KEY] == "ERR: boom"

    def test_fired_assertions_keep_the_assertion_count(self, tmp_path: Path):
        results = _grade(tmp_path, FINAL_ASSERT_LOG, 1, assertions=True)
        assert results["result"] == "FAIL"
        assert results["desc"].startswith("1 SVA assertion failure(s)")
        assert results["assertions"] == {"enabled": True, "fired": 1}

    def test_no_verdict_is_still_a_non_verdict_failure(self, tmp_path: Path):
        results = _grade(tmp_path, "Aborting...\n", 1)
        assert results["result"] == "FAIL"
        assert results[FAIL_STAGE_KEY] == "sim"

    def test_cocotb_grades_from_its_results_file(self, tmp_path: Path):
        """cocotb reads ``cocotb_results.xml``; the exit status does not change its verdict."""
        xml = tmp_path / "cocotb_results.xml"
        suite = ET.Element("testsuites")
        ET.SubElement(ET.SubElement(suite, "testsuite"), "testcase", name="t0")
        ET.ElementTree(suite).write(xml)
        sim = CocotbSim.__new__(CocotbSim)
        sim.test_name = "t"
        sim.run_id = None
        sim._get_cocotb_results_path = lambda run_id=None: str(xml)
        assert sim.post(sim_returncode=1).results["result"] == "PASS"


class TestUnderXfail:
    """The failure is the simulator's own report, so an xfail marker covers it."""

    def _result(self, tmp_path: Path):
        return RtlBuddyTestResults("t", _grade(tmp_path, FINAL_ASSERT_LOG, 1))

    def test_xfail_excuses_it(self, tmp_path: Path):
        result = apply_xfail(self._result(tmp_path))
        assert result.results["result"] == "XFAIL"

    def test_strict_xfail_is_not_an_unexpected_pass(self, tmp_path: Path):
        result = apply_xfail(self._result(tmp_path), strict=True)
        assert result.results["result"] == "XFAIL"
        assert not is_run_failure(result)

    def test_xfail_match_reads_the_error_line(self, tmp_path: Path):
        result = apply_xfail(self._result(tmp_path), match=r"tb\.empty_chk")
        assert result.results["result"] == "XFAIL"

    def test_xfail_match_for_another_failure_stays_fail(self, tmp_path: Path):
        result = apply_xfail(self._result(tmp_path), match=r"scoreboard")
        assert result.results["result"] == "FAIL"


def test_vlog_sim_post_reads_stderr_for_the_error_line(tmp_path: Path):
    """``VlogSim.post`` passes both transcripts to the grader."""
    log = tmp_path / "test.log"
    log.write_text("PASS done\n")
    err = tmp_path / "test.err"
    err.write_text("%Error: tb.sv:1: Verilog $stop\n")
    sim = VlogSim.__new__(VlogSim)
    sim.test_name = "t"
    sim.run_id = None
    sim.test_cfg = SimpleNamespace(uvm=None)
    sim._get_log_path = lambda run_id=None: str(log)
    sim._get_err_path = lambda run_id=None: str(err)
    sim._assertions_enabled = lambda: False
    sim._coverage_enabled = lambda: False
    results = sim.post(sim_returncode=1).results
    assert results["result"] == "FAIL"
    assert results[FIRST_ERROR_KEY] == "%Error: tb.sv:1: Verilog $stop"
