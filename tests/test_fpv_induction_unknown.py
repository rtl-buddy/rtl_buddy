"""Grading of sby ``mode prove`` outcomes against xfail (rtl-buddy/rtl_buddy#792).

The fixtures under ``tests/fixtures/sby_logs/`` are sby v0.66 workdir ``status`` and ``logfile.txt``
captures (paths scrubbed). ``prove_unknown_no_induction`` is derived from ``prove_induction_unknown``
with the induction verdict rewritten to ``UNKNOWN``, standing in for an UNKNOWN with no failed induction.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.runner.xfail import apply_xfail
from rtl_buddy.tools.fpv_log_parse import induction_failed

from test_fpv import _make_fpv_cfg, _tool_cfg

FIXTURES = Path(__file__).parent / "fixtures" / "sby_logs"

# fixture -> sby exit code it was captured with
_RC = {
    "prove_induction_unknown": 4,
    "prove_unknown_no_induction": 4,
    "prove_basecase_fail": 2,
    "prove_error": 16,
}


def _log(case: str) -> str:
    return (FIXTURES / case / "logfile.txt").read_text()


def test_induction_failed_detects_captured_noninductive_proof():
    assert induction_failed(_log("prove_induction_unknown")) is True


@pytest.mark.parametrize(
    "case", ["prove_unknown_no_induction", "prove_basecase_fail", "prove_error"]
)
def test_induction_failed_ignores_other_outcomes(case):
    assert induction_failed(_log(case)) is False


def test_induction_failed_needs_the_basecase_of_the_same_engine():
    """An induction FAIL whose engine never reported a passing base case is not a clean verdict."""
    text = (
        "SBY [wd] summary: engine_0 (smtbmc yices) returned FAIL for induction\n"
        "SBY [wd] summary: engine_1 (smtbmc z3) returned pass for basecase\n"
    )
    assert induction_failed(text) is False


def _run_with_fixture(tmp_path, monkeypatch, case, *, mode="prove"):
    """Run SbyFpv.run() with sby replaced by a copy of a captured workdir."""
    from rtl_buddy.tools.sby_fpv import SbyFpv

    src = tmp_path / "design.sv"
    src.write_text("module design(); endmodule\n")
    fpv_cfg = _make_fpv_cfg(name="demo", top="design", mode=mode, depth=4)
    fpv_cfg.vacuity = False
    fpv_cfg.coi = False
    sby = SbyFpv(
        name="t/sby", fpv_cfg=fpv_cfg, tool_cfg=_tool_cfg(), suite_dir=str(tmp_path)
    )
    fl = Path(sby._filelist_path())
    fl.write_text(f"{src}\n")
    monkeypatch.setattr(sby, "_write_filelist", lambda: str(fl))
    monkeypatch.setattr(sby, "_probe_sby_version", lambda exe: None)

    def fake_run(cmd, log_path):
        workdir = Path(sby._workdir_path())
        shutil.copytree(FIXTURES / case, workdir)
        Path(log_path).write_text((workdir / "logfile.txt").read_text())
        return SimpleNamespace(returncode=_RC[case])

    monkeypatch.setattr(sby, "_run", fake_run)
    return sby.run()


@pytest.mark.parametrize("strict", [False, True])
def test_prove_unknown_from_failed_induction_is_an_xfail_eligible_verdict(
    tmp_path, monkeypatch, strict
):
    res = _run_with_fixture(tmp_path, monkeypatch, "prove_induction_unknown")
    assert res.results["result"] == "FAIL"
    assert "fail_stage" not in res.results
    assert res.results["desc"].startswith("induction failed: ")
    assert "not k-inductive" in res.results["desc"]

    apply_xfail(res, strict=strict)
    assert res.results["result"] == "XFAIL"
    assert res.is_pass() is True


def test_prove_unknown_from_failed_induction_without_marker_still_fails(
    tmp_path, monkeypatch
):
    res = _run_with_fixture(tmp_path, monkeypatch, "prove_induction_unknown")
    assert res.is_pass() is False


@pytest.mark.parametrize("case", ["prove_unknown_no_induction", "prove_error"])
def test_prove_unknown_or_error_without_failed_induction_stays_a_tool_failure(
    tmp_path, monkeypatch, case
):
    res = _run_with_fixture(tmp_path, monkeypatch, case)
    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "tool"

    apply_xfail(res, strict=True)
    assert res.results["result"] == "FAIL"
    assert res.results["desc"].startswith(
        "xfail not applied (tool failure before a verdict): "
    )


def test_induction_reclassification_is_limited_to_prove(tmp_path, monkeypatch):
    """Only `mode prove` runs k-induction; an UNKNOWN in any other mode stays a tool failure."""
    res = _run_with_fixture(
        tmp_path, monkeypatch, "prove_induction_unknown", mode="bmc"
    )
    assert res.results["fail_stage"] == "tool"


def test_prove_basecase_fail_is_a_disproof(tmp_path, monkeypatch):
    res = _run_with_fixture(tmp_path, monkeypatch, "prove_basecase_fail")
    assert res.results["result"] == "FAIL"
    assert "fail_stage" not in res.results
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XFAIL"
