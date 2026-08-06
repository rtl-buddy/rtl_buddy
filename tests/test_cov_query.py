"""
Unit tests for the `rb cov` payload builders (#399).

These are the dicts the CLI prints under `--machine` and the MCP tools
(phase 3) wrap verbatim, so they are asserted on directly rather than
through the CLI.
"""

import pytest

from rtl_buddy.cov.manifest import build_manifest, write_manifest
from rtl_buddy.cov.model import TestArtefacts, build_model, write_model
from rtl_buddy.cov.query import (
    COV_QUERY_SCHEMA_VERSION,
    CovQueryError,
    load_context,
    module_names,
    module_payload,
    summary_payload,
)


def _dat_record(*, file, line, type_, name, module, col=1, hits=1):
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


@pytest.fixture
def project(tmp_path):
    """A project with one run's coverage artefacts already on disk."""
    root = tmp_path / "repo"
    suite = root / "verif" / "blk"
    run_dir = suite / "artefacts" / "basic"
    run_dir.mkdir(parents=True)
    for name in ("blk", "other"):
        source = root / "design" / f"{name}.sv"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"module {name};\nendmodule\n")

    raw = run_dir / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_record(
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
            file="../../../design/other.sv",
            line=9,
            type_="line",
            name="",
            module="other",
        )
        + _dat_record(
            file="../../../design/blk.sv",
            line=4,
            type_="user",
            name="BLK_WRITE",
            module="blk",
            hits=3,
        ),
        encoding="utf-8",
    )

    model = build_model(
        [
            TestArtefacts(
                name="basic",
                raw=str(raw),
                suite=str(suite / "tests.yaml"),
                source_roots=(str(run_dir), str(suite)),
            )
        ],
        project_root=root,
        simulator="verilator",
    )
    cov_dir = suite / "cov_dir"
    model_path = write_model(model, cov_dir)
    write_manifest(
        build_manifest(
            project_root=root,
            cov_dir=cov_dir,
            command="regression",
            suite=str(suite / "regression.yaml"),
            builder="verilator",
            simulator_family="verilator",
            merge_mode="raw",
            model_path=model_path,
            totals=model["totals"],
            merged={
                "info": str(cov_dir / "coverage_merged.info"),
                "html_dir": str(suite / "coverage_merge.html"),
            },
            tests=[{"name": "basic", "raw": str(raw)}],
        ),
        cov_dir,
    )
    return root


def test_summary_reports_totals_tests_and_artefact_paths(project):
    payload = summary_payload(load_context(project))

    assert payload["schema_version"] == COV_QUERY_SCHEMA_VERSION
    assert payload["command"] == "regression"
    assert payload["merge_mode"] == "raw"
    assert payload["totals"]["line"] == {"found": 3, "hit": 2, "ratio": 2 / 3}
    assert [row["name"] for row in payload["tests"]] == ["basic"]
    assert payload["artefacts"]["manifest"] == "verif/blk/cov_dir/manifest.json"
    assert payload["artefacts"]["merged_info"] == (
        "verif/blk/cov_dir/coverage_merged.info"
    )
    assert payload["artefacts"]["html_dir"] == "verif/blk/coverage_merge.html"
    assert payload["artefacts"]["model"] == "verif/blk/cov_dir/coverage-model.json"


def test_summary_lists_the_coldest_files_first(project):
    payload = summary_payload(load_context(project))

    assert [row["path"] for row in payload["files"]] == [
        "design/blk.sv",
        "design/other.sv",
    ]


def test_summary_limit_truncates_the_file_list(project):
    payload = summary_payload(load_context(project), limit=1)

    assert [row["path"] for row in payload["files"]] == ["design/blk.sv"]
    assert payload["counts"]["files"] == 2


def test_summary_carries_observed_cover_points(project):
    payload = summary_payload(load_context(project))

    assert payload["covers"] == [
        {
            "name": "BLK_WRITE",
            "file": "design/blk.sv",
            "line": 4,
            "module": "blk",
            "hits": 3,
        }
    ]


def test_module_payload_is_per_file_per_point(project):
    payload = module_payload(load_context(project), "blk")

    assert payload["module"] == "blk"
    assert [row["path"] for row in payload["files"]] == ["design/blk.sv"]
    (file_row,) = payload["files"]
    assert file_row["line"] == [
        {"line": 1, "hits": 1, "tests": {"basic": 1}},
        {"line": 2, "hits": 0, "tests": {"basic": 0}},
    ]
    assert file_row["toggle"][0]["name"] == "q[0]"
    assert payload["totals"]["line"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert payload["tests"] == ["basic"]


def test_module_name_matching_is_case_insensitive(project):
    assert module_payload(load_context(project), "BLK")["module"] == "blk"


def test_unknown_module_reports_near_misses(project):
    ctx = load_context(project)

    with pytest.raises(CovQueryError) as excinfo:
        module_payload(ctx, "blkk")

    assert "blk" in excinfo.value.candidates
    assert module_names(ctx) == ["blk", "other"]


def test_missing_manifest_names_the_command_that_writes_one(tmp_path):
    with pytest.raises(CovQueryError) as excinfo:
        load_context(tmp_path)

    assert "cov_dir/manifest.json" in str(excinfo.value)


def test_explicit_cov_dir_overrides_discovery(project):
    ctx = load_context(project, cov_dir=project / "verif" / "blk" / "cov_dir")

    assert ctx.manifest["command"] == "regression"

    with pytest.raises(CovQueryError):
        load_context(project, cov_dir=project / "verif")
