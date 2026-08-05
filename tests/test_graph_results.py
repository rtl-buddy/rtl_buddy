"""Tests for #379 — the regression-results overlay.

``graph.json`` is the static half of the design knowledge graph and must
stay byte-stable no matter how many regressions run; everything volatile
(status, ``run_token``, seed, artefact paths) lives in
``artefacts/graph/results-overlay.json``, keyed by the same
``test:<suite dir>#<name>`` node ids the config tier emits.

What these tests pin:

* the overlay is keyed by node id and reports what the *result envelope*
  says, not what a log looks like;
* the timestamp comes off the envelope file, never the wall clock, so a
  refresh with nothing re-run rewrites identical bytes;
* refreshing the overlay does not touch ``graph.json`` (the acceptance
  criterion), and does not disturb the build fingerprint either;
* the join hooks #380 will use — ``load_overlay()`` plus the node-id
  lookup — resolve a test node to its status and artefacts.

No simulator runs here: result envelopes are written with the same
``runner.result_io`` writer the runner uses, and the artefact layout is
fabricated exactly as ``docs/development/guidelines.md`` documents it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.graph import results as graph_results
from rtl_buddy.graph.results import (
    FROM_ARTEFACTS,
    FROM_ENVELOPE,
    OVERLAY_FILETYPE,
    OVERLAY_SCHEMA_VERSION,
    RESULTS_OVERLAY_NAME,
    UNKNOWN,
    annotate_graph,
    collect_results,
    load_overlay,
    overlay_for_node,
    refresh_results_overlay,
    results_overlay_path,
)
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import TestResults as _TestResults

_FIXTURES = Path(__file__).parent / "fixtures"

# Fixed mtimes so every timestamp in these tests is a property of the
# files, never of when the suite happened to run.
_T_FIRST = 1_750_000_000
_T_SECOND = 1_750_000_600


@pytest.fixture
def results_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The config-tier fixture, runnable as a project root."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", target)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", target)
    for name in ("blk_a", "blk_b"):
        (target / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    monkeypatch.chdir(target)
    return target


def _results(result: str, desc: str = "ok") -> _TestResults:
    return _TestResults(name="t", results={"result": result, "desc": desc})


def _artefact_dir(project: Path, test: str, run_id: int | None = None) -> Path:
    directory = project / "verif" / "blk_a" / "artefacts" / test
    if run_id is not None:
        directory /= f"run-{run_id:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _stamp(path: Path, when: int) -> Path:
    os.utime(path, (when, when))
    return path


def _seed_run(
    project: Path,
    test: str,
    *,
    run_id: int | None = None,
    status: str = "PASS",
    seed: int = 4242,
    token: str = "tok-1",
    when: int = _T_FIRST,
    envelope: bool = True,
    dispatch: bool = False,
    trace: bool = True,
) -> Path:
    """Fabricate one run's artefacts (and, by default, its envelope)."""
    directory = _artefact_dir(project, test, run_id)
    (directory / "test.log").write_text(f"{status}\n")
    (directory / "test.err").write_text("")
    (directory / "test.randseed").write_text(f"{seed}\n")
    (directory / "coverage.dat").write_text("# coverage\n")
    if trace:
        (directory / "dump.fst").write_text("fst")
    if envelope:
        tag = "single" if run_id is None else f"{run_id:04d}"
        target = (
            _artefact_dir(project, test) / "dispatch" / f"result-{tag}.json"
            if dispatch
            else directory / "result.json"
        )
        write_result_json(
            target,
            test_name=test,
            run_id=run_id,
            results=_results(status),
            run_token=token,
        )
        _stamp(target, when)
    for name in ("test.log", "test.err", "test.randseed", "coverage.dat", "dump.fst"):
        if (directory / name).exists():
            _stamp(directory / name, when)
    return directory


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_graph_results")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def test_entry_is_keyed_by_test_node_id_and_reports_the_envelope(
    results_project: Path,
):
    _seed_run(results_project, "t_basic", status="PASS", seed=17, token="tok-abc")

    overlay = collect_results(results_project)

    assert set(overlay.entries) == {"test:verif/blk_a#t_basic"}
    entry = overlay.entries["test:verif/blk_a#t_basic"]
    assert entry["suite"] == "verif/blk_a"
    assert entry["test"] == "t_basic"
    assert entry["status"] == "PASS"
    assert entry["run_token"] == "tok-abc"
    assert entry["randseed"] == 17
    assert entry["source"] == FROM_ENVELOPE
    assert entry["result_json"] == "verif/blk_a/artefacts/t_basic/result.json"
    assert entry["artefacts"] == {
        "dir": "verif/blk_a/artefacts/t_basic",
        "log": "verif/blk_a/artefacts/t_basic/test.log",
        "err": "verif/blk_a/artefacts/t_basic/test.err",
        "randseed": "verif/blk_a/artefacts/t_basic/test.randseed",
        "coverage": "verif/blk_a/artefacts/t_basic/coverage.dat",
        "trace": "verif/blk_a/artefacts/t_basic/dump.fst",
    }
    assert overlay.with_results() == 1
    assert overlay.status_counts() == {"PASS": 1}


def test_timestamp_comes_from_the_envelope_not_the_wall_clock(results_project: Path):
    _seed_run(results_project, "t_basic", when=_T_FIRST)

    first = collect_results(results_project).entries["test:verif/blk_a#t_basic"]
    assert first["timestamp"] == "2025-06-15T15:06:40Z"

    # Nothing re-ran, so a refresh must reproduce the same stamp — the
    # overlay is a function of the files, not of when it was refreshed.
    again = collect_results(results_project).entries["test:verif/blk_a#t_basic"]
    assert again["timestamp"] == first["timestamp"]


def test_dispatch_envelope_is_read_from_the_documented_location(
    results_project: Path,
):
    _seed_run(results_project, "t_cocotb", status="FAIL", dispatch=True, token="tok-d")

    entry = collect_results(results_project).entries["test:verif/blk_a#t_cocotb"]

    assert entry["status"] == "FAIL"
    assert entry["run_token"] == "tok-d"
    assert (
        entry["result_json"]
        == "verif/blk_a/artefacts/t_cocotb/dispatch/result-single.json"
    )


def test_newest_envelope_wins_when_both_producers_wrote_one(results_project: Path):
    _seed_run(results_project, "t_basic", status="FAIL", token="old", when=_T_FIRST)
    _seed_run(
        results_project,
        "t_basic",
        status="PASS",
        token="new",
        when=_T_SECOND,
        dispatch=True,
    )

    entry = collect_results(results_project).entries["test:verif/blk_a#t_basic"]

    assert (entry["status"], entry["run_token"]) == ("PASS", "new")


def test_randtest_runs_are_listed_and_the_last_one_is_the_status(
    results_project: Path,
):
    _seed_run(
        results_project, "t_basic", run_id=1, status="PASS", seed=1, when=_T_FIRST
    )
    _seed_run(
        results_project, "t_basic", run_id=2, status="FAIL", seed=2, when=_T_SECOND
    )

    entry = collect_results(results_project).entries["test:verif/blk_a#t_basic"]

    assert entry["status"] == "FAIL"
    assert entry["run_id"] == 2
    assert [r["run_id"] for r in entry["runs"]] == [1, 2]
    assert [r["status"] for r in entry["runs"]] == ["PASS", "FAIL"]
    assert [r["randseed"] for r in entry["runs"]] == [1, 2]
    assert (
        entry["runs"][0]["artefacts"]["log"]
        == "verif/blk_a/artefacts/t_basic/run-0001/test.log"
    )
    # The bare test dir is only a container for the run dirs here; it is
    # not reported as a phantom UNKNOWN run of its own.
    assert all(r["run_id"] is not None for r in entry["runs"])


def test_artefacts_without_an_envelope_are_reported_as_unknown(results_project: Path):
    _seed_run(results_project, "t_basic", envelope=False, seed=99)

    entry = collect_results(results_project).entries["test:verif/blk_a#t_basic"]

    assert entry["status"] == UNKNOWN
    assert entry["source"] == FROM_ARTEFACTS
    assert "run_token" not in entry
    # Still useful: the seed and the paths are there to be replayed from.
    assert entry["randseed"] == 99
    assert entry["artefacts"]["log"].endswith("t_basic/test.log")
    assert entry["timestamp"] == "2025-06-15T15:06:40Z"


def test_sanitized_directory_maps_back_to_the_declared_test_name(
    results_project: Path, tmp_path: Path
):
    # A test whose name needs sanitizing on disk, with no envelope to
    # carry the real name: the suite config is what un-sanitizes it.
    tests_yaml = results_project / "verif" / "blk_a" / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        + """
  - name: "t_odd/name"
    desc: "sanitized on disk"
    reglvl: 0
    model: "blk_a"
    model_path: "../../design/blk_a/models.yaml"
    testbench: "tb_hdl"
"""
    )
    directory = results_project / "verif" / "blk_a" / "artefacts" / "t_odd_name"
    directory.mkdir(parents=True)
    (directory / "test.log").write_text("PASS\n")

    entries = collect_results(results_project).entries

    assert "test:verif/blk_a#t_odd/name" in entries


def test_non_test_directories_are_not_mistaken_for_tests(results_project: Path):
    artefacts = results_project / "verif" / "blk_a" / "artefacts"
    for name in ("hier", "axi", ".dispatch", ".shared-builds", "obj_dir_t_basic"):
        (artefacts / name).mkdir(parents=True)
        (artefacts / name / "test.log").write_text("noise\n")
    _seed_run(results_project, "t_basic")

    assert set(collect_results(results_project).entries) == {"test:verif/blk_a#t_basic"}


def test_a_directory_with_no_run_evidence_is_not_a_test(results_project: Path):
    # Another command's per-suite workspace (an fpv.yaml beside the
    # tests.yaml) holds no envelope and no recognized artefact.
    workspace = results_project / "verif" / "blk_a" / "artefacts" / "fpv_blk_a"
    (workspace / "sby_workdir").mkdir(parents=True)
    (workspace / "sby_workdir" / "status").write_text("PASS\n")
    _seed_run(results_project, "t_basic")

    assert set(collect_results(results_project).entries) == {"test:verif/blk_a#t_basic"}


def test_a_malformed_envelope_is_a_problem_not_a_crash(results_project: Path):
    directory = _seed_run(results_project, "t_basic", envelope=False)
    (directory / "result.json").write_text("{not json")

    overlay = collect_results(results_project)

    assert overlay.entries["test:verif/blk_a#t_basic"]["status"] == UNKNOWN
    assert len(overlay.problems) == 1
    assert "verif/blk_a/artefacts/t_basic" in overlay.problems[0]["dir"]


# ---------------------------------------------------------------------------
# Cross-check against the graph
# ---------------------------------------------------------------------------


def _config_graph(project: Path) -> dict:
    from rtl_buddy.graph import build_config_tier

    return build_config_tier(project)


def test_cross_check_flags_missing_and_unmatched_ids(results_project: Path):
    _seed_run(results_project, "t_basic")
    _seed_run(results_project, "t_swept_1")  # a sweep expansion, not in the config

    overlay = collect_results(results_project, graph=_config_graph(results_project))

    assert overlay.entries["test:verif/blk_a#t_basic"]["in_graph"] is True
    assert overlay.entries["test:verif/blk_a#t_swept_1"]["in_graph"] is False
    assert overlay.unmatched == ["test:verif/blk_a#t_swept_1"]
    assert overlay.missing == ["test:verif/blk_a#t_cocotb"]


# ---------------------------------------------------------------------------
# Writing, loading, joining
# ---------------------------------------------------------------------------


def test_refresh_writes_the_overlay_beside_the_graph(results_project: Path):
    _seed_run(results_project, "t_basic")

    overlay = refresh_results_overlay(results_project)

    assert overlay.path == results_overlay_path(results_project)
    assert (
        overlay.path == results_project / "artefacts" / "graph" / RESULTS_OVERLAY_NAME
    )
    payload = json.loads(overlay.path.read_text())
    assert payload["rtl-buddy-filetype"] == OVERLAY_FILETYPE
    assert payload["schema_version"] == OVERLAY_SCHEMA_VERSION
    assert payload["summary"]["tests"] == 1
    assert payload["summary"]["with_results"] == 1
    assert payload["summary"]["statuses"] == {"PASS": 1}
    assert list(payload["tests"]) == sorted(payload["tests"])


def test_refresh_is_byte_stable_when_nothing_re_ran(results_project: Path):
    _seed_run(results_project, "t_basic")

    first = refresh_results_overlay(results_project).path.read_bytes()
    second = refresh_results_overlay(results_project).path.read_bytes()

    assert first == second


def test_load_overlay_and_node_id_join(results_project: Path):
    _seed_run(results_project, "t_basic", status="FAIL", seed=7)
    refresh_results_overlay(results_project)

    # The three ways #380 can reach it: the file, its directory, the root.
    from_file = load_overlay(results_overlay_path(results_project))
    from_dir = load_overlay(results_project / "artefacts" / "graph")
    from_root = load_overlay(results_project)
    assert from_file == from_dir == from_root

    entry = overlay_for_node(from_file, "test:verif/blk_a#t_basic")
    assert entry["status"] == "FAIL"
    assert entry["randseed"] == 7
    assert entry["artefacts"]["log"].endswith("t_basic/test.log")
    # Non-test nodes and unknown ids simply have no results.
    assert overlay_for_node(from_file, "module:blk_a") is None
    assert overlay_for_node(None, "test:verif/blk_a#t_basic") is None


def test_annotate_graph_attaches_entries_without_touching_the_file(
    results_project: Path,
):
    _seed_run(results_project, "t_basic")
    refresh_results_overlay(results_project)
    graph = _config_graph(results_project)
    overlay = load_overlay(results_project)

    annotated = annotate_graph(graph, overlay)

    assert annotated == 1
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["test:verif/blk_a#t_basic"]["results"]["status"] == "PASS"
    assert "results" not in nodes["test:verif/blk_a#t_cocotb"]


def test_load_overlay_rejects_a_foreign_or_missing_file(
    results_project: Path, tmp_path: Path
):
    assert load_overlay(tmp_path / "nope.json") is None
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"rtl-buddy-filetype": "test_result"}))
    assert load_overlay(foreign) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert load_overlay(broken) is None


# ---------------------------------------------------------------------------
# The graph stays put
# ---------------------------------------------------------------------------


def test_overlay_refresh_leaves_graph_json_hash_stable(results_project: Path):
    """The acceptance criterion: results never churn the graph."""
    runner, rb = _runner()
    built = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-graphify", "--no-bind"]
    )
    assert built.exit_code == 0, built.output
    graph_json = results_project / "artefacts" / "graph" / "graph.json"
    meta_json = results_project / "artefacts" / "graph" / "graph-meta.json"
    before, meta_before = _sha256(graph_json), _sha256(meta_json)

    _seed_run(results_project, "t_basic", status="PASS")
    assert runner.invoke(rb.app, ["graph", "results"]).exit_code == 0
    _seed_run(results_project, "t_basic", status="FAIL", when=_T_SECOND)
    assert runner.invoke(rb.app, ["graph", "results"]).exit_code == 0

    assert _sha256(graph_json) == before
    assert _sha256(meta_json) == meta_before

    # And the next build is still a cache hit: results live outside the
    # fingerprint's input set, so a regression run cannot invalidate it.
    again = runner.invoke(
        rb.app,
        ["--machine", "graph", "build", "--no-design", "--no-graphify", "--no-bind"],
    )
    assert json.loads(again.output.strip().splitlines()[-1])["payload"]["unchanged"]
    assert _sha256(graph_json) == before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_machine_envelope(results_project: Path):
    _seed_run(results_project, "t_basic", status="PASS")
    runner, rb = _runner()
    runner.invoke(rb.app, ["graph", "build", "--no-design", "--no-graphify"])

    result = runner.invoke(rb.app, ["--machine", "graph", "results"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["command"] == "graph results"
    payload = envelope["payload"]
    assert payload["overlay"] == "artefacts/graph/results-overlay.json"
    assert payload["tests"] == 1
    assert payload["with_results"] == 1
    assert payload["statuses"] == {"PASS": 1}
    assert payload["missing"] == ["test:verif/blk_a#t_cocotb"]
    assert payload["graph"]["fingerprint"]


def test_cli_strict_exits_non_zero_when_a_test_node_has_no_result(
    results_project: Path,
):
    _seed_run(results_project, "t_basic")
    runner, rb = _runner()
    runner.invoke(rb.app, ["graph", "build", "--no-design", "--no-graphify"])

    lenient = runner.invoke(rb.app, ["graph", "results"])
    strict = runner.invoke(rb.app, ["graph", "results", "--strict"])

    assert lenient.exit_code == 0, lenient.output
    assert strict.exit_code == 1

    _seed_run(results_project, "t_cocotb")
    assert runner.invoke(rb.app, ["graph", "results", "--strict"]).exit_code == 0


def test_cli_out_dir_and_verif_dir_overrides(results_project: Path, tmp_path: Path):
    _seed_run(results_project, "t_basic")
    out = tmp_path / "elsewhere"
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "graph",
            "results",
            "-o",
            str(out),
            "--verif-dir",
            str(results_project / "verif"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out / RESULTS_OVERLAY_NAME).is_file()
    assert not (results_project / "artefacts" / "graph" / RESULTS_OVERLAY_NAME).exists()


# ---------------------------------------------------------------------------
# The producer side: every run leaves an envelope behind
# ---------------------------------------------------------------------------


class _StubTestCfg:
    def __init__(self, name: str):
        self._name = name

    def get_name(self) -> str:
        return self._name


def test_in_process_runs_write_a_result_envelope_per_run(results_project: Path):
    rb = RtlBuddy(name="test_graph_results")
    suite_dir = results_project / "verif" / "blk_a"

    rb._record_run_results(
        _StubTestCfg("t_basic"), str(suite_dir), [None], [_results("PASS")]
    )
    rb._record_run_results(
        _StubTestCfg("t_rand"),
        str(suite_dir),
        [1, 2],
        [_results("PASS"), _results("FAIL")],
    )

    entries = collect_results(results_project).entries
    assert entries["test:verif/blk_a#t_basic"]["status"] == "PASS"
    assert entries["test:verif/blk_a#t_basic"]["source"] == FROM_ENVELOPE
    rand = entries["test:verif/blk_a#t_rand"]
    assert [r["status"] for r in rand["runs"]] == ["PASS", "FAIL"]
    # One nonce per invocation, shared by every run it recorded.
    tokens = {e["run_token"] for e in entries.values()} | {
        r["run_token"] for r in rand["runs"]
    }
    assert len(tokens) == 1


def test_recording_a_result_never_fails_a_run(
    results_project: Path, monkeypatch: pytest.MonkeyPatch
):
    rb = RtlBuddy(name="test_graph_results")

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.write_result_json", _boom)

    rb._record_run_results(
        _StubTestCfg("t_basic"),
        str(results_project / "verif" / "blk_a"),
        [None],
        [_results("PASS")],
    )

    assert collect_results(results_project).entries == {}


def test_module_is_importable_from_the_package():
    assert graph_results.RESULTS_OVERLAY_NAME == RESULTS_OVERLAY_NAME
