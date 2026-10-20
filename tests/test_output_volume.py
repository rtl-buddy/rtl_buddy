"""Large runs retain structured results without printing every passing item."""

import json
import logging

import pytest

from rtl_buddy.dispatch.rightsize import RightsizeFinding
from rtl_buddy.logging_utils import render_summary, setup_logging, task_status
from rtl_buddy.rtl_buddy import _log_reservation_advice


def _finding(**overrides):
    fields = dict(
        suite="verif/tests.yaml",
        test="basic",
        resource="mem",
        reserved="8G",
        peak="1G",
        utilization=0.125,
        direction="reduce",
        suggested="1536M",
        runs=1,
        reg_level=1000,
        states=["COMPLETED"],
        edit_hint={
            "file": "/project/verif/tests.yaml",
            "path": "tests[name=basic].resources.mem",
        },
    )
    return RightsizeFinding(**(fields | overrides))


@pytest.mark.parametrize("debug", [False, True])
def test_large_advice_list_has_one_info_aggregate(tmp_path, debug):
    log_path = tmp_path / "rtl_buddy.log"
    setup_logging(machine=True, debug=debug, log_path=log_path)
    findings = [_finding(test=f"test_{i}") for i in range(1000)]
    findings.insert(0, _finding(direction="raise"))

    _log_reservation_advice(findings)

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    [summary] = [e for e in events if e["event"] == "rightsize.summary"]
    assert summary["level"] == "INFO"
    assert summary["total"] == 1001
    assert summary["counts"] == {"raise": 1, "reduce": 1000}
    details = [e for e in events if e["event"] == "rightsize.advice"]
    assert len(events) == (1002 if debug else 1)
    if debug:
        assert all(e["level"] == "DEBUG" for e in details)
        assert [e["test"] for e in details] == [f.test for f in findings]
        assert details[0]["direction"] == "raise"


def test_empty_advice_emits_no_events(tmp_path):
    log_path = tmp_path / "rtl_buddy.log"
    setup_logging(machine=True, log_path=log_path)
    _log_reservation_advice([])
    assert log_path.read_text() == ""


def test_machine_phase_status_does_not_print_per_test(capsys):
    setup_logging(machine=True)
    for i in range(1000):
        with task_status(f"Running simulation pass_{i}") as status:
            assert status is None
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("machine,verbose", [(False, False), (True, True)])
def test_phase_status_remains_visible_in_human_and_verbose_modes(
    capsys, machine, verbose
):
    setup_logging(machine=machine, verbose=verbose, color=False)
    with task_status("Running simulation basic"):
        pass
    assert "Running simulation basic" in capsys.readouterr().err


def test_compact_advice_preserves_edit_provenance_and_optional_values():
    finding = _finding(
        reg_level=None,
        allocated="16G",
        phase="compile",
        suggested_total="4G",
        aggregate_delta="2G",
        edit_hint={
            "file": "/orchestration/root_config.yaml",
            "path": "cfg-dispatch.compile.modes.cov.mem",
            "note": "Aggregated build reservation",
        },
    )
    record = finding.as_event()
    assert record["edit_hint"] == finding.edit_hint
    assert record["allocated"] == "16G"
    assert record["suggested_total"] == "4G"
    assert record["aggregate_delta"] == "2G"
    assert record["phase"] == "compile"
    assert "event" not in record
    assert "reg_level" not in record
    assert all(v is not None for v in record.values())
    finding = _finding(edit_hint={"path": "env", "note": "SBATCH_CPUS_PER_TASK=4"})
    assert finding.as_event()["edit_hint"] == finding.edit_hint
    assert "allocated" not in finding.as_event()
    assert "suggested_total" not in finding.as_event()
    assert "aggregate_delta" not in finding.as_event()


def test_machine_summary_hides_successes_and_preserves_all_verdicts(tmp_path, capsys):
    log_path = tmp_path / "rtl_buddy.log"
    setup_logging(machine=True, log_path=log_path)
    rows = [{"test": f"pass_{i}", "result": "PASS"} for i in range(1000)]
    rows += [
        {"test": verdict.lower(), "result": verdict}
        for verdict in ("SKIP", "XFAIL", "FAIL", "XPASS", "NA", "UNKNOWN")
    ]
    render_summary(
        title="Regression",
        columns=[("test", "Test"), ("result", "Result")],
        rows=rows,
        logger=logging.getLogger("rtl_buddy.tests"),
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "pass_" not in output.err
    assert "skip" not in output.err
    assert "xfail" not in output.err
    for name in ("fail", "xpass", "na", "unknown"):
        assert name in output.err
    assert "1000 PASS" in output.err
    assert "1006 total" in output.err
    assert len(output.err.splitlines()) < 12
    [summary] = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert summary["rows"] == rows
    assert summary["counts"]["PASS"] == 1000
