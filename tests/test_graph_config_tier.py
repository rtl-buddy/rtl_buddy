"""Tests for #376 — the config tier of the design knowledge graph.

The extractor reads ``specs.yaml`` / ``models.yaml`` / ``tests.yaml``
through the existing loaders and emits NetworkX node-link JSON. These
tests pin the contract that the design tier (rtl-buddy-view#126) and the
merge step (#377) rely on: node ids, edge types, and the ``module:<name>``
stitch point.

Fixture (``tests/fixtures/graph_config_tier/``):
  spec/blk_a/specs.yaml   -- block "blk_a": 2 docs (one missing), 3 cov items
  spec/blk_a/README.md    -- the doc that exists
  spec/blk_a/blk_a_model.py  -- golden model, referenced from verif
  spec/blk_a/_helper.py   -- private, must not become a node
  spec/blk_b/specs.yaml   -- block "blk_b", re-declares SHARED-COV
  design/blk_a/models.yaml -- model "blk_a", spec: -> spec/blk_a
  design/blk_b/models.yaml -- model "blk_b", no spec: back-pointer
  verif/blk_a/tests.yaml  -- 3 testbenches (one unused), 2 tests
  verif/empty_suite/tests.yaml -- declares a testbench, has no tests
  regression.yaml         -- sim flow; claims verif/blk_a only
  synth_regression.yaml   -- synth flow -> impl/blk_a/synth.yaml
  cdc_regression.yaml     -- cdc flow   -> impl/blk_a/cdc.yaml (same dir)
  fpv_regression.yaml     -- fpv flow   -> fpv/blk_a/fpv.yaml
  (no fpga_regression.yaml -- a flow the project does not run)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.graph import (
    CONFIG_TIER,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    SCHEMA_VERSION,
    build_config_tier,
    default_graph_dir,
    extract_config_tier,
    serialize_graph,
    write_graph_json,
    write_graph_meta,
)
from rtl_buddy.tools.spec_trace import build_coverage_map, discover_suite_tests

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_config_tier"


@pytest.fixture(scope="module")
def graph() -> dict:
    return build_config_tier(_FIXTURE)


def _nodes_by_type(graph: dict, node_type: str) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"] if n["type"] == node_type}


def _links_of_type(graph: dict, link_type: str) -> set[tuple[str, str]]:
    return {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == link_type
    }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_envelope_is_node_link_json_tagged_config_tier(graph):
    assert graph["directed"] is True
    assert graph["multigraph"] is True
    meta = graph["graph"]
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["project_root_rel"] == "."
    assert meta["generator"]["tool"] == "rtl_buddy"
    assert meta["generator"]["tier"] == CONFIG_TIER
    assert isinstance(meta["generator"]["version"], str)
    assert isinstance(graph["nodes"], list) and graph["nodes"]
    assert isinstance(graph["links"], list) and graph["links"]


def test_every_node_carries_id_type_label_and_tier(graph):
    for node in graph["nodes"]:
        assert set(node) >= {"id", "type", "label", "tier"}
        assert node["tier"] == CONFIG_TIER
    assert len({n["id"] for n in graph["nodes"]}) == len(graph["nodes"])


def test_config_tier_links_are_all_extracted(graph):
    # Nothing here is guessed — INFERRED/AMBIGUOUS belong to the binding
    # tier's dut.<signal> scan (#378), not to config readback.
    assert {link["confidence"] for link in graph["links"]} == {"EXTRACTED"}


def test_no_volatile_data_leaks_into_the_graph(graph):
    forbidden = ("seed", "status", "passed", "failed", "run_id", "artefact")
    blob = json.dumps(graph).lower()
    for token in forbidden:
        assert token not in blob, f"volatile key {token!r} leaked into graph.json"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def test_suite_test_and_testbench_node_ids(graph):
    assert set(_nodes_by_type(graph, "suite")) == {
        "suite:verif/blk_a",
        "suite:verif/empty_suite",
        # The non-simulation flows' suites: no `verif/` walk reaches
        # these, they come from the repo-level regression files.
        "suite:impl/blk_a",
        "suite:fpv/blk_a",
    }
    assert set(_nodes_by_type(graph, "test")) == {
        "test:verif/blk_a#t_basic",
        "test:verif/blk_a#t_cocotb",
        "test:impl/blk_a#blk_a_generic",
        "test:impl/blk_a#blk_a_lint",
        "test:fpv/blk_a#blk_a_safety",
    }
    # tb_unused is declared but referenced by no test — still a node, or a
    # dead testbench would be invisible to the graph.
    assert set(_nodes_by_type(graph, "testbench")) == {
        "tb:verif/blk_a#tb_hdl",
        "tb:verif/blk_a#tb_cocotb",
        "tb:verif/blk_a#tb_unused",
        "tb:verif/empty_suite#tb_orphan",
    }


def test_test_node_carries_reglvl_and_cocotb_module(graph):
    tests = _nodes_by_type(graph, "test")
    basic = tests["test:verif/blk_a#t_basic"]
    assert basic["reglvl"] == 0
    assert "cocotb_modules" not in basic
    assert "xfail" not in basic

    cocotb = tests["test:verif/blk_a#t_cocotb"]
    # The raw per-builder mapping is kept: resolving it needs a builder,
    # which is a run-time choice with no place in a static graph.
    assert cocotb["reglvl"] == {"default": 100, "verilator": 0}
    assert cocotb["cocotb_modules"] == ["cocotb_blk_a"]
    assert cocotb["xfail"] is True


def test_testbench_node_carries_toplevel_and_kind(graph):
    tbs = _nodes_by_type(graph, "testbench")
    assert tbs["tb:verif/blk_a#tb_cocotb"]["toplevel"] == "blk_a"
    assert tbs["tb:verif/blk_a#tb_cocotb"]["kind"] == "cocotb"
    assert tbs["tb:verif/blk_a#tb_hdl"]["kind"] == "hdl"
    assert "toplevel" not in tbs["tb:verif/blk_a#tb_hdl"]


def test_model_node_ids_are_keyed_by_models_yaml_path(graph):
    assert set(_nodes_by_type(graph, "model")) == {
        "model:design/blk_a/models.yaml#blk_a",
        "model:design/blk_b/models.yaml#blk_b",
    }


def test_spec_block_and_coverage_item_node_ids(graph):
    assert set(_nodes_by_type(graph, "spec_block")) == {"spec:blk_a", "spec:blk_b"}
    assert set(_nodes_by_type(graph, "coverage_item")) == {
        "covitem:blk_a#A-COV-1",
        "covitem:blk_a#A-COV-2",
        "covitem:blk_a#SHARED-COV",
        "covitem:blk_b#SHARED-COV",
    }


def test_spec_doc_nodes_record_whether_the_file_exists(graph):
    docs = _nodes_by_type(graph, "spec_doc")
    assert set(docs) == {"doc:spec/blk_a/README.md", "doc:spec/blk_a/missing.md"}
    assert docs["doc:spec/blk_a/README.md"]["exists"] is True
    assert docs["doc:spec/blk_a/missing.md"]["exists"] is False


def test_golden_model_discovered_by_convention_with_referencing_files(graph):
    goldens = _nodes_by_type(graph, "golden_model")
    # _helper.py is private plumbing, not a model of the block.
    assert set(goldens) == {"golden:spec/blk_a/blk_a_model.py"}
    node = goldens["golden:spec/blk_a/blk_a_model.py"]
    assert node["label"] == "blk_a_model"
    # Scan is textual across verif sources, so both the cocotb module and
    # the test's `desc:` mention of the file count as references.
    assert node["referenced_by"] == [
        "verif/blk_a/cocotb_blk_a.py",
        "verif/blk_a/tests.yaml",
    ]


# ---------------------------------------------------------------------------
# Flow provenance
#
# The repo-level regression files are the only place that says which flow
# owns a suite. Everything below is about that stamp reaching the nodes.
# ---------------------------------------------------------------------------


def _flows(graph: dict, node_type: str) -> dict[str, object]:
    return {
        node_id: node.get("flow")
        for node_id, node in _nodes_by_type(graph, node_type).items()
    }


def test_suites_are_stamped_with_the_flow_that_runs_them(graph):
    assert _flows(graph, "suite") == {
        "suite:verif/blk_a": "sim",
        # Claimed by no regression file at all. A tests.yaml nobody has
        # wired up yet is still a simulation suite.
        "suite:verif/empty_suite": "sim",
        "suite:fpv/blk_a": "fpv",
        # One directory, two flows -> a list, in FLOW_SOURCES order.
        "suite:impl/blk_a": ["synth", "cdc"],
    }


def test_tests_and_testbenches_inherit_their_suites_flow(graph):
    assert _flows(graph, "testbench") == {
        "tb:verif/blk_a#tb_hdl": "sim",
        "tb:verif/blk_a#tb_cocotb": "sim",
        "tb:verif/blk_a#tb_unused": "sim",
        "tb:verif/empty_suite#tb_orphan": "sim",
    }
    # A run in a two-flow directory is stamped with the flow of the file
    # it was declared in, not with its suite's list: `blk_a_lint` is a CDC
    # analysis whatever else shares its directory.
    assert _flows(graph, "test") == {
        "test:verif/blk_a#t_basic": "sim",
        "test:verif/blk_a#t_cocotb": "sim",
        "test:impl/blk_a#blk_a_generic": "synth",
        "test:impl/blk_a#blk_a_lint": "cdc",
        "test:fpv/blk_a#blk_a_safety": "fpv",
    }


def test_flow_runs_carry_their_tool_reglvl_and_top(graph):
    tests = _nodes_by_type(graph, "test")
    synth = tests["test:impl/blk_a#blk_a_generic"]
    assert (synth["tool"], synth["reglvl"], synth["toplevel"]) == ("yosys", 0, "blk_a")
    assert tests["test:fpv/blk_a#blk_a_safety"]["tool"] == "sby"
    # `reglvl:` is absent from the cdc entry and stays absent — the graph
    # does not invent a default a tool would have to resolve anyway.
    assert "reglvl" not in tests["test:impl/blk_a#blk_a_lint"]


def test_an_fpv_top_that_overrides_the_model_is_where_maps_to_lands(tmp_path):
    """`top:` names the module the run elaborates, `model:` the DUT.

    A formal verification often tops at a wrapper that binds the checker
    alongside the DUT, and it is that wrapper the hierarchy is rooted at.
    """

    design = tmp_path / "design" / "blk"
    design.mkdir(parents=True)
    (design / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n"
        "  - name: blk\n    filelist: [blk.sv]\n"
    )
    fpv = tmp_path / "fpv" / "blk"
    fpv.mkdir(parents=True)
    (fpv / "fpv.yaml").write_text(
        "rtl-buddy-filetype: fpv_config\nverifications:\n"
        "  - name: safety\n    desc: bounded proof\n    model: blk\n"
        "    model_path: ../../design/blk/models.yaml\n    tool: sby\n"
        "    top: blk_fv\n"
    )
    (tmp_path / "fpv_regression.yaml").write_text(
        "rtl-buddy-filetype: fpv_reg_config\nfpv-configs: [fpv/blk/fpv.yaml]\n"
    )

    graph = build_config_tier(tmp_path)
    assert _nodes_by_type(graph, "test")["test:fpv/blk#safety"]["toplevel"] == "blk_fv"
    assert ("test:fpv/blk#safety", "module:blk_fv") in _links_of_type(graph, "maps_to")
    # The model still stitches to its own module — the two are different
    # design-tier nodes and the run is attached to both.
    assert ("model:design/blk/models.yaml#blk", "module:blk") in _links_of_type(
        graph, "maps_to"
    )


def test_cocotb_is_stamped_on_the_test_and_its_testbench(graph):
    """A flat boolean, on both node types.

    ``kind: cocotb`` already says it on a testbench and ``cocotb_modules``
    implies it on a test, but a consumer bucketing nodes should not have
    to know which type spells it which way.
    """
    cocotb = {n["id"] for n in graph["nodes"] if n.get("cocotb")}
    assert cocotb == {"test:verif/blk_a#t_cocotb", "tb:verif/blk_a#tb_cocotb"}
    # Absent rather than false, so the attribute set stays minimal.
    assert "cocotb" not in _nodes_by_type(graph, "test")["test:verif/blk_a#t_basic"]


def test_a_flow_with_no_regression_file_contributes_nothing(graph):
    """There is no ``fpga_regression.yaml`` in the fixture."""

    assert not [n for n in graph["nodes"] if n.get("flow") == "fpga"]


def test_an_unloadable_regression_file_is_reported_not_raised(tmp_path):
    (tmp_path / "cdc_regression.yaml").write_text(
        "rtl-buddy-filetype: cdc_reg_config\ncdc-configs: [nope/cdc.yaml]\n"
    )
    result = extract_config_tier(tmp_path)
    assert result.suite_load_failures == ["cdc_regression.yaml"]
    assert result.graph["nodes"] == []


def test_flow_stamp_changes_the_input_hashes(tmp_path):
    """The fingerprint has to see a suite being wired into a flow."""

    verif = tmp_path / "verif" / "blk"
    verif.mkdir(parents=True)
    (verif / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\ntestbenches:\n"
        "  - name: tb\n    filelist: []\ntests: []\n"
    )
    before = extract_config_tier(tmp_path)
    assert _nodes_by_type(before.graph, "suite")["suite:verif/blk"]["flow"] == "sim"
    before_inputs = {
        e["path"]: e["sha256"] for e in before.meta["tiers"]["config"]["inputs"]
    }

    (tmp_path / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs: [verif/blk/tests.yaml]\n"
    )
    after = extract_config_tier(tmp_path)
    after_inputs = {
        e["path"]: e["sha256"] for e in after.meta["tiers"]["config"]["inputs"]
    }
    assert "regression.yaml" not in before_inputs
    assert "regression.yaml" in after_inputs


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_declares_edges_cover_suite_contents_and_block_coverage_items(graph):
    declares = _links_of_type(graph, "declares")
    assert ("suite:verif/blk_a", "test:verif/blk_a#t_basic") in declares
    assert ("suite:verif/blk_a", "tb:verif/blk_a#tb_unused") in declares
    assert ("suite:verif/empty_suite", "tb:verif/empty_suite#tb_orphan") in declares
    assert ("spec:blk_a", "covitem:blk_a#A-COV-1") in declares
    assert ("spec:blk_b", "covitem:blk_b#SHARED-COV") in declares


def test_runs_on_and_exercises_edges(graph):
    assert _links_of_type(graph, "runs_on") == {
        ("test:verif/blk_a#t_basic", "tb:verif/blk_a#tb_hdl"),
        ("test:verif/blk_a#t_cocotb", "tb:verif/blk_a#tb_cocotb"),
    }
    assert _links_of_type(graph, "exercises") == {
        ("tb:verif/blk_a#tb_hdl", "model:design/blk_a/models.yaml#blk_a"),
        ("tb:verif/blk_a#tb_cocotb", "model:design/blk_a/models.yaml#blk_a"),
        # A synth / cdc / fpv run has no testbench between it and the
        # model, so the edge starts at the run itself.
        ("test:impl/blk_a#blk_a_generic", "model:design/blk_a/models.yaml#blk_a"),
        ("test:impl/blk_a#blk_a_lint", "model:design/blk_a/models.yaml#blk_a"),
        ("test:fpv/blk_a#blk_a_safety", "model:design/blk_a/models.yaml#blk_a"),
    }


def test_specified_by_documented_by_and_implements_edges(graph):
    assert _links_of_type(graph, "specified_by") == {
        ("model:design/blk_a/models.yaml#blk_a", "spec:blk_a")
    }
    assert _links_of_type(graph, "documented_by") == {
        ("spec:blk_a", "doc:spec/blk_a/README.md"),
        ("spec:blk_a", "doc:spec/blk_a/missing.md"),
    }
    assert _links_of_type(graph, "implements") == {
        ("golden:spec/blk_a/blk_a_model.py", "spec:blk_a")
    }


def test_covers_edges_fan_out_to_every_block_declaring_the_id(graph):
    covers = _links_of_type(graph, "covers")
    assert covers == {
        ("test:verif/blk_a#t_basic", "covitem:blk_a#A-COV-1"),
        ("test:verif/blk_a#t_cocotb", "covitem:blk_a#A-COV-1"),
        ("test:verif/blk_a#t_cocotb", "covitem:blk_a#A-COV-2"),
        # SHARED-COV is declared by both blocks; `rb spec check-coverage`
        # matches on the bare id, so the graph must link both.
        ("test:verif/blk_a#t_cocotb", "covitem:blk_a#SHARED-COV"),
        ("test:verif/blk_a#t_cocotb", "covitem:blk_b#SHARED-COV"),
    }


def test_covers_edges_agree_with_the_coverage_map_the_cli_uses(graph):
    """Single source of truth check against ``rb spec check-coverage``."""
    suite_tests, failures = discover_suite_tests(str(_FIXTURE / "verif"))
    assert failures == []
    cov_map = build_coverage_map(suite_tests)

    blocks_by_item: dict[str, set[str]] = {}
    for node in graph["nodes"]:
        if node["type"] == "coverage_item":
            blocks_by_item.setdefault(node["label"], set()).add(node["block"])

    expected = set()
    for item_id, entries in cov_map.items():
        for tests_path, test_name in entries:
            suite_rel = Path(tests_path).parent.relative_to(_FIXTURE.resolve())
            for block in blocks_by_item.get(item_id, ()):
                expected.add(
                    (
                        f"test:{suite_rel.as_posix()}#{test_name}",
                        f"covitem:{block}#{item_id}",
                    )
                )
    assert _links_of_type(graph, "covers") == expected


def test_unknown_coverage_id_produces_no_edge(graph):
    # t_basic claims GHOST-COV, which no spec block declares.
    assert not [link for link in graph["links"] if "GHOST-COV" in link["target"]]


# ---------------------------------------------------------------------------
# The design-tier stitch
# ---------------------------------------------------------------------------


def test_maps_to_targets_design_tier_module_ids(graph):
    assert _links_of_type(graph, "maps_to") == {
        ("model:design/blk_a/models.yaml#blk_a", "module:blk_a"),
        ("model:design/blk_b/models.yaml#blk_b", "module:blk_b"),
        # A testbench with a declared `toplevel:` stitches the same way
        # a model does — `rb graph build` exports that hierarchy rooted
        # at the testbench, and this is where the metadata node meets it.
        ("tb:verif/blk_a#tb_cocotb", "module:blk_a"),
        # A non-simulation run's `top:` is the same relation: the module
        # the run elaborates. For synth/cdc it is the model's own name;
        # for fpv it may be a wrapper the checker binds into (see
        # test_an_fpv_top_that_overrides_the_model_is_where_maps_to_lands).
        ("test:impl/blk_a#blk_a_generic", "module:blk_a"),
        ("test:impl/blk_a#blk_a_lint", "module:blk_a"),
        ("test:fpv/blk_a#blk_a_safety", "module:blk_a"),
    }


def test_a_testbench_without_a_toplevel_gets_no_maps_to(graph):
    """Guessing the top from the testbench name would be inference.

    The config tier is pure config readback — every link it emits is
    EXTRACTED. `rb graph build` adds the edge for these from the top the
    viewer really elaborated.
    """
    declared = {
        node["id"]
        for node in graph["nodes"]
        if node["type"] == "testbench" and node.get("toplevel")
    }
    sourced = {
        link["source"]
        for link in graph["links"]
        if link["type"] == "maps_to" and link["source"].startswith("tb:")
    }
    assert sourced == declared


def test_module_nodes_are_not_created_by_the_config_tier(graph):
    # `module:<name>` is the design tier's id. Emitting a stub here would
    # collide on merge; the dangling target IS the stitch point.
    assert not [n for n in graph["nodes"] if n["id"].startswith("module:")]


def test_cocotb_test_reaches_its_spec_block_through_tb_model_spec(graph):
    """Acceptance criterion: a path query resolves test -> ... -> spec block."""
    adjacency: dict[str, set[str]] = {}
    for link in graph["links"]:
        adjacency.setdefault(link["source"], set()).add(link["target"])

    start = "test:verif/blk_a#t_cocotb"
    seen = {start}
    frontier = [(start, [start])]
    path = None
    while frontier and path is None:
        node, trail = frontier.pop(0)
        for nxt in sorted(adjacency.get(node, ())):
            if nxt == "spec:blk_a":
                path = trail + [nxt]
                break
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, trail + [nxt]))

    assert path == [
        "test:verif/blk_a#t_cocotb",
        "tb:verif/blk_a#tb_cocotb",
        "model:design/blk_a/models.yaml#blk_a",
        "spec:blk_a",
    ]


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_missing_search_directories_yield_an_empty_but_valid_graph(tmp_path):
    graph = build_config_tier(tmp_path)
    assert graph["nodes"] == []
    assert graph["links"] == []
    assert graph["graph"]["schema_version"] == SCHEMA_VERSION


def test_unloadable_suite_is_reported_not_raised(tmp_path):
    suite = tmp_path / "verif" / "broken"
    suite.mkdir(parents=True)
    (suite / "tests.yaml").write_text("rtl-buddy-filetype: not_a_test_config\n")

    result = extract_config_tier(tmp_path)
    assert result.suite_load_failures == ["verif/broken/tests.yaml"]
    assert result.meta["tiers"]["config"]["suite_load_failures"] == [
        "verif/broken/tests.yaml"
    ]


def test_search_directories_can_be_overridden(tmp_path):
    graph = build_config_tier(
        _FIXTURE,
        spec_dir=_FIXTURE / "spec" / "blk_b",
        verif_dir=tmp_path,
        design_dir=tmp_path,
    )
    assert set(_nodes_by_type(graph, "spec_block")) == {"spec:blk_b"}
    # The three search dirs govern spec/design/verif discovery. The
    # repo-level regression files are found at the project root itself and
    # are deliberately not overridable — they are what makes the root a
    # project, so the non-simulation flows survive.
    assert set(_nodes_by_type(graph, "test")) == {
        "test:impl/blk_a#blk_a_generic",
        "test:impl/blk_a#blk_a_lint",
        "test:fpv/blk_a#blk_a_safety",
    }


# ---------------------------------------------------------------------------
# Meta and serialization
# ---------------------------------------------------------------------------


def test_meta_hashes_every_config_file_read(tmp_path):
    result = extract_config_tier(_FIXTURE)
    inputs = result.meta["tiers"][CONFIG_TIER]["inputs"]
    paths = {entry["path"] for entry in inputs}
    assert paths == {
        "spec/blk_a/specs.yaml",
        "spec/blk_b/specs.yaml",
        "design/blk_a/models.yaml",
        "design/blk_b/models.yaml",
        "verif/blk_a/tests.yaml",
        "verif/empty_suite/tests.yaml",
        # Flow provenance changes the graph, so the files that carry it
        # have to be able to invalidate `rb graph build`'s no-op check.
        "regression.yaml",
        "synth_regression.yaml",
        "cdc_regression.yaml",
        "fpv_regression.yaml",
        "impl/blk_a/synth.yaml",
        "impl/blk_a/cdc.yaml",
        "fpv/blk_a/fpv.yaml",
    }
    assert all(len(entry["sha256"]) == 64 for entry in inputs)
    # Hashes are provenance, not graph content — they must not be baked
    # into graph.json, where they would churn every merge.
    assert "inputs" not in result.graph["graph"]


def test_extraction_is_deterministic_byte_for_byte():
    assert serialize_graph(build_config_tier(_FIXTURE)) == serialize_graph(
        build_config_tier(_FIXTURE)
    )


def test_write_helpers_round_trip_through_the_contracted_paths(tmp_path):
    result = extract_config_tier(_FIXTURE)
    out_dir = default_graph_dir(tmp_path)
    assert out_dir == tmp_path / "artefacts" / "graph"

    graph_path = write_graph_json(result.graph, out_dir / GRAPH_JSON_NAME)
    meta_path = write_graph_meta(result.meta, out_dir / GRAPH_META_NAME)

    assert json.loads(graph_path.read_text()) == result.graph
    assert json.loads(meta_path.read_text()) == result.meta
    assert not list(out_dir.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Downstream consumers
#
# networkx and Graphify are optional: nothing in rtl_buddy imports them,
# and the merge step (#377) may run elsewhere. These guard the envelope
# against the real readers when they happen to be installed.
# ---------------------------------------------------------------------------


def test_graph_loads_as_a_networkx_multidigraph(graph):
    nx = pytest.importorskip("networkx")
    loaded = nx.node_link_graph(graph, edges="links")
    assert loaded.is_directed() and loaded.is_multigraph()
    # Every node we emit survives, plus the dangling design-tier module
    # targets that node-link auto-creates — the stitch points.
    assert set(loaded.nodes) >= {n["id"] for n in graph["nodes"]}
    assert "module:blk_a" in loaded.nodes
    assert loaded.number_of_edges() == len(graph["links"])


def test_graphify_accepts_the_envelope_when_installed(graph, tmp_path):
    pytest.importorskip("graphify")
    nx = pytest.importorskip("networkx")
    path = write_graph_json(graph, tmp_path / GRAPH_JSON_NAME)
    reloaded = nx.node_link_graph(json.loads(path.read_text()), edges="links")
    assert reloaded.number_of_nodes() >= len(graph["nodes"])
