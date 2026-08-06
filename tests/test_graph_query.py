"""Tests for #380 — the ``rb graph query`` / ``path`` / ``explain`` verbs.

The graph only earns its keep if an agent can interrogate it more
cheaply than reading the tree, which means the answers have to be
*complete* (context attached), *stable* (byte-identical across runs) and
*citable* (file, line, and the command that quotes them).

What these tests pin:

* the acceptance criterion — "which tests cover coverage-item X and what
  is their last status" is one command, with the results overlay joined
  onto the neighbours;
* matching is deterministic keyword scoring: type words steer but never
  filter out the node the question is really about, and ties break on
  the node id so two runs agree;
* ``path`` is undirected by default (edge direction encodes role, not
  reachability) and reports every shortest path it was asked for;
* ``explain`` resolves both edge directions and hands back the
  ``rb hier-query ... source-snippet`` citation for an instance node;
* an unknown or ambiguous node reference fails loudly with candidates,
  never by silently answering about a different node;
* the ``--machine`` envelopes carry the payload the MCP surface reuses.

The config-tier fixture is the graph under test, with a synthetic
design tier merged in where module/instance/port nodes are needed —
building a real design tier would need ``rtl-buddy-view`` on PATH, and
these verbs are indifferent to which tier produced a node.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.graph import query as graph_query
from rtl_buddy.graph.query import (
    GraphQueryError,
    explain,
    load_context,
    path as graph_path,
    query as run_query,
    resolve_node,
    # Renamed on import: pytest would collect `test_status` as a test.
    test_status as overlay_status,
    tokenize,
)
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import TestResults as _TestResults

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def graph_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


_LIVE: list[RtlBuddy] = []


def _runner() -> tuple[CliRunner, RtlBuddy]:
    """A fresh CLI object, with the previous one's artefact lock released.

    The lock is held for the whole process lifetime by design, so two
    ``RtlBuddy`` instances in one test would contend with each other
    rather than with a real concurrent run.
    """
    while _LIVE:
        _LIVE.pop()._artifact_locks.release_all()
    rb = RtlBuddy(name="test_graph_query")
    _LIVE.append(rb)
    return CliRunner(), rb


def _build(project: Path, *extra: str) -> Path:
    """Config-tier-only build; no viewer, no extractor, so it runs anywhere."""
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["graph", "build", "--no-design", "--no-extract", *extra]
    )
    assert result.exit_code == 0, result.output
    return project / "artefacts" / "graph" / "graph.json"


def _seed_run(project: Path, test: str, *, status: str = "PASS") -> None:
    """Write the result envelope a real run would leave behind."""
    directory = project / "verif" / "blk_a" / "artefacts" / test
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test.log").write_text(f"{status}\n")
    write_result_json(
        directory / "result.json",
        test_name=test,
        run_id=None,
        results=_TestResults(name=test, results={"result": status, "desc": "ok"}),
        run_token="tok0",
    )


def _refresh_results(project: Path) -> None:
    runner, rb = _runner()
    assert runner.invoke(rb.app, ["graph", "results"]).exit_code == 0


def _with_design_tier(graph_json: Path) -> None:
    """Splice a minimal design tier into a config-only graph.

    ``rb graph build`` would need ``rtl-buddy-view`` on PATH for this;
    the query verbs care only that the nodes exist and carry the
    contract's ids, so the cheap fake is the honest test.
    """
    graph = json.loads(graph_json.read_text())
    graph["nodes"] += [
        {
            "id": "module:blk_a",
            "type": "module",
            "label": "blk_a",
            "tier": "design",
            "file": "design/blk_a/blk_a.sv",
            "line": 1,
        },
        {
            "id": "inst:blk_a/u_sub",
            "type": "instance",
            "label": "u_sub",
            "tier": "design",
            "file": "design/blk_a/blk_a.sv",
            "line": 4,
        },
        {
            "id": "port:blk_a.clk",
            "type": "port",
            "label": "clk",
            "tier": "design",
            "dir": "input",
        },
    ]
    graph["links"] += [
        {
            "source": "inst:blk_a/u_sub",
            "target": "module:blk_a",
            "type": "instance_of",
            "confidence": "EXTRACTED",
        },
        {
            "source": "inst:blk_a/u_sub",
            "target": "port:blk_a.clk",
            "type": "connects",
            "confidence": "EXTRACTED",
            "formal": "clk",
            "actual": "clk",
        },
    ]
    graph_json.write_text(json.dumps(graph, indent=2))


# ---------------------------------------------------------------------------
# Tokenizing and scoring
# ---------------------------------------------------------------------------


def test_type_words_become_hints_not_search_terms():
    """ "which tests cover A-COV-1" must score on the identifier alone."""
    terms, hints = tokenize("which tests cover A-COV-1")

    assert "test" in hints
    assert "tests" not in terms and "which" not in terms
    assert "a-cov-1" in terms


def test_a_type_word_promotes_but_never_conjures_a_match(graph_project: Path):
    """A type preference reorders real matches; it never invents one."""
    _build(graph_project)
    ctx = load_context(graph_project)

    node = {"id": "test:verif/blk_a#t_basic", "type": "test", "label": "t_basic"}
    other = {"id": "tb:verif/blk_a#tb_hdl", "type": "testbench", "label": "tb_hdl"}

    assert graph_query.score_node(node, ["t_basic"], {"test"}) > graph_query.score_node(
        node, ["t_basic"], set()
    )
    # No term hit at all: the type word alone leaves the score at zero.
    assert graph_query.score_node(other, ["t_basic"], {"testbench"}) == 0
    assert ctx.index.node("test:verif/blk_a#t_basic") is not None


def test_matches_are_ordered_deterministically(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    first = run_query(ctx, "blk_a", limit=20)
    second = run_query(ctx, "blk_a", limit=20)

    assert [m["id"] for m in first["matches"]] == [m["id"] for m in second["matches"]]
    scores = [m["score"] for m in first["matches"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_one_query_answers_which_tests_cover_an_item_and_their_status(
    graph_project: Path,
):
    """#380's acceptance criterion, in one round-trip."""
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)

    ctx = load_context(graph_project)
    payload = run_query(ctx, "which tests cover A-COV-1")

    assert payload["matches"], payload
    match = payload["matches"][0]
    assert match["id"] == "covitem:blk_a#A-COV-1"

    covering = {
        neighbor["id"]: neighbor
        for neighbor in match["neighbors"]
        if neighbor["via"]["type"] == "covers"
    }
    assert set(covering) == {
        "test:verif/blk_a#t_basic",
        "test:verif/blk_a#t_cocotb",
    }
    # The overlay is joined onto the neighbour, so the status arrives
    # with the answer instead of costing a second command.
    assert covering["test:verif/blk_a#t_basic"]["results"]["status"] == "PASS"
    assert "results" not in covering["test:verif/blk_a#t_cocotb"]


def test_no_results_flag_drops_the_overlay_join(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic")
    _refresh_results(graph_project)

    ctx = load_context(graph_project, with_results=False)
    payload = run_query(ctx, "A-COV-1", results=False)

    assert payload["overlay"] is None
    for neighbor in payload["matches"][0]["neighbors"]:
        assert "results" not in neighbor


def test_query_without_an_overlay_still_answers(graph_project: Path):
    """A graph with no overlay is fully queryable — never an error."""
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "A-COV-1")

    assert ctx.overlay is None
    assert payload["matches"][0]["id"] == "covitem:blk_a#A-COV-1"


# ---------------------------------------------------------------------------
# Neighbourhood expansion
# ---------------------------------------------------------------------------


def test_expansion_follows_edges_in_both_directions(graph_project: Path):
    """ "which tests cover X" reads `covers` backwards — it must."""
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=1)
    directions = {n["via"]["direction"] for n in payload["matches"][0]["neighbors"]}

    assert directions == {"in"}
    payload_out = run_query(ctx, "test:verif/blk_a#t_basic", depth=1)
    assert {n["via"]["direction"] for n in payload_out["matches"][0]["neighbors"]} == {
        "out",
        "in",
    }


def test_depth_zero_returns_the_bare_match(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=0)

    assert payload["matches"][0]["neighbors"] == []


def test_depth_is_capped(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=99)

    assert payload["depth"] == graph_query.MAX_DEPTH


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------


def test_path_is_undirected_by_default(graph_project: Path):
    """Suite -> test -> testbench runs one way; the question does not."""
    _build(graph_project)
    ctx = load_context(graph_project)

    found = graph_path(ctx, "test:verif/blk_a#t_basic", "tb:verif/blk_a#tb_cocotb")

    assert found["found"]
    # Only reachable by walking `declares` backwards to the suite.
    assert found["length"] == 2
    assert found["paths"][0]["nodes"][1]["id"] == "suite:verif/blk_a"


def test_directed_path_respects_edge_direction(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    found = graph_path(
        ctx, "test:verif/blk_a#t_basic", "tb:verif/blk_a#tb_cocotb", directed=True
    )

    assert not found["found"]
    assert found["paths"] == []


def test_path_reports_the_edges_it_walked(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    found = graph_path(ctx, "test:verif/blk_a#t_basic", "covitem:blk_a#A-COV-1")

    assert found["length"] == 1
    links = found["paths"][0]["edges"][0]["links"]
    assert [link["type"] for link in links] == ["covers"]


def test_path_crosses_the_config_to_design_stitch(graph_project: Path):
    """The config->design stitch is the whole point of one id namespace."""
    graph_json = _build(graph_project)
    _with_design_tier(graph_json)
    ctx = load_context(graph_project)

    found = graph_path(ctx, "test:verif/blk_a#t_basic", "module:blk_a")

    assert found["found"]
    assert "module:blk_a" == found["paths"][0]["nodes"][-1]["id"]


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def test_explain_resolves_both_edge_directions_and_the_result(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="FAIL")
    _refresh_results(graph_project)
    ctx = load_context(graph_project)

    payload = explain(ctx, "test:verif/blk_a#t_basic")

    assert payload["results"]["status"] == "FAIL"
    assert payload["degree"]["out"]["runs_on"] == 1
    assert payload["degree"]["in"]["declares"] == 1
    peers = {edge["peer"] for edge in payload["outgoing"]}
    assert "tb:verif/blk_a#tb_hdl" in peers
    # Every edge carries the far endpoint already resolved, so an agent
    # never needs a second lookup to know what it is looking at.
    assert all(edge["node"].get("type") for edge in payload["outgoing"])


def test_explain_hands_back_a_runnable_source_snippet_command(graph_project: Path):
    """Locate in the graph, cite from source — the payload does half.

    ``-c`` comes from the config tier's ``maps_to`` edge — the *model*
    stitch. Without it the command only runs from the models.yaml's own
    directory, which is not a citation an agent invoked from the project
    root can use.
    """
    graph_json = _build(graph_project)
    _with_design_tier(graph_json)
    ctx = load_context(graph_project)

    payload = explain(ctx, "inst:blk_a/u_sub")

    assert payload["node"]["cite"]["command"] == (
        "rb hier-query blk_a source-snippet u_sub -c design/blk_a/models.yaml"
    )
    assert payload["node"]["cite"]["file"] == "design/blk_a/blk_a.sv"


def test_the_cite_command_omits_c_when_no_model_declares_the_module(
    graph_project: Path,
):
    """A design tier exported without a config tier still cites its file.

    Dropping only ``maps_to`` leaves the fixture's ``elaborates_as`` edge
    into ``module:blk_a`` standing, so this also pins the reason the
    stitch is three verbs and not one: a testbench points at the same
    module from a ``tests.yaml``, and a ``-c`` filled in from *that*
    would hand back a command that cannot run.
    """
    graph_json = _build(graph_project)
    _with_design_tier(graph_json)
    graph = json.loads(graph_json.read_text())
    graph["links"] = [link for link in graph["links"] if link["type"] != "maps_to"]
    graph_json.write_text(json.dumps(graph))
    ctx = load_context(graph_project)

    cite = explain(ctx, "inst:blk_a/u_sub")["node"]["cite"]

    assert cite["command"] == "rb hier-query blk_a source-snippet u_sub"
    assert cite["file"] == "design/blk_a/blk_a.sv"


def test_explain_keeps_the_node_attributes_it_did_not_summarize(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = explain(ctx, "test:verif/blk_a#t_cocotb")

    assert payload["attributes"]["xfail"] is True
    assert payload["node"]["type"] == "test"


# ---------------------------------------------------------------------------
# Node reference resolution
# ---------------------------------------------------------------------------


def test_a_bare_unambiguous_name_resolves(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    assert resolve_node(ctx, "t_cocotb")["id"] == "test:verif/blk_a#t_cocotb"


def test_an_ambiguous_name_fails_with_its_candidates(graph_project: Path):
    """Silently answering about the wrong blk_a is worse than asking."""
    _build(graph_project)
    ctx = load_context(graph_project)

    with pytest.raises(GraphQueryError) as excinfo:
        resolve_node(ctx, "blk_a")

    # spec block, model, design-tier module, and one suite per flow that
    # runs blk_a — sim, synth+cdc, fpv — all of which label themselves
    # after the block.
    assert "matches 6 nodes" in str(excinfo.value)
    assert "spec:blk_a" in excinfo.value.candidates


def _collision_ctx(tmp_path: Path) -> graph_query.GraphContext:
    """A minimal context holding two suite-qualified tb_top copies with
    indexed labels, the shape `_index_collision_labels` emits."""
    graph = {
        "nodes": [
            {
                "id": f"module:tb_top@verif/blk_{s}",
                "type": "module",
                "label": f"tb_top({i})",
                "base_label": "tb_top",
                "unqualified_id": "module:tb_top",
                "qualified_by": f"verif/blk_{s}",
            }
            for i, s in enumerate("ab")
        ],
        "links": [],
    }
    return graph_query.GraphContext(
        project_root=tmp_path,
        graph_path=tmp_path / "graph.json",
        graph=graph,
        index=graph_query.GraphIndex.build(graph),
    )


def test_indexed_collision_labels_score_at_the_exact_name_tier():
    """`tb_top(0)` tokenizes away from `tb_top`; base_label restores the
    exact-label tier so the rubric is unchanged for the node class a
    name query most likely means."""
    indexed = {
        "id": "module:tb_top@verif/blk_a",
        "type": "module",
        "label": "tb_top(0)",
        "base_label": "tb_top",
    }
    plain = dict(indexed, label="tb_top")
    del plain["base_label"]
    assert graph_query.score_node(indexed, ["tb_top"], set()) == graph_query.score_node(
        plain, ["tb_top"], set()
    )


def test_a_base_label_name_still_resolves_ambiguously_with_candidates(
    tmp_path: Path,
):
    """`rb graph explain tb_top` on a collision must keep raising the
    matches-N-use-a-full-id error — asking again beats answering about
    the wrong copy — not fall through to the fuzzy did-you-mean path."""
    ctx = _collision_ctx(tmp_path)

    with pytest.raises(GraphQueryError) as excinfo:
        resolve_node(ctx, "tb_top")

    assert "matches 2 nodes" in str(excinfo.value)
    assert excinfo.value.candidates == [
        "module:tb_top@verif/blk_a",
        "module:tb_top@verif/blk_b",
    ]


def test_an_unknown_name_fails_with_near_misses(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    with pytest.raises(GraphQueryError) as excinfo:
        resolve_node(ctx, "t_basi")

    assert "no node matches" in str(excinfo.value)
    assert "test:verif/blk_a#t_basic" in excinfo.value.candidates


def test_a_missing_graph_names_the_command_that_makes_one(tmp_path: Path):
    with pytest.raises(GraphQueryError) as excinfo:
        load_context(tmp_path)

    assert "rb graph build" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Overlay-only queries
# ---------------------------------------------------------------------------


def test_test_status_filters_by_name_and_verdict(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    ctx = load_context(graph_project)

    assert overlay_status(ctx, test="t_basic")["matched"] == 1
    assert overlay_status(ctx, status="FAIL")["matched"] == 0
    assert overlay_status(ctx)["statuses"] == {"PASS": 1}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_query_machine_envelope(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "graph", "query", "which tests cover A-COV-1"]
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["command"] == "graph query"
    payload = envelope["payload"]
    assert payload["graph"] == "artefacts/graph/graph.json"
    assert payload["overlay"] == "artefacts/graph/results-overlay.json"
    assert payload["schema_version"] == graph_query.QUERY_SCHEMA_VERSION
    assert payload["matches"][0]["id"] == "covitem:blk_a#A-COV-1"


def test_cli_query_with_no_match_exits_one(graph_project: Path):
    """A graceful no, not a crash: exit 1 so a shell loop can branch."""
    _build(graph_project)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "graph", "query", "no_such_thing"])

    assert result.exit_code == 1
    assert (
        json.loads(result.output.strip().splitlines()[-1])["payload"]["matches"] == []
    )


def test_cli_path_machine_envelope(graph_project: Path):
    _build(graph_project)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "graph",
            "path",
            "test:verif/blk_a#t_basic",
            "covitem:blk_a#A-COV-1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert payload["found"] is True
    assert payload["length"] == 1


def test_cli_path_with_an_ambiguous_endpoint_exits_two(graph_project: Path):
    _build(graph_project)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "graph", "path", "blk_a", "covitem:blk_a#A-COV-1"]
    )

    assert result.exit_code == 2
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert "matches 6 nodes" in envelope["payload"]["error"]
    assert envelope["payload"]["candidates"]


def test_cli_explain_machine_envelope(graph_project: Path):
    _build(graph_project)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "graph", "explain", "test:verif/blk_a#t_cocotb"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert payload["node"]["id"] == "test:verif/blk_a#t_cocotb"
    assert payload["degree"]["out"]["runs_on"] == 1


def test_the_read_verbs_do_not_take_the_artefact_lock(graph_project: Path):
    """Querying must work *while* a regression is running in the tree.

    The artefact lock is exclusive and held to process exit, so a read
    verb that took it would fail exactly when an agent most wants to ask
    the graph what it is looking at.
    """
    _build(graph_project)
    runner, rb = _runner()  # drops the builder's lock before the holder takes it
    holder = RtlBuddy(name="test_graph_query_holder")
    holder._artifact_locks.acquire(graph_project / "artefacts", command="regression")
    try:
        for argv in (
            ["graph", "query", "A-COV-1"],
            ["graph", "path", "test:verif/blk_a#t_basic", "covitem:blk_a#A-COV-1"],
            ["graph", "explain", "test:verif/blk_a#t_basic"],
        ):
            result = runner.invoke(rb.app, argv)
            assert result.exit_code == 0, (argv, result.output)
    finally:
        holder._artifact_locks.release_all()


def test_cli_human_output_names_the_covering_tests(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["graph", "query", "which tests cover A-COV-1"])

    assert result.exit_code == 0, result.output
    assert "covers test:verif/blk_a#t_basic (PASS)" in result.output
