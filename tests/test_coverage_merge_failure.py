"""Tests for a raw coverage merge that dies being reported as a failure.

The metrics only the merge carried print `FAIL` (never-instrumented ones stay `UNSP`), the payload and manifest carry `merge_failed` and `failed_metrics`, `totals` stays intact, and the run exits 1 after writing every result."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.cov.manifest import MANIFEST_FILENAME
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import TestResults
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools import vlog_cov as vlog_cov_module
from rtl_buddy.tools.vlog_cov import CoverageMetrics, VlogCov

_KILLED = -15


def _dat_record(*, file, line, type_, name, module, col=1, hits=1):
    """One Verilator raw-database counter record."""
    keys = [
        ("f", file),
        ("l", str(line)),
        ("n", str(col)),
        ("t", type_),
        ("page", f"v_{type_}/{module}"),
        ("o", name),
        ("h", f"tb_top.{module}.{name}"),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {hits}\n"


class _RootCfg:
    """The slice of RootConfig the coverage reporter reads."""

    def __init__(self, root):
        self._root = str(root)

    def get_project_rootdir(self):
        return self._root

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: "verilator",
            get_name=lambda: "verilator",
        )

    def get_use_lcov(self, _simulator_name):
        return True

    def get_coverview_cfg(self, _simulator_name):
        return None


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root with one test's raw coverage database on disk."""
    root = tmp_path / "repo"
    (root / "design").mkdir(parents=True)
    (root / "design" / "blk.sv").write_text(
        "module blk;\n  always_comb begin end\nendmodule\n", encoding="utf-8"
    )
    raw = root / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_record(
            file="../../../design/blk.sv", line=1, type_="line", name="", module="blk"
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=2,
            type_="toggle",
            name="q",
            module="blk",
            hits=0,
        ),
        encoding="utf-8",
    )
    return root


def _suite_results(project: Path):
    raw = project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat"
    return [
        {
            "test_name": "basic",
            "results": TestResults(
                name="basic",
                results={
                    "result": "PASS",
                    "desc": "ok",
                    "coverage": {"raw_paths": [str(raw)]},
                },
            ),
        }
    ]


def _shim_verilator_coverage(monkeypatch, project, *, merge_returncode):
    """Shim `verilator_coverage` so the merge dies on demand while `--write-info` keeps working."""
    import subprocess

    blk = project / "design" / "blk.sv"
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "verilator_coverage" and cmd[1] == "--write":
            if merge_returncode == 0:
                Path(cmd[2]).write_text("# SystemC::Coverage-3\n", encoding="utf-8")
            return SimpleNamespace(returncode=merge_returncode, stdout="", stderr="")
        if cmd[0] == "verilator_coverage" and cmd[1] == "--write-info":
            Path(cmd[2]).write_text(
                f"SF:{blk}\nDA:1,1\nDA:2,0\nend_of_record\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        # `strings` probing and `--annotate` summaries report nothing, like an unsupported metric.
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    def fake_managed(cmd, **kwargs):
        result = fake_run(cmd)
        return SimpleNamespace(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=False,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(vlog_cov_module, "run_managed_process", fake_managed)
    return calls


def test_dead_merge_fails_only_the_metrics_it_alone_carried(monkeypatch, project):
    """`FAIL` for what only the merge carried, `UNSP` for what was never there."""
    _shim_verilator_coverage(monkeypatch, project, merge_returncode=_KILLED)
    cov = VlogCov(simulator_name="verilator", use_lcov=True, root_cfg=_RootCfg(project))
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)

    metrics = cov.merge(
        [str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")],
        outdir=str(cov_dir),
    )

    # A failed merge always returns metrics, so callers need not infer failure from None.
    assert metrics is not None
    assert metrics.merged_path is None
    assert metrics.merge_failed is True
    # Line survives through the LCOV export; branch is None because the merged `.info` records no branch point.
    assert metrics.failed_metrics == ["toggle", "expression", "functional"]
    assert metrics.line == pytest.approx(0.5)
    assert metrics.summary_str() == "L:0.50 B:UNSP T:FAIL F:FAIL"

    payload = metrics.to_dict()
    assert payload["merge_failed"] is True
    assert payload["failed_metrics"] == ["toggle", "expression", "functional"]
    assert payload["merged_path"] is None


def test_healthy_merge_keeps_an_unmeasured_metric_unsupported(monkeypatch, project):
    """A merge that lives reports `UNSP` for an unmeasured metric, not `FAIL`."""
    _shim_verilator_coverage(monkeypatch, project, merge_returncode=0)
    cov = VlogCov(simulator_name="verilator", use_lcov=True, root_cfg=_RootCfg(project))
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)

    metrics = cov.merge(
        [str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")],
        outdir=str(cov_dir),
    )

    assert metrics is not None
    assert metrics.merged_path is not None
    assert metrics.merge_failed is False
    assert metrics.failed_metrics is None
    assert metrics.summary_str() == "L:0.50 B:UNSP T:UNSP F:UNSP"
    assert metrics.to_dict()["merge_failed"] is False
    assert metrics.to_dict()["failed_metrics"] == []


def _lcov_export_inputs(calls):
    """The raw database each `verilator_coverage --write-info` call read."""
    return [
        cmd[-1] for cmd in calls if cmd[:2] == ["verilator_coverage", "--write-info"]
    ]


def test_merge_without_lcov_exports_only_the_merged_database(monkeypatch, project):
    """With `use-lcov` off and no HTML, the per-test exports are skipped and the merged database is exported once."""
    calls = _shim_verilator_coverage(monkeypatch, project, merge_returncode=0)
    cov = VlogCov(
        simulator_name="verilator", use_lcov=False, root_cfg=_RootCfg(project)
    )
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)
    raw = str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")

    metrics = cov.merge([raw, raw], outdir=str(cov_dir))

    assert metrics is not None
    assert _lcov_export_inputs(calls) == [str(cov_dir / "coverage_merged.dat")]
    assert metrics.lcov_path is None
    assert not (cov_dir / "coverage_merged.info").exists()
    assert metrics.line == pytest.approx(0.5)


def test_merge_with_lcov_still_exports_every_part(monkeypatch, project):
    """`use-lcov` keeps the per-test exports, which build the merged `.info`."""
    calls = _shim_verilator_coverage(monkeypatch, project, merge_returncode=0)
    cov = VlogCov(simulator_name="verilator", use_lcov=True, root_cfg=_RootCfg(project))
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)
    raw = str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")

    metrics = cov.merge([raw, raw], outdir=str(cov_dir))

    assert _lcov_export_inputs(calls) == [raw, raw]
    assert metrics.lcov_path == str(cov_dir / "coverage_merged.info")


def test_dead_merge_without_lcov_fails_line_and_branch_too(monkeypatch, project):
    """Without per-test exports the merged database is the only source, so a dead merge fails every metric and no export runs."""
    calls = _shim_verilator_coverage(monkeypatch, project, merge_returncode=_KILLED)
    cov = VlogCov(
        simulator_name="verilator", use_lcov=False, root_cfg=_RootCfg(project)
    )
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)

    metrics = cov.merge(
        [str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")],
        outdir=str(cov_dir),
    )

    assert _lcov_export_inputs(calls) == []
    assert metrics.merge_failed is True
    assert metrics.failed_metrics == [
        "line",
        "branch",
        "toggle",
        "expression",
        "functional",
    ]


def test_summary_cells_keep_their_width():
    """`FAIL` is four characters like `UNSP`, so the table does not reflow."""
    failed = CoverageMetrics(
        merge_failed=True, failed_metrics=["toggle", "functional"]
    ).summary_str()
    unsupported = CoverageMetrics().summary_str()
    assert failed == "L:UNSP B:UNSP T:FAIL F:FAIL"
    assert len(failed) == len(unsupported)


def test_failed_merge_reaches_the_payload_manifest_and_console(monkeypatch, project):
    """A failed merge reaches the payload, manifest and console."""
    _shim_verilator_coverage(monkeypatch, project, merge_returncode=_KILLED)
    reporter = CoverageReporter(_RootCfg(project))
    suite = project / "verif" / "blk"

    metadata, coverage = reporter.build_metadata(
        _suite_results(project),
        outdir=str(suite),
        suite_name=str(suite / "tests.yaml"),
        coverage_merge_raw=True,
    )

    assert "Merged Coverage: L:0.50 B:UNSP T:FAIL F:FAIL" in metadata
    # One line under the table says what failed.
    assert any(line.startswith("Coverage merge FAILED:") for line in metadata)
    assert any("toggle" in line for line in metadata if "FAILED" in line)

    assert coverage["merge_failed"] is True
    assert coverage["failed_metrics"] == ["toggle", "expression", "functional"]
    assert coverage["merged"]["toggle"] is None
    assert coverage["artefacts"]["merge_failed"] is True
    assert coverage["artefacts"]["merged_raw"] is None

    manifest = json.loads((suite / "cov_dir" / MANIFEST_FILENAME).read_text())
    assert manifest["merge_mode"] == "raw"
    assert manifest["merged"]["raw"] is None
    # `merge_failed` makes the failure explicit.
    assert manifest["merge_failed"] is True
    assert manifest["failed_metrics"] == ["toggle", "expression", "functional"]
    # `totals` is built from per-test databases, which the merge never touched.
    assert manifest["totals"]["toggle"]["found"] == 1


def test_healthy_merge_writes_the_keys_as_false_and_empty(monkeypatch, project):
    """The failure keys are always present, false and empty for a healthy merge."""
    _shim_verilator_coverage(monkeypatch, project, merge_returncode=0)
    reporter = CoverageReporter(_RootCfg(project))
    suite = project / "verif" / "blk"

    metadata, coverage = reporter.build_metadata(
        _suite_results(project),
        outdir=str(suite),
        suite_name=str(suite / "tests.yaml"),
        coverage_merge_raw=True,
    )

    assert not any("FAILED" in line for line in metadata)
    assert coverage["merge_failed"] is False
    assert coverage["failed_metrics"] == []
    manifest = json.loads((suite / "cov_dir" / MANIFEST_FILENAME).read_text())
    assert manifest["merge_failed"] is False
    assert manifest["failed_metrics"] == []
    assert manifest["merged"]["raw"] is not None


def test_read_verbs_forward_the_verdict(monkeypatch, project):
    """`rb cov` reads the manifest and forwards the verdict."""
    from rtl_buddy.cov import query as query_mod

    _shim_verilator_coverage(monkeypatch, project, merge_returncode=_KILLED)
    reporter = CoverageReporter(_RootCfg(project))
    suite = project / "verif" / "blk"
    reporter.build_metadata(
        _suite_results(project),
        outdir=str(suite),
        suite_name=str(suite / "tests.yaml"),
        coverage_merge_raw=True,
    )

    ctx = query_mod.load_context(
        str(project), manifest=str(suite / "cov_dir" / MANIFEST_FILENAME)
    )
    payload = query_mod.summary_payload(ctx)
    assert payload["merge_failed"] is True
    assert payload["failed_metrics"] == ["toggle", "expression", "functional"]


def test_failed_merge_exits_one_with_every_result_still_in_the_envelope(
    minimal_project: Path, capsys, monkeypatch
):
    """A failed merge exits 1 after every result and artefact has been written."""
    failed = ["toggle", "expression", "functional"]

    def fake_build_metadata(self, suite_results, **kwargs):
        return (
            ["Merged Coverage: L:0.50 B:1.00 T:FAIL F:FAIL"],
            {
                "merged": {
                    "line": 0.5,
                    "branch": 1.0,
                    "toggle": None,
                    "expression": None,
                    "functional": None,
                },
                "dir_summary": [],
                "merge_failed": True,
                "failed_metrics": failed,
            },
        )

    monkeypatch.setattr(CoverageReporter, "build_metadata", fake_build_metadata)
    monkeypatch.setattr(
        CoverageReporter, "collect_paths", lambda self, results: ["/x/coverage.dat"]
    )
    monkeypatch.setattr(
        "sys.argv",
        ["rb", "--machine", "-E", "comp", "test", "basic", "--coverage-merge"],
    )

    exit_code = RtlBuddy(name="test_coverage_merge_failure").run()
    captured = capsys.readouterr()

    assert exit_code == 1, captured
    envelope = json.loads(captured.out)
    assert envelope["exit_code"] == 1
    assert envelope["payload"]["coverage"]["merge_failed"] is True
    assert envelope["payload"]["coverage"]["failed_metrics"] == failed
    # The run's results survive the failure.
    assert [row["name"] for row in envelope["payload"]["results"]] == ["basic"]
