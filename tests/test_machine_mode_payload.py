"""Machine-mode result payloads must carry the guardrail data the run computes.

Covers the regression/test coverage summary (#347) and the FPV vacuity / COI /
dead-assume results (#348), both of which were dropped on the ``--machine``
surfaces while the human summary rendered them.
"""

import json
import logging
from types import SimpleNamespace

from rtl_buddy.logging_utils import render_summary, setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.vlog_cov import CoverageMetrics


def _results(**fields):
    return SimpleNamespace(results=fields)


class _DummyRootCfg:
    def get_project_rootdir(self):
        return "/repo"

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: "verilator",
            get_name=lambda: "verilator",
        )

    def get_use_lcov(self, _simulator_name):
        return True

    def get_coverview_cfg(self, _simulator_name):
        return None


# --- #348: FPV guardrails on the machine row --------------------------------


def test_fpv_result_row_includes_vacuity_and_coi():
    rb = RtlBuddy(name="rtl_buddy")
    vacuity = {"vacuous": 1, "candidates": 4, "covers": []}
    coi = {
        "percent": 83.0,
        "coi_cells": 210,
        "total_cells": 253,
        "assumes": {"total": 3, "in_assert_coi": 2, "dead": 1},
    }
    r = {
        "fpv_name": "p",
        "results": _results(
            result="PASS",
            desc="ok",
            mode="bmc",
            depth=20,
            engines=["bmc"],
            runtime_s=1.5,
            vacuity=vacuity,
            coi=coi,
        ),
    }
    row = rb._fpv_result_row(r)
    assert row["vacuity"] == vacuity
    assert row["coi"] == coi
    assert row["coi"]["assumes"]["dead"] == 1


def test_fpv_result_row_omits_absent_guardrails():
    rb = RtlBuddy(name="rtl_buddy")
    r = {"fpv_name": "p", "results": _results(result="PASS", desc="ok")}
    row = rb._fpv_result_row(r)
    assert "vacuity" not in row
    assert "coi" not in row


def test_fpv_result_row_omits_empty_guardrails():
    rb = RtlBuddy(name="rtl_buddy")
    r = {
        "fpv_name": "p",
        "results": _results(result="PASS", desc="ok", vacuity={}, coi=None),
    }
    row = rb._fpv_result_row(r)
    assert "vacuity" not in row
    assert "coi" not in row


# --- #347: structured coverage on the machine row and payload ---------------


def test_machine_coverage_extracts_metric_percentages():
    tr = _results(
        coverage={
            "line": 0.92,
            "branch": 0.88,
            "toggle": 0.75,
            "functional": 1.0,
            "summary": "L:0.92 B:0.88 T:0.75 F:1.00",
            "lcov_path": "/x/merged.info",
        }
    )
    assert RtlBuddy._machine_coverage(tr) == {
        "line": 0.92,
        "branch": 0.88,
        "toggle": 0.75,
        "functional": 1.0,
    }


def test_machine_coverage_none_without_coverage():
    assert RtlBuddy._machine_coverage(_results()) is None
    assert RtlBuddy._machine_coverage(_results(coverage={})) is None


def test_machine_coverage_none_when_all_metrics_missing():
    tr = _results(coverage={"summary": "-", "raw_paths": []})
    assert RtlBuddy._machine_coverage(tr) is None


def test_machine_test_row_attaches_coverage_suite_and_run_id():
    rb = RtlBuddy(name="rtl_buddy")
    tr = _results(result="PASS", desc="ok", coverage={"line": 0.5})
    row = rb._machine_test_row("basic", tr, suite="alu", run_id=3)
    assert row == {
        "name": "basic",
        "result": "PASS",
        "desc": "ok",
        "suite": "alu",
        "run_id": 3,
        "coverage": {"line": 0.5, "branch": None, "toggle": None, "functional": None},
    }


def test_machine_test_row_without_coverage_stays_minimal():
    rb = RtlBuddy(name="rtl_buddy")
    row = rb._machine_test_row("basic", _results(result="FAIL", desc="boom"))
    assert row == {"name": "basic", "result": "FAIL", "desc": "boom"}


def test_machine_coverage_payload_gates_on_data():
    assert (
        RtlBuddy._machine_coverage_payload({"merged": None, "dir_summary": []}) is None
    )
    merged = {"merged": {"line": 0.9}, "dir_summary": []}
    assert RtlBuddy._machine_coverage_payload(merged) is merged
    dirs = {"merged": None, "dir_summary": [{"prefix": "rtl"}]}
    assert RtlBuddy._machine_coverage_payload(dirs) is dirs


def test_machine_coverage_payload_survives_on_cover_points_alone():
    """Cover points are recorded without any --coverage-merge* flag (#367)."""
    covers = {
        "merged": None,
        "dir_summary": [],
        "covers": [{"name": "APB_IF_WRITE", "file": "tb.sv", "line": 89, "hits": 13}],
    }
    assert RtlBuddy._machine_coverage_payload(covers) is covers
    assert (
        RtlBuddy._machine_coverage_payload(
            {"merged": None, "dir_summary": [], "covers": None}
        )
        is None
    )


def test_machine_test_row_carries_per_test_cover_points():
    covers = [{"name": "APB_IF_WRITE", "file": "tb.sv", "line": 89, "hits": 4}]
    rb = RtlBuddy(name="rtl_buddy")
    row = rb._machine_test_row(
        "basic",
        _results(
            result="PASS",
            desc="",
            coverage={
                "line": 0.9,
                "branch": None,
                "toggle": None,
                "functional": 1.0,
                "covers": covers,
            },
        ),
    )
    assert row["coverage"]["covers"] == covers
    assert row["coverage"]["functional"] == 1.0


def test_machine_test_row_omits_covers_when_absent():
    rb = RtlBuddy(name="rtl_buddy")
    row = rb._machine_test_row(
        "basic",
        _results(
            result="PASS",
            desc="",
            coverage={"line": 0.9, "branch": None, "toggle": None, "functional": None},
        ),
    )
    assert "covers" not in row["coverage"]


def test_build_metadata_aggregates_cover_points_across_tests(tmp_path):
    """Per-test lists fold into one run-level list, no merge flag needed."""
    reporter = CoverageReporter(_DummyRootCfg())
    suite_results = [
        {
            "test_name": "a",
            "results": SimpleNamespace(
                results={
                    "coverage": {
                        "raw_paths": ["/tmp/a.dat"],
                        "covers": [
                            {
                                "name": "C1",
                                "file": "tb.sv",
                                "line": 10,
                                "module": "dut",
                                "hits": 2,
                            },
                            {
                                "name": "C2",
                                "file": "tb.sv",
                                "line": 20,
                                "module": "dut",
                                "hits": 0,
                            },
                        ],
                    }
                }
            ),
        },
        {
            "test_name": "b",
            "results": SimpleNamespace(
                results={
                    "coverage": {
                        "raw_paths": ["/tmp/b.dat"],
                        "covers": [
                            {
                                "name": "C1",
                                "file": "tb.sv",
                                "line": 10,
                                "module": "dut",
                                "hits": 5,
                            },
                        ],
                    }
                }
            ),
        },
    ]

    _, coverage = reporter.build_metadata(
        suite_results, outdir=str(tmp_path), suite_name="suite"
    )

    assert coverage["covers"] == [
        {"name": "C1", "file": "tb.sv", "line": 10, "module": "dut", "hits": 7},
        {"name": "C2", "file": "tb.sv", "line": 20, "module": "dut", "hits": 0},
    ]


def test_build_metadata_returns_structured_merged_coverage(tmp_path, monkeypatch):
    reporter = CoverageReporter(_DummyRootCfg())

    class FakeCov:
        def merge(
            self,
            raw_paths,
            outdir,
            merge_basename,
            html_output,
            source_roots,
            html_outdir=None,
        ):
            return CoverageMetrics(line=0.92, branch=0.88, toggle=0.75, functional=1.0)

    monkeypatch.setattr(reporter, "_get_cov_tool", lambda: FakeCov())
    suite_results = [
        {
            "test_name": "basic",
            "results": SimpleNamespace(
                results={"coverage": {"raw_paths": ["/tmp/basic.dat"]}}
            ),
        }
    ]

    metadata, coverage = reporter.build_metadata(
        suite_results,
        outdir=str(tmp_path),
        suite_name="suite",
        coverage_merge_raw=True,
    )

    assert coverage["merged"] == {
        "line": 0.92,
        "branch": 0.88,
        "toggle": 0.75,
        "functional": 1.0,
    }
    assert any("Merged Coverage:" in line for line in metadata)


def test_build_metadata_without_coverage_returns_empty_payload(tmp_path):
    reporter = CoverageReporter(_DummyRootCfg())
    metadata, coverage = reporter.build_metadata(
        [], outdir=str(tmp_path), suite_name="suite"
    )
    # `covers` is omitted, not null, when no user points were recorded — so
    # "absent means not collected" holds on the run level as well as the rows.
    assert coverage == {"merged": None, "dir_summary": []}
    assert metadata == []


def test_dir_summary_records_are_structured_and_format_back(tmp_path, monkeypatch):
    reporter = CoverageReporter(_DummyRootCfg())
    lcov = tmp_path / "merged.info"
    lcov.write_text("SF:/x\nend_of_record\n")

    class FakeCov:
        def parse_lcov_summary_for_prefix(self, path, prefix):
            return (0.9, 0.8)  # (line, branch)

    monkeypatch.setattr(reporter, "_get_cov_tool", lambda: FakeCov())

    records = reporter._dir_summary_records(str(lcov), ["rtl/core"])
    assert records == [
        {
            "prefix": "rtl/core",
            "line": 0.9,
            "branch": 0.8,
            "toggle": None,
            "functional": None,
        }
    ]
    assert reporter._dir_summary_lines(records) == [
        "Coverage rtl/core: L:0.90 B:0.80 T:UNSP F:UNSP"
    ]


# --- the shared surface: render_summary emits the "summary" event in --machine


def test_render_summary_emits_summary_event_in_machine_mode(tmp_path):
    log_path = tmp_path / "rtl_buddy.log"
    setup_logging(machine=True, color=False, log_path=log_path)
    logger = logging.getLogger("rtl_buddy.tests.machine")

    render_summary(
        title="Regression Results Summary",
        columns=[("name", "Test"), ("result", "Result")],
        rows=[{"name": "basic", "result": "PASS"}],
        logger=logger,
        metadata=["Merged Coverage: L:0.92 B:0.88 T:0.75 F:1.00"],
    )

    events = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    summary = [e for e in events if e.get("event") == "summary"]
    assert summary, events
    assert summary[0]["rows"] == [{"name": "basic", "result": "PASS"}]
    assert "Merged Coverage: L:0.92 B:0.88 T:0.75 F:1.00" in summary[0]["metadata"]
