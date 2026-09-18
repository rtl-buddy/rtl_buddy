"""Unit tests for the shared xfail helper (runner/xfail.py), the single
source of truth for expected-fail re-interpretation used by every
command's result classes — plus the suite-level grading that applies it."""

import json

import pytest

from rtl_buddy import rtl_buddy as rtl_buddy_mod
from rtl_buddy.config.suite import SuiteConfig
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
    apply_xfail,
    is_pass_with_xfail,
    xfail_refusal,
)


class _Result:
    """Minimal stand-in for a *Results object: just a mutable dict."""

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
    # absent flag behaves as non-strict
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
    apply_xfail(res)  # strict defaults to False
    assert is_pass_with_xfail(res.results) is True


# ---------------------------------------------------------------------------
# xfail_refusal / the stage rule (#553, #594)
#
# The marker excuses only a verdict the flow's own tool reported. A failure
# that happened *instead of* a verdict carries FAIL_STAGE_KEY and keeps its
# FAIL, so a negative control that stopped compiling — or that was killed at
# the sim timeout before reaching the mismatch it exists to catch — cannot
# read green.
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
    # The refusal never records strictness: a strict marker is about an
    # unexpected *pass*, and this is not a pass either way.
    assert "xfail_strict" not in res.results


def test_xfail_refusal_is_none_for_a_tool_verdict():
    assert xfail_refusal({"result": "FAIL", "desc": "mismatch at 120ns"}) is None
    # A falsy stage (an older envelope's explicit null) excuses as before.
    assert xfail_refusal({"result": "FAIL", FAIL_STAGE_KEY: None}) is None


def test_xfail_refusal_reports_an_unknown_stage_verbatim():
    # Fail loud rather than excuse silently: a stage this rtl_buddy does not
    # know (a newer job's envelope) still refuses the marker.
    assert xfail_refusal({"result": "FAIL", FAIL_STAGE_KEY: "future"}) == "future"


def test_apply_xfail_still_excuses_a_pass_under_a_stage_key():
    # A stage marker only ever speaks about a failure; an unexpected PASS is
    # still an XPASS, strict or not.
    res = _Result("PASS", **{FAIL_STAGE_KEY: "compile"})
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert is_pass_with_xfail(res.results) is False


# ---------------------------------------------------------------------------
# Suite grading: the rule as a `rb test` run sees it (#553, #594)
# ---------------------------------------------------------------------------


class _StubBuilderCfg:
    def get_name(self):
        return "verilator"


class _StubRootCfg:
    """Duck-typed root_cfg: `_do_test_suite` only resolves a builder."""

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
    # Machine mode so the assertions can read the event's structured
    # fields, which is what a CI consumer reads too.
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
    """The last ``name`` event out of the machine-mode JSONL log."""
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
    """#594: a marked test killed at the sim timeout must not read green."""
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
    # The event a CI reader greps, not just the table cell.
    event = _read_event(tmp_path, "suite.xfail")
    assert event["excused"] is False
    assert event["reason"] == "sim timeout"
    assert event["reported"] == "FAIL"


def test_suite_grades_a_compile_failure_as_fail_under_a_marker(tmp_path, monkeypatch):
    """#553: same for a negative control that stopped compiling."""
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
    """The marker's whole point still works: a sim that ran and failed."""
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
    """Unchanged: a strict marker over a passing test is still a failure."""
    rb, suite_results = _run_suite(
        tmp_path,
        monkeypatch,
        "xfail_strict: true",
        lambda: TestPassResults(name="neg_control/results"),
    )

    res = suite_results[0]["results"]
    assert res.results["result"] == "XPASS"
    assert rb._exit_code_from_results(suite_results) != 0
