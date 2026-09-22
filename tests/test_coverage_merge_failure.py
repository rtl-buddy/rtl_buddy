"""Tests for #638 — a raw coverage merge that dies is reported as a failure.

`verilator_coverage --write` is the only source for toggle, expression and
functional coverage: an LCOV `.info` cannot represent them. When that
process is killed, the per-test LCOV exports still succeed, so line and
branch report normally and the metrics the merge carried used to print
`UNSP` — the same token an uninstrumented metric gets — while
`cov_dir/manifest.json` kept reporting the per-test toggle totals as
measured. The console and the manifest disagreed and neither said why, and
the command exited 0.

What these pin:

* the metrics the dead merge carried print `FAIL`, and a metric that was
  genuinely never instrumented still prints `UNSP`;
* `CoverageMetrics.to_dict()`, the run payload, the `artefacts` block and
  the manifest all carry `merge_failed` / `failed_metrics` explicitly, so
  no consumer has to infer the failure from `merged.raw == null`;
* `totals` is left intact — it is built from the per-test databases, which
  the merge never touched;
* the console summary says a merge failed, underneath the table;
* the run exits 1, after every result and artefact has been written.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.cov.manifest import MANIFEST_FILENAME
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import TestResults
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.vlog_cov import CoverageMetrics, VlogCov

_KILLED = -15  # SIGTERM, the return code the reporter observed


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
    """The slice of RootConfig the coverage reporter actually reads."""

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
    """Shim `verilator_coverage` so the merge can be made to die on demand.

    `--write-info` keeps working — that is the whole point: the per-test
    LCOV exports succeed and only the raw merge is lost.
    """
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
        # `strings` probing and `--annotate` summaries: nothing to report,
        # which is how a genuinely unsupported metric behaves.
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# --- the metrics object -----------------------------------------------------


def test_dead_merge_fails_only_the_metrics_it_alone_carried(monkeypatch, project):
    """`FAIL` for what the merge carried, `UNSP` for what was never there."""
    _shim_verilator_coverage(monkeypatch, project, merge_returncode=_KILLED)
    cov = VlogCov(simulator_name="verilator", use_lcov=True, root_cfg=_RootCfg(project))
    cov_dir = project / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)

    metrics = cov.merge(
        [str(project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat")],
        outdir=str(cov_dir),
    )

    # A failed merge always returns metrics: "the merge died" must not have
    # to be inferred from a None return.
    assert metrics is not None
    assert metrics.merged_path is None
    assert metrics.merge_failed is True
    # Line survives through the LCOV export. Branch is None because the
    # merged `.info` records no branch point — never instrumented, not lost.
    assert metrics.failed_metrics == ["toggle", "expression", "functional"]
    assert metrics.line == pytest.approx(0.5)
    assert metrics.summary_str() == "L:0.50 B:UNSP T:FAIL F:FAIL"

    payload = metrics.to_dict()
    assert payload["merge_failed"] is True
    assert payload["failed_metrics"] == ["toggle", "expression", "functional"]
    assert payload["merged_path"] is None


def test_healthy_merge_keeps_an_unmeasured_metric_unsupported(monkeypatch, project):
    """The same run with a merge that lives reports `UNSP`, not `FAIL`."""
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


def test_summary_cells_keep_their_width():
    """`FAIL` is four characters, like `UNSP`: the table does not reflow."""
    failed = CoverageMetrics(
        merge_failed=True, failed_metrics=["toggle", "functional"]
    ).summary_str()
    unsupported = CoverageMetrics().summary_str()
    assert failed == "L:UNSP B:UNSP T:FAIL F:FAIL"
    assert len(failed) == len(unsupported)


# --- the run payload, the manifest and the console --------------------------


def test_failed_merge_reaches_the_payload_manifest_and_console(monkeypatch, project):
    """One run, end to end: what the summary says the manifest now says too."""
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
    # The reader's half: one line under the table saying what failed.
    assert any(line.startswith("Coverage merge FAILED:") for line in metadata)
    assert any("toggle" in line for line in metadata if "FAILED" in line)

    assert coverage["merge_failed"] is True
    assert coverage["failed_metrics"] == ["toggle", "expression", "functional"]
    assert coverage["merged"]["toggle"] is None
    assert coverage["artefacts"]["merge_failed"] is True
    assert coverage["artefacts"]["merged_raw"] is None

    manifest = json.loads((suite / "cov_dir" / MANIFEST_FILENAME).read_text())
    # The discriminator the reporter had to infer downstream, still true...
    assert manifest["merge_mode"] == "raw"
    assert manifest["merged"]["raw"] is None
    # ...and the explicit fact, so nobody has to.
    assert manifest["merge_failed"] is True
    assert manifest["failed_metrics"] == ["toggle", "expression", "functional"]
    # `totals` is built from the per-test databases, which the merge never
    # touched: it stays a real measurement rather than being blanked.
    assert manifest["totals"]["toggle"]["found"] == 1


def test_healthy_merge_writes_the_keys_as_false_and_empty(monkeypatch, project):
    """The keys are always present: absent must never read as "fine"."""
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
    """`rb cov` reads the manifest, so it answers the question too."""
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


# --- the exit code ----------------------------------------------------------


def test_failed_merge_exits_one_with_every_result_still_in_the_envelope(
    minimal_project: Path, capsys, monkeypatch
):
    """#334 refused to exit 0 on a coverage request that produced nothing;
    a request that produced half gets the same answer — after the results
    and the artefacts have been written, so nothing already produced is
    lost."""
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
    # The whole point of not raising: the run's results survive the failure.
    assert [row["name"] for row in envelope["payload"]["results"]] == ["basic"]
