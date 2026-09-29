"""Tests for the ``rb graph query`` / ``path`` / ``explain`` verbs.

The tests pin complete answers with the results overlay joined on, deterministic
keyword scoring (ties break on node id), undirected ``path``, both-direction
``explain`` with a source-snippet citation, loud failure on unknown or ambiguous
nodes, and the ``--machine`` envelopes the MCP surface reuses. The graph under
test is the config-tier fixture with a synthetic design tier merged in.
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
    """A fresh CLI object with the previous one's artefact lock released.

    The lock is held for the process lifetime, so two ``RtlBuddy`` instances in one
    test would contend with each other.
    """
    while _LIVE:
        _LIVE.pop()._artifact_locks.release_all()
    rb = RtlBuddy(name="test_graph_query")
    _LIVE.append(rb)
    return CliRunner(), rb


def _build(project: Path, *extra: str) -> Path:
    """Config-tier-only build; needs no viewer or extractor."""
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
    """Splice a minimal design tier into a config-only graph; the verbs only need the
    nodes and their ids."""
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


# Tokenizing and scoring


def test_type_words_become_hints_not_search_terms():
    """Type words become hints: "which tests cover A-COV-1" scores on the identifier
    alone."""
    terms, hints = tokenize("which tests cover A-COV-1")

    assert "test" in hints
    assert "tests" not in terms and "which" not in terms
    assert "a-cov-1" in terms


def test_a_type_word_promotes_but_never_conjures_a_match(graph_project: Path):
    """A type word reorders real matches but never invents one."""
    _build(graph_project)
    ctx = load_context(graph_project)

    node = {"id": "test:verif/blk_a#t_basic", "type": "test", "label": "t_basic"}
    other = {"id": "tb:verif/blk_a#tb_hdl", "type": "testbench", "label": "tb_hdl"}

    assert graph_query.score_node(node, ["t_basic"], {"test"}) > graph_query.score_node(
        node, ["t_basic"], set()
    )
    # No term hit: the type word alone scores zero.
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


# The acceptance criterion


def test_one_query_answers_which_tests_cover_an_item_and_their_status(
    graph_project: Path,
):
    """One query answers which tests cover an item and their last status."""
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
    # The overlay is joined onto the neighbour.
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
    """A graph with no overlay is fully queryable."""
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "A-COV-1")

    assert ctx.overlay is None
    assert payload["matches"][0]["id"] == "covitem:blk_a#A-COV-1"


# Neighbourhood expansion


def test_expansion_follows_edges_in_both_directions(graph_project: Path):
    """Expansion follows edges in both directions, so "which tests cover X" reads
    `covers` backwards."""
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


def test_neighbors_are_lean_references_by_default(graph_project: Path):
    """Neighbours are lean references by default: id, label, type and the joins."""
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=1)

    for neighbor in payload["matches"][0]["neighbors"]:
        assert set(neighbor) <= {
            "id",
            "type",
            "label",
            "base_label",
            "dangling",
            "results",
            "coverage",
            "distance",
            "via",
        }, neighbor
    # The overlay join survives.
    covering = {
        n["id"]: n
        for n in payload["matches"][0]["neighbors"]
        if n["via"]["type"] == "covers"
    }
    assert covering["test:verif/blk_a#t_basic"]["results"]["status"] == "PASS"


def test_query_expand_restores_full_neighbor_summaries(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=1, expand=True)

    neighbor = next(
        n
        for n in payload["matches"][0]["neighbors"]
        if n["id"] == "test:verif/blk_a#t_cocotb"
    )
    assert neighbor["tier"] == "config"
    assert neighbor["file"]
    assert neighbor["attributes"]["xfail"] is True


def test_a_match_carries_its_own_attributes(graph_project: Path):
    """A match carries its own attributes."""
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "test:verif/blk_a#t_cocotb", depth=0)

    match = payload["matches"][0]
    assert match["id"] == "test:verif/blk_a#t_cocotb"
    assert match["attributes"]["xfail"] is True


def test_neighbor_truncation_reports_what_it_dropped(graph_project: Path):
    """Neighbour truncation reports the count and kinds it dropped."""
    _build(graph_project)
    ctx = load_context(graph_project)

    payload = run_query(ctx, "covitem:blk_a#A-COV-1", depth=1, max_neighbors=1)

    match = payload["matches"][0]
    assert len(match["neighbors"]) == 1
    truncated = match["neighbors_truncated"]
    assert truncated["dropped"] >= 1
    assert sum(truncated["kinds"].values()) == truncated["dropped"]
    assert "test" in truncated["kinds"]


def test_explain_truncation_names_the_bucket_that_lost_edges(graph_project: Path):
    """`explain` and `query` spell "nothing was cut" the same way, and name the bucket
    that lost edges."""
    _build(graph_project)
    ctx = load_context(graph_project)

    whole = graph_query.explain(ctx, "module:blk_a")
    # Nothing dropped: the key is absent.
    assert "truncated" not in whole

    cut = graph_query.explain(ctx, "module:blk_a", limit=1)
    truncated = cut["truncated"]
    assert truncated["dropped"] == (
        len(whole["outgoing"])
        + len(whole["incoming"])
        - len(cut["outgoing"])
        - len(cut["incoming"])
    )
    assert sum(truncated["kinds"].values()) == truncated["dropped"]
    # Kinds here are edge types, not node types.
    assert set(truncated["kinds"]) <= {
        str(edge.get("type")) for edge in whole["outgoing"] + whole["incoming"]
    }
    assert set(truncated.get("buckets", {})) <= {"outgoing", "incoming"}
    assert sum(truncated.get("buckets", {}).values()) == truncated["dropped"]


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


# path


def test_path_is_undirected_by_default(graph_project: Path):
    """Path is undirected by default."""
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
    """Path crosses the config->design stitch."""
    graph_json = _build(graph_project)
    _with_design_tier(graph_json)
    ctx = load_context(graph_project)

    found = graph_path(ctx, "test:verif/blk_a#t_basic", "module:blk_a")

    assert found["found"]
    assert "module:blk_a" == found["paths"][0]["nodes"][-1]["id"]


# explain


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
    # Every edge names the far endpoint (id, label, type); a full peer summary needs
    # `--expand`.
    assert all(edge["peer_type"] for edge in payload["outgoing"])
    assert all(edge["peer_label"] for edge in payload["outgoing"])
    assert all("node" not in edge for edge in payload["outgoing"])


def test_explain_expand_restores_the_full_peer_summaries(graph_project: Path):
    """`--expand` returns every peer whole."""
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    ctx = load_context(graph_project)

    payload = explain(ctx, "covitem:blk_a#A-COV-1", expand=True)

    edge = next(e for e in payload["incoming"] if e.get("type") == "covers")
    assert edge["node"]["type"] == "test"
    assert edge["node"]["id"] == edge["peer"]
    # The expanded peer carries its attributes and overlay join.
    covering = {e["peer"]: e for e in payload["incoming"] if e["type"] == "covers"}
    assert covering["test:verif/blk_a#t_basic"]["node"]["results"]["status"] == "PASS"
    assert covering["test:verif/blk_a#t_cocotb"]["node"]["attributes"]["xfail"] is True


def test_explain_hands_back_a_runnable_source_snippet_command(graph_project: Path):
    """Explain returns a runnable source-snippet command.

    ``-c`` comes from the config tier's ``maps_to`` edge (the model stitch), so the
    command runs from the project root.
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
    """The cite command omits ``-c`` when no model declares the module.

    Dropping ``maps_to`` leaves the ``elaborates_as`` edge standing; a ``-c`` taken
    from a testbench's edge would give a command that cannot run.
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


# Node reference resolution


def test_a_bare_unambiguous_name_resolves(graph_project: Path):
    _build(graph_project)
    ctx = load_context(graph_project)

    assert resolve_node(ctx, "t_cocotb")["id"] == "test:verif/blk_a#t_cocotb"


def test_an_ambiguous_name_fails_with_its_candidates(graph_project: Path):
    """An ambiguous name fails with its candidates."""
    _build(graph_project)
    ctx = load_context(graph_project)

    with pytest.raises(GraphQueryError) as excinfo:
        resolve_node(ctx, "blk_a")

    # Spec block, model, module and one suite per flow all label themselves blk_a.
    assert "matches 6 nodes" in str(excinfo.value)
    assert "spec:blk_a" in excinfo.value.candidates


def _collision_ctx(tmp_path: Path) -> graph_query.GraphContext:
    """A context holding two suite-qualified tb_top copies with indexed labels."""
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
    """`tb_top(0)` scores at the exact-name tier through base_label."""
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
    """`rb graph explain tb_top` on a collision still raises the ambiguity error rather
    than a did-you-mean."""
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


# Overlay-only queries


def test_test_status_filters_by_name_and_verdict(graph_project: Path):
    _build(graph_project)
    _seed_run(graph_project, "t_basic", status="PASS")
    _refresh_results(graph_project)
    ctx = load_context(graph_project)

    assert overlay_status(ctx, test="t_basic")["matched"] == 1
    assert overlay_status(ctx, status="FAIL")["matched"] == 0
    assert overlay_status(ctx)["statuses"] == {"PASS": 1}


# CLI


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
    """A query with no match exits 1."""
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
    assert all("node" not in edge for edge in payload["outgoing"])


def test_cli_explain_expand_flag_expands_the_peers(graph_project: Path):
    _build(graph_project)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["--machine", "graph", "explain", "test:verif/blk_a#t_cocotb", "--expand"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert all(edge["node"]["id"] == edge["peer"] for edge in payload["outgoing"])


def test_cli_query_expand_flag_expands_the_neighbors(graph_project: Path):
    _build(graph_project)
    runner, rb = _runner()

    lean = runner.invoke(rb.app, ["--machine", "graph", "query", "A-COV-1"])
    expanded = runner.invoke(
        rb.app, ["--machine", "graph", "query", "A-COV-1", "--expand"]
    )

    assert lean.exit_code == 0 and expanded.exit_code == 0
    lean_n = json.loads(lean.output.strip().splitlines()[-1])["payload"]["matches"][0][
        "neighbors"
    ]
    exp_n = json.loads(expanded.output.strip().splitlines()[-1])["payload"]["matches"][
        0
    ]["neighbors"]
    assert all("tier" not in n for n in lean_n)
    assert all(n.get("tier") for n in exp_n)


def test_the_read_verbs_do_not_take_the_artefact_lock(graph_project: Path):
    """Read verbs work while a regression holds the artefact lock."""
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
