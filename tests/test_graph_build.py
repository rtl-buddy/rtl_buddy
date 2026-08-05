"""Tests for #377 — ``rb graph build``, the design-graph orchestrator.

``build_graph()`` runs three independent extractors and unions them:

  design   ``rtl-buddy-view graph`` per model (subprocess)
  config   :func:`rtl_buddy.graph.extract_config_tier` (in-process)
  binding  ``graphify`` (subprocess, optional)

Neither external tool is on PATH in CI, so both are stubbed with tiny
scripts that record their argv and write a canned node-link graph. That
is deliberate: what these tests pin is the *orchestration* contract —
argv shape, tier statuses, the node-id union, the fingerprint no-op, and
the machine envelope — none of which should depend on a real parse. One
end-to-end test against the installed ``rtl-buddy-view`` is included and
skips when the binary is absent.

Fixture: ``tests/fixtures/graph_config_tier`` (see
``test_graph_config_tier.py``), copied to a tmp dir and topped up with
the two ``.sv`` files its ``models.yaml`` entries name plus a
``root_config.yaml`` / ``regression.yaml``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.graph import build as graph_build
from rtl_buddy.graph import graphify as graphify_mod
from rtl_buddy.graph import (
    BINDING_TIER,
    CONFIG_TIER,
    DESIGN_TIER,
    MERGED_TIER,
    SCHEMA_VERSION,
    VIEW_GRAPH_MIN_VERSION,
    build_graph,
    dangling_targets,
    merge_graphs,
    stitch_points,
)
from rtl_buddy.graph.merge import fingerprint, hash_inputs
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the config-tier fixture that can also be design-exported."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", target)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", target)
    (target / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs:\n  - verif/blk_a/tests.yaml\n"
    )
    for name in ("blk_a", "blk_b"):
        (target / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    monkeypatch.chdir(target)
    return target


def _design_graph(top: str) -> dict:
    """A minimal but contract-shaped design-tier export for ``top``."""
    return {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": 1,
            "generator": {
                "tool": "rtl-buddy-view",
                "version": "0.4.0",
                "tier": "design",
            },
            "project_root_rel": "../../../..",
            "design": {"top": top, "dut_top": top, "tb_top": None},
        },
        "nodes": [
            {
                "id": f"module:{top}",
                "type": "module",
                "label": top,
                "tier": "design",
                "file": f"design/{top}/{top}.sv",
                "line": 1,
            },
            {
                "id": f"port:{top}.clk",
                "type": "port",
                "label": "clk",
                "tier": "design",
                "file": f"design/{top}/{top}.sv",
                "line": 1,
                "dir": "input",
            },
            {
                "id": f"inst:{top}/{top}",
                "type": "instance",
                "label": top,
                "tier": "design",
                "file": f"design/{top}/{top}.sv",
                "line": 1,
            },
        ],
        "links": [
            {
                "source": f"inst:{top}/{top}",
                "target": f"module:{top}",
                "type": "instance_of",
                "confidence": "EXTRACTED",
            }
        ],
    }


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_view(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """Stub ``rtl-buddy-view`` writing a design graph for ``--top``.

    Appends its argv to a JSON-lines record so a test can assert both the
    CLI shape and that a cached re-run invoked it zero times.
    """
    record = tmp_path / "view-argv.jsonl"
    script = _write_script(
        tmp_path / "rtl-buddy-view",
        f"""
import json, sys
argv = sys.argv[1:]
with open({json.dumps(str(record))}, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv and argv[0] == "--version":
    print("rtl-buddy-view 0.4.0")
    sys.exit(0)
top = argv[argv.index("--top") + 1]
out = argv[argv.index("--output") + 1]
graph = json.loads({json.dumps(json.dumps(_design_graph("__TOP__")))}.replace(
    "__TOP__", top))
import os
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as fh:
    json.dump(graph, fh)
sys.exit({exit_code})
""",
    )
    return script, record


def _fake_graphify(
    tmp_path: Path, *, exit_code: int = 0, merge_phantom: bool = False
) -> tuple[Path, Path]:
    """Stub ``graphify`` handling ``extract`` and ``merge-graphs``.

    ``extract`` emits one binding-tier node bound to the fixture's cocotb
    test; ``merge-graphs`` unions the tier files it is handed, which is
    what the cross-check compares against. ``merge_phantom`` makes its
    union invent a node the tier files never had, i.e. a disagreement.
    """
    record = tmp_path / "graphify-argv.jsonl"
    node = "pymod:verif/blk_a/cocotb_blk_a.py"
    script = _write_script(
        tmp_path / "graphify",
        f"""
import json, os, sys
argv = sys.argv[1:]
with open({json.dumps(str(record))}, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv and argv[0] == "--version":
    print("graphify 1.2.3")
    sys.exit(0)
verb = argv[0]
out = argv[argv.index("--output") + 1]
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
if verb == "extract":
    graph = {{
        "directed": True,
        "multigraph": True,
        "graph": {{"schema_version": 1, "generator": {{
            "tool": "graphify", "version": "1.2.3", "tier": "binding"}}}},
        "nodes": [{{"id": {json.dumps(node)}, "type": "python_module",
                   "label": "cocotb_blk_a", "tier": "binding"}}],
        "links": [{{"source": "test:verif/blk_a#t_cocotb",
                   "target": {json.dumps(node)},
                   "type": "binds_to", "confidence": "EXTRACTED"}}],
    }}
else:
    inputs = [a for a in argv[1:] if a.endswith(".json") and a != out]
    nodes, links = {{}}, []
    for path in inputs:
        with open(path) as fh:
            data = json.load(fh)
        for n in data.get("nodes") or []:
            nodes.setdefault(n["id"], n)
        links.extend(data.get("links") or [])
    if {merge_phantom!r}:
        nodes["phantom:1"] = {{"id": "phantom:1", "type": "x"}}
    graph = {{"directed": True, "multigraph": True, "graph": {{}},
             "nodes": list(nodes.values()), "links": links}}
with open(out, "w") as fh:
    json.dump(graph, fh)
sys.exit({exit_code})
""",
    )
    return script, record


def _argv_lines(record: Path) -> list[list[str]]:
    if not record.is_file():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line]


def _graph_calls(record: Path) -> list[list[str]]:
    """Only the ``graph`` invocations — the CLI probes ``--version`` first."""
    return [argv for argv in _argv_lines(record) if argv[:1] == ["graph"]]


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_graph_build")


def _nodes(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


# ---------------------------------------------------------------------------
# Merge (pure)
# ---------------------------------------------------------------------------


def test_merge_is_a_node_id_union_with_earlier_tiers_winning():
    design = {
        "graph": {"generator": {"tool": "rtl-buddy-view", "tier": "design"}},
        "nodes": [
            {
                "id": "module:blk_a",
                "type": "module",
                "label": "blk_a",
                "file": "design/blk_a/blk_a.sv",
            }
        ],
        "links": [],
    }
    config = {
        "graph": {"generator": {"tool": "rtl_buddy", "tier": "config"}},
        # Same id, less detail, and a bogus label: the design tier parsed
        # the RTL, so it must not be overwritten by a later tier.
        "nodes": [
            {"id": "module:blk_a", "type": "module", "label": "stale", "desc": "d"},
            {"id": "model:design/blk_a/models.yaml#blk_a", "type": "model"},
        ],
        "links": [
            {
                "source": "model:design/blk_a/models.yaml#blk_a",
                "target": "module:blk_a",
                "type": "maps_to",
            }
        ],
    }
    merged = merge_graphs(
        [(CONFIG_TIER, config), (DESIGN_TIER, design)],
        generator={"tool": "rtl_buddy", "version": "9.9.9"},
        schema_version=SCHEMA_VERSION,
    )
    nodes = _nodes(merged)
    assert set(nodes) == {"module:blk_a", "model:design/blk_a/models.yaml#blk_a"}
    assert nodes["module:blk_a"]["label"] == "blk_a"
    assert nodes["module:blk_a"]["file"] == "design/blk_a/blk_a.sv"
    # Attributes only the later tier knows still get filled in.
    assert nodes["module:blk_a"]["desc"] == "d"
    # And the node records that two tiers know it — that is the stitch.
    assert nodes["module:blk_a"]["tiers"] == ["design", "config"]
    assert merged["graph"]["generator"]["tier"] == MERGED_TIER
    assert merged["graph"]["generator"]["tiers"] == ["design", "config"]


def test_merge_keeps_distinct_links_and_collapses_identical_ones():
    graph = {
        "nodes": [{"id": "a", "type": "x"}, {"id": "b", "type": "y"}],
        "links": [
            {"source": "a", "target": "b", "type": "connects", "formal": "clk"},
            {"source": "a", "target": "b", "type": "connects", "formal": "rst"},
            {"source": "a", "target": "b", "type": "connects", "formal": "clk"},
        ],
    }
    merged = merge_graphs(
        [(DESIGN_TIER, graph)],
        generator={"tool": "rtl_buddy", "version": "9.9.9"},
        schema_version=SCHEMA_VERSION,
    )
    assert len(merged["links"]) == 2


def test_merge_output_is_deterministic():
    graphs = [(DESIGN_TIER, _design_graph("blk_a"))]
    kwargs = {
        "generator": {"tool": "rtl_buddy", "version": "9.9.9"},
        "schema_version": SCHEMA_VERSION,
    }
    assert json.dumps(merge_graphs(graphs, **kwargs)) == json.dumps(
        merge_graphs(graphs, **kwargs)
    )


def test_stitch_points_counts_cross_tier_references_not_just_shared_nodes():
    design = {"nodes": [{"id": "module:blk_a", "type": "module"}], "links": []}
    config = {
        "nodes": [{"id": "model:m#blk_a", "type": "model"}],
        "links": [
            {"source": "model:m#blk_a", "target": "module:blk_a", "type": "maps_to"}
        ],
    }
    # Neither tier defines a node the other also defines; the join is the
    # config tier's link pointing INTO the design tier's node.
    assert stitch_points([(DESIGN_TIER, design), (CONFIG_TIER, config)]) == [
        "module:blk_a"
    ]


def test_dangling_targets_names_unresolved_link_endpoints():
    config_only = {
        "nodes": [{"id": "model:m#blk_a", "type": "model"}],
        "links": [
            {"source": "model:m#blk_a", "target": "module:blk_a", "type": "maps_to"}
        ],
    }
    assert dangling_targets(config_only) == ["module:blk_a"]


def test_fingerprint_changes_with_inputs_and_with_tool_versions(tmp_path: Path):
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n")
    base = {
        "schema_version": SCHEMA_VERSION,
        "tools": {"rtl-buddy-view": "0.4.0"},
        "tier_inputs": {"design": hash_inputs(tmp_path, [src])},
    }
    first = fingerprint(**base)
    assert first == fingerprint(**base)

    src.write_text("module a; wire w; endmodule\n")
    changed = dict(base, tier_inputs={"design": hash_inputs(tmp_path, [src])})
    assert fingerprint(**changed) != first

    upgraded = dict(base, tools={"rtl-buddy-view": "0.5.0"})
    assert fingerprint(**upgraded) != first


def test_hash_inputs_records_a_missing_file_instead_of_raising(tmp_path: Path):
    entries = hash_inputs(tmp_path, [tmp_path / "gone.sv"])
    assert entries == [{"path": "gone.sv", "sha256": None}]


# ---------------------------------------------------------------------------
# Viewer version gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,gated",
    [
        (None, False),  # unprobeable: let the invocation decide
        ("0.3.1", True),
        ("0.3.1.dev1+gabc", False),  # editable feature branch
        (VIEW_GRAPH_MIN_VERSION, False),
        ("1.0.0", False),
    ],
)
def test_view_version_gate(version, gated):
    reason = graph_build.check_view_supports_graph(version)
    assert (reason is not None) is gated
    if gated:
        assert VIEW_GRAPH_MIN_VERSION in reason


# ---------------------------------------------------------------------------
# build_graph()
# ---------------------------------------------------------------------------


def test_build_merges_design_and_config_and_writes_both_files(
    graph_project: Path, tmp_path: Path
):
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_enabled=False,
    )

    assert build.graph_path == graph_project / "artefacts" / "graph" / "graph.json"
    assert build.meta_path.is_file()
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)

    # Both tiers landed, and the config tier's maps_to target resolves to
    # the design tier's module node — the whole point of the merge.
    assert "module:blk_a" in nodes and "module:blk_b" in nodes
    assert "model:design/blk_a/models.yaml#blk_a" in nodes
    # The design tier is the only tier that *defines* a module node; the
    # config tier only points maps_to at it, so a resolved stitch shows up
    # as an empty dangling list, not as a shared node.
    assert nodes["module:blk_a"]["tier"] == "design"
    assert dangling_targets(graph) == []
    assert build.merge["strategy"] == "node-id-union"
    assert build.merge["stitch_points"] >= 2

    statuses = {t.tier: t.status for t in build.tiers}
    assert statuses[DESIGN_TIER] == "built"
    assert statuses[CONFIG_TIER] == "built"
    assert statuses[BINDING_TIER] == "skipped"

    # One viewer invocation per model, with the shared hier.f filelist.
    calls = _argv_lines(record)
    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "graph"
        assert argv[argv.index("--filelist") + 1].endswith("hier.f")
        assert Path(argv[argv.index("--filelist") + 1]).is_file()
        assert argv[argv.index("--project-root") + 1] == str(graph_project)
    assert (graph_project / "artefacts" / "hier" / "blk_a" / "hier.f").is_file()


def test_meta_records_tool_versions_input_hashes_and_per_tier_provenance(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_enabled=False,
    )
    meta = json.loads(build.meta_path.read_text())

    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["generated_by"]["command"] == "graph build"
    assert meta["tools"]["rtl-buddy-view"] == "0.4.0"
    assert isinstance(meta["tools"]["rtl-buddy"], str)
    assert meta["fingerprint"] == build.fingerprint

    design_inputs = {e["path"] for e in meta["tiers"][DESIGN_TIER]["inputs"]}
    assert "design/blk_a/blk_a.sv" in design_inputs
    config_inputs = {e["path"] for e in meta["tiers"][CONFIG_TIER]["inputs"]}
    assert "verif/blk_a/tests.yaml" in config_inputs
    assert all(
        e["sha256"] and len(e["sha256"]) == 64
        for e in meta["tiers"][CONFIG_TIER]["inputs"]
    )
    assert meta["tiers"][DESIGN_TIER]["generator"]["tool"] == "rtl-buddy-view"
    assert meta["tiers"][CONFIG_TIER]["generator"]["tool"] == "rtl_buddy"


def test_rebuild_with_no_change_is_a_no_op(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "graphify_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    assert first.unchanged is False
    before = first.graph_path.read_bytes()
    record.unlink()

    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is True
    assert second.fingerprint == first.fingerprint
    assert second.nodes == first.nodes and second.links == first.links
    assert first.graph_path.read_bytes() == before
    # Nothing re-ran: the expensive parse is what the fingerprint buys.
    assert _argv_lines(record) == []
    # The envelope still describes the graph on disk rather than zeroes.
    cached = {t.tier: t for t in second.tiers}
    assert cached[DESIGN_TIER].status == "cached"
    assert cached[DESIGN_TIER].nodes == first.tiers[0].nodes
    assert cached[CONFIG_TIER].status == "cached"
    assert cached[CONFIG_TIER].nodes > 0


def test_changed_rtl_invalidates_the_cached_graph(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "graphify_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    record.unlink()
    (graph_project / "design" / "blk_a" / "blk_a.sv").write_text(
        "module blk_a (input logic clk, input logic rst_n);\nendmodule\n"
    )
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint
    assert len(_argv_lines(record)) == 2


def test_changed_yaml_invalidates_the_cached_graph(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "graphify_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    tests_yaml = graph_project / "verif" / "blk_a" / "tests.yaml"
    tests_yaml.write_text(tests_yaml.read_text().replace("reglvl: 0", "reglvl: 5"))
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint


def test_force_rebuilds_an_unchanged_project(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "graphify_enabled": False,
    }
    build_graph(graph_project, **kwargs)
    record.unlink()
    again = build_graph(graph_project, force=True, **kwargs)
    assert again.unchanged is False
    assert len(_argv_lines(record)) == 2


def test_a_failed_model_export_does_not_sink_the_build(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path, exit_code=3)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "failed"
    # ... but the config tier is still written and queryable.
    assert build.graph_path.is_file()
    graph = json.loads(build.graph_path.read_text())
    assert "model:design/blk_a/models.yaml#blk_a" in _nodes(graph)
    meta = json.loads(build.meta_path.read_text())
    failure = meta["tiers"][DESIGN_TIER]["failures"][0]
    assert failure["model"] in {"blk_a", "blk_b"}
    assert "log" in failure


def test_a_still_failing_tier_is_not_reported_as_cached(
    graph_project: Path, tmp_path: Path
):
    """A matching fingerprint proves the inputs held still, not that the
    tier works — otherwise a permanently broken viewer goes green on the
    second run."""
    view, _ = _fake_view(tmp_path, exit_code=3)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "graphify_enabled": False,
    }
    build_graph(graph_project, **kwargs)
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is True
    assert {t.tier for t in second.failed_tiers()} == {DESIGN_TIER}


def test_an_outdated_viewer_fails_the_design_tier_with_an_upgrade_hint(
    graph_project: Path, tmp_path: Path
):
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.3.1",
        graphify_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "failed"
    assert VIEW_GRAPH_MIN_VERSION in design.detail
    assert _argv_lines(record) == []  # gated before invoking
    assert build.graph_path.is_file()


def test_no_design_writes_a_config_only_graph(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        design=False,
        graphify_enabled=False,
    )
    assert _argv_lines(record) == []
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "skipped"
    graph = json.loads(build.graph_path.read_text())
    assert not [n for n in graph["nodes"] if n["type"] == "module"]
    # The stitch targets dangle, which is exactly what a config-only
    # export means.
    assert "module:blk_a" in dangling_targets(graph)


def test_explicit_model_selection_limits_the_design_tier(
    graph_project: Path, tmp_path: Path
):
    view, record = _fake_view(tmp_path)
    models = [
        m
        for m in graph_build.models_from_design_tree(graph_project / "design")
        if m.name == "blk_a"
    ]
    build = build_graph(
        graph_project,
        models=models,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_enabled=False,
    )
    assert [argv[argv.index("--top") + 1] for argv in _argv_lines(record)] == ["blk_a"]
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a" in _nodes(graph)
    assert "module:blk_b" not in _nodes(graph)


def test_models_from_regression_follows_the_suites_it_lists(graph_project: Path):
    models = graph_build.models_from_regression(graph_project / "regression.yaml")
    assert [m.name for m in models] == ["blk_a"]


# ---------------------------------------------------------------------------
# Graphify (optional binding tier)
# ---------------------------------------------------------------------------


def test_graphify_absent_still_succeeds_and_says_so(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_version=None,
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "skipped"
    assert "graphify" in binding.detail
    assert not build.failed_tiers()
    assert build.graph_path.is_file()
    meta = json.loads(build.meta_path.read_text())
    assert meta["merge"]["graphify_cross_check"]["status"] == "skipped"


def test_graphify_binding_tier_merges_and_cross_checks(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, gfy_record = _fake_graphify(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_executable=str(gfy),
        graphify_version="1.2.3",
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "built"
    graph = json.loads(build.graph_path.read_text())
    assert "pymod:verif/blk_a/cocotb_blk_a.py" in _nodes(graph)
    # binds_to lands on the config tier's test node: the binding stitch.
    assert any(
        link["type"] == "binds_to" and link["source"] == "test:verif/blk_a#t_cocotb"
        for link in graph["links"]
    )

    verbs = [argv[0] for argv in _argv_lines(gfy_record)]
    assert verbs == ["extract", "merge-graphs"]
    extract_argv = _argv_lines(gfy_record)[0]
    # The LLM pass is opt-in — it ships project source to a model.
    assert graphify_mod.LLM_FLAG not in extract_argv
    assert any(a.endswith("cocotb_blk_a.py") for a in extract_argv)
    assert any(a.endswith("README.md") for a in extract_argv)

    cross = json.loads(build.meta_path.read_text())["merge"]["graphify_cross_check"]
    assert cross["status"] == "ok"
    assert cross["internal_nodes"] == cross["graphify_nodes"] == build.nodes


def test_graphify_llm_pass_is_opt_in(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    gfy, gfy_record = _fake_graphify(tmp_path)
    build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_executable=str(gfy),
        graphify_version="1.2.3",
        graphify_llm=True,
        graphify_cross_check=False,
    )
    assert graphify_mod.LLM_FLAG in _argv_lines(gfy_record)[0]


def test_graphify_failure_leaves_the_other_tiers_intact(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, _ = _fake_graphify(tmp_path, exit_code=4)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_executable=str(gfy),
        graphify_version="1.2.3",
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "failed"
    assert "exit 4" in binding.detail
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a" in _nodes(graph)


def test_graphify_merge_disagreement_is_reported_not_adopted(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, _ = _fake_graphify(tmp_path, merge_phantom=True)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_executable=str(gfy),
        graphify_version="1.2.3",
    )
    cross = json.loads(build.meta_path.read_text())["merge"]["graphify_cross_check"]
    assert cross["status"] == "mismatch"
    assert cross["only_graphify"] == ["phantom:1"]
    # The internal union is always what ships — the cross-check is a
    # report, never a substitute.
    assert build.merge["strategy"] == "node-id-union"
    graph = json.loads(build.graph_path.read_text())
    assert "phantom:1" not in _nodes(graph)


def test_graphify_argv_shapes():
    extract = graphify_mod.build_extract_cmd("graphify", ["a.py"], "out.json")
    assert extract[:2] == ["graphify", graphify_mod.EXTRACT_VERB]
    assert extract[extract.index("--output") + 1] == "out.json"
    assert extract[-1] == "a.py"
    merge = graphify_mod.build_merge_cmd("graphify", ["a.json", "b.json"], "m.json")
    assert merge[:2] == ["graphify", graphify_mod.MERGE_VERB]
    assert merge[-2:] == ["a.json", "b.json"]


def test_graphify_collect_inputs_is_verif_python_and_spec_markdown(
    graph_project: Path,
):
    found = graphify_mod.collect_inputs(graph_project / "verif", graph_project / "spec")
    names = {os.path.basename(p) for p in found}
    assert "cocotb_blk_a.py" in names
    assert "README.md" in names
    # The RTL is the design tier's job, and YAML is the config tier's.
    assert not any(p.endswith((".sv", ".yaml")) for p in found)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_machine_envelope_reports_every_tier(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "graph", "build", "--tool", str(view), "--no-graphify"]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["command"] == "graph build"
    assert envelope["exit_code"] == 0
    payload = envelope["payload"]
    assert payload["graph"] == "artefacts/graph/graph.json"
    assert payload["meta"] == "artefacts/graph/graph-meta.json"
    assert payload["unchanged"] is False
    assert payload["nodes"] > 0 and payload["links"] > 0
    assert {t["tier"] for t in payload["tiers"]} == {
        DESIGN_TIER,
        CONFIG_TIER,
        BINDING_TIER,
    }
    assert payload["merge"]["strategy"] == "node-id-union"
    assert payload["fingerprint"]


def test_cli_model_selection_and_unknown_model(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    runner, rb = _runner()
    ok = runner.invoke(
        rb.app,
        ["graph", "build", "--model", "blk_a", "--tool", str(view), "--no-graphify"],
    )
    assert ok.exit_code == 0, ok.output
    assert [a[a.index("--top") + 1] for a in _graph_calls(record)] == ["blk_a"]

    bad = runner.invoke(
        rb.app, ["graph", "build", "--model", "nope", "--tool", str(view)]
    )
    assert bad.exit_code != 0


def test_cli_model_and_regression_are_mutually_exclusive(graph_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["graph", "build", "--model", "blk_a", "-c", "regression.yaml"]
    )
    assert result.exit_code != 0


def test_cli_regression_selects_the_models_its_suites_run(
    graph_project: Path, tmp_path: Path
):
    view, record = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "graph",
            "build",
            "-c",
            "regression.yaml",
            "--tool",
            str(view),
            "--no-graphify",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [a[a.index("--top") + 1] for a in _graph_calls(record)] == ["blk_a"]


def test_cli_exits_nonzero_when_a_tier_fails(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path, exit_code=3)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "graph", "build", "--tool", str(view), "--no-graphify"],
    )
    assert result.exit_code == 1
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["exit_code"] == 1
    design = next(t for t in envelope["payload"]["tiers"] if t["tier"] == DESIGN_TIER)
    assert design["status"] == "failed"
    # The graph is still on disk: a dead tier is not a dead build.
    assert (graph_project / "artefacts" / "graph" / "graph.json").is_file()


def test_cli_strict_promotes_a_per_item_failure_to_a_nonzero_exit(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    # Break one model's filelist so a single model fails while the other
    # still exports: not a dead tier, but --strict should still complain.
    (graph_project / "design" / "blk_b" / "blk_b.sv").unlink()
    runner, rb = _runner()
    lenient = runner.invoke(
        rb.app, ["graph", "build", "--tool", str(view), "--no-graphify"]
    )
    assert lenient.exit_code == 0, lenient.output
    strict = runner.invoke(
        rb.app,
        ["graph", "build", "--tool", str(view), "--no-graphify", "--strict", "--force"],
    )
    assert strict.exit_code == 1


def test_cli_second_run_reports_unchanged(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    runner, rb = _runner()
    args = ["--machine", "graph", "build", "--tool", str(view), "--no-graphify"]
    assert runner.invoke(rb.app, args).exit_code == 0
    second = runner.invoke(rb.app, args)
    assert second.exit_code == 0, second.output
    envelope = json.loads(second.output.strip().splitlines()[-1])
    assert envelope["payload"]["unchanged"] is True


# ---------------------------------------------------------------------------
# End to end against the real viewer (skipped when it isn't installed)
# ---------------------------------------------------------------------------


def _real_view_supports_graph() -> bool:
    exe = shutil.which("rtl-buddy-view")
    if exe is None:
        return False
    probe = subprocess.run(
        [exe, "graph", "--help"], capture_output=True, text=True, timeout=60
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    not _real_view_supports_graph(),
    reason="rtl-buddy-view with the `graph` verb is not installed",
)
def test_end_to_end_with_the_installed_viewer(graph_project: Path):
    build = build_graph(graph_project, graphify_enabled=False)
    assert not build.failed_tiers(), build.tiers
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert nodes["module:blk_a"]["file"] == "design/blk_a/blk_a.sv"
    assert nodes["module:blk_a"]["tier"] == "design"
    assert "port:blk_a.clk" in nodes
    assert dangling_targets(graph) == []
    # The merged envelope is loadable by NetworkX readers.
    assert graph["graph"]["schema_version"] == SCHEMA_VERSION
    assert graph["directed"] is True and graph["multigraph"] is True
