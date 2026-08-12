from types import SimpleNamespace

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


def _suite_results():
    return [
        {
            "test_name": "basic",
            "results": SimpleNamespace(results={}),
        }
    ]


# Records + the lines their real `_dir_summary_lines` formatter would produce
# for them — used so tests can assert on the *lines that reach metadata*
# without re-implementing `_dir_summary_lines`.
_FAKE_RECORDS = [
    {
        "prefix": "design/x",
        "line": 1.0,
        "branch": 1.0,
        "toggle": None,
        "functional": None,
    },
    {
        "prefix": "verif/x",
        "line": 0.5,
        "branch": 0.5,
        "toggle": None,
        "functional": None,
    },
]


def test_build_metadata_coverview_missing_falls_back_to_lcov_summary(
    monkeypatch, tmp_path
):
    """Regression for #403: when --coverage-coverview is set but the
    Coverview tool is unavailable, merge_info_process() returns None. The
    LCOV-based directory summary must be used instead of silently vanishing.
    This assertion fails on unfixed code (dir_summary output disappears).
    """
    reporter = CoverageReporter(DummyRootCfg(tmp_path))

    fake_lcov_path = str(tmp_path / "merged.info")
    monkeypatch.setattr(
        reporter,
        "merge",
        lambda *args, **kwargs: CoverageMetrics(lcov_path=fake_lcov_path),
    )
    monkeypatch.setattr(reporter, "merge_info_process", lambda *args, **kwargs: None)

    captured = {}

    def _fake_dir_summary_records(lcov_path, dir_summary_paths):
        captured["lcov_path"] = lcov_path
        captured["dir_summary_paths"] = dir_summary_paths
        return _FAKE_RECORDS

    monkeypatch.setattr(reporter, "_dir_summary_records", _fake_dir_summary_records)

    metadata, coverage = reporter.build_metadata(
        _suite_results(),
        outdir=str(tmp_path),
        suite_name="verif/sandbox/tests.yaml",
        coverage_merge=True,
        coverage_coverview=True,
        dir_summary_paths=["design/x", "verif/x"],
    )

    assert any("design/x" in line for line in metadata)
    assert any("verif/x" in line for line in metadata)
    assert captured["lcov_path"] == fake_lcov_path
    assert captured["dir_summary_paths"] == ["design/x", "verif/x"]
    assert coverage["dir_summary"] == _FAKE_RECORDS


def test_build_metadata_coverview_present_uses_dataset_files_summary(
    monkeypatch, tmp_path
):
    """Coverview-present flow is unchanged by the fallback: dataset-files
    based summary is used and the LCOV-based summary is never invoked.
    """
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
    # merge_info_process returns (metrics, coverview_zip, dataset_files,
    # description_files) — four values, as its docstring says. The caller
    # unpacks all four and reads `description_files["line"]`.
    description_files = {"line": str(tmp_path / "line.desc.json")}
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

    lcov_calls = []

    def _fake_dir_summary_records(*args, **kwargs):
        lcov_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(reporter, "_dir_summary_records", _fake_dir_summary_records)

    dataset_calls = {}

    def _fake_dataset_records(dataset_files_arg, dir_summary_paths):
        dataset_calls["dataset_files"] = dataset_files_arg
        dataset_calls["dir_summary_paths"] = dir_summary_paths
        return _FAKE_RECORDS

    monkeypatch.setattr(
        reporter, "_dir_summary_records_from_dataset_files", _fake_dataset_records
    )

    artefact_calls = {}

    def _fake_write_artefacts(suite_results, **kwargs):
        artefact_calls.update(kwargs)
        return None

    monkeypatch.setattr(reporter, "write_artefacts", _fake_write_artefacts)

    metadata, coverage = reporter.build_metadata(
        _suite_results(),
        outdir=str(tmp_path),
        suite_name="verif/sandbox/tests.yaml",
        coverage_merge=True,
        coverage_coverview=True,
        dir_summary_paths=["design/x", "verif/x"],
    )

    assert any("design/x" in line for line in metadata)
    assert any("verif/x" in line for line in metadata)
    assert "Merged Coverview: cv.zip" in metadata
    assert dataset_calls["dataset_files"] == dataset_files
    assert dataset_calls["dir_summary_paths"] == ["design/x", "verif/x"]
    # The LCOV-based summary must not run at all in the coverview flow.
    assert lcov_calls == []
    assert coverage["dir_summary"] == _FAKE_RECORDS
    # The fourth return value is not decoration: the line description file
    # is what the manifest records as the merge's `desc`.
    assert artefact_calls["merged"]["desc"] == description_files["line"]
    assert artefact_calls["descriptions"] == description_files


def test_build_metadata_no_coverview_uses_lcov_summary(monkeypatch, tmp_path):
    """coverage_coverview=False path is unchanged: LCOV-based summary runs."""
    reporter = CoverageReporter(DummyRootCfg(tmp_path))

    fake_lcov_path = str(tmp_path / "merged.info")
    monkeypatch.setattr(
        reporter,
        "merge",
        lambda *args, **kwargs: CoverageMetrics(lcov_path=fake_lcov_path),
    )

    captured = {}

    def _fake_dir_summary_records(lcov_path, dir_summary_paths):
        captured["lcov_path"] = lcov_path
        captured["dir_summary_paths"] = dir_summary_paths
        return _FAKE_RECORDS

    monkeypatch.setattr(reporter, "_dir_summary_records", _fake_dir_summary_records)

    metadata, coverage = reporter.build_metadata(
        _suite_results(),
        outdir=str(tmp_path),
        suite_name="verif/sandbox/tests.yaml",
        coverage_merge=True,
        coverage_coverview=False,
        dir_summary_paths=["design/x", "verif/x"],
    )

    assert any("design/x" in line for line in metadata)
    assert any("verif/x" in line for line in metadata)
    assert captured["lcov_path"] == fake_lcov_path
    assert captured["dir_summary_paths"] == ["design/x", "verif/x"]
    assert coverage["dir_summary"] == _FAKE_RECORDS
