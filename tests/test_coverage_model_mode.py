"""Tests for #660 — `--coverage-model` chooses how much of the model to write.

The per-point `tests` map is the points x tests term that makes the model
the biggest and slowest thing a large coverage run produces, and a CI job
that only records the suite figure never reads it. What these pin:

* `totals` builds the model without per-point attribution and changes no
  figure: totals, per-test rows and source totals match `full`;
* `none` writes no model, removes a stale one, and still writes the
  manifest with its totals, which headless consumers read on their own;
* the manifest records the choice, and `rb cov` names the flag when there
  is no model to read;
* `test` and `regression` pass the flag to the reporter.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rtl_buddy.cov import query as query_mod
from rtl_buddy.cov.manifest import MANIFEST_FILENAME
from rtl_buddy.cov.model import MODEL_FILENAME, load_model
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.coverage import CoverageReporter


def _dat_record(*, file, line, type_, name, module="blk", col=1, hits=1):
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
    def __init__(self, root):
        self._root = str(root)

    def get_project_rootdir(self):
        return self._root

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: "verilator",
            get_name=lambda: "verilator",
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    suite = root / "verif" / "blk"
    src = root / "design" / "blk.sv"
    src.parent.mkdir(parents=True)
    src.write_text("module blk;\n  logic q;\nendmodule\n")
    for name, hits in (("basic", (1, 0)), ("random", (0, 3))):
        test_dir = suite / "artefacts" / name
        test_dir.mkdir(parents=True)
        (test_dir / "coverage.dat").write_text(
            "# SystemC::Coverage-3\n"
            + _dat_record(
                file="../../../design/blk.sv",
                line=1,
                type_="line",
                name="",
                hits=hits[0],
            )
            + _dat_record(
                file="../../../design/blk.sv",
                line=2,
                type_="toggle",
                name="q",
                hits=hits[1],
            ),
            encoding="utf-8",
        )
    return root


def _suite_results(project):
    suite = project / "verif" / "blk"
    return [
        {
            "test_name": name,
            "results": SimpleNamespace(
                results={
                    "coverage": {
                        "raw_paths": [str(suite / "artefacts" / name / "coverage.dat")]
                    }
                }
            ),
        }
        for name in ("basic", "random")
    ]


def _write(project, model_mode):
    suite = project / "verif" / "blk"
    artefacts = CoverageReporter(_RootCfg(project)).write_artefacts(
        _suite_results(project),
        outdir=str(suite),
        suite_name=str(suite / "tests.yaml"),
        command="regression",
        model_mode=model_mode,
    )
    cov_dir = suite / "cov_dir"
    manifest = json.loads((cov_dir / MANIFEST_FILENAME).read_text())
    return artefacts, manifest, cov_dir


def _points(model):
    return [
        point
        for row in model["files"]
        for metric in ("line", "toggle")
        for point in row[metric]
    ]


def test_full_is_the_default_and_keeps_attribution(project):
    _artefacts, manifest, cov_dir = _write(project, "full")
    model = load_model(cov_dir / MODEL_FILENAME)

    assert manifest["coverage_model"] == "full"
    assert manifest["model"] is not None
    assert model["attribution"] is True
    assert all("tests" in point for point in _points(model))


def test_totals_drops_only_the_per_point_attribution(project):
    _, _, cov_dir = _write(project, "full")
    full = load_model(cov_dir / MODEL_FILENAME)
    _artefacts, manifest, cov_dir = _write(project, "totals")
    totals = load_model(cov_dir / MODEL_FILENAME)

    assert manifest["coverage_model"] == "totals"
    assert manifest["model"] is not None
    assert totals["attribution"] is False
    assert not any("tests" in point for point in _points(totals))
    # Every figure is the same numbers, per run, per test and per file.
    assert totals["totals"] == full["totals"]
    assert totals["source_totals"] == full["source_totals"]
    assert totals["tests"] == full["tests"]
    assert [p["hits"] for p in _points(totals)] == [p["hits"] for p in _points(full)]
    assert [row["totals"] for row in totals["files"]] == [
        row["totals"] for row in full["files"]
    ]


def test_none_writes_the_manifest_and_its_totals_without_a_model(project):
    _, full_manifest, cov_dir = _write(project, "full")
    assert (cov_dir / MODEL_FILENAME).exists()

    artefacts, manifest, cov_dir = _write(project, "none")

    # The earlier run's model is gone, so the directory holds nothing the
    # manifest does not describe.
    assert not (cov_dir / MODEL_FILENAME).exists()
    assert manifest["coverage_model"] == "none"
    assert manifest["model"] is None
    assert artefacts["model"] is None
    assert manifest["totals"] == full_manifest["totals"]
    assert manifest["source_totals"] == full_manifest["source_totals"]
    assert [row["name"] for row in manifest["tests"]] == ["basic", "random"]


def test_rb_cov_names_the_flag_when_the_run_skipped_the_model(project):
    _, _, cov_dir = _write(project, "none")

    with pytest.raises(query_mod.CovQueryError, match="--coverage-model none"):
        query_mod.load_context(str(project), manifest=str(cov_dir / MANIFEST_FILENAME))


def test_rb_cov_reads_a_totals_model(project):
    _, _, cov_dir = _write(project, "totals")

    ctx = query_mod.load_context(
        str(project), manifest=str(cov_dir / MANIFEST_FILENAME)
    )
    payload = query_mod.module_payload(ctx, "blk")
    assert payload["tests"] == []
    assert payload["totals"]["toggle"] == {"found": 1, "hit": 1, "ratio": 1.0}


def _spy_on_build_metadata(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        CoverageReporter, "collect_paths", lambda self, results: ["stub.dat"]
    )

    def spy(self, suite_results, **kwargs):
        captured.update(kwargs)
        return [], {"merged": None, "dir_summary": []}

    monkeypatch.setattr(CoverageReporter, "build_metadata", spy)
    return captured


@pytest.mark.parametrize(
    "argv",
    [
        ["-E", "comp", "test", "basic"],
        ["-M", "debug", "-E", "comp", "regression"],
    ],
)
def test_the_commands_pass_the_flag_to_the_reporter(
    minimal_project: Path, monkeypatch, argv
):
    captured = _spy_on_build_metadata(monkeypatch)
    runner, rb = CliRunner(), RtlBuddy(name="test_coverage_model_mode")

    result = runner.invoke(rb.app, argv + ["--coverage-model", "none"])
    assert result.exit_code == 0, result.output
    assert captured["model_mode"] == "none"

    result = runner.invoke(rb.app, argv)
    assert result.exit_code == 0, result.output
    assert captured["model_mode"] == "full"


def test_an_unknown_mode_is_rejected(minimal_project: Path):
    runner, rb = CliRunner(), RtlBuddy(name="test_coverage_model_mode")

    result = runner.invoke(
        rb.app, ["-E", "comp", "test", "basic", "--coverage-model", "some"]
    )
    assert result.exit_code != 0
    assert "'some' is not one of 'full', 'totals', 'none'" in str(result.exception)
