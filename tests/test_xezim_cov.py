"""Tests for xezim code coverage: the `coverage.json` reader, per-test collection and merge, the model built from it, and the family wiring in `VlogSim`.

`tests/fixtures/xezim_cov/coverage.json` was captured from xezim 0.11.3 on the
icarus_smoke counter plus a labelled and an unlabelled `cover property`; its source
paths are relative to that directory, as a filelist would give them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.config.coverage import CoverageConfigFile
from rtl_buddy.cov.model import TestArtefacts, build_model
from rtl_buddy.cov.raw import BRANCH, COVER, EXPRESSION, LINE, TOGGLE
from rtl_buddy.cov.xezim_raw import parse_xezim_records
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.graph.results import _artefacts
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import TestResults as _TestResults
from rtl_buddy.tools import vlog_sim as vlog_sim_module
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.coverview import CoverviewPacker
from rtl_buddy.tools.vlog_cov import VlogCov, coverage_collector_for
from rtl_buddy.tools.xezim_cov import XezimCov, merge_xezim_docs
from serde.yaml import from_yaml

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "xezim_cov"


class _RootCfg:
    """The slice of RootConfig the coverage tools read."""

    def __init__(self, root, *, use_lcov=False, timeout=None, family="xezim"):
        self._root = str(root)
        self._use_lcov = use_lcov
        self._timeout = timeout
        self._family = family

    def get_project_rootdir(self):
        return self._root

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: self._family,
            get_name=lambda: self._family,
        )

    def get_use_lcov(self, _family):
        return self._use_lcov

    def get_coverage_merge_timeout(self, _family):
        return self._timeout

    def get_coverview_cfg(self, _family):
        return None


@pytest.fixture
def project(tmp_path: Path):
    """A project whose suite holds the fixture sources and one test's `coverage.json`."""
    root = tmp_path / "repo"
    suite = root / "verif" / "cnt"
    raw_dir = suite / "artefacts" / "basic"
    raw_dir.mkdir(parents=True)
    design = root / "design"
    design.mkdir()
    shutil.copy(FIXTURE_DIR / "counter.sv", design / "counter.sv")
    shutil.copy(FIXTURE_DIR / "tb_counter.sv", suite / "tb_counter.sv")
    # Rewrite the fixture's paths as xezim reports them from the artefact dir.
    doc = json.loads((FIXTURE_DIR / "coverage.json").read_text())
    text = json.dumps(doc)
    text = text.replace('"counter.sv"', '"../../../../design/counter.sv"')
    text = text.replace('"tb_counter.sv"', '"../../tb_counter.sv"')
    raw = raw_dir / "coverage.json"
    raw.write_text(text)
    return SimpleNamespace(root=root, suite=suite, raw=raw)


def _records(path=FIXTURE_DIR / "coverage.json"):
    records = parse_xezim_records(path)
    assert records is not None
    return records


# --- reader --------------------------------------------------------------------


def test_reader_emits_one_line_point_per_statement_with_ordinal_columns():
    lines = [r for r in _records() if r["metric"] == LINE]
    assert len(lines) == 14  # xezim's own statement total
    on_12 = [r for r in lines if r["file"] == "tb_counter.sv" and r["line"] == 12]
    assert [(r["column"], r["name"]) for r in on_12] == [
        (0, "always"),
        (1, "statement"),
    ]
    assert {r["module"] for r in lines} == {"counter", "tb_counter"}
    assert all(r["type"] == "line" and r["hier"] is None for r in lines)


def test_reader_emits_one_branch_point_per_arm_on_the_arms_line():
    arms = [r for r in _records() if r["metric"] == BRANCH]
    assert [(r["line"], r["column"], r["name"], r["hits"]) for r in arms] == [
        (14, 0, "if", 2),
        (15, 0, "else if", 10),
        (14, 1, "else (implicit)", 2),
    ]
    assert {r["module"] for r in arms} == {"counter"}


def test_reader_folds_toggle_bits_to_min_rise_fall():
    toggles = {(r["module"], r["name"]): r for r in _records() if r["metric"] == TOGGLE}
    # counter.count: rise [5,3,1,1], fall [5,2,1,0].
    assert [toggles[("counter", f"count[{i}]")]["hits"] for i in range(4)] == [
        5,
        2,
        1,
        0,
    ]
    # rst_n rose once and never fell: not hit.
    assert toggles[("tb_counter", "rst_n[0]")]["hits"] == 0
    point = toggles[("counter", "count[0]")]
    assert (point["file"], point["line"], point["column"]) == ("counter.sv", 0, None)
    assert len(toggles) == 11


def test_reader_drops_toggles_of_a_unit_without_statements(tmp_path: Path):
    doc = {
        "assertions": [],
        "code_coverage": {
            "design_units": [
                {
                    "design_unit": "empty",
                    "statements": [],
                    "branches": [],
                    "toggles": [
                        {"signal": "x", "width": 2, "rise": [1, 1], "fall": [1, 1]}
                    ],
                }
            ]
        },
    }
    raw = tmp_path / "coverage.json"
    raw.write_text(json.dumps(doc))
    assert parse_xezim_records(raw) == []


def test_reader_recovers_cover_labels_and_falls_back_to_the_line():
    covers = [r for r in _records() if r["metric"] == COVER]
    assert [(r["name"], r["line"], r["hits"], r["module"]) for r in covers] == [
        ("c_wrap", 14, 0, "tb_counter"),
        ("cover@15", 15, 1, "tb_counter"),
    ]


def test_reader_cover_label_falls_back_when_the_source_is_unreadable(tmp_path):
    raw = tmp_path / "coverage.json"
    raw.write_text(
        json.dumps(
            {
                "assertions": [
                    {"file": "gone.sv", "line": 3, "kind": "cover", "pass": 2},
                    {"file": "gone.sv", "line": 4, "kind": "assert", "pass": 9},
                ]
            }
        )
    )
    records = parse_xezim_records(raw)
    assert [(r["metric"], r["name"], r["hits"], r["module"]) for r in records] == [
        (COVER, "cover@3", 2, None)
    ]


def test_reader_prefers_the_files_name_over_the_source_label(tmp_path):
    src = tmp_path / "tb.sv"
    src.write_text(
        "module tb;\n  c_src: cover property (1);\n  cover property (1);\nendmodule\n"
    )
    raw = tmp_path / "coverage.json"
    raw.write_text(
        json.dumps(
            {
                "assertions": [
                    {
                        "name": "c_json",
                        "file": "tb.sv",
                        "line": 2,
                        "kind": "cover",
                        "pass": 1,
                    },
                    {"file": "tb.sv", "line": 3, "kind": "cover", "pass": 0},
                    {
                        "name": "a_lbl",
                        "file": "tb.sv",
                        "line": 3,
                        "kind": "assert",
                        "pass": 4,
                    },
                    {
                        "name": "c_gone",
                        "file": "gone.sv",
                        "line": 7,
                        "kind": "cover",
                        "pass": 2,
                    },
                ]
            }
        )
    )
    records = parse_xezim_records(raw)
    assert [(r["name"], r["line"], r["hits"]) for r in records] == [
        ("c_json", 2, 1),
        ("cover@3", 3, 0),
        ("c_gone", 7, 2),
    ]


def test_reader_has_no_expression_points_and_returns_none_when_unreadable(tmp_path):
    assert not [r for r in _records() if r["metric"] == EXPRESSION]
    bad = tmp_path / "coverage.json"
    bad.write_text("{not json")
    assert parse_xezim_records(bad) is None
    assert parse_xezim_records(tmp_path / "missing.json") is None


def test_reader_metrics_filter():
    assert {
        r["metric"]
        for r in parse_xezim_records(FIXTURE_DIR / "coverage.json", metrics=[COVER])
    } == {COVER}


# --- collector -----------------------------------------------------------------


def test_factory_picks_the_collector_by_family():
    assert type(coverage_collector_for("xezim")) is XezimCov
    assert type(coverage_collector_for("verilator")) is VlogCov
    icarus = coverage_collector_for("icarus")
    assert type(icarus) is VlogCov and not icarus.is_supported()
    assert icarus.collect(str(FIXTURE_DIR / "coverage.json")) is None


def test_collect_reports_scalars_and_covers():
    metrics = XezimCov().collect(str(FIXTURE_DIR / "coverage.json"))
    assert metrics.line == 1.0
    assert metrics.branch == 1.0
    assert metrics.toggle == pytest.approx(8 / 11)
    assert metrics.expression is None
    assert metrics.functional == 0.5
    assert metrics.summary_str() == "L:1.00 B:1.00 T:0.73 F:0.50"
    assert [c["name"] for c in metrics.covers] == ["c_wrap", "cover@15"]
    assert metrics.to_dict()["raw_paths"] == [str(FIXTURE_DIR / "coverage.json")]


def test_collect_without_code_coverage_reports_only_functional(tmp_path):
    raw = tmp_path / "coverage.json"
    raw.write_text(
        json.dumps(
            {"assertions": [{"file": "a.sv", "line": 1, "kind": "cover", "pass": 1}]}
        )
    )
    metrics = XezimCov().collect(str(raw))
    assert (metrics.line, metrics.branch, metrics.toggle) == (None, None, None)
    assert metrics.functional == 1.0


def test_collect_missing_file_is_none(tmp_path):
    assert XezimCov().collect(str(tmp_path / "coverage.json")) is None


def test_lcov_gives_a_branch_line_without_a_statement_a_da_record(tmp_path):
    raw = tmp_path / "coverage.json"
    raw.write_text(
        json.dumps(
            {
                "assertions": [],
                "code_coverage": {
                    "design_units": [
                        {
                            "design_unit": "m",
                            "statements": [
                                {"file": "/m.sv", "line": 3, "kind": "s", "count": 4}
                            ],
                            "branches": [
                                {
                                    "file": "/m.sv",
                                    "line": 5,
                                    "kind": "case",
                                    "arms": [
                                        {"line": 6, "arm": "0", "count": 1},
                                        {"line": 7, "arm": "default", "count": 0},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            }
        )
    )
    cov = XezimCov(root_cfg=_RootCfg(tmp_path))
    info = tmp_path / "out.info"
    assert cov._write_lcov(str(raw), str(info))
    text = info.read_text()
    assert "DA:3,4\nDA:5,1\n" in text
    assert "BRDA:5,0,0,1\nBRDA:5,0,1,0\nBRF:2\nBRH:1\n" in text


def test_generate_artifacts_writes_line_and_branch_lcov(project):
    cov = XezimCov(use_lcov=True, root_cfg=_RootCfg(project.root))
    outdir = project.root / "cov"
    outdir.mkdir()
    metrics = cov.generate_artifacts(
        str(project.raw),
        str(outdir),
        artifact_name="cnt__basic",
        source_roots=[str(project.suite)],
    )
    info = Path(metrics.lcov_path).read_text()
    counter = str((project.root / "design" / "counter.sv").resolve())
    assert f"SF:{counter}\n" in info
    assert "DA:13,14\n" in info
    # One block on the `if` line, the arms in xezim's order: if, else if, else.
    assert "BRDA:14,0,0,2\nBRDA:14,0,1,10\nBRDA:14,0,2,2\n" in info
    assert info.count("end_of_record") == 2
    assert cov.parse_lcov_summary(metrics.lcov_path) == (1.0, 1.0)


def _second_run(project, *, scale_rst=True):
    """A second test's file, one directory deeper, with rst_n's fall hit."""
    doc = json.loads(project.raw.read_text())
    for unit in doc["code_coverage"]["design_units"]:
        for toggle in unit["toggles"]:
            if toggle["signal"] == "rst_n" and scale_rst:
                toggle["fall"] = [1]
    doc["assertions"][0]["pass"] = 3
    text = json.dumps(doc).replace("../../../../design", "../../../../../design")
    text = text.replace('"../../tb_counter.sv"', '"../../../tb_counter.sv"')
    raw = project.suite / "artefacts" / "random" / "run-0001" / "coverage.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(text)
    return raw


def test_merge_sums_counts_across_runs_with_different_relative_paths(project):
    second = _second_run(project)
    outdir = project.root / "cov"
    outdir.mkdir()
    cov = XezimCov(use_lcov=True, root_cfg=_RootCfg(project.root))
    metrics = cov.merge([str(project.raw), str(second)], str(outdir))
    assert not metrics.merge_failed and metrics.failed_metrics is None
    assert metrics.functional == 1.0
    assert metrics.toggle == pytest.approx(9 / 11)
    merged = json.loads(Path(metrics.merged_path).read_text())
    assert Path(metrics.merged_path).name == "coverage_merged.json"
    units = {u["design_unit"]: u for u in merged["code_coverage"]["design_units"]}
    # One unit per module even though the two runs spelled the paths differently.
    assert set(units) == {"counter", "tb_counter"}
    assert [s["count"] for s in units["counter"]["statements"]] == [28, 4, 20]
    assert units["counter"]["toggle"] == {"covered": 7, "total": 8, "percent": 87.5}
    assert merged["code_coverage"]["statement"]["total"] == 14
    assert merged["assertion_pass_total"] == 1 * 2 + 3
    assert "DA:13,28" in Path(metrics.lcov_path).read_text()


def test_merge_with_an_unreadable_input_fails_loud(project):
    bad = project.suite / "artefacts" / "bad" / "coverage.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{truncated")
    outdir = project.root / "cov"
    outdir.mkdir()
    cov = XezimCov(root_cfg=_RootCfg(project.root))
    metrics = cov.merge([str(project.raw), str(bad)], str(outdir))
    assert metrics.merge_failed
    assert metrics.merged_path is None
    assert metrics.failed_metrics == ["line", "branch", "toggle", "functional"]
    assert metrics.summary_str() == "L:FAIL B:FAIL T:FAIL F:FAIL"
    # Expression is unsupported, not failed.
    assert not metrics.is_failed("expression")


def test_merge_failure_keeps_line_and_branch_from_lcov(project):
    bad = project.suite / "artefacts" / "bad" / "coverage.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("[]")
    outdir = project.root / "cov"
    outdir.mkdir()
    cov = XezimCov(use_lcov=True, root_cfg=_RootCfg(project.root))
    metrics = cov.merge([str(project.raw), str(bad)], str(outdir))
    assert metrics.merge_failed
    assert (metrics.line, metrics.branch) == (1.0, 1.0)
    assert metrics.failed_metrics == ["toggle", "functional"]


def test_merge_timeout_fails_the_merge(project, monkeypatch):
    outdir = project.root / "cov"
    outdir.mkdir()
    cov = XezimCov(root_cfg=_RootCfg(project.root, timeout=5))
    clock = iter([0.0, 100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr("rtl_buddy.tools.xezim_cov.time.monotonic", lambda: next(clock))
    metrics = cov.merge([str(project.raw), str(project.raw)], str(outdir))
    assert metrics.merge_failed
    assert not (outdir / "coverage_merged.json").exists()


def test_merge_docs_unions_disjoint_designs():
    a = {
        "assertions": [],
        "code_coverage": {
            "design_units": [
                {
                    "design_unit": "a",
                    "statements": [
                        {"file": "/a.sv", "line": 1, "kind": "s", "count": 1}
                    ],
                }
            ]
        },
    }
    b = {
        "assertions": [
            {"file": "/b.sv", "line": 2, "kind": "cover", "pass": 1, "fail": 0}
        ]
    }
    merged = merge_xezim_docs([a, b])
    assert [u["design_unit"] for u in merged["code_coverage"]["design_units"]] == ["a"]
    assert merged["assertion_sites"] == 1


# --- model ---------------------------------------------------------------------


def test_model_from_xezim_raw_has_totals_source_totals_and_modules(project):
    second = _second_run(project)
    tests = [
        TestArtefacts(
            name="basic",
            raw=str(project.raw),
            suite="verif/cnt/tests.yaml",
            source_roots=(str(project.raw.parent), str(project.suite)),
        ),
        TestArtefacts(
            name="random",
            raw=str(second),
            suite="verif/cnt/tests.yaml",
            source_roots=(str(second.parent), str(project.suite)),
        ),
    ]
    model = build_model(tests, project_root=project.root, simulator="xezim")
    assert model["simulator"] == "xezim"
    assert model["modules"] == {
        "counter": ["design/counter.sv"],
        "tb_counter": ["verif/cnt/tb_counter.sv"],
    }
    totals = model["totals"]
    assert (totals["line"]["found"], totals["line"]["hit"]) == (14, 14)
    assert (totals["branch"]["found"], totals["branch"]["hit"]) == (3, 3)
    assert (totals["toggle"]["found"], totals["toggle"]["hit"]) == (11, 9)
    assert (totals["cover"]["found"], totals["cover"]["hit"]) == (2, 2)
    assert totals["expression"]["found"] == 0
    # The count signal is toggled in both modules but lives in two files' rows here,
    # so source collapse (which drops the module) changes nothing.
    assert model["source_totals"]["toggle"]["found"] == 11
    rows = {row["name"]: row for row in model["tests"]}
    assert rows["basic"]["totals"]["toggle"]["hit"] == 8
    assert rows["random"]["totals"]["toggle"]["hit"] == 9
    assert rows["basic"]["raw"] == "verif/cnt/artefacts/basic/coverage.json"
    by_path = {row["path"]: row for row in model["files"]}
    counter_lines = by_path["design/counter.sv"]["line"]
    assert [(p["line"], p["column"], p["hits"]) for p in counter_lines] == [
        (13, 0, 28),
        (14, 0, 4),
        (15, 0, 20),
    ]
    assert counter_lines[0]["tests"] == {"basic": 14, "random": 14}


def test_reporter_merge_and_model_for_xezim(project):
    results = _TestResults("basic", {"result": "PASS", "desc": "ok"})
    metrics = XezimCov().collect(str(project.raw))
    results.results["coverage"] = metrics.to_dict()
    suite_results = [{"test_name": "basic", "results": results}]
    reporter = CoverageReporter(_RootCfg(project.root, use_lcov=True))
    metadata, payload = reporter.build_metadata(
        suite_results,
        outdir=str(project.suite),
        suite_name=str(project.suite / "tests.yaml"),
        coverage_merge=True,
        command="test",
    )
    assert payload["merged"]["line"] == 1.0
    assert not payload["merge_failed"]
    manifest = json.loads((project.root / payload["artefacts"]["manifest"]).read_text())
    assert manifest["simulator_family"] == "xezim"
    assert manifest["merged"]["raw"].endswith("coverage_merged.json")
    assert any(line.startswith("Merged Coverage: L:1.00") for line in metadata)


# --- guard, coverview, graph ---------------------------------------------------


def test_guard_accepts_xezim_raw_paths(project):
    results = _TestResults("basic", {"result": "PASS", "desc": "ok"})
    results.results["coverage"] = XezimCov().collect(str(project.raw)).to_dict()
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.coverage = CoverageReporter(_RootCfg(project.root))
    flags = dict(
        coverage_merge=True,
        coverage_merge_raw=False,
        coverage_merge_info_process=False,
        coverage_html=False,
        coverage_coverview=False,
        coverage_dir_summary=None,
        coverage_dir_summary_file=None,
    )
    rb._guard_coverage_requested(
        [{"test_name": "basic", "results": results}], 0, **flags
    )
    empty = _TestResults("basic", {"result": "PASS", "desc": "ok"})
    with pytest.raises(FatalRtlBuddyError, match="no coverage data"):
        rb._guard_coverage_requested(
            [{"test_name": "basic", "results": empty}], 0, **flags
        )


def test_coverview_makes_no_typed_dataset_from_a_xezim_file(project, tmp_path):
    packer = CoverviewPacker(cfg=None, project_root=str(project.root))
    assert packer.write_toggle_info(str(project.raw), str(tmp_path), "x") is None
    assert packer.write_expression_info(str(project.raw), str(tmp_path), "x") is None


def test_graph_results_lists_a_xezim_coverage_file(project):
    scope = project.raw.parent
    found = _artefacts(project.root, scope, scope)
    assert found["coverage"] == "verif/cnt/artefacts/basic/coverage.json"
    (scope / "coverage.dat").write_text("# SystemC::Coverage-3\n")
    assert _artefacts(project.root, scope, scope)["coverage"].endswith("coverage.dat")


def test_cfg_coverage_accepts_a_xezim_entry():
    cfg = from_yaml(
        CoverageConfigFile, "name: xezim\nuse-lcov: true\nmerge-timeout: 60\n"
    ).initialise()
    assert (cfg.get_name(), cfg.get_use_lcov(), cfg.get_merge_timeout()) == (
        "xezim",
        True,
        60,
    )


# --- VlogSim wiring ------------------------------------------------------------


def _sim(tmp_path, family, compile_opts, *, assertions=False):
    sim = vlog_sim_module.VlogSim.__new__(vlog_sim_module.VlogSim)
    sim.test_cfg = SimpleNamespace(assertions=assertions)
    sim.test_name = "basic"
    sim.run_tag = None
    sim.suite_work_dir = str(tmp_path / "verif" / "cnt")
    sim._get_simulator_family = lambda: family
    sim._get_builder_compile_opts = lambda: list(compile_opts)
    return sim


@pytest.mark.parametrize(
    "family, opts, assertions, expected",
    [
        ("xezim", ["--code-coverage=all"], False, True),
        ("xezim", ["--code-coverage"], False, True),
        ("xezim", ["+cover=stmt"], False, True),
        ("xezim", ["--coverage"], False, False),
        ("xezim", [], True, False),
        ("verilator", ["--coverage"], False, True),
        ("verilator", [], True, True),
        ("verilator", ["--code-coverage"], False, False),
        ("icarus", [], True, False),
    ],
)
def test_coverage_enabled_per_family(tmp_path, family, opts, assertions, expected):
    sim = _sim(tmp_path, family, opts, assertions=assertions)
    assert sim._coverage_enabled() is expected


def test_raw_name_and_env_follow_the_family(tmp_path):
    xezim = _sim(tmp_path, "xezim", ["--code-coverage=all"])
    assert xezim._get_cov_path().endswith("artefacts/basic/coverage.json")
    assert xezim._get_cov_path(run_id=3).endswith("basic/run-0003/coverage.json")
    env = xezim._get_coverage_sim_env(run_id=3)
    assert env == {"XEZIM_COV_DB": xezim._get_cov_abspath(run_id=3)}

    off = _sim(tmp_path, "xezim", [])
    assert off._get_coverage_sim_env() == {}

    verilator = _sim(tmp_path, "verilator", ["--coverage"])
    assert verilator._get_cov_path().endswith("artefacts/basic/coverage.dat")
    assert verilator._get_coverage_sim_env() == {}


def test_a_stale_xezim_coverage_file_is_cleared_before_a_run(tmp_path):
    sim = _sim(tmp_path, "xezim", ["--code-coverage=all"])
    names = {p.name for p in sim._run_output_paths(None)}
    assert {"coverage.json", "coverage.dat"} <= names
