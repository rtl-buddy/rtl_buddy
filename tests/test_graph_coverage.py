"""Tests for the declared-vs-observed coverage join on the graph.

The join reads coverage numbers from ``cov_dir/manifest.json`` and the model it
names, never from ``verilator_coverage``, so a refresh with nothing re-run
writes identical bytes. Each declared item lands in exactly one status, the
item-id to SVA-label correlation records its match rung, and the overlay is the
single carrier read by the query verbs and the ``/graph`` pane. The coverage
artefacts are fabricated with the real :mod:`rtl_buddy.cov` writers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.cov.manifest import build_manifest, write_manifest
from rtl_buddy.cov.model import TestArtefacts, build_model, write_model
from rtl_buddy.graph.coverage import (
    STATUS_DECLARED_ONLY,
    STATUS_EXERCISED,
    STATUS_OBSERVED_UNDECLARED,
    _design_entries,
    annotate_coverage,
    base_module_name,
    coverage_for_node,
    join_coverage,
)
from rtl_buddy.graph.query import explain, load_context
from rtl_buddy.graph.query import test_status as query_test_status
from rtl_buddy.graph.results import collect_results, refresh_results_overlay
from rtl_buddy.hub import graph_page
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import TestResults as _TestResults

_FIXTURES = Path(__file__).parent / "fixtures"

_T_FIRST = 1_750_000_000

_BLK_A = "design/blk_a/blk_a.sv"
_BLK_B = "design/blk_b/blk_b.sv"


def _dat_record(*, file, line, type_, name, module, col=1, hits=1):
    """One record in Verilator's raw coverage database format."""
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


# One run's raw database: blk_a half-covered (`cov_a_cov_1` fired, `A-COV-2` did not),
# blk_b fully covered with a cover point no `covers:` entry claims.
_RECORDS = (
    (_BLK_A, 1, "line", "", "blk_a", 1),
    (_BLK_A, 2, "line", "", "blk_a", 0),
    (_BLK_A, 4, "user", "cov_a_cov_1", "blk_a", 3),
    (_BLK_A, 6, "user", "A-COV-2", "blk_a", 0),
    (_BLK_B, 3, "user", "stray_cover", "blk_b", 7),
    (_BLK_B, 5, "line", "", "blk_b", 2),
)

# The same run as _RECORDS with parameterised module names (`blk_a__W1`, `blk_a__Wc`,
# `blk_b`, `blk_b__W4`); line points are recorded once per elaboration and merged by
# line, so a per-elaboration sum would double count.
_ELABORATED_RECORDS = (
    (_BLK_A, 1, "line", "", "blk_a__W1", 1),
    (_BLK_A, 2, "line", "", "blk_a__W1", 0),
    (_BLK_A, 1, "line", "", "blk_a__Wc", 1),
    (_BLK_A, 2, "line", "", "blk_a__Wc", 0),
    (_BLK_A, 4, "user", "cov_a_cov_1", "blk_a__W1", 3),
    (_BLK_A, 4, "user", "cov_a_cov_1", "blk_a__Wc", 0),
    (_BLK_B, 5, "line", "", "blk_b", 2),
    (_BLK_B, 6, "line", "", "blk_b__W4", 0),
)


def _write_run(project: Path, records) -> Path:
    """One test run's raw database and result."""
    run_dir = project / "verif" / "blk_a" / "artefacts" / "t_basic"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.log").write_text("PASS\n")
    raw = run_dir / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + "".join(
            _dat_record(
                file="../../../../" + path,
                line=line,
                type_=type_,
                name=name,
                module=module,
                hits=hits,
            )
            for path, line, type_, name, module, hits in records
        ),
        encoding="utf-8",
    )
    write_result_json(
        run_dir / "result.json",
        test_name="t_basic",
        run_id=None,
        results=_TestResults(name="t", results={"result": "PASS", "desc": "ok"}),
        run_token="tok-1",
    )
    return raw


def _cov_tree(tmp_path: Path, records) -> Path:
    """The config-tier fixture plus one test run with coverage on disk."""
    project = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", project)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", project)
    for name in ("blk_a", "blk_b"):
        (project / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    _write_cov_artefacts(project, _write_run(project, records))
    return project


@pytest.fixture
def cov_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = _cov_tree(tmp_path, _RECORDS)
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def elaborated_cov_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """:func:`cov_project` with the simulator's parameterised names."""
    project = _cov_tree(tmp_path, _ELABORATED_RECORDS)
    monkeypatch.chdir(project)
    return project


def _write_cov_artefacts(project: Path, raw: Path, *, cov_dir: Path | None = None):
    """Write ``coverage-model.json`` + ``manifest.json`` for one run."""
    cov_dir = cov_dir or project / "verif" / "blk_a" / "cov_dir"
    model = build_model(
        [
            TestArtefacts(
                name="t_basic",
                raw=str(raw),
                suite="verif/blk_a/tests.yaml",
                source_roots=(str(raw.parent), str(project)),
            )
        ],
        project_root=project,
        simulator="verilator",
    )
    model_path = write_model(model, cov_dir)
    write_manifest(
        build_manifest(
            project_root=project,
            cov_dir=cov_dir,
            command="test",
            suite=str(project / "verif" / "blk_a" / "tests.yaml"),
            builder="verilator",
            simulator_family="verilator",
            model_path=model_path,
            totals=model["totals"],
        ),
        cov_dir,
    )
    return cov_dir


def _config_graph(project: Path) -> dict:
    from rtl_buddy.graph import build_config_tier

    return build_config_tier(project)


def _design_graph(project: Path) -> dict:
    """The config tier plus the module and instance nodes a design tier would
    contribute."""
    graph = _config_graph(project)
    for name in ("blk_a", "blk_b"):
        graph["nodes"].append(
            {
                "id": f"module:{name}",
                "type": "module",
                "label": name,
                "tier": "design",
                "file": f"design/{name}/{name}.sv",
            }
        )
        graph["nodes"].append(
            {
                "id": f"inst:{name}/{name}",
                "type": "instance",
                "label": name,
                "tier": "design",
                "module": name,
            }
        )
    return graph


def _join(project: Path, *, graph: dict | None = None, **kwargs):
    entries = collect_results(project, coverage=False).entries
    return join_coverage(project, entries=entries, graph=graph, **kwargs)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_graph_coverage")


# The join itself


def test_no_coverage_artefacts_is_not_an_error(tmp_path: Path):
    """A tree that never ran coverage has no coverage block and no problem."""
    join = join_coverage(tmp_path, entries={}, graph=None)

    assert join.block is None
    assert join.available() is False
    assert join.problems == []


def test_a_named_cov_dir_that_is_not_there_is_a_problem(tmp_path: Path):
    """A named cov dir that does not exist is a problem."""
    join = join_coverage(tmp_path, entries={}, cov_dir=tmp_path / "nope")

    assert join.block is None
    assert len(join.problems) == 1
    assert join.problems[0]["scope"] == "coverage"


def _break_the_model(project: Path) -> Path:
    """Leave a manifest that loads with a model of the wrong shape."""
    model_path = project / "verif" / "blk_a" / "cov_dir" / "coverage-model.json"
    assert model_path.exists(), "the cov fixture moved"
    model_path.write_text(
        json.dumps({"files": [{"cover": [{"name": "cov_a_cov_1", "hits": 1}]}]}),
        encoding="utf-8",
    )
    return model_path


def test_a_broken_model_degrades_to_a_problem_row(cov_project: Path):
    """A broken model degrades to a problem row instead of a traceback."""
    _break_the_model(cov_project)

    overlay = refresh_results_overlay(cov_project)

    coverage_problems = [p for p in overlay.problems if p.get("scope") == "coverage"]
    assert len(coverage_problems) == 1
    assert coverage_problems[0]["error"]
    # The rest of the overlay still landed.
    assert overlay.entries
    assert "coverage" not in overlay.overlay


def test_a_broken_model_still_fails_under_strict(cov_project: Path):
    """A broken model still exits non-zero under `--strict`."""
    runner, rb = _runner()
    assert (
        runner.invoke(
            rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
        ).exit_code
        == 0
    )
    _break_the_model(cov_project)

    ok = runner.invoke(rb.app, ["graph", "results"])
    assert ok.exit_code == 0, ok.output
    assert "Traceback" not in ok.output

    strict = runner.invoke(rb.app, ["graph", "results", "--strict"])
    assert strict.exit_code == 1, strict.output


def test_per_test_scalars_are_keyed_by_test_node_id(cov_project: Path):
    join = _join(cov_project, graph=_config_graph(cov_project))

    scalars = join.per_test["test:verif/blk_a#t_basic"]
    # 3 line points across two files, 2 hit.
    assert scalars["totals"]["line"] == {"found": 3, "hit": 2, "ratio": 2 / 3}
    assert scalars["raw"] == "verif/blk_a/artefacts/t_basic/coverage.dat"
    assert join.block["summary"]["tests"] == 1
    assert join.block["summary"]["unjoined_tests"] == []


def test_a_declared_item_whose_cover_fired_is_exercised(cov_project: Path):
    join = _join(cov_project, graph=_config_graph(cov_project))

    item = join.block["nodes"]["covitem:blk_a#A-COV-1"]
    assert item["status"] == STATUS_EXERCISED
    assert item["hits"] == 3
    assert item["hit_by"] == ["t_basic"]
    # Both tests declare it; only one has a result.
    assert item["declared_by"] == [
        "test:verif/blk_a#t_basic",
        "test:verif/blk_a#t_cocotb",
    ]
    assert item["declared_by_status"] == {"PASS": 1, "UNKNOWN": 1}


def test_a_cover_that_never_fired_is_declared_only_but_says_it_exists(
    cov_project: Path,
):
    """A cover that never fired is declared-only but exists in the RTL."""
    join = _join(cov_project, graph=_config_graph(cov_project))
    nodes = join.block["nodes"]

    never_fired = nodes["covitem:blk_a#A-COV-2"]
    no_such_cover = nodes["covitem:blk_a#SHARED-COV"]

    assert never_fired["status"] == no_such_cover["status"] == STATUS_DECLARED_ONLY
    assert never_fired["hits"] == 0
    # One has a cover point in the RTL and the other does not.
    assert [o["name"] for o in never_fired["observed"]] == ["A-COV-2"]
    assert no_such_cover["observed"] == []


def test_a_cover_point_nothing_declares_is_reported_not_dropped(cov_project: Path):
    join = _join(cov_project, graph=_config_graph(cov_project))

    (undeclared,) = join.block["undeclared"]
    assert undeclared["status"] == STATUS_OBSERVED_UNDECLARED
    assert undeclared["name"] == "stray_cover"
    assert undeclared["module"] == "blk_b"
    assert undeclared["hits"] == 7
    assert undeclared["hit_by"] == ["t_basic"]
    assert join.block["summary"][STATUS_OBSERVED_UNDECLARED] == 1


@pytest.mark.parametrize(
    "label,tier",
    [
        ("A-COV-1", "exact"),
        ("a-cov-1", "nocase"),
        ("A_COV_1", "normalized"),
        ("cov_a_cov_1", "affix"),
        ("tb_top.cov_A_COV_1", "affix"),
    ],
)
def test_the_match_ladder_records_the_rung_it_came_off(
    cov_project: Path, label: str, tier: str
):
    """The match ladder records the rung a correlation came off."""
    raw = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic" / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_record(
            file="../../../../" + _BLK_A,
            line=4,
            type_="user",
            name=label,
            module="blk_a",
            hits=2,
        ),
        encoding="utf-8",
    )
    _write_cov_artefacts(cov_project, raw)

    join = _join(cov_project, graph=_config_graph(cov_project))

    item = join.block["nodes"]["covitem:blk_a#A-COV-1"]
    assert item["status"] == STATUS_EXERCISED
    assert [o["match"] for o in item["observed"]] == [tier]


def test_an_id_two_blocks_declare_is_matched_on_both(cov_project: Path):
    """An id declared by two blocks is matched on both."""
    raw = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic" / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_record(
            file="../../../../" + _BLK_A,
            line=8,
            type_="user",
            name="SHARED-COV",
            module="blk_a",
            hits=1,
        ),
        encoding="utf-8",
    )
    _write_cov_artefacts(cov_project, raw)

    nodes = _join(cov_project, graph=_config_graph(cov_project)).block["nodes"]

    assert nodes["covitem:blk_a#SHARED-COV"]["status"] == STATUS_EXERCISED
    assert nodes["covitem:blk_b#SHARED-COV"]["status"] == STATUS_EXERCISED


def test_module_ratios_reach_modules_instances_and_models(cov_project: Path):
    join = _join(cov_project, graph=_design_graph(cov_project))
    nodes = join.block["nodes"]

    assert nodes["module:blk_a"]["ratio"] == 0.5
    assert nodes["module:blk_b"]["ratio"] == 1.0
    # An instance carries its module's answer...
    assert nodes["inst:blk_a/blk_a"]["ratio"] == 0.5
    # ...and so does the model node, because `maps_to` is an identity.
    assert nodes["model:design/blk_a/models.yaml#blk_a"]["ratio"] == 0.5
    assert nodes["module:blk_a"]["files"] == [_BLK_A]
    assert nodes["module:blk_a"]["tests"] == ["t_basic"]
    assert join.block["summary"]["unmatched_modules"] == []


def test_a_module_with_no_design_node_still_reports_and_says_so(cov_project: Path):
    """A module with no design node is still reported, unmatched, under the id a design
    tier would emit."""
    join = _join(cov_project, graph=None)

    assert join.block["nodes"]["module:blk_a"]["ratio"] == 0.5
    assert join.block["summary"]["unmatched_modules"] == ["blk_a", "blk_b"]


def test_a_config_only_graph_still_reaches_the_design_through_its_model(
    cov_project: Path,
):
    """A `--no-design` graph reaches the design through its `model:` node."""
    join = _join(cov_project, graph=_config_graph(cov_project))

    assert join.block["nodes"]["model:design/blk_a/models.yaml#blk_a"]["ratio"] == 0.5
    assert join.block["summary"]["unmatched_modules"] == []


# Elaborated model names vs source graph names


@pytest.mark.parametrize(
    "elaborated,base",
    [
        ("ip_async_fifo__DB13", "ip_async_fifo"),
        ("ip_cdc_handshake__Wc", "ip_cdc_handshake"),
        ("apb_intf__A8", "apb_intf"),
        # Only the last group goes.
        ("demo_tiny_alu_subsys_top__Az1", "demo_tiny_alu_subsys_top"),
        ("a__b__c", "a__b"),
        # Nothing survives, so nothing is stripped.
        ("__A8", "__A8"),
        ("ip_cdc_sync", "ip_cdc_sync"),
        # The suffix is alphanumeric; a trailing `__` with punctuation is not one.
        ("blk__a-1", "blk__a-1"),
    ],
)
def test_base_module_name_strips_one_parameterisation_suffix(elaborated, base):
    """The base module name drops one parameterisation suffix, matching
    `cov_page.html`'s `module-names` block."""
    assert base_module_name(elaborated) == base


def test_parameterised_modules_join_onto_the_source_named_nodes(
    elaborated_cov_project: Path,
):
    """`blk_a__W1` joins onto `module:blk_a`."""
    join = _join(elaborated_cov_project, graph=_design_graph(elaborated_cov_project))
    nodes = join.block["nodes"]

    assert join.block["summary"]["unmatched_modules"] == []
    assert nodes["module:blk_a"]["module"] == "blk_a"
    assert nodes["module:blk_a"]["ratio"] == 0.5
    # Every node that is that module comes with it.
    assert nodes["inst:blk_a/blk_a"]["ratio"] == 0.5
    assert nodes["model:design/blk_a/models.yaml#blk_a"]["ratio"] == 0.5


def test_several_elaborations_aggregate_onto_the_one_node(
    elaborated_cov_project: Path,
):
    """Several elaborations aggregate onto one node, counting module-less line points
    once."""
    nodes = _join(
        elaborated_cov_project, graph=_design_graph(elaborated_cov_project)
    ).block["nodes"]
    entry = nodes["module:blk_a"]

    assert entry["elaborations"] == ["blk_a__W1", "blk_a__Wc"]
    # Both elaborations recorded both line points: two, not four.
    assert entry["totals"]["line"] == {"found": 2, "hit": 1, "ratio": 0.5}
    # A cover property is a point per elaboration.
    assert entry["totals"]["cover"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert entry["files"] == [_BLK_A]
    assert entry["tests"] == ["t_basic"]


def test_a_plain_elaboration_shares_the_node_with_its_parameterised_twin(
    elaborated_cov_project: Path,
):
    """`blk_b` and `blk_b__W4` share one node."""
    nodes = _join(
        elaborated_cov_project, graph=_design_graph(elaborated_cov_project)
    ).block["nodes"]

    assert nodes["module:blk_b"]["elaborations"] == ["blk_b", "blk_b__W4"]
    assert nodes["module:blk_b"]["totals"]["line"] == {
        "found": 2,
        "hit": 1,
        "ratio": 0.5,
    }


def test_an_exact_name_beats_a_stripped_one():
    """An exact module name such as `axi__lite` beats a stripped one."""
    model = {"modules": {"axi": [], "axi__lite": []}, "files": []}
    graph = {
        "nodes": [
            {"id": "module:axi", "type": "module"},
            {"id": "module:axi__lite", "type": "module"},
        ],
        "links": [],
    }

    attached, unmatched = _design_entries(model, graph)

    assert unmatched == []
    assert attached["module:axi"]["elaborations"] == ["axi"]
    assert attached["module:axi__lite"]["elaborations"] == ["axi__lite"]


def test_a_stripped_name_that_matches_nothing_stays_unmatched():
    """A stripped name that matches no node stays unmatched, reported under the
    elaborated name."""
    model = {"modules": {"tb_top__A1": [], "blk_a__W1": []}, "files": []}
    graph = {"nodes": [{"id": "module:blk_a", "type": "module"}], "links": []}

    attached, unmatched = _design_entries(model, graph)

    assert unmatched == ["tb_top__A1"]
    assert attached["module:tb_top__A1"]["module"] == "tb_top__A1"
    assert attached["module:blk_a"]["elaborations"] == ["blk_a__W1"]


def test_the_module_ratio_is_the_one_rb_cov_module_reports(cov_project: Path):
    """The module ratio is the one `rb cov module` reports."""
    from rtl_buddy.cov.query import load_context as load_cov, module_payload

    join = _join(cov_project, graph=_design_graph(cov_project))
    verb = module_payload(load_cov(cov_project), "blk_a")

    assert join.block["nodes"]["module:blk_a"]["totals"] == verb["totals"]


# The overlay carries it and stays byte-stable


def test_the_overlay_carries_the_block_and_the_per_test_scalars(cov_project: Path):
    overlay = refresh_results_overlay(cov_project)
    payload = json.loads(overlay.path.read_text())

    assert payload["coverage"]["schema_version"] == 1
    assert payload["coverage"]["manifest"] == "verif/blk_a/cov_dir/manifest.json"
    assert payload["summary"]["coverage"] == payload["coverage"]["summary"]
    entry = payload["tests"]["test:verif/blk_a#t_basic"]
    assert entry["coverage"]["totals"]["line"]["hit"] == 2
    # Beside the artefact path.
    assert entry["artefacts"]["coverage"].endswith("coverage.dat")


def test_refresh_is_byte_stable_when_nothing_re_ran(cov_project: Path):
    """A refresh is byte-stable when nothing re-ran."""
    first = refresh_results_overlay(cov_project).path.read_bytes()
    second = refresh_results_overlay(cov_project).path.read_bytes()

    assert first == second
    assert b'"coverage"' in first


def test_the_coverage_join_leaves_graph_json_hash_stable(cov_project: Path):
    runner, rb = _runner()
    built = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    assert built.exit_code == 0, built.output
    graph_json = cov_project / "artefacts" / "graph" / "graph.json"
    meta_json = cov_project / "artefacts" / "graph" / "graph-meta.json"
    before, meta_before = _sha256(graph_json), _sha256(meta_json)

    assert runner.invoke(rb.app, ["graph", "results"]).exit_code == 0

    assert _sha256(graph_json) == before
    assert _sha256(meta_json) == meta_before
    overlay = json.loads(
        (cov_project / "artefacts" / "graph" / "results-overlay.json").read_text()
    )
    assert overlay["coverage"]["summary"][STATUS_EXERCISED] == 1


def test_no_coverage_flag_leaves_the_block_out_entirely(cov_project: Path):
    """`--no-coverage` leaves the coverage block out entirely."""
    payload = json.loads(
        refresh_results_overlay(cov_project, coverage=False).path.read_text()
    )

    assert "coverage" not in payload
    assert "coverage" not in payload["summary"]
    assert "coverage" not in payload["tests"]["test:verif/blk_a#t_basic"]


# The read side: query verbs, CLI, pane


def test_explain_answers_the_question_the_issue_asks(cov_project: Path):
    """Explain reports whether a spec item is exercised, by which tests, and whether
    they passed."""
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    refresh_results_overlay(cov_project)

    payload = explain(load_context(cov_project), "covitem:blk_a#A-COV-1")

    assert payload["coverage"]["status"] == STATUS_EXERCISED
    assert payload["coverage"]["hit_by"] == ["t_basic"]
    assert payload["coverage"]["declared_by_status"] == {"PASS": 1, "UNKNOWN": 1}
    # The manifest the numbers came from rides along, without its maps.
    assert payload["coverage_run"]["manifest"].endswith("manifest.json")
    assert "nodes" not in payload["coverage_run"]


def test_explain_prints_the_verdict_and_names_the_run(cov_project: Path):
    """Explain prints the verdict and names the run."""
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    runner.invoke(rb.app, ["graph", "results"])

    exercised = runner.invoke(rb.app, ["graph", "explain", "covitem:blk_a#A-COV-1"])
    declared = runner.invoke(rb.app, ["graph", "explain", "covitem:blk_a#SHARED-COV"])
    module = runner.invoke(
        rb.app, ["graph", "explain", "model:design/blk_a/models.yaml#blk_a"]
    )

    assert exercised.exit_code == 0, exercised.output
    # The match rung must survive the console; Rich would read `[affix]` as a style tag.
    assert (
        f"cov:    {STATUS_EXERCISED} (3 hit(s); cov_a_cov_1 ×3 [affix])"
        in exercised.output
    )
    assert "verif/blk_a/cov_dir/manifest.json" in exercised.output
    # An item with no cover point says so instead of printing an empty parenthesis.
    assert f"cov:    {STATUS_DECLARED_ONLY} (0 hit(s)" in declared.output
    assert "no cover point in the model" in declared.output
    assert "cov:    50.0% line (blk_a)" in module.output


def test_explain_prints_a_bracketed_cover_label_verbatim(cov_project: Path):
    """Explain prints a bracketed cover label such as `[0]` verbatim, as plain text."""
    raw = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic" / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + "".join(
            _dat_record(
                file=path,
                line=line,
                type_=type_,
                name="gen[0].cov_a_cov_1" if name == "cov_a_cov_1" else name,
                module=module,
                hits=hits,
            )
            for path, line, type_, name, module, hits in _RECORDS
        ),
        encoding="utf-8",
    )
    _write_cov_artefacts(cov_project, raw)
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    runner.invoke(rb.app, ["graph", "results"])

    result = runner.invoke(rb.app, ["graph", "explain", "covitem:blk_a#A-COV-1"])

    assert result.exit_code == 0, result.output
    assert "gen[0].cov_a_cov_1 ×3 [affix]" in result.output


def test_explain_says_nothing_about_coverage_when_there_is_none(cov_project: Path):
    """Explain prints no coverage lines for a node the join knows nothing about."""
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    runner.invoke(rb.app, ["graph", "results"])

    result = runner.invoke(rb.app, ["graph", "explain", "spec:blk_a"])

    assert result.exit_code == 0, result.output
    assert "cov:" not in result.output


def test_test_status_carries_the_per_test_scalars(cov_project: Path):
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    refresh_results_overlay(cov_project)

    payload = query_test_status(load_context(cov_project))

    assert payload["with_coverage"] == 1
    (entry,) = [e for e in payload["tests"] if e["test"] == "t_basic"]
    assert entry["coverage"]["totals"]["line"]["found"] == 3
    assert payload["coverage_run"]["run_command"] == "test"


def test_cli_reports_the_join_on_both_surfaces(cov_project: Path):
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )

    human = runner.invoke(rb.app, ["graph", "results"])
    machine = runner.invoke(rb.app, ["--machine", "graph", "results"])

    assert human.exit_code == 0, human.output
    assert "1/4 spec item(s) exercised" in human.output
    payload = json.loads(machine.output.strip().splitlines()[-1])["payload"]
    assert payload["coverage"][STATUS_EXERCISED] == 1
    assert payload["coverage"][STATUS_OBSERVED_UNDECLARED] == 1


def test_cli_no_coverage_reports_nothing(cov_project: Path):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "graph", "results", "--no-coverage"])

    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert payload["coverage"] is None


def test_cli_cov_dir_selects_the_run_to_join(cov_project: Path, tmp_path: Path):
    """`--cov-dir` picks which of two runs the graph reports."""
    raw = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic" / "coverage.dat"
    other = _write_cov_artefacts(
        cov_project,
        raw,
        cov_dir=cov_project / "verif" / "blk_a" / "old_cov" / "cov_dir",
    )
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "graph", "results", "--cov-dir", str(other)]
    )

    assert result.exit_code == 0, result.output
    overlay = json.loads(
        (cov_project / "artefacts" / "graph" / "results-overlay.json").read_text()
    )
    assert (
        overlay["coverage"]["manifest"] == "verif/blk_a/old_cov/cov_dir/manifest.json"
    )


def test_annotate_coverage_attaches_without_touching_the_file(cov_project: Path):
    from rtl_buddy.graph.results import load_overlay

    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    refresh_results_overlay(cov_project)
    graph_json = cov_project / "artefacts" / "graph" / "graph.json"
    before = _sha256(graph_json)
    graph = json.loads(graph_json.read_text())
    overlay = load_overlay(cov_project)

    annotated = annotate_coverage(graph, overlay)

    nodes = {n["id"]: n for n in graph["nodes"]}
    assert annotated == sum(1 for n in graph["nodes"] if "coverage" in n)
    assert nodes["model:design/blk_a/models.yaml#blk_a"]["coverage"]["ratio"] == 0.5
    assert nodes["covitem:blk_a#A-COV-1"]["coverage"]["status"] == STATUS_EXERCISED
    # A node the join says nothing about stays untouched...
    assert "coverage" not in nodes["spec:blk_a"]
    assert coverage_for_node(overlay, "spec:blk_a") is None
    assert coverage_for_node(None, "module:blk_a") is None
    # ...and the file on disk is not annotated.
    assert _sha256(graph_json) == before


def test_the_pane_payload_carries_the_same_join(cov_project: Path):
    """The pane payload carries the same join as the verbs."""
    runner, rb = _runner()
    runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    refresh_results_overlay(cov_project)

    payload = graph_page.build_graph_payload(cov_project)

    hub = payload["graph"]["hub"]
    assert hub["counts"]["with_coverage"] >= 1
    assert hub["coverage"]["summary"][STATUS_EXERCISED] == 1
    # Header only: the per-node map would double the body.
    assert "nodes" not in hub["coverage"]
    assert hub["coverage"]["undeclared"][0]["name"] == "stray_cover"
    assert hub["item_statuses"] == [
        STATUS_EXERCISED,
        STATUS_DECLARED_ONLY,
        STATUS_OBSERVED_UNDECLARED,
    ]
    item = [n for n in payload["nodes"] if n["id"] == "covitem:blk_a#A-COV-1"][0]
    assert item["coverage"]["status"] == STATUS_EXERCISED


def test_the_pane_renders_the_ramp_from_the_shared_tokens(cov_project: Path):
    """The pane renders the ramp from the shared tokens."""
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")

    assert "hsl(var(--h), var(--tint-s), var(--cov-l))" in body
    assert "var(--cov-none)" in body
    for status in (STATUS_EXERCISED, STATUS_DECLARED_ONLY, STATUS_OBSERVED_UNDECLARED):
        assert f"'{status}'" in body, status
    # Fallback tokens for a sheet that 404s.
    for token in ("--cov-l:", "--cov-none:", "--tint-s:"):
        assert token in body, token


# Manifest-less sources: per-test raw databases and a merged .info


def _drop_manifest(project: Path) -> None:
    shutil.rmtree(project / "verif" / "blk_a" / "cov_dir")


def test_auto_falls_back_to_the_per_test_raw_databases(cov_project: Path):
    """With no manifest, `auto` synthesizes a model from each test's raw `coverage.dat`."""
    _drop_manifest(cov_project)

    join = _join(cov_project, graph=_design_graph(cov_project), source="auto")

    assert join.available()
    assert join.problems == []
    assert join.block["source"] == "artefacts"
    assert join.block["manifest"] is None
    scalars = join.per_test["test:verif/blk_a#t_basic"]
    assert scalars["totals"]["line"] == {"found": 3, "hit": 2, "ratio": 2 / 3}
    nodes = join.block["nodes"]
    assert nodes["module:blk_a"]["ratio"] == 0.5
    assert nodes["covitem:blk_a#A-COV-1"]["status"] == STATUS_EXERCISED


def test_model_source_never_falls_back(cov_project: Path):
    """`--coverage model` never falls back to raw databases."""
    _drop_manifest(cov_project)

    join = _join(cov_project, graph=_design_graph(cov_project), source="model")

    assert join.block is None
    assert join.problems == []


def test_the_artefact_fallback_is_byte_stable(cov_project: Path):
    _drop_manifest(cov_project)

    first = refresh_results_overlay(cov_project).path.read_bytes()
    second = refresh_results_overlay(cov_project).path.read_bytes()

    assert first == second
    assert json.loads(first)["coverage"]["source"] == "artefacts"


def _write_info(path: Path, blocks: list[tuple[str, list[str]]]) -> Path:
    """One LCOV .info: ``blocks`` is ``[(SF path, [records...])]``."""
    text = "TN:\n"
    for sf, records in blocks:
        text += f"SF:{sf}\n" + "".join(f"{r}\n" for r in records) + "end_of_record\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_an_explicit_info_joins_module_heat_by_file(cov_project: Path):
    """An explicit .info joins module heat by file; its test-workspace-relative SF paths
    are absolutized against the .info's directory."""
    run_dir = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic"
    info = _write_info(
        run_dir / "merged.info",
        [
            (
                "../../../../" + _BLK_A,
                ["DA:1,1", "DA:2,0", "BRDA:3,0,0,1", "BRDA:3,0,1,-"],
            ),
            ("../../../../" + _BLK_B, ["DA:5,2"]),
        ],
    )

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    assert join.problems == []
    assert join.block["source"] == "info"
    assert join.block["info"] == "verif/blk_a/artefacts/t_basic/merged.info"
    nodes = join.block["nodes"]
    entry = nodes["module:blk_a"]
    assert entry["joined_by"] == "file"
    assert entry["ratio"] == 0.5
    assert entry["totals"]["branch"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert entry["files"] == [_BLK_A]
    assert nodes["module:blk_b"]["ratio"] == 1.0
    # The fan-out the model join does: instances and the model alias.
    assert nodes["inst:blk_a/blk_a"]["ratio"] == 0.5
    assert join.block["totals"]["line"] == {"found": 3, "hit": 2, "ratio": 2 / 3}
    assert join.block["summary"]["matched_files"] == 2


def test_an_explicit_info_still_badges_tests_and_items_from_the_dats(
    cov_project: Path,
):
    """An explicit .info still badges tests and items from the per-test raw databases."""
    _drop_manifest(cov_project)
    info = _write_info(
        cov_project / "merged.info",
        [(_BLK_A, ["DA:1,1", "DA:2,0"])],
    )

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    scalars = join.per_test["test:verif/blk_a#t_basic"]
    assert scalars["totals"]["line"] == {"found": 3, "hit": 2, "ratio": 2 / 3}
    assert join.block["nodes"]["covitem:blk_a#A-COV-1"]["status"] == STATUS_EXERCISED
    # The named file stays authoritative for design heat.
    assert join.block["nodes"]["module:blk_a"]["joined_by"] == "file"
    assert "module:blk_b" not in join.block["nodes"]


def test_an_info_without_dats_has_no_item_verdicts(cov_project: Path):
    """An .info without raw databases has no item verdicts, rather than a false
    `declared-only`."""
    _drop_manifest(cov_project)
    run_dir = cov_project / "verif" / "blk_a" / "artefacts" / "t_basic"
    (run_dir / "coverage.dat").unlink()
    info = _write_info(cov_project / "merged.info", [(_BLK_A, ["DA:1,1"])])

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    assert join.per_test == {}
    assert join.block["summary"]["items"] == 4
    assert join.block["summary"][STATUS_EXERCISED] == 0
    # `items` counts declared items; `items_scored` counts those the source could reach
    # a verdict on.
    assert join.block["summary"]["items_scored"] == 0
    assert not any(k.startswith("covitem:") for k in join.block["nodes"])
    assert join.block["nodes"]["module:blk_a"]["ratio"] == 1.0


def test_items_are_scored_when_the_dats_are_there(cov_project: Path):
    """With cover points present, every declared item is scored."""
    info = _write_info(cov_project / "merged.info", [(_BLK_A, ["DA:1,1"])])

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    summary = join.block["summary"]
    assert summary["items_scored"] == summary["items"] == 4


def test_a_bare_basename_under_the_root_is_never_evidence(cov_project: Path):
    """A bare basename under the root is never evidence; the SF trimming walk stops
    before it."""
    (cov_project / "blk_c.sv").write_text("module blk_c; endmodule\n")
    graph = _design_graph(cov_project)
    graph["nodes"].append(
        {
            "id": "module:blk_c",
            "type": "module",
            "label": "blk_c",
            "tier": "design",
            "file": "blk_c.sv",
        }
    )
    info = _write_info(
        cov_project / "merged.info",
        [(_BLK_A, ["DA:1,1"]), ("verif/other_suite/blk_c.sv", ["DA:9,7"])],
    )

    join = _join(cov_project, graph=graph, source=str(info))

    assert "module:blk_c" not in join.block["nodes"]
    assert join.block["summary"]["unresolved_files"] == ["verif/other_suite/blk_c.sv"]
    assert any("duplicate-basename" in p["error"] for p in join.problems)


def test_a_re_anchored_sf_is_recorded_as_inferred(cov_project: Path):
    """A re-anchored SF path is recorded as inferred."""
    info = _write_info(
        cov_project / "merged.info",
        [("other_elab/" + _BLK_A, ["DA:1,1", "DA:2,0"]), (_BLK_B, ["DA:5,2"])],
    )

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    assert join.block["nodes"]["module:blk_a"]["ratio"] == 0.5
    summary = join.block["summary"]
    assert summary["reanchored_files"] == ["other_elab/" + _BLK_A]
    # A record that needed no trimming is not listed.
    assert summary["matched_files"] == 2


def test_a_wrong_elaborations_sf_set_is_reported_not_attributed(cov_project: Path):
    """A wrong-elaboration SF set attributes to nothing and the join says why."""
    info = _write_info(
        cov_project / "merged.info",
        [("verif/other_suite/blk_a.sv", ["DA:1,1"])],
    )

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    assert join.block["nodes"] == {} or not any(
        v.get("kind") == "design" for v in join.block["nodes"].values()
    )
    (problem,) = join.problems
    assert "duplicate-basename" in problem["error"]
    assert "nothing was attributed" in problem["error"]


def test_a_suspect_sf_beside_good_ones_is_flagged_but_not_joined(cov_project: Path):
    info = _write_info(
        cov_project / "merged.info",
        [
            (_BLK_A, ["DA:1,1", "DA:2,0"]),
            ("verif/other_suite/blk_b.sv", ["DA:5,2"]),
        ],
    )

    join = _join(cov_project, graph=_design_graph(cov_project), source=str(info))

    nodes = join.block["nodes"]
    assert nodes["module:blk_a"]["ratio"] == 0.5
    assert "module:blk_b" not in nodes
    assert join.block["summary"]["unresolved_files"] == ["verif/other_suite/blk_b.sv"]
    (problem,) = join.problems
    assert "verif/other_suite/blk_b.sv" in problem["error"]
    assert "duplicate-basename" in problem["error"]


def test_an_info_against_a_graph_with_no_design_files_says_so(cov_project: Path):
    """An .info against a `--no-design` graph produces a problem row."""
    info = _write_info(cov_project / "merged.info", [(_BLK_A, ["DA:1,1"])])

    join = _join(cov_project, graph=_config_graph(cov_project), source=str(info))

    (problem,) = join.problems
    assert "no design-tier module files" in problem["error"]
    # The per-test scalars still joined.
    assert join.per_test["test:verif/blk_a#t_basic"]["totals"]["line"]["found"] == 3


def test_a_missing_info_path_is_a_problem(cov_project: Path):
    join = _join(cov_project, source=str(cov_project / "nope.info"))

    assert join.block is None
    (problem,) = join.problems
    assert "no .info file" in problem["error"]


def test_a_qualified_duplicate_module_stays_on_its_own_node(cov_project: Path):
    """A qualified duplicate module (`module:tb_top@verif/x`) stays on its own node."""
    graph = _design_graph(cov_project)
    graph["nodes"].append(
        {
            "id": "module:blk_a@verif/other",
            "type": "module",
            "label": "blk_a(1)",
            "tier": "design",
            "file": "verif/other/blk_a.sv",
        }
    )
    info = _write_info(cov_project / "merged.info", [(_BLK_A, ["DA:1,1", "DA:2,0"])])

    join = _join(cov_project, graph=graph, source=str(info))

    nodes = join.block["nodes"]
    assert nodes["module:blk_a"]["ratio"] == 0.5
    assert "module:blk_a@verif/other" not in nodes
    # The name is ambiguous, so instances are not guessed.
    assert "inst:blk_a/blk_a" not in nodes


def test_cli_coverage_source_flag_end_to_end(cov_project: Path):
    """`rb graph results --coverage <info|auto|none>` works end to end and leaves
    graph.json byte-identical."""
    runner, rb = _runner()
    built = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    assert built.exit_code == 0, built.output
    graph_json = cov_project / "artefacts" / "graph" / "graph.json"
    before = _sha256(graph_json)
    overlay_path = cov_project / "artefacts" / "graph" / "results-overlay.json"
    info = _write_info(cov_project / "merged.info", [(_BLK_A, ["DA:1,1"])])

    named = runner.invoke(rb.app, ["graph", "results", "--coverage", str(info)])
    assert named.exit_code == 0, named.output
    assert "coverage (from info):" in named.output
    payload = json.loads(overlay_path.read_text())
    assert payload["coverage"]["source"] == "info"
    assert payload["coverage"]["info"] == "merged.info"

    _drop_manifest(cov_project)
    auto = runner.invoke(rb.app, ["graph", "results", "--coverage", "auto"])
    assert auto.exit_code == 0, auto.output
    assert "coverage (from artefacts):" in auto.output
    assert json.loads(overlay_path.read_text())["coverage"]["source"] == "artefacts"

    off = runner.invoke(rb.app, ["graph", "results", "--coverage", "none"])
    assert off.exit_code == 0, off.output
    assert "coverage" not in json.loads(overlay_path.read_text())

    assert _sha256(graph_json) == before


def test_the_no_coverage_flag_survives_the_source_rework(cov_project: Path):
    """`--no-coverage` works alone; a bare `--coverage` without a value is rejected."""
    runner, rb = _runner()
    built = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    assert built.exit_code == 0, built.output
    overlay_path = cov_project / "artefacts" / "graph" / "results-overlay.json"

    bare = runner.invoke(rb.app, ["graph", "results", "--coverage"])
    assert bare.exit_code != 0
    assert "requires an argument" in bare.output

    off = runner.invoke(rb.app, ["graph", "results", "--no-coverage"])
    assert off.exit_code == 0, off.output
    assert "coverage" not in json.loads(overlay_path.read_text())


def test_an_undocumented_source_keyword_is_read_as_a_path(cov_project: Path):
    """A keyword outside the three the help lists is read as a path and fails as one."""
    runner, rb = _runner()
    built = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", "--no-bind"]
    )
    assert built.exit_code == 0, built.output

    result = runner.invoke(
        rb.app, ["graph", "results", "--coverage", "off", "--strict"]
    )

    assert result.exit_code != 0
    assert "no .info file" in result.output
