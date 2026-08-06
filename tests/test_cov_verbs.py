"""Tests for #399 — the `rb cov` verbs and the artefacts they read.

What these pin:

* a run writes `cov_dir/manifest.json` and the coverage model **whenever it
  produced coverage at all** — no merge flag, no Coverview packaging, and in
  particular per-test attribution is not conditional on either;
* the run envelope's `payload.coverage.artefacts` names paths, not the display
  lines (`Merged LCOV: <path>`) it used to be the only record of;
* `rb cov summary` / `rb cov module` answer from those artefacts alone, and
  their `--machine` payloads are exactly the dicts the payload builders return
  (phase 3 wraps them verbatim);
* an unknown module fails loudly with near misses rather than answering about
  a different one;
* the per-run `result.json` side-car is re-persisted after coverage
  post-processing, so the durable record names its own artefacts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rtl_buddy.cov.manifest import MANIFEST_FILENAME
from rtl_buddy.cov.model import MODEL_FILENAME
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import TestResults
from rtl_buddy.tools.coverage import CoverageReporter

_FIXTURES = Path(__file__).parent / "fixtures"


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


def _write_raw(path: Path, records: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# SystemC::Coverage-3\n" + records, encoding="utf-8")
    return path


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
    """A project root with two tests' raw coverage databases on disk."""
    root = tmp_path / "repo"
    (root / "design").mkdir(parents=True)
    for name in ("blk", "other"):
        (root / "design" / f"{name}.sv").write_text(
            f"module {name};\nendmodule\n", encoding="utf-8"
        )

    suite = root / "verif" / "blk"
    _write_raw(
        suite / "artefacts" / "basic" / "coverage.dat",
        _dat_record(
            file="../../../design/blk.sv", line=1, type_="line", name="", module="blk"
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=2,
            type_="line",
            name="",
            module="blk",
            hits=0,
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=2,
            type_="toggle",
            name="q[0]",
            module="blk",
            hits=0,
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=3,
            type_="expr",
            name="a && b",
            module="blk",
            hits=4,
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=4,
            type_="user",
            name="BLK_WRITE",
            module="blk",
            hits=3,
        ),
    )
    _write_raw(
        suite / "artefacts" / "extra" / "coverage.dat",
        _dat_record(
            file="../../../design/blk.sv",
            line=2,
            type_="line",
            name="",
            module="blk",
            hits=7,
        )
        + _dat_record(
            file="../../../design/other.sv",
            line=9,
            type_="line",
            name="",
            module="other",
        ),
    )
    return root


def _suite_results(project: Path):
    suite = project / "verif" / "blk"
    return [
        {
            "test_name": name,
            "results": TestResults(
                name=name,
                results={
                    "result": "PASS",
                    "desc": "ok",
                    "coverage": {
                        "raw_paths": [str(suite / "artefacts" / name / "coverage.dat")],
                    },
                },
            ),
        }
        for name in ("basic", "extra")
    ]


def _build_artefacts(project: Path, **kwargs):
    reporter = CoverageReporter(_RootCfg(project))
    suite_results = _suite_results(project)
    metadata, coverage = reporter.build_metadata(
        suite_results,
        outdir=str(project / "verif" / "blk"),
        suite_name=str(project / "verif" / "blk" / "tests.yaml"),
        **kwargs,
    )
    return suite_results, metadata, coverage


# --- artefact emission ------------------------------------------------------


def test_manifest_and_model_are_written_without_any_merge_flag(project):
    """The discovery contract does not depend on how (or whether) we merged."""
    _, _, coverage = _build_artefacts(project)

    cov_dir = project / "verif" / "blk" / "cov_dir"
    assert (cov_dir / MANIFEST_FILENAME).is_file()
    assert (cov_dir / MODEL_FILENAME).is_file()
    assert coverage["artefacts"]["manifest"] == "verif/blk/cov_dir/manifest.json"
    assert coverage["artefacts"]["model"] == "verif/blk/cov_dir/coverage-model.json"
    # No merge ran, so there is nothing merged to name — null, not absent.
    assert coverage["artefacts"]["merged_info"] is None


def test_attribution_is_unconditional(project):
    """Every point carries the tests behind it, with no Coverview packaging."""
    _build_artefacts(project)

    model = json.loads(
        (project / "verif" / "blk" / "cov_dir" / MODEL_FILENAME).read_text()
    )
    blk = next(row for row in model["files"] if row["path"] == "design/blk.sv")
    line2 = next(point for point in blk["line"] if point["line"] == 2)
    # Cold in `basic`, hit in `extra` — that is the question attribution is
    # for, and it used to require packaging an archive to answer.
    assert line2["tests"] == {"basic": 0, "extra": 7}
    assert [row["name"] for row in model["tests"]] == ["basic", "extra"]


def test_toggle_and_expression_detail_survive_per_signal(project):
    """The LCOV export folds both into anonymous records; the model does not."""
    _build_artefacts(project)

    model = json.loads(
        (project / "verif" / "blk" / "cov_dir" / MODEL_FILENAME).read_text()
    )
    blk = next(row for row in model["files"] if row["path"] == "design/blk.sv")
    assert [point["name"] for point in blk["toggle"]] == ["q[0]"]
    assert [point["name"] for point in blk["expression"]] == ["a && b"]
    assert blk["totals"]["expression"] == {"found": 1, "hit": 1, "ratio": 1.0}


def test_manifest_is_not_written_when_nothing_parsed(tmp_path):
    """A named database that yields no point must not advertise coverage."""
    reporter = CoverageReporter(_RootCfg(tmp_path))
    suite_results = [
        {
            "test_name": "ghost",
            "results": TestResults(
                name="ghost",
                results={
                    "result": "PASS",
                    "desc": "ok",
                    "coverage": {"raw_paths": [str(tmp_path / "gone.dat")]},
                },
            ),
        }
    ]

    _, coverage = reporter.build_metadata(
        suite_results, outdir=str(tmp_path), suite_name="suite"
    )

    assert "artefacts" not in coverage
    assert not (tmp_path / "cov_dir" / MANIFEST_FILENAME).exists()


def test_result_side_cars_are_refreshed_after_coverage(project):
    """The durable per-run record names the artefacts, not just the console."""
    rb = RtlBuddy(name="test_cov_verbs")
    suite_results = _suite_results(project)
    envelopes = []
    for suite_result in suite_results:
        path = (
            project
            / "verif"
            / "blk"
            / "artefacts"
            / suite_result["test_name"]
            / "result.json"
        )
        write_result_json(
            path,
            test_name=suite_result["test_name"],
            run_id=None,
            results=suite_result["results"],
            run_token="tok0",
        )
        suite_result["results"].result_json_path = str(path)
        envelopes.append(path)

    # What coverage post-processing does: mutate the per-test dict in place
    # once the export exists, long after the envelope was written.
    suite_results[0]["results"].results["coverage"]["lcov_path"] = "cov_dir/a.info"
    rb._refresh_result_side_cars(suite_results)

    envelope = json.loads(envelopes[0].read_text())
    assert envelope["run_token"] == "tok0"
    assert envelope["result"]["results"]["coverage"]["lcov_path"] == "cov_dir/a.info"


# --- the CLI verbs ----------------------------------------------------------


_LIVE: list[RtlBuddy] = []


def _runner() -> tuple[CliRunner, RtlBuddy]:
    """A fresh CLI object, with the previous one's artefact lock released."""
    while _LIVE:
        _LIVE.pop()._artifact_locks.release_all()
    rb = RtlBuddy(name="test_cov_verbs")
    _LIVE.append(rb)
    return CliRunner(), rb


@pytest.fixture
def cov_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with coverage artefacts, as a runnable project root."""
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", project)
    _build_artefacts(project)
    monkeypatch.chdir(project)
    return project


def _machine(result) -> dict:
    assert result.exit_code in (0, 1, 2), result.output
    return json.loads(result.output.strip().splitlines()[-1])


def test_cov_summary_reads_the_newest_manifest(cov_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "cov", "summary"])

    envelope = _machine(result)
    assert envelope["command"] == "cov summary"
    assert envelope["exit_code"] == 0
    payload = envelope["payload"]
    assert payload["counts"] == {"files": 2, "tests": 2, "modules": 2}
    # Three distinct lines across both files; `blk.sv:2` is cold in `basic`
    # and hit in `extra`, so the run total counts it covered.
    assert payload["totals"]["line"] == {"found": 3, "hit": 3, "ratio": 1.0}
    assert payload["totals"]["toggle"] == {"found": 1, "hit": 0, "ratio": 0.0}
    assert [row["name"] for row in payload["tests"]] == ["basic", "extra"]
    assert payload["artefacts"]["manifest"] == "verif/blk/cov_dir/manifest.json"
    assert payload["covers"][0]["name"] == "BLK_WRITE"


def test_cov_summary_renders_without_machine_mode(cov_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["cov", "summary"])

    assert result.exit_code == 0, result.output
    assert "verif/blk/cov_dir/manifest.json" in result.output


def test_cov_module_reports_points_and_their_tests(cov_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "cov", "module", "blk"])

    payload = _machine(result)["payload"]
    assert payload["module"] == "blk"
    assert [row["path"] for row in payload["files"]] == ["design/blk.sv"]
    (file_row,) = payload["files"]
    assert file_row["line"] == [
        {"line": 1, "hits": 1, "tests": {"basic": 1}},
        {"line": 2, "hits": 7, "tests": {"basic": 0, "extra": 7}},
    ]
    assert payload["tests"] == ["basic", "extra"]


def test_cov_module_unknown_name_exits_two_with_candidates(cov_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "cov", "module", "blkk"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    assert "blk" in envelope["payload"]["candidates"]


def test_cov_verbs_fail_loudly_with_no_artefacts(tmp_path, monkeypatch):
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", tmp_path)
    monkeypatch.chdir(tmp_path)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "cov", "summary"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    # The error names the command that produces the missing artefacts.
    assert "cov_dir/manifest.json" in envelope["payload"]["error"]


def test_explicit_cov_dir_beats_discovery(cov_project):
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["--machine", "cov", "summary", "--cov-dir", "verif/blk/cov_dir"],
    )

    assert _machine(result)["payload"]["counts"]["tests"] == 2
