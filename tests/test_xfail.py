"""Tests for the shared xfail helper (runner/xfail.py) and the suite grading that applies it."""

import json

import pytest

from rtl_buddy import rtl_buddy as rtl_buddy_mod
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.config.test import TestConfig as TC
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    SimTimeoutResults,
    TestPassResults,
    TestResults,
)
from rtl_buddy.runner.xfail import (
    FAIL_STAGE_KEY,
    FAIL_STAGE_REASONS,
    FIRST_ERROR_KEY,
    apply_xfail,
    compile_xfail_match,
    is_pass_with_xfail,
    xfail_mismatch,
    xfail_refusal,
)
from rtl_buddy.tools.vlog_post import UvmVlogPost, VlogPost


class _Result:
    """Stand-in for a results object."""

    def __init__(self, status, desc="d", **extra):
        self.results = {"result": status, "desc": desc, **extra}


# ---------------------------------------------------------------------------
# is_pass_with_xfail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PASS", "SKIP", "XFAIL"])
def test_is_pass_true_statuses(status):
    assert is_pass_with_xfail({"result": status}) is True


@pytest.mark.parametrize("status", ["FAIL", "NA", "WHATEVER"])
def test_is_pass_false_statuses(status):
    assert is_pass_with_xfail({"result": status}) is False


def test_is_pass_xpass_nonstrict_passes():
    assert is_pass_with_xfail({"result": "XPASS", "xfail_strict": False}) is True
    # An absent flag means non-strict.
    assert is_pass_with_xfail({"result": "XPASS"}) is True


def test_is_pass_xpass_strict_fails():
    assert is_pass_with_xfail({"result": "XPASS", "xfail_strict": True}) is False


# ---------------------------------------------------------------------------
# apply_xfail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
def test_apply_xfail_fail_to_xfail_passes_either_strictness(strict):
    res = _Result("FAIL", desc="boom")
    apply_xfail(res, strict=strict)
    assert res.results["result"] == "XFAIL"
    assert is_pass_with_xfail(res.results) is True
    assert res.results["desc"].startswith("xfail (expected fail): ")
    assert res.results["desc"].endswith("boom")


def test_apply_xfail_pass_nonstrict_to_xpass_still_passes():
    res = _Result("PASS")
    apply_xfail(res, strict=False)
    assert res.results["result"] == "XPASS"
    assert res.results["xfail_strict"] is False
    assert is_pass_with_xfail(res.results) is True
    assert res.results["desc"].startswith("XPASS (expected fail but passed): ")


def test_apply_xfail_pass_strict_to_xpass_fails():
    res = _Result("PASS")
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert res.results["xfail_strict"] is True
    assert is_pass_with_xfail(res.results) is False
    assert res.results["desc"].startswith(
        "XPASS (expected fail but passed — strict, failing): "
    )


@pytest.mark.parametrize("status", ["SKIP", "NA"])
def test_apply_xfail_skip_and_na_pass_through(status):
    res = _Result(status, desc="kept")
    apply_xfail(res, strict=True)
    assert res.results["result"] == status
    assert res.results["desc"] == "kept"


def test_apply_xfail_default_strict_is_false():
    res = _Result("PASS")
    apply_xfail(res)
    assert is_pass_with_xfail(res.results) is True


# ---------------------------------------------------------------------------
# xfail_refusal / the stage rule
#
# The marker excuses only a verdict the flow's tool reported. A failure that
# carries FAIL_STAGE_KEY happened instead of a verdict and stays FAIL.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,reason",
    sorted(FAIL_STAGE_REASONS.items()),
)
@pytest.mark.parametrize("strict", [False, True])
def test_apply_xfail_refuses_every_known_stage(stage, reason, strict):
    res = _Result("FAIL", desc="boom", **{FAIL_STAGE_KEY: stage})
    apply_xfail(res, strict=strict)
    assert res.results["result"] == "FAIL"
    assert is_pass_with_xfail(res.results) is False
    assert res.results["desc"] == f"xfail not applied ({reason}): boom"
    # Strictness concerns an unexpected pass, so a refusal does not record it.
    assert "xfail_strict" not in res.results


def test_xfail_refusal_is_none_for_a_tool_verdict():
    assert xfail_refusal({"result": "FAIL", "desc": "mismatch at 120ns"}) is None
    # A falsy stage (an explicit null) does not refuse the marker.
    assert xfail_refusal({"result": "FAIL", FAIL_STAGE_KEY: None}) is None


def test_xfail_refusal_reports_an_unknown_stage_verbatim():
    # An unknown stage still refuses the marker.
    assert xfail_refusal({"result": "FAIL", FAIL_STAGE_KEY: "future"}) == "future"


def test_apply_xfail_still_excuses_a_pass_under_a_stage_key():
    # A stage key applies only to failures; a PASS is still an XPASS.
    res = _Result("PASS", **{FAIL_STAGE_KEY: "compile"})
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert is_pass_with_xfail(res.results) is False


# ---------------------------------------------------------------------------
# Suite grading as `rb test` sees it
# ---------------------------------------------------------------------------


class _StubBuilderCfg:
    def get_name(self):
        return "verilator"


class _StubRootCfg:
    """Duck-typed root_cfg; `_do_test_suite` only resolves a builder."""

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return _StubBuilderCfg()


def _suite_with_marker(tmp_path, marker: str):
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
        "  - name: neg_control\n"
        "    desc: a negative control\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    testbench: tb1\n"
        f"    {marker}\n"
    )
    return SuiteConfig(str(tmp_path / "tests.yaml"))


def _run_suite(tmp_path, monkeypatch, marker: str, result_factory):
    # Machine mode exposes the event's structured fields.
    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    suite_cfg = _suite_with_marker(tmp_path, marker)

    class _StubRunner:
        def __init__(self, **_kwargs):
            self.last_compile = None
            self.last_build_stamp = None

        def run(self):
            return result_factory()

    monkeypatch.setattr(rtl_buddy_mod, "TestRunner", _StubRunner)

    rb = RtlBuddy(name="rtl_buddy")
    rb.builder = "verilator"
    rb.root_cfg = _StubRootCfg()
    rb.rtl_builder_mode = "debug"
    return rb, rb._do_test_suite(suite_cfg, run_ids=[None])


def _read_event(tmp_path, name: str) -> dict:
    """Return the last ``name`` event in the machine-mode JSONL log."""
    records = [
        json.loads(line)
        for line in (tmp_path / "rtl_buddy.log").read_text().splitlines()
        if line.strip()
    ]
    matching = [r for r in records if r.get("event") == name]
    assert matching, f"no {name} event in {records}"
    return matching[-1]


@pytest.mark.parametrize("marker", ["xfail: true", "xfail_strict: true"])
def test_suite_grades_a_sim_timeout_as_fail_under_a_marker(
    tmp_path, monkeypatch, marker
):
    """A marked test killed at the sim timeout stays FAIL."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        marker,
        lambda: SimTimeoutResults(name="neg_control/results"),
    )

    assert len(suite_results) == 1
    res = suite_results[0]["results"]
    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == "xfail not applied (sim timeout): Sim hit timeout"
    assert rb._exit_code_from_results(suite_results) != 0
    # Check the event as well as the table cell.
    event = _read_event(tmp_path, "suite.xfail")
    assert event["excused"] is False
    assert event["reason"] == "sim timeout"
    assert event["reported"] == "FAIL"


def test_suite_grades_a_compile_failure_as_fail_under_a_marker(tmp_path, monkeypatch):
    """A marked test that stopped compiling stays FAIL."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        "xfail_strict: true",
        lambda: CompileFailResults(name="neg_control/results"),
    )

    res = suite_results[0]["results"]
    assert res.results["result"] == "FAIL"
    assert res.results["desc"].startswith("xfail not applied (compile failure): ")
    assert rb._exit_code_from_results(suite_results) != 0


def test_suite_still_excuses_a_sim_verdict_under_a_marker(tmp_path, monkeypatch):
    """A marked sim that ran and failed is excused."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        "xfail_strict: true",
        lambda: TestResults(
            name="neg_control/results",
            results={"result": "FAIL", "desc": "mismatch at 120ns"},
        ),
    )

    res = suite_results[0]["results"]
    assert res.results["result"] == "XFAIL"
    assert rb._exit_code_from_results(suite_results) == 0
    event = _read_event(tmp_path, "suite.xfail")
    assert event["excused"] is True
    assert "reason" not in event


def test_suite_still_fails_a_strict_xpass(tmp_path, monkeypatch):
    """A strict marker over a passing test is a failure."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        "xfail_strict: true",
        lambda: TestPassResults(name="neg_control/results"),
    )

    res = suite_results[0]["results"]
    assert res.results["result"] == "XPASS"
    assert rb._exit_code_from_results(suite_results) != 0


# ---------------------------------------------------------------------------
# xfail_match: the marker names the failure it expects (#832)
# ---------------------------------------------------------------------------


def test_apply_xfail_match_hits_the_desc():
    res = _Result("FAIL", desc="ECP-1017 loader refused the deadlock")
    apply_xfail(res, strict=True, match=r"ECP-10\d\d")
    assert res.results["result"] == "XFAIL"
    assert is_pass_with_xfail(res.results) is True


def test_apply_xfail_match_hits_the_first_error_line():
    # The desc may be generic (UVM counts, a hook's text); the grader's first error line still names the check.
    res = _Result(
        "FAIL",
        desc="1 uvm error detected",
        **{FIRST_ERROR_KEY: "UVM_ERROR tb.sv(42) @ 100: chk [ECP-1017] refused"},
    )
    apply_xfail(res, strict=True, match="ECP-1017")
    assert res.results["result"] == "XFAIL"


@pytest.mark.parametrize("strict", [False, True])
def test_apply_xfail_match_miss_stays_fail_with_the_reason(strict):
    res = _Result(
        "FAIL",
        desc="watchdog fired at 1ms",
        **{FIRST_ERROR_KEY: "ERR: watchdog fired at 1ms"},
    )
    apply_xfail(res, strict=strict, match="ECP-1017")
    assert res.results["result"] == "FAIL"
    assert is_pass_with_xfail(res.results) is False
    assert res.results["desc"] == (
        "xfail: failed for a different reason (expected /ECP-1017/): "
        "watchdog fired at 1ms"
    )
    assert "xfail_strict" not in res.results


def test_apply_xfail_match_is_searched_not_anchored():
    res = _Result("FAIL", desc="prefix ECP-1017 suffix")
    apply_xfail(res, match="^ECP")
    assert res.results["result"] == "FAIL"
    res = _Result("FAIL", desc="prefix ECP-1017 suffix")
    apply_xfail(res, match="ECP-1017")
    assert res.results["result"] == "XFAIL"


@pytest.mark.parametrize("strict", [False, True])
def test_apply_xfail_match_leaves_a_pass_graded_as_before(strict):
    res = _Result("PASS")
    apply_xfail(res, strict=strict, match="ECP-1017")
    assert res.results["result"] == "XPASS"
    assert is_pass_with_xfail(res.results) is (not strict)


def test_apply_xfail_stage_refusal_wins_over_match():
    # A failure instead of a verdict is reported as such even when the regex would match.
    res = _Result("FAIL", desc="ECP-1017", **{FAIL_STAGE_KEY: "compile"})
    apply_xfail(res, strict=True, match="ECP-1017")
    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == "xfail not applied (compile failure): ECP-1017"


def test_xfail_mismatch_none_without_a_pattern():
    assert xfail_mismatch({"result": "FAIL", "desc": "x"}, None) is None


def test_compile_xfail_match_unset_is_none():
    assert compile_xfail_match(None, xfail=False, where="test 't'") is None


def test_compile_xfail_match_requires_a_marker():
    with pytest.raises(FatalRtlBuddyError, match="requires xfail"):
        compile_xfail_match("ECP", xfail=False, where="test 't'")


@pytest.mark.parametrize("bad", ["", 42])
def test_compile_xfail_match_rejects_a_non_regex_value(bad):
    with pytest.raises(FatalRtlBuddyError, match="non-empty regex"):
        compile_xfail_match(bad, xfail=True, where="test 't'")


def test_compile_xfail_match_rejects_an_invalid_regex():
    with pytest.raises(FatalRtlBuddyError, match=r"test 't'.*not a valid regex"):
        compile_xfail_match("ECP-(1017", xfail=True, where="test 't'")


def test_suite_config_loads_xfail_match(tmp_path):
    cfg = _suite_with_marker(
        tmp_path, 'xfail_strict: true\n    xfail_match: "ECP-1017"'
    )
    test = cfg.tests["neg_control"]
    assert test.get_xfail_match() == "ECP-1017"
    assert TC.from_plan_dict(test.to_plan_dict()).get_xfail_match() == ("ECP-1017")


def test_suite_config_rejects_xfail_match_without_a_marker(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="neg_control.*requires xfail"):
        _suite_with_marker(tmp_path, 'xfail_match: "ECP-1017"')


def test_suite_config_rejects_an_invalid_xfail_match(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="neg_control.*not a valid regex"):
        _suite_with_marker(tmp_path, 'xfail: true\n    xfail_match: "ECP-(1017"')


_MATCH_MARKER = 'xfail_strict: true\n    xfail_match: "ECP-1017"'


def test_suite_excuses_the_named_failure(tmp_path, monkeypatch):
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        _MATCH_MARKER,
        lambda: TestResults(
            name="neg_control/results",
            results={
                "result": "FAIL",
                "desc": "loader check fired",
                FIRST_ERROR_KEY: "ERR: ECP-1017 deadlock refused",
            },
        ),
    )
    res = suite_results[0]["results"]
    assert res.results["result"] == "XFAIL"
    assert rb._exit_code_from_results(suite_results) == 0
    assert _read_event(tmp_path, "suite.xfail")["excused"] is True


def test_suite_fails_a_different_failure(tmp_path, monkeypatch):
    """The check under test went silent and the watchdog fired instead."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        _MATCH_MARKER,
        lambda: TestResults(
            name="neg_control/results",
            results={
                "result": "FAIL",
                "desc": "watchdog expired",
                FIRST_ERROR_KEY: "ERR: watchdog expired",
            },
        ),
    )
    res = suite_results[0]["results"]
    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == (
        "xfail: failed for a different reason (expected /ECP-1017/): watchdog expired"
    )
    assert rb._exit_code_from_results(suite_results) != 0
    event = _read_event(tmp_path, "suite.xfail")
    assert event["excused"] is False
    assert event["reason"] == "failed for a different reason (expected /ECP-1017/)"


def test_suite_with_match_still_fails_a_strict_xpass(tmp_path, monkeypatch):
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        _MATCH_MARKER,
        lambda: TestPassResults(name="neg_control/results"),
    )
    assert suite_results[0]["results"].results["result"] == "XPASS"
    assert rb._exit_code_from_results(suite_results) != 0


# ---------------------------------------------------------------------------
# The sim graders record the first error line xfail_match searches
# ---------------------------------------------------------------------------


def test_vlog_post_records_the_first_err_line(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("starting\nERR: ECP-1017 refused\nFAT: later\nFAIL check\n")
    res = VlogPost(name="t", path=str(log)).get_results()
    assert res.results["result"] == "FAIL"
    assert res.results[FIRST_ERROR_KEY] == "ERR: ECP-1017 refused"


def test_vlog_post_pass_has_no_first_error(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("PASS all good\n")
    res = VlogPost(name="t", path=str(log)).get_results()
    assert FIRST_ERROR_KEY not in res.results


def test_uvm_vlog_post_records_the_first_uvm_error(tmp_path):
    log = tmp_path / "test.log"
    log.write_text(
        "UVM_INFO tb.sv(10) @ 0: reporter [START] go\n"
        "# UVM_ERROR tb.sv(42) @ 100: chk [ECP-1017] refused\n"
        "UVM_FATAL tb.sv(50) @ 200: wd [WDOG] expired\n"
        "--- UVM Report Summary ---\n"
        "** Report counts by severity\n"
        "UVM_INFO :    1\n"
        "UVM_WARNING :    0\n"
        "UVM_ERROR :    1\n"
        "UVM_FATAL :    1\n"
    )
    res = UvmVlogPost(name="t", path=str(log), max_warns=0, max_errors=0).get_results()
    assert res.results["result"] == "FAIL"
    assert res.results[FIRST_ERROR_KEY] == (
        "# UVM_ERROR tb.sv(42) @ 100: chk [ECP-1017] refused"
    )
