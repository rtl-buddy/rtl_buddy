"""`--coverage-dir-summary` records come from the coverage model's source points, with or without a merge."""

import json
from types import SimpleNamespace

import pytest

from rtl_buddy.cov.model import MODEL_FILENAME
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.vlog_cov import CoverageMetrics


class DummyRootCfg:
    def __init__(self, project_root):
        self._project_root = str(project_root)

    def get_project_rootdir(self):
        return self._project_root

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: "verilator",
            get_name=lambda: "verilator",
        )

    def get_use_lcov(self, _simulator_name):
        return True

    def get_coverview_cfg(self, _simulator_name):
        return None


def _dat_record(*, file, line, type_, name, module, hits):
    keys = [
        ("f", file),
        ("l", str(line)),
        ("n", "1"),
        ("t", type_),
        ("page", f"v_{type_}/{module}"),
        ("o", name),
        ("h", f"tb_top.{module}.{name}"),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {hits}\n"


BLK = "../../../design/blk/blk.sv"
IFC = "../../../design/ifc/bus_if.sv"

# A block with line, branch and port-toggle points, line 5 elaborated twice; and an
# interface with toggle points only.
_RECORDS = [
    (BLK, 2, "toggle", "a[0]", "blk", 0),
    (BLK, 2, "toggle", "a[1]", "blk", 4),
    (BLK, 5, "line", "", "blk", 3),
    (BLK, 5, "line", "", "blk_wide", 0),
    (BLK, 6, "line", "", "blk", 2),
    (BLK, 6, "branch", "if", "blk", 2),
    (BLK, 6, "branch", "else", "blk", 0),
    (BLK, 7, "line", "", "blk", 0),
    (IFC, 3, "toggle", "valid", "bus_if", 5),
    (IFC, 4, "toggle", "ready", "bus_if", 0),
]

# What `verilator_coverage --write-info` makes of the same points: a `DA` per touched
# line, toggle lines included, and a `BRDA` per point on a line with several.
_LCOV = """SF:{root}/design/blk/blk.sv
DA:2,4
DA:5,3
DA:6,2
DA:7,0
BRDA:2,0,0,0
BRDA:2,0,1,4
BRDA:6,0,0,2
BRDA:6,0,1,0
end_of_record
SF:{root}/design/ifc/bus_if.sv
DA:3,5
DA:4,0
end_of_record
"""


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    for rel in ("design/blk/blk.sv", "design/ifc/bus_if.sv"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("// source\n")
    test_dir = root / "verif" / "blk" / "artefacts" / "basic"
    test_dir.mkdir(parents=True)
    (test_dir / "coverage.dat").write_text(
        "# SystemC::Coverage-3\n"
        + "".join(
            _dat_record(file=f, line=ln, type_=t, name=n, module=m, hits=h)
            for f, ln, t, n, m, h in _RECORDS
        ),
        encoding="utf-8",
    )
    (root / "merged.info").write_text(_LCOV.format(root=root), encoding="utf-8")
    return root


def _suite_results(project):
    raw = project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat"
    return [
        {
            "test_name": "basic",
            "results": SimpleNamespace(results={"coverage": {"raw_paths": [str(raw)]}}),
        }
    ]


def _build(project, monkeypatch, **flags):
    reporter = CoverageReporter(DummyRootCfg(project))
    # The merge's LCOV disagrees with the model; the summary must not read it.
    monkeypatch.setattr(
        reporter,
        "merge",
        lambda *args, **kwargs: CoverageMetrics(lcov_path=str(project / "merged.info")),
    )
    suite = project / "verif" / "blk"
    return reporter.build_metadata(
        _suite_results(project),
        outdir=str(suite),
        suite_name=str(suite / "tests.yaml"),
        dir_summary_paths=["design/blk", "design/ifc", "design"],
        **flags,
    )


def _counts(record, metric):
    entry = record["source_totals"][metric]
    return entry["found"], entry["hit"]


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({"coverage_merge": True}, id="merge"),
        pytest.param({}, id="no-merge"),
        pytest.param({"model_mode": "none"}, id="model-not-written"),
    ],
)
def test_dir_summary_counts_the_model_source_points(project, monkeypatch, flags):
    metadata, coverage = _build(project, monkeypatch, **flags)

    blk, ifc, design = coverage["dir_summary"]
    # Port toggles are not line points; line 5's two elaborations are one source point.
    assert blk["prefix"] == "design/blk"
    assert _counts(blk, "line") == (3, 2)
    assert _counts(blk, "branch") == (2, 1)
    assert _counts(blk, "toggle") == (2, 1)
    assert (blk["line"], blk["branch"], blk["toggle"], blk["functional"]) == (
        2 / 3,
        0.5,
        0.5,
        None,
    )
    # An interface with toggle points only has no line or branch figure.
    assert _counts(ifc, "line") == (0, 0)
    assert (ifc["line"], ifc["branch"], ifc["toggle"]) == (None, None, 0.5)
    assert _counts(design, "line") == (3, 2)
    assert _counts(design, "toggle") == (4, 2)
    assert "Coverage design/blk: L:0.67 B:0.50 T:0.50 F:UNSP" in metadata
    assert "Coverage design/ifc: L:UNSP B:UNSP T:0.50 F:UNSP" in metadata


def test_dir_summary_agrees_with_the_written_model(project, monkeypatch):
    _, coverage = _build(project, monkeypatch, coverage_merge=True)

    model = json.loads(
        (project / "verif" / "blk" / "cov_dir" / MODEL_FILENAME).read_text()
    )
    blk_row = next(r for r in model["files"] if r["path"] == "design/blk/blk.sv")
    assert coverage["dir_summary"][0]["source_totals"] == blk_row["source_totals"]
    assert coverage["dir_summary"][2]["source_totals"] == model["source_totals"]


def test_dir_summary_without_a_model_says_so(project, monkeypatch):
    raw = project / "verif" / "blk" / "artefacts" / "basic" / "coverage.dat"
    raw.unlink()

    metadata, coverage = _build(project, monkeypatch)

    assert coverage["dir_summary"] == []
    assert "Coverage directory summary: unavailable (no coverage model)" in metadata


def test_build_metadata_coverview_records_the_merge_descriptions(monkeypatch, tmp_path):
    """With Coverview present, the info-process merge's archive and description files reach the summary and the manifest."""
    reporter = CoverageReporter(DummyRootCfg(tmp_path))
    fake_lcov_path = str(tmp_path / "merged.info")
    monkeypatch.setattr(
        reporter,
        "merge",
        lambda *args, **kwargs: CoverageMetrics(lcov_path=fake_lcov_path),
    )
    dataset_files = {
        "line": str(tmp_path / "line.info"),
        "branch": str(tmp_path / "branch.info"),
        "toggle": None,
        "expression": None,
    }
    description_files = {
        "line": str(tmp_path / "line.desc.json"),
        "branch": None,
        "expression": None,
        "toggle": None,
    }
    monkeypatch.setattr(
        reporter,
        "merge_info_process",
        lambda *args, **kwargs: (
            CoverageMetrics(lcov_path=fake_lcov_path),
            "cv.zip",
            dataset_files,
            description_files,
        ),
    )
    artefact_calls = {}

    def _fake_write_artefacts(suite_results, **kwargs):
        artefact_calls.update(kwargs)
        return None

    monkeypatch.setattr(reporter, "write_artefacts", _fake_write_artefacts)

    metadata, _ = reporter.build_metadata(
        [{"test_name": "basic", "results": SimpleNamespace(results={})}],
        outdir=str(tmp_path),
        suite_name="verif/sandbox/tests.yaml",
        coverage_merge=True,
        coverage_coverview=True,
    )

    assert "Merged Coverview: cv.zip" in metadata
    assert artefact_calls["datasets"] == dataset_files
    # The line description file is what the manifest records as the merge's `desc`.
    assert artefact_calls["merged"]["desc"] == description_files["line"]
    assert artefact_calls["descriptions"] == description_files
