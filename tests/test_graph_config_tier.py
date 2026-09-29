"""Tests for the config tier of the design knowledge graph.

The extractor reads ``specs.yaml`` / ``models.yaml`` / ``tests.yaml`` through the
existing loaders and emits NetworkX node-link JSON. The tests pin the node ids,
edge types and ``module:<name>`` stitch point that the design tier and the merge
step rely on, using ``tests/fixtures/graph_config_tier/``.
"""

from __future__ import annotations

import json
import logging
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


# Envelope


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
    # Nothing is guessed: INFERRED/AMBIGUOUS belong to the binding tier.
    assert {link["confidence"] for link in graph["links"]} == {"EXTRACTED"}


def test_no_volatile_data_leaks_into_the_graph(graph):
    forbidden = ("seed", "status", "passed", "failed", "run_id", "artefact")
    blob = json.dumps(graph).lower()
    for token in forbidden:
        assert token not in blob, f"volatile key {token!r} leaked into graph.json"


# Nodes


def test_suite_test_and_testbench_node_ids(graph):
    assert set(_nodes_by_type(graph, "suite")) == {
        "suite:verif/blk_a",
        "suite:verif/empty_suite",
        # Non-simulation suites come from the repo-level regression files, not a
        # `verif/` walk.
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
    # tb_unused is declared but referenced by no test; it is still a node.
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
    # The per-builder mapping is kept raw; resolving it needs a run-time builder.
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
    # _helper.py is private, not a model of the block.
    assert set(goldens) == {"golden:spec/blk_a/blk_a_model.py"}
    node = goldens["golden:spec/blk_a/blk_a_model.py"]
    assert node["label"] == "blk_a_model"
    # The scan is textual, so a test's `desc:` mention counts as a reference.
    assert node["referenced_by"] == [
        "verif/blk_a/cocotb_blk_a.py",
        "verif/blk_a/tests.yaml",
    ]


# Flow provenance


def _flows(graph: dict, node_type: str) -> dict[str, object]:
    return {
        node_id: node.get("flow")
        for node_id, node in _nodes_by_type(graph, node_type).items()
    }


def test_suites_are_stamped_with_the_flow_that_runs_them(graph):
    assert _flows(graph, "suite") == {
        "suite:verif/blk_a": "sim",
        # A tests.yaml no regression file claims is a simulation suite.
        "suite:verif/empty_suite": "sim",
        "suite:fpv/blk_a": "fpv",
        # One directory, two flows: a list in FLOW_SOURCES order.
        "suite:impl/blk_a": ["synth", "cdc"],
    }


def test_tests_and_testbenches_inherit_their_suites_flow(graph):
    assert _flows(graph, "testbench") == {
        "tb:verif/blk_a#tb_hdl": "sim",
        "tb:verif/blk_a#tb_cocotb": "sim",
        "tb:verif/blk_a#tb_unused": "sim",
        "tb:verif/empty_suite#tb_orphan": "sim",
    }
    # A run is stamped with the flow of the file that declared it, not its suite's list.
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
    # `reglvl:` is absent from the cdc entry and stays absent.
    assert "reglvl" not in tests["test:impl/blk_a#blk_a_lint"]


def test_an_fpv_top_that_overrides_the_model_is_where_targets_lands(tmp_path):
    """`top:` names the module an fpv run elaborates and `model:` the DUT, so `targets`
    lands on the top."""

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
    assert ("test:fpv/blk#safety", "module:blk_fv") in _links_of_type(graph, "targets")
    # The model still stitches to its own module; a run never emits `maps_to`.
    assert ("model:design/blk/models.yaml#blk", "module:blk") in _links_of_type(
        graph, "maps_to"
    )


def test_an_fpv_run_with_covers_reaches_the_spec_tier(tmp_path):
    """`covers:` on an fpv run emits the same run -> coverage-item edge as a simulation
    test."""

    spec = tmp_path / "spec" / "blk"
    spec.mkdir(parents=True)
    (spec / "specs.yaml").write_text(
        "rtl-buddy-filetype: spec_config\nblocks:\n"
        "  - name: blk\n    desc: block\n"
        "    coverage-items:\n"
        "      - id: BLK-SAFE-1\n        desc: never overflows\n"
    )
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
        "    covers: [BLK-SAFE-1, GHOST-COV]\n"
    )
    (tmp_path / "fpv_regression.yaml").write_text(
        "rtl-buddy-filetype: fpv_reg_config\nfpv-configs: [fpv/blk/fpv.yaml]\n"
    )

    graph = build_config_tier(tmp_path)
    covers = _links_of_type(graph, "covers")
    assert ("test:fpv/blk#safety", "covitem:blk#BLK-SAFE-1") in covers
    # An id no block declares gets no edge.
    assert not [t for _, t in covers if t.endswith("#GHOST-COV")]
    # `rb graph path` walks run -> item -> block through the block's `declares`.
    assert ("spec:blk", "covitem:blk#BLK-SAFE-1") in _links_of_type(graph, "declares")


def test_cocotb_is_stamped_on_the_test_and_its_testbench(graph):
    """``cocotb`` is a flat boolean on both the test and its testbench."""
    cocotb = {n["id"] for n in graph["nodes"] if n.get("cocotb")}
    assert cocotb == {"test:verif/blk_a#t_cocotb", "tb:verif/blk_a#tb_cocotb"}
    # Absent rather than false.
    assert "cocotb" not in _nodes_by_type(graph, "test")["test:verif/blk_a#t_basic"]


def test_a_flow_with_no_regression_file_contributes_nothing(graph):
    """A flow with no regression file contributes nothing."""

    assert not [n for n in graph["nodes"] if n.get("flow") == "fpga"]


def test_an_unloadable_regression_file_is_reported_not_raised(tmp_path):
    (tmp_path / "cdc_regression.yaml").write_text(
        "rtl-buddy-filetype: cdc_reg_config\ncdc-configs: [nope/cdc.yaml]\n"
    )
    result = extract_config_tier(tmp_path)
    assert result.suite_load_failures == ["cdc_regression.yaml"]
    assert result.graph["nodes"] == []


def test_flow_stamp_changes_the_input_hashes(tmp_path):
    """Wiring a suite into a flow changes the input hashes."""

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


# cfg-rtl-reg manifest paths


def _write_non_root_cdc_project(tmp_path: Path) -> None:
    """A model plus one CDC analysis whose manifest lives at lint/cdc/."""

    design = tmp_path / "design" / "blk"
    design.mkdir(parents=True)
    (design / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n"
        "  - name: blk\n    filelist: [blk.sv]\n"
    )
    cdc_dir = tmp_path / "lint" / "cdc"
    cdc_dir.mkdir(parents=True)
    (cdc_dir / "cdc.yaml").write_text(
        "rtl-buddy-filetype: cdc_config\nanalyses:\n"
        "  - name: blk_lint\n    desc: CDC lint of blk\n    model: blk\n"
        "    model_path: ../../design/blk/models.yaml\n"
        "    tool: rtl-buddy-cdc\n    constraints: blk.sdc\n"
    )
    (cdc_dir / "cdc_regression.yaml").write_text(
        "rtl-buddy-filetype: cdc_reg_config\ncdc-configs: [cdc.yaml]\n"
    )


def _root_config_with(reg_block: str) -> str:
    return f"rtl-buddy-filetype: project_root_config\ncfg-rtl-reg:\n{reg_block}"


def test_a_configured_manifest_away_from_the_root_gains_the_flow_nodes(tmp_path):
    """A `cdc_regression.yaml` under `lint/cdc/` declared via `cfg-rtl-reg.cdc-reg-cfg-
    path` gains the flow nodes."""

    _write_non_root_cdc_project(tmp_path)
    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: regression.yaml\n"
            "  cdc-reg-cfg-path: lint/cdc/cdc_regression.yaml\n"
        )
    )

    graph = build_config_tier(tmp_path)
    assert _nodes_by_type(graph, "suite")["suite:lint/cdc"]["flow"] == "cdc"
    assert _nodes_by_type(graph, "test")["test:lint/cdc#blk_lint"]["flow"] == "cdc"
    assert (
        "test:lint/cdc#blk_lint",
        "model:design/blk/models.yaml#blk",
    ) in _links_of_type(graph, "exercises")


def test_an_undeclared_non_root_manifest_stays_invisible(tmp_path):
    """Without a root file or cfg-rtl-reg key, `lint/cdc/` stays invisible."""

    _write_non_root_cdc_project(tmp_path)
    graph = build_config_tier(tmp_path)
    assert "suite:lint/cdc" not in _nodes_by_type(graph, "suite")


def test_the_root_filename_wins_over_the_configured_path(tmp_path):
    """A root manifest is read first and cfg-rtl-reg is only the fallback."""

    _write_non_root_cdc_project(tmp_path)
    (tmp_path / "cdc_regression.yaml").write_text(
        "rtl-buddy-filetype: cdc_reg_config\ncdc-configs: []\n"
    )
    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: regression.yaml\n"
            "  cdc-reg-cfg-path: lint/cdc/cdc_regression.yaml\n"
        )
    )

    graph = build_config_tier(tmp_path)
    # The root manifest claims nothing and wins: lint/cdc stays out.
    assert "suite:lint/cdc" not in _nodes_by_type(graph, "suite")


def test_a_configured_path_pointing_nowhere_is_skipped_not_failed(tmp_path):
    """A configured manifest path that does not exist is skipped, not a load failure."""

    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: regression.yaml\n"
            "  cdc-reg-cfg-path: nope/cdc_regression.yaml\n"
        )
    )
    result = extract_config_tier(tmp_path)
    assert result.graph["nodes"] == []
    assert result.suite_load_failures == []


def test_a_malformed_cfg_rtl_reg_block_degrades_to_the_filename_convention(
    tmp_path,
):
    _write_non_root_cdc_project(tmp_path)
    (tmp_path / "root_config.yaml").write_text("cfg-rtl-reg: [not, a, mapping]\n")
    result = extract_config_tier(tmp_path)
    assert "suite:lint/cdc" not in _nodes_by_type(result.graph, "suite")
    assert result.suite_load_failures == []


def test_a_misspelled_cfg_rtl_reg_key_is_reported_by_name(tmp_path, caplog):
    """A misspelled cfg-rtl-reg key is reported by name, since `from_dict` drops unknown
    keys."""

    _write_non_root_cdc_project(tmp_path)
    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: regression.yaml\n"
            "  cdc-reg-cfg-paths: lint/cdc/cdc_regression.yaml\n"
        )
    )
    with caplog.at_level(logging.WARNING):
        result = extract_config_tier(tmp_path)

    # It degrades to the filename convention...
    assert "suite:lint/cdc" not in _nodes_by_type(result.graph, "suite")
    # ...but names the key.
    messages = [r.message for r in caplog.records]
    assert any(
        "unknown key(s) cdc-reg-cfg-paths" in m and "cdc-reg-cfg-path" in m
        for m in messages
    ), messages


def test_the_correctly_spelled_keys_survive_an_unknown_neighbour(tmp_path):
    """Correctly spelled keys survive an unknown neighbour."""

    _write_non_root_cdc_project(tmp_path)
    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: regression.yaml\n"
            "  cdc-reg-cfg-path: lint/cdc/cdc_regression.yaml\n"
            "  synth-reg-cfg-pathz: nowhere.yaml\n"
        )
    )
    result = extract_config_tier(tmp_path)
    assert "suite:lint/cdc" in _nodes_by_type(result.graph, "suite")


def test_configured_manifests_and_root_config_join_the_input_hashes(tmp_path):
    """root_config.yaml and configured manifests join the input hashes."""

    _write_non_root_cdc_project(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs: []\n"
    )
    (tmp_path / "root_config.yaml").write_text(
        _root_config_with(
            "  reg-cfg-path: cfg/regression.yaml\n"
            "  cdc-reg-cfg-path: lint/cdc/cdc_regression.yaml\n"
        )
    )

    result = extract_config_tier(tmp_path)
    paths = {e["path"] for e in result.meta["tiers"][CONFIG_TIER]["inputs"]}
    assert {
        "root_config.yaml",
        # The sim flow goes through the same reg-cfg-path machinery.
        "cfg/regression.yaml",
        "lint/cdc/cdc_regression.yaml",
        "lint/cdc/cdc.yaml",
    } <= paths


# Edges


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
        # A synth/cdc/fpv run has no testbench, so the edge starts at the run.
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
        # SHARED-COV is declared by both blocks; `rb spec check-coverage` matches the
        # bare id, so both link.
        ("test:verif/blk_a#t_cocotb", "covitem:blk_a#SHARED-COV"),
        ("test:verif/blk_a#t_cocotb", "covitem:blk_b#SHARED-COV"),
    }


def test_covers_edges_agree_with_the_coverage_map_the_cli_uses(graph):
    """Covers edges agree with ``rb spec check-coverage``."""
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
    # t_basic claims GHOST-COV, which no block declares.
    assert not [link for link in graph["links"] if "GHOST-COV" in link["target"]]


# The design-tier stitch


def test_the_three_stitches_target_design_tier_module_ids(graph):
    """Models, testbenches and non-simulation runs each stitch to a design-tier
    `module:<name>` id with their own edge type."""
    # A model declares a module.
    assert _links_of_type(graph, "maps_to") == {
        ("model:design/blk_a/models.yaml#blk_a", "module:blk_a"),
        ("model:design/blk_b/models.yaml#blk_b", "module:blk_b"),
    }
    # A testbench with a declared `toplevel:` elaborates from one.
    assert _links_of_type(graph, "elaborates_as") == {
        ("tb:verif/blk_a#tb_cocotb", "module:blk_a"),
    }
    # A non-simulation run runs against one: the model's name for synth/cdc, possibly a
    # wrapper for fpv (see
    # test_an_fpv_top_that_overrides_the_model_is_where_targets_lands).
    assert _links_of_type(graph, "targets") == {
        ("test:impl/blk_a#blk_a_generic", "module:blk_a"),
        ("test:impl/blk_a#blk_a_lint", "module:blk_a"),
        ("test:fpv/blk_a#blk_a_safety", "module:blk_a"),
    }


def test_a_testbench_without_a_toplevel_gets_no_elaborates_as(graph):
    """A testbench without a `toplevel:` gets no `elaborates_as`; the config tier never
    infers."""
    declared = {
        node["id"]
        for node in graph["nodes"]
        if node["type"] == "testbench" and node.get("toplevel")
    }
    sourced = {
        link["source"]
        for link in graph["links"]
        if link["type"] == "elaborates_as" and link["source"].startswith("tb:")
    }
    assert sourced == declared


def test_module_nodes_are_not_created_by_the_config_tier(graph):
    # `module:<name>` is the design tier's id, so no stub is emitted; the dangling
    # target is the stitch point.
    assert not [n for n in graph["nodes"] if n["id"].startswith("module:")]


def test_cocotb_test_reaches_its_spec_block_through_tb_model_spec(graph):
    """A path query resolves test -> ... -> spec block."""
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


# Degenerate inputs


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
    # The search dirs govern spec/design/verif discovery. The repo-level regression
    # files are found at the project root and are not overridable.
    assert set(_nodes_by_type(graph, "test")) == {
        "test:impl/blk_a#blk_a_generic",
        "test:impl/blk_a#blk_a_lint",
        "test:fpv/blk_a#blk_a_safety",
    }


# Meta and serialization


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
        # Flow provenance must invalidate `rb graph build`'s no-op check.
        "regression.yaml",
        "synth_regression.yaml",
        "cdc_regression.yaml",
        "fpv_regression.yaml",
        "impl/blk_a/synth.yaml",
        "impl/blk_a/cdc.yaml",
        "fpv/blk_a/fpv.yaml",
    }
    assert all(len(entry["sha256"]) == 64 for entry in inputs)
    # Hashes are provenance, not graph content, so they stay out of graph.json.
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


# Downstream consumers (networkx is optional; this guards the envelope against the real
# reader)


def test_graph_loads_as_a_networkx_multidigraph(graph):
    nx = pytest.importorskip("networkx")
    loaded = nx.node_link_graph(graph, edges="links")
    assert loaded.is_directed() and loaded.is_multigraph()
    # Every node survives, plus the dangling design-tier targets that node-link auto-
    # creates.
    assert set(loaded.nodes) >= {n["id"] for n in graph["nodes"]}
    assert "module:blk_a" in loaded.nodes
    assert loaded.number_of_edges() == len(graph["links"])
