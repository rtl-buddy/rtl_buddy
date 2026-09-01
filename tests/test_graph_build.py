"""Tests for #377 — ``rb graph build``, the design-graph orchestrator.

``build_graph()`` runs three independent extractors and unions them:

  design   ``rtl-buddy-view graph`` per model (subprocess)
  config   :func:`rtl_buddy.graph.extract_config_tier` (in-process)
  binding  ``rb-graph-extract`` (subprocess, optional)

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
import tomllib
from pathlib import Path

from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement
from typer.testing import CliRunner

from rtl_buddy.graph import build as graph_build
from rtl_buddy.graph import extract as extract_mod
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
from rtl_buddy.errors import FatalRtlBuddyError
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
    # `verif/blk_a`'s `tb_hdl` names this in its filelist; without it the
    # TB-rooted export has no filelist to write and the whole TB path is
    # silently untested.
    (target / "verif" / "blk_a" / "tb_top.sv").write_text(
        "module tb_top;\n  logic clk;\n  blk_a i_dut (.clk(clk));\nendmodule\n"
    )
    monkeypatch.chdir(target)
    return target


def _second_suite(project: Path, *, tb_name: str) -> Path:
    """A blk_b suite whose testbench top collides with blk_a's.

    Both testbenches are topped by a module the stub names after the
    testbench, and both live in a file called ``tb_top.sv`` — the exact
    shape of the real collision (every suite in the project template
    calls its testbench top ``tb_top``).
    """
    suite = project / "verif" / "blk_b"
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "tb_top.sv").write_text(
        "module tb_top;\n  logic clk;\n  blk_b i_dut (.clk(clk));\nendmodule\n"
    )
    (suite / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        f'  - name: "{tb_name}"\n'
        '    filelist: ["tb_top.sv"]\n'
        "tests:\n"
        '  - name: "t_b"\n'
        '    desc: "blk_b HDL test"\n'
        "    reglvl: 0\n"
        '    model: "blk_b"\n'
        '    model_path: "../../design/blk_b/models.yaml"\n'
        f'    testbench: "{tb_name}"\n'
    )
    return suite


def _flow_suite(
    project: Path,
    *,
    suite: str = "blk_a_chk",
    dut: str = "blk_a",
    top: str = "blk_a_chk",
    runs: tuple[str, ...] = ("chk_bmc", "chk_prove"),
) -> Path:
    """An fpv suite whose runs top at a checker in its own properties file.

    The template shape #385 exists for: the checker module elaborates
    only over model filelist + ``properties:``, so without a run-rooted
    export the config tier's ``targets`` stitch has nowhere to land.
    Two runs (bmc + prove) share the checker, which is what the
    de-duplication rule is about.
    """
    suite_dir = project / "fpv" / suite
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / f"{suite}_props.sv").write_text(
        f"module {top} (input logic clk);\n  {dut} i_dut (.clk(clk));\nendmodule\n"
    )
    entries = "\n".join(
        f'  - name: "{run}"\n'
        f'    desc: "{run} over the checker"\n'
        '    tool: "sby"\n'
        f'    model: "{dut}"\n'
        f'    model_path: "../../design/{dut}/models.yaml"\n'
        f'    top: "{top}"\n'
        "    properties:\n"
        f'      - "{suite}_props.sv"\n'
        f'    mode: "{mode}"\n'
        for run, mode in zip(runs, ("bmc", "prove"))
    )
    (suite_dir / "fpv.yaml").write_text(
        f"rtl-buddy-filetype: fpv_config\nverifications:\n{entries}"
    )
    reg = project / "fpv_regression.yaml"
    reg.write_text(reg.read_text() + f'  - "fpv/{suite}/fpv.yaml"\n')
    return suite_dir


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


def _tb_design_graph(tb_top: str, dut_top: str, tb_file: str) -> dict:
    """A TB-rooted export: the testbench, its DUT instance, and the DUT.

    The DUT's ``module:``/``port:`` nodes carry the *same* ids and the
    same ``file`` as the DUT-rooted export of the same design — that
    identity is the weld the merge relies on, so the stub has to
    reproduce it or the tests would be checking a fiction.
    """
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
            "project_root_rel": "../../../../..",
            "design": {"top": tb_top, "dut_top": dut_top, "tb_top": tb_top},
        },
        "nodes": [
            {
                "id": f"module:{tb_top}",
                "type": "module",
                "label": tb_top,
                "tier": "design",
                "file": tb_file,
                "line": 1,
            },
            {
                "id": f"inst:{tb_top}/{tb_top}",
                "type": "instance",
                "label": tb_top,
                "tier": "design",
                "file": tb_file,
                "line": 1,
            },
            {
                "id": f"inst:{tb_top}/{tb_top}.i_dut",
                "type": "instance",
                "label": "i_dut",
                "tier": "design",
                "file": tb_file,
                "line": 3,
            },
            {
                "id": f"module:{dut_top}",
                "type": "module",
                "label": dut_top,
                "tier": "design",
                "file": f"design/{dut_top}/{dut_top}.sv",
                "line": 1,
            },
            {
                "id": f"port:{dut_top}.clk",
                "type": "port",
                "label": "clk",
                "tier": "design",
                "file": f"design/{dut_top}/{dut_top}.sv",
                "line": 1,
                "dir": "input",
            },
        ],
        "links": [
            {
                "source": f"inst:{tb_top}/{tb_top}",
                "target": f"module:{tb_top}",
                "type": "instance_of",
                "confidence": "EXTRACTED",
            },
            {
                "source": f"inst:{tb_top}/{tb_top}.i_dut",
                "target": f"module:{dut_top}",
                "type": "instance_of",
                "confidence": "EXTRACTED",
            },
            {
                "source": f"inst:{tb_top}/{tb_top}.i_dut",
                "target": f"inst:{tb_top}/{tb_top}",
                "type": "child_of",
                "confidence": "EXTRACTED",
            },
            {
                "source": f"module:{tb_top}",
                "target": f"module:{dut_top}",
                "type": "instantiates",
                "confidence": "EXTRACTED",
            },
        ],
    }


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_view(
    tmp_path: Path, *, exit_code: int = 0, tb_exit_code: int | None = None
) -> tuple[Path, Path]:
    """Stub ``rtl-buddy-view`` writing a design graph for ``--top``.

    With ``--tb-top`` it writes the TB-rooted shape instead, rooted at
    ``$FAKE_VIEW_TB_TOP`` when that is set — which is how the real
    viewer behaves when it auto-corrects a ``--tb-top`` hint that names
    no module in the elaborated design.

    Appends its argv to a JSON-lines record so a test can assert both the
    CLI shape and that a cached re-run invoked it zero times.
    ``tb_exit_code`` fails only the TB invocations, leaving the DUT
    exports healthy.
    """
    record = tmp_path / "view-argv.jsonl"
    script = _write_script(
        tmp_path / "rtl-buddy-view",
        f"""
import json, os, sys
argv = sys.argv[1:]
with open({json.dumps(str(record))}, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv and argv[0] == "--version":
    print("rtl-buddy-view 0.4.0")
    sys.exit(0)
top = argv[argv.index("--top") + 1]
out = argv[argv.index("--output") + 1]
code = {exit_code}
if "--tb-top" in argv:
    hint = argv[argv.index("--tb-top") + 1]
    tb_top = os.environ.get("FAKE_VIEW_TB_TOP") or hint
    filelist = argv[argv.index("--filelist") + 1]
    root = argv[argv.index("--project-root") + 1]
    with open(filelist) as fh:
        entries = [ln.strip() for ln in fh
                   if ln.strip() and not ln.startswith(("#", "//"))]
    base = os.path.dirname(os.path.abspath(filelist))
    tb_file = os.path.relpath(
        os.path.abspath(os.path.join(base, entries[-1])), root)
    graph = json.loads(
        {json.dumps(json.dumps(_tb_design_graph("__TB__", "__DUT__", "__FILE__")))}
        .replace("__TB__", tb_top).replace("__DUT__", top)
        .replace("__FILE__", tb_file))
    tb_code = {tb_exit_code!r}
    if tb_code is not None:
        code = tb_code
else:
    graph = json.loads({json.dumps(json.dumps(_design_graph("__TOP__")))}.replace(
        "__TOP__", top))
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as fh:
    json.dump(graph, fh)
sys.exit(code)
""",
    )
    return script, record


def _fake_extractor(
    tmp_path: Path, *, exit_code: int = 0, merge_phantom: bool = False
) -> tuple[Path, Path]:
    """Stub extractor handling ``extract`` and ``merge-graphs``.

    ``extract`` emits one binding-tier node bound to the fixture's cocotb
    test; ``merge-graphs`` unions the tier files it is handed, which is
    what the cross-check compares against. ``merge_phantom`` makes its
    union invent a node the tier files never had, i.e. a disagreement.
    """
    record = tmp_path / "extract-argv.jsonl"
    node = "pymod:verif/blk_a/cocotb_blk_a.py"
    script = _write_script(
        tmp_path / "extractor",
        f"""
import json, os, sys
argv = sys.argv[1:]
with open({json.dumps(str(record))}, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv and argv[0] == "--version":
    print("rb-graph-extract 1.2.3")
    sys.exit(0)
verb = argv[0]
out = argv[argv.index("--output") + 1]
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
if verb == "extract":
    graph = {{
        "directed": True,
        "multigraph": True,
        "graph": {{"schema_version": 1, "generator": {{
            "tool": "rb-graph-extract", "version": "1.2.3", "tier": "binding"}}}},
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


def _dut_calls(record: Path) -> list[list[str]]:
    """DUT-rooted exports: one per model, no ``--tb-top``."""
    return [argv for argv in _graph_calls(record) if "--tb-top" not in argv]


def _call_filelist(argv: list[str]) -> Path:
    return Path(argv[argv.index("--filelist") + 1])


def _tb_calls(record: Path) -> list[list[str]]:
    """TB-rooted exports: one per testbench."""
    return [
        argv
        for argv in _graph_calls(record)
        if "--tb-top" in argv and _call_filelist(argv).parent.parent.name != "run"
    ]


def _run_calls(record: Path) -> list[list[str]]:
    """Run-rooted exports (#385): one per flow run top."""
    return [
        argv
        for argv in _graph_calls(record)
        if "--tb-top" in argv and _call_filelist(argv).parent.parent.name == "run"
    ]


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

    # A tier can hash the same inputs and still owe a different sidecar:
    # what it *selected* is part of what `graph-meta.json` reports.
    narrowed = dict(base, selection={"design": {"models": ["design/a#a"]}})
    assert fingerprint(**narrowed) != first
    assert fingerprint(**narrowed) == fingerprint(**narrowed)
    assert fingerprint(**dict(base, selection={})) == first


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
        extract_enabled=False,
    )

    assert build.graph_path == graph_project / "artefacts" / "graph" / "graph.json"
    assert build.meta_path.is_file()
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)

    # Both tiers landed, and the config tier's `maps_to` target resolves to
    # the design tier's module node — the whole point of the merge.
    assert "module:blk_a" in nodes and "module:blk_b" in nodes
    assert "model:design/blk_a/models.yaml#blk_a" in nodes
    # The design tier is the only tier that *defines* a module node; the
    # config tier only points its stitches at it, so a resolved stitch shows up
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
    calls = _dut_calls(record)
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
        extract_enabled=False,
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
        "extract_enabled": False,
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
        "extract_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    record.unlink()
    (graph_project / "design" / "blk_a" / "blk_a.sv").write_text(
        "module blk_a (input logic clk, input logic rst_n);\nendmodule\n"
    )
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint
    assert len(_dut_calls(record)) == 2


def test_changed_yaml_invalidates_the_cached_graph(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "extract_enabled": False,
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
        "extract_enabled": False,
    }
    build_graph(graph_project, **kwargs)
    record.unlink()
    again = build_graph(graph_project, force=True, **kwargs)
    assert again.unchanged is False
    assert len(_dut_calls(record)) == 2


def test_a_failed_model_export_does_not_sink_the_build(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path, exit_code=3)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "failed"
    # ... but the config tier is still written and queryable.
    assert build.graph_path.is_file()
    graph = json.loads(build.graph_path.read_text())
    assert "model:design/blk_a/models.yaml#blk_a" in _nodes(graph)
    meta = json.loads(build.meta_path.read_text())
    failures = meta["tiers"][DESIGN_TIER]["failures"]
    failure = next(f for f in failures if "model" in f)
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
        "extract_enabled": False,
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
        extract_enabled=False,
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
        extract_enabled=False,
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
        extract_enabled=False,
    )
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record)] == ["blk_a"]
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a" in _nodes(graph)
    assert "module:blk_b" not in _nodes(graph)


def test_models_from_regression_follows_the_suites_it_lists(graph_project: Path):
    models = graph_build.models_from_regression(graph_project / "regression.yaml")
    assert [m.name for m in models] == ["blk_a"]


# ---------------------------------------------------------------------------
# TB-rooted design tier
#
# The design tier used to be DUT hierarchies only: a `tb:` node carried
# what tests.yaml said about a testbench and nothing about the modules it
# elaborates. These pin the other half — one TB-rooted export per
# testbench, welded to the DUT it instantiates.
# ---------------------------------------------------------------------------


def test_testbench_selection_skips_dut_rooted_tbs_and_dedupes(graph_project: Path):
    targets = graph_build.testbenches_from_suites(
        graph_project, graph_project / "verif"
    )
    # `tb_cocotb` declares `toplevel: blk_a` — its top *is* the DUT top,
    # so the DUT export already covers it and re-elaborating would only
    # produce the same nodes a second time. `tb_unused` is declared but
    # no test names it, so nothing says which model it runs against.
    assert [(t.suite_rel, t.tb_name, t.tb_top) for t in targets] == [
        ("verif/blk_a", "tb_hdl", "tb_hdl")
    ]
    assert targets[0].node_id == "tb:verif/blk_a#tb_hdl"
    assert targets[0].model.name == "blk_a"


def test_testbench_selection_honours_the_model_filter(graph_project: Path):
    _second_suite(graph_project, tb_name="tb_hdl_b")
    only_b = [
        m
        for m in graph_build.models_from_design_tree(graph_project / "design")
        if m.name == "blk_b"
    ]
    targets = graph_build.testbenches_from_suites(
        graph_project, graph_project / "verif", only_b
    )
    assert [t.tb_name for t in targets] == ["tb_hdl_b"]


def test_two_tests_sharing_a_testbench_export_it_once(
    graph_project: Path, tmp_path: Path
):
    tests_yaml = graph_project / "verif" / "blk_a" / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text() + '\n  - name: "t_second"\n'
        '    desc: "same testbench, different plusargs"\n'
        "    reglvl: 0\n"
        '    model: "blk_a"\n'
        '    model_path: "../../design/blk_a/models.yaml"\n'
        '    testbench: "tb_hdl"\n'
    )
    view, record = _fake_view(tmp_path)
    build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    assert len(_tb_calls(record)) == 1


def test_tb_export_is_rooted_at_the_tb_top_and_welds_to_the_dut(
    graph_project: Path, tmp_path: Path
):
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    (tb_call,) = _tb_calls(record)
    assert tb_call[tb_call.index("--tb-top") + 1] == "tb_hdl"
    # --top stays the DUT, so the viewer can record which subtree is the
    # design under test; the filelist is the DUT+TB merge under the
    # `rb hier --view tb` artefact path.
    assert tb_call[tb_call.index("--top") + 1] == "blk_a"
    filelist = Path(tb_call[tb_call.index("--filelist") + 1])
    assert filelist.parts[-3:] == ("tb", "tb_hdl", "hier.f")
    assert "verif/blk_a/tb_top.sv" in filelist.read_text().replace("\\", "/")

    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert nodes["module:tb_hdl"]["file"] == "verif/blk_a/tb_top.sv"
    assert "inst:tb_hdl/tb_hdl.i_dut" in nodes
    # The weld: the DUT module node the DUT-rooted export produced now
    # has an `instance_of` edge from the testbench's instance of it.
    into_dut = {
        link["source"]
        for link in graph["links"]
        if link["type"] == "instance_of" and link["target"] == "module:blk_a"
    }
    assert {"inst:blk_a/blk_a", "inst:tb_hdl/tb_hdl.i_dut"} <= into_dut
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.extra["testbenches"] == ["verif/blk_a#tb_hdl"]


def test_tb_node_elaborates_as_the_top_the_viewer_elaborated(
    graph_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`tb_hdl` declares no `toplevel:`; the viewer's answer is the fact.

    rtl-buddy-view auto-corrects a `--tb-top` hint that names no module
    in the design, so the stitch has to read the elaborated top back off
    the export instead of trusting what was passed in.
    """
    monkeypatch.setenv("FAKE_VIEW_TB_TOP", "tb_top")
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    graph = json.loads(build.graph_path.read_text())
    stitches = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "elaborates_as" and link["source"].startswith("tb:")
    }
    assert ("tb:verif/blk_a#tb_hdl", "module:tb_top") in stitches
    assert "module:tb_top" in _nodes(graph)
    assert not dangling_targets(graph)
    # The testbench's verb is its own: a `tb:` node never emits `maps_to`,
    # which is what lets a reader tell a declaration of a module from an
    # elaboration of one without parsing the source id.
    assert not [
        link
        for link in graph["links"]
        if link["type"] == "maps_to" and not link["source"].startswith("model:")
    ]


def test_declared_toplevel_stitches_without_an_export(
    graph_project: Path, tmp_path: Path
):
    """`tb_cocotb` is never TB-exported, but its `tb:` node still reaches
    the hierarchy — the config tier reads `toplevel:` straight out of
    tests.yaml."""
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    graph = json.loads(build.graph_path.read_text())
    assert {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "elaborates_as"
        and link["source"] == "tb:verif/blk_a#tb_cocotb"
    } == {("tb:verif/blk_a#tb_cocotb", "module:blk_a")}


def test_colliding_testbench_ids_are_qualified_by_suite(
    graph_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two suites, two different modules, one name.

    Merged naively, `module:tb_top` would be one node instantiating both
    DUTs and `inst:tb_top/tb_top.i_dut` would be `instance_of` two
    different modules. Neither is true, so the ids are qualified with the
    suite that owns them — while the DUT ids they weld to are left alone.
    """
    _second_suite(graph_project, tb_name="tb_hdl_b")
    monkeypatch.setenv("FAKE_VIEW_TB_TOP", "tb_top")
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert "module:tb_top" not in nodes
    # Indexed rendered labels, deterministic by suite path: blk_a sorts
    # before blk_b, so it takes (0). Reusing `tb_top` across suites is a
    # supported pattern — the graph, not the project, disambiguates.
    for i, (suite, dut) in enumerate(
        (("verif/blk_a", "blk_a"), ("verif/blk_b", "blk_b"))
    ):
        module = nodes[f"module:tb_top@{suite}"]
        assert module["file"] == f"{suite}/tb_top.sv"
        assert module["label"] == f"tb_top({i})"
        assert module["base_label"] == "tb_top"  # still findable by name
        assert module["unqualified_id"] == "module:tb_top"
        root_inst = nodes[f"inst:tb_top/tb_top@{suite}"]
        assert root_inst["label"] == f"tb_top({i})"
        # Deeper nodes keep their own labels — they render nested under
        # an indexed parent.
        assert nodes[f"inst:tb_top/tb_top.i_dut@{suite}"]["label"] == "i_dut"
        assert (
            f"inst:tb_top/tb_top.i_dut@{suite}",
            f"module:{dut}",
        ) in {
            (link["source"], link["target"])
            for link in graph["links"]
            if link["type"] == "instance_of"
        }
    # The DUT ids are the weld and are never qualified.
    assert {"module:blk_a", "module:blk_b", "port:blk_a.clk"} <= set(nodes)
    assert not dangling_targets(graph)

    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    by_id = {c["id"]: c for c in design.extra["id_collisions"]}
    assert by_id["module:tb_top"]["labels"] == ["tb_top(0)", "tb_top(1)"]
    # `qualified` is sorted and deduped — a stated property of the meta
    # payload, since it is what the label index derives from.
    assert by_id["module:tb_top"]["qualified"] == [
        "module:tb_top@verif/blk_a",
        "module:tb_top@verif/blk_b",
    ]
    meta = json.loads(build.meta_path.read_text())
    assert meta["tiers"][DESIGN_TIER]["id_collisions"]


def test_a_failed_tb_export_is_reported_per_testbench(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path, tb_exit_code=3)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    # The tier is still built — the DUT exports were fine.
    assert design.status == "built"
    assert design.extra["testbenches"] == []
    failure = next(f for f in design.failures if "testbench" in f)
    assert failure["testbench"] == "verif/blk_a#tb_hdl"
    assert "log" in failure
    assert "1 failed" in design.row_detail()
    graph = json.loads(build.graph_path.read_text())
    assert "module:tb_hdl" not in _nodes(graph)


def test_no_tb_leaves_a_dut_only_design_tier(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
        tb=False,
    )
    assert _tb_calls(record) == []
    graph = json.loads(build.graph_path.read_text())
    assert "module:tb_hdl" not in _nodes(graph)
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert "testbenches" not in design.extra


def test_a_changed_tb_source_invalidates_the_cached_graph(
    graph_project: Path, tmp_path: Path
):
    """TB sources are design-tier inputs, so the no-op check sees them."""
    view, record = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "extract_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    assert build_graph(graph_project, **kwargs).unchanged is True

    tb_source = graph_project / "verif" / "blk_a" / "tb_top.sv"
    tb_source.write_text(tb_source.read_text().replace("i_dut", "u_dut"))
    record.unlink()
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint
    assert len(_tb_calls(record)) == 1


def test_no_tb_and_tb_do_not_share_a_fingerprint(graph_project: Path, tmp_path: Path):
    """Dropping the TB half must not read as "nothing changed"."""
    view, _ = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "extract_enabled": False,
    }
    with_tb = build_graph(graph_project, **kwargs)
    without_tb = build_graph(graph_project, tb=False, **kwargs)
    assert without_tb.unchanged is False
    assert without_tb.fingerprint != with_tb.fingerprint


# ---------------------------------------------------------------------------
# Run-rooted design tier (#385)
#
# A formal/synth/cdc run's `top:` often only elaborates inside the flow's
# own filelist — the template's fpv checker tops live in `properties:`
# files no models.yaml names. These pin the run-rooted exports: the same
# TB mechanism, keyed off the repo-level regression files, stitched with
# the run's own verb (`targets`).
# ---------------------------------------------------------------------------


def test_flow_run_selection_skips_model_topped_runs_and_dedupes(graph_project: Path):
    # The fixture's `blk_a_safety` run tops at the model itself: the DUT
    # export already covers that hierarchy, so there is nothing to add.
    assert graph_build.flow_runs_from_regressions(graph_project) == []

    _flow_suite(graph_project)
    targets = graph_build.flow_runs_from_regressions(graph_project)
    # Two runs (bmc + prove), one checker: (suite, model, sources, top)
    # is the entire input to the viewer, so they are one export.
    assert [(t.suite_rel, t.run_name, t.top, t.flow) for t in targets] == [
        ("fpv/blk_a_chk", "chk_bmc", "blk_a_chk", "fpv")
    ]
    target = targets[0]
    assert target.node_id == "test:fpv/blk_a_chk#chk_bmc"
    assert target.label == "fpv/blk_a_chk#chk_bmc"
    assert target.stitch_type == "targets"
    assert target.tb_top == "blk_a_chk"
    assert [Path(s).name for s in target.sources] == ["blk_a_chk_props.sv"]
    assert target.model.name == "blk_a"
    # The collapsed twin is remembered: each run keeps its own stitch.
    assert target.run_names == ["chk_bmc", "chk_prove"]
    assert target.node_ids == [
        "test:fpv/blk_a_chk#chk_bmc",
        "test:fpv/blk_a_chk#chk_prove",
    ]


def test_flow_run_selection_honours_the_model_filter(graph_project: Path):
    _flow_suite(graph_project)
    only_b = [
        m
        for m in graph_build.models_from_design_tree(graph_project / "design")
        if m.name == "blk_b"
    ]
    assert graph_build.flow_runs_from_regressions(graph_project, only_b) == []


def test_run_export_is_rooted_at_the_run_top_and_welds_to_the_dut(
    graph_project: Path, tmp_path: Path
):
    _flow_suite(graph_project)
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    (run_call,) = _run_calls(record)
    assert run_call[run_call.index("--tb-top") + 1] == "blk_a_chk"
    # --top stays the DUT, exactly as a TB export: the filelist is the
    # model's plus the flow's own sources, cached under run/<top>.
    assert run_call[run_call.index("--top") + 1] == "blk_a"
    filelist = _call_filelist(run_call)
    assert filelist.parts[-4:] == ("blk_a", "run", "blk_a_chk", "hier.f")
    assert "blk_a_chk_props.sv" in filelist.read_text()

    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert nodes["module:blk_a_chk"]["file"] == "fpv/blk_a_chk/blk_a_chk_props.sv"
    # The weld: the checker's DUT instance lands on the module node the
    # DUT-rooted export produced.
    assert ("inst:blk_a_chk/blk_a_chk.i_dut", "module:blk_a") in {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "instance_of"
    }
    # Both runs' `targets` stitches resolve: the exported run's is the
    # observation, the de-duplicated twin keeps its declaration — same
    # module node either way. THE acceptance: no dangling run tops.
    targets_links = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "targets"
    }
    assert {
        ("test:fpv/blk_a_chk#chk_bmc", "module:blk_a_chk"),
        ("test:fpv/blk_a_chk#chk_prove", "module:blk_a_chk"),
    } <= targets_links
    assert not dangling_targets(graph)
    # A run's stitch is `targets`, never the testbench's verb.
    assert not [
        link
        for link in graph["links"]
        if link["type"] == "elaborates_as" and link["source"].startswith("test:")
    ]
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    # The envelope counts *exports*, but a collapsed one names the runs
    # it swallowed — otherwise a reader of graph-meta.json would infer
    # that the `prove` twin was dropped, when it has its own stitch.
    assert design.extra["flow_runs"] == ["fpv/blk_a_chk#chk_bmc (+chk_prove)"]
    assert "1 flow run top(s)" in design.row_detail()


def test_a_changed_properties_file_invalidates_the_cached_graph(
    graph_project: Path, tmp_path: Path
):
    """Flow sources are design-tier inputs, so the no-op check sees them."""
    suite_dir = _flow_suite(graph_project)
    view, record = _fake_view(tmp_path)
    kwargs = {
        "view_executable": str(view),
        "view_version": "0.4.0",
        "extract_enabled": False,
    }
    first = build_graph(graph_project, **kwargs)
    second = build_graph(graph_project, **kwargs)
    assert second.unchanged is True
    # The cached envelope still names the run exports it is reusing.
    cached = next(t for t in second.tiers if t.tier == DESIGN_TIER)
    assert cached.extra["flow_runs"] == ["fpv/blk_a_chk#chk_bmc (+chk_prove)"]

    props = suite_dir / "blk_a_chk_props.sv"
    props.write_text(props.read_text().replace("i_dut", "u_dut"))
    record.unlink()
    third = build_graph(graph_project, **kwargs)
    assert third.unchanged is False
    assert third.fingerprint != first.fingerprint
    assert len(_run_calls(record)) == 1


def test_a_failed_run_export_is_reported_per_run(graph_project: Path, tmp_path: Path):
    _flow_suite(graph_project)
    view, _ = _fake_view(tmp_path, tb_exit_code=3)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    # The tier is still built — the DUT exports were fine.
    assert design.status == "built"
    assert design.extra["flow_runs"] == []
    failure = next(f for f in design.failures if "run" in f)
    assert failure["run"] == "fpv/blk_a_chk#chk_bmc"
    assert "log" in failure
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a_chk" not in _nodes(graph)
    # The declared `targets` stitch survives — dangling, which is what a
    # failed export means, not silently dropped.
    assert "module:blk_a_chk" in dangling_targets(graph)


def test_no_flow_tops_leaves_run_tops_dangling(graph_project: Path, tmp_path: Path):
    _flow_suite(graph_project)
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
        flow_tops=False,
    )
    assert _run_calls(record) == []
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert "flow_runs" not in design.extra
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a_chk" in dangling_targets(graph)


def test_colliding_run_top_ids_are_qualified_by_suite(
    graph_project: Path, tmp_path: Path
):
    """Two fpv suites, two different checker modules, one name.

    The same collision testbench tops have — and the same resolution:
    the run copies are qualified with the suite that owns them, the
    `targets` stitches follow the rename — for every run collapsed into
    the export, not just the first — and the DUT ids stay the weld.
    """
    _flow_suite(graph_project, suite="chk_one", dut="blk_a", top="chk_top")
    _flow_suite(graph_project, suite="chk_two", dut="blk_b", top="chk_top")
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert "module:chk_top" not in nodes
    targets_links = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "targets"
    }
    for suite, dut in (("fpv/chk_one", "blk_a"), ("fpv/chk_two", "blk_b")):
        module = nodes[f"module:chk_top@{suite}"]
        assert module["file"] == f"{suite}/{suite.split('/')[1]}_props.sv"
        assert module["unqualified_id"] == "module:chk_top"
        # Both runs stitch to the qualified module — the de-duplicated
        # `chk_prove` twin included, or its declared edge would dangle.
        assert (f"test:{suite}#chk_bmc", f"module:chk_top@{suite}") in targets_links
        assert (f"test:{suite}#chk_prove", f"module:chk_top@{suite}") in targets_links
        assert (
            f"inst:chk_top/chk_top.i_dut@{suite}",
            f"module:{dut}",
        ) in {
            (link["source"], link["target"])
            for link in graph["links"]
            if link["type"] == "instance_of"
        }
    assert not dangling_targets(graph)
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    by_id = {c["id"]: c for c in design.extra["id_collisions"]}
    assert by_id["module:chk_top"]["qualified"] == [
        "module:chk_top@fpv/chk_one",
        "module:chk_top@fpv/chk_two",
    ]


# ---------------------------------------------------------------------------
# Extractor (optional binding tier)
# ---------------------------------------------------------------------------


def test_extractor_absent_still_succeeds_and_says_so(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_version=None,
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "skipped"
    assert "no binding-tier extractor" in binding.detail
    assert "rtl-buddy-graph-extract" in binding.detail
    assert not build.failed_tiers()
    assert build.graph_path.is_file()
    meta = json.loads(build.meta_path.read_text())
    assert meta["merge"]["extract_cross_check"]["status"] == "skipped"


def test_extractor_binding_tier_merges_and_cross_checks(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, gfy_record = _fake_extractor(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_executable=str(gfy),
        extract_version="1.2.3",
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
    assert any(a.endswith("cocotb_blk_a.py") for a in extract_argv)
    assert any(a.endswith("README.md") for a in extract_argv)

    cross = json.loads(build.meta_path.read_text())["merge"]["extract_cross_check"]
    assert cross["status"] == "ok"
    assert cross["internal_nodes"] == cross["extract_nodes"] == build.nodes


def test_extractor_failure_leaves_the_other_tiers_intact(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, _ = _fake_extractor(tmp_path, exit_code=4)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_executable=str(gfy),
        extract_version="1.2.3",
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "failed"
    assert "exit 4" in binding.detail
    graph = json.loads(build.graph_path.read_text())
    assert "module:blk_a" in _nodes(graph)


def test_extractor_merge_disagreement_is_reported_not_adopted(
    graph_project: Path, tmp_path: Path
):
    view, _ = _fake_view(tmp_path)
    gfy, _ = _fake_extractor(tmp_path, merge_phantom=True)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_executable=str(gfy),
        extract_version="1.2.3",
    )
    cross = json.loads(build.meta_path.read_text())["merge"]["extract_cross_check"]
    assert cross["status"] == "mismatch"
    assert cross["only_extract"] == ["phantom:1"]
    # The internal union is always what ships — the cross-check is a
    # report, never a substitute.
    assert build.merge["strategy"] == "node-id-union"
    graph = json.loads(build.graph_path.read_text())
    assert "phantom:1" not in _nodes(graph)


def test_extractor_argv_shapes():
    extract = extract_mod.build_extract_cmd("rb-graph-extract", ["a.py"], "out.json")
    assert extract[:2] == ["rb-graph-extract", extract_mod.EXTRACT_VERB]
    assert extract[extract.index("--output") + 1] == "out.json"
    assert extract[-1] == "a.py"
    merge = extract_mod.build_merge_cmd(
        "rb-graph-extract", ["a.json", "b.json"], "m.json"
    )
    assert merge[:2] == ["rb-graph-extract", extract_mod.MERGE_VERB]
    assert merge[-2:] == ["a.json", "b.json"]


# ---------------------------------------------------------------------------
# Extractor resolution (#391)
# ---------------------------------------------------------------------------


def _fake_status(status: str, version: str | None = None):
    return SimpleNamespace(status=status, version=version)


def test_resolve_extractor_finds_the_bundled_tool(monkeypatch):
    def fake_check(spec):
        assert spec.name == extract_mod.GRAPH_EXTRACT_TOOL
        return _fake_status("ok", "0.1.0")

    monkeypatch.setattr(extract_mod, "check_tool", fake_check)
    choice = extract_mod.resolve_extractor()
    assert choice is not None
    assert choice.executable == extract_mod.GRAPH_EXTRACT_BINARY
    # A found-but-unprobeable tool would carry "unknown" instead of
    # None — either way the string lands in the build fingerprint.
    assert choice.version == "0.1.0"


def test_resolve_extractor_none_when_not_installed(monkeypatch):
    monkeypatch.setattr(extract_mod, "check_tool", lambda spec: _fake_status("missing"))
    assert extract_mod.resolve_extractor() is None


def test_graph_extract_is_a_published_extra():
    """The extra must only exist while pip can resolve it — v6.26.0
    advertised `rtl_buddy[graph-extract]` before the extractor had a
    PyPI release, and the install failed at resolution. Since
    rtl-buddy-graph-extract 0.1.0 landed (rtl-buddy-graph-extract#1)
    the extra is legal: assert it is a real floor-carrying extra, that
    the interim dependency group is gone, and that no uv git pin
    shadows the PyPI resolution."""
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
    extras = pyproject["project"]["optional-dependencies"]
    # Parse rather than string-compare: the invariant is "one dependency,
    # this name, this floor" — not the author's whitespace.
    (req,) = [Requirement(s) for s in extras["graph-extract"]]
    assert req.name == "rtl-buddy-graph-extract"
    assert str(req.specifier) == ">=0.1.0"
    assert "graph-extract" not in pyproject.get("dependency-groups", {})
    assert "rtl-buddy-graph-extract" not in pyproject.get("tool", {}).get("uv", {}).get(
        "sources", {}
    )


@pytest.mark.skipif(
    shutil.which(extract_mod.GRAPH_EXTRACT_BINARY) is None,
    reason="rb-graph-extract not installed (uv sync --extra graph-extract)",
)
def test_resolve_extractor_against_the_real_binary():
    """Discovery for real, no stubs and no hand-fed strings: the manifest
    detectors must find the installed tool, and the version regex must
    parse the binary's actual ``--version`` output. Asserting the regex
    only against a literal the author chose is exactly the assumption the
    dead Graphify contract was built on — this is the test that a real
    install keeps honest."""
    choice = extract_mod.resolve_extractor()
    assert choice is not None
    assert choice.executable == extract_mod.GRAPH_EXTRACT_BINARY
    # The regex is only exercised when the version came from the PATH
    # probe — check_tool() skips probe_version() for a python-metadata
    # detection. The skipif guard makes PathDetector win today; this
    # states that dependency instead of relying on it.
    spec = next(
        s
        for s in extract_mod.get_manifest()
        if s.name == extract_mod.GRAPH_EXTRACT_TOOL
    )
    assert extract_mod.check_tool(spec).kind == "path"
    # "unknown" would mean the tool was found but its --version output
    # did not match the manifest regex — a fingerprint regression.
    assert choice.version != "unknown"
    assert choice.version[0].isdigit()


@pytest.mark.skipif(
    shutil.which(extract_mod.GRAPH_EXTRACT_BINARY) is None,
    reason="rb-graph-extract not installed (uv sync --extra graph-extract)",
)
def test_bundled_extractor_end_to_end(graph_project: Path, tmp_path: Path):
    """The real rb-graph-extract binary, not a stub, reached the same way
    `rb graph build` reaches it — through resolve_extractor(): binding
    tier builds, its nodes stitch into the merge, the cross-check agrees,
    and the fingerprint carries the tool's manifest name and its real
    probed version."""
    choice = extract_mod.resolve_extractor()
    assert choice is not None
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_executable=choice.executable,
        extract_version=choice.version,
    )
    binding = next(t for t in build.tiers if t.tier == BINDING_TIER)
    assert binding.status == "built"
    assert binding.generator["tool"] == "rb-graph-extract"
    graph = json.loads(build.graph_path.read_text())
    assert "py:verif/blk_a/cocotb_blk_a.py" in _nodes(graph)
    meta = json.loads(build.meta_path.read_text())
    assert meta["tools"][extract_mod.GRAPH_EXTRACT_TOOL] == choice.version
    assert meta["merge"]["extract_cross_check"]["status"] == "ok"


def test_collect_inputs_is_verif_python_and_spec_markdown(
    graph_project: Path,
):
    found = extract_mod.collect_inputs(graph_project / "verif", graph_project / "spec")
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
        rb.app, ["--machine", "graph", "build", "--tool", str(view), "--no-extract"]
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
        ["graph", "build", "--model", "blk_a", "--tool", str(view), "--no-extract"],
    )
    assert ok.exit_code == 0, ok.output
    assert [a[a.index("--top") + 1] for a in _dut_calls(record)] == ["blk_a"]

    bad = runner.invoke(
        rb.app, ["graph", "build", "--model", "nope", "--tool", str(view)]
    )
    assert bad.exit_code != 0


def test_cli_no_tb_and_the_envelope_testbench_list(graph_project: Path, tmp_path: Path):
    view, record = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "graph", "build", "--tool", str(view), "--no-extract"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    design = next(t for t in payload["tiers"] if t["tier"] == DESIGN_TIER)
    assert design["testbenches"] == ["verif/blk_a#tb_hdl"]

    record.unlink()
    off = runner.invoke(
        rb.app,
        [
            "--machine",
            "graph",
            "build",
            "--no-tb",
            "--tool",
            str(view),
            "--no-extract",
        ],
    )
    assert off.exit_code == 0, off.output
    payload = json.loads(off.output.strip().splitlines()[-1])["payload"]
    design = next(t for t in payload["tiers"] if t["tier"] == DESIGN_TIER)
    assert "testbenches" not in design
    assert _tb_calls(record) == []


def test_cli_no_flow_tops_and_the_envelope_run_list(
    graph_project: Path, tmp_path: Path
):
    _flow_suite(graph_project)
    view, record = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "graph", "build", "--tool", str(view), "--no-extract"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    design = next(t for t in payload["tiers"] if t["tier"] == DESIGN_TIER)
    assert design["flow_runs"] == ["fpv/blk_a_chk#chk_bmc (+chk_prove)"]

    record.unlink()
    off = runner.invoke(
        rb.app,
        [
            "--machine",
            "graph",
            "build",
            "--no-flow-tops",
            "--tool",
            str(view),
            "--no-extract",
        ],
    )
    assert off.exit_code == 0, off.output
    payload = json.loads(off.output.strip().splitlines()[-1])["payload"]
    design = next(t for t in payload["tiers"] if t["tier"] == DESIGN_TIER)
    assert "flow_runs" not in design
    assert _run_calls(record) == []


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
            "--no-extract",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [a[a.index("--top") + 1] for a in _dut_calls(record)] == ["blk_a"]


def test_cli_exits_nonzero_when_a_tier_fails(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path, exit_code=3)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "graph", "build", "--tool", str(view), "--no-extract"],
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
        rb.app, ["graph", "build", "--tool", str(view), "--no-extract"]
    )
    assert lenient.exit_code == 0, lenient.output
    strict = runner.invoke(
        rb.app,
        ["graph", "build", "--tool", str(view), "--no-extract", "--strict", "--force"],
    )
    assert strict.exit_code == 1


def test_cli_second_run_reports_unchanged(graph_project: Path, tmp_path: Path):
    view, _ = _fake_view(tmp_path)
    runner, rb = _runner()
    args = ["--machine", "graph", "build", "--tool", str(view), "--no-extract"]
    assert runner.invoke(rb.app, args).exit_code == 0
    second = runner.invoke(rb.app, args)
    assert second.exit_code == 0, second.output
    envelope = json.loads(second.output.strip().splitlines()[-1])
    assert envelope["payload"]["unchanged"] is True


# ---------------------------------------------------------------------------
# models.yaml `graph:` / `top:` (#479)
#
# Two legitimate model shapes have no module named after the model: an SV
# `interface` published as a library entry, and a filelist of vendored IP.
# Before the knobs, both produced a permanent design-tier failure row and a
# dangling `model --maps_to--> module:<name>` in every merged graph.
# ---------------------------------------------------------------------------


def _rewrite_model(project: Path, name: str, extra: str) -> None:
    """Append ``extra`` YAML lines to one model entry in its models.yaml."""
    path = project / "design" / name / "models.yaml"
    path.write_text(path.read_text().rstrip("\n") + "\n" + extra)


def test_graph_false_model_is_skipped_not_failed(graph_project: Path, tmp_path: Path):
    _rewrite_model(graph_project, "blk_b", "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )

    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "built"
    # Skipped, never failed — an opt-out is not a degradation, so
    # `--strict` must stay silent about it.
    assert design.failures == []
    assert design.skipped == [{"model": "blk_b", "reason": graph_build.GRAPH_OPT_OUT}]
    assert not build.has_failures()

    # The viewer was never asked about blk_b.
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record)] == ["blk_a"]

    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    # The config tier still carries the model — spec/test cross-references
    # resolve — but there is no module node and no stitch inventing one.
    assert "model:design/blk_b/models.yaml#blk_b" in nodes
    assert nodes["model:design/blk_b/models.yaml#blk_b"]["graph"] is False
    assert "module:blk_b" not in nodes
    assert dangling_targets(graph) == []

    meta = json.loads(build.meta_path.read_text())
    assert meta["tiers"][DESIGN_TIER]["skipped"] == design.skipped
    assert "failures" not in meta["tiers"][DESIGN_TIER]
    assert design.row_detail().endswith("1 skipped")


def test_graph_false_model_skips_its_testbench_and_run_exports(
    graph_project: Path, tmp_path: Path
):
    """A TB or flow run over an opted-out model is skipped the same way.

    Both export shapes still pass ``--top <the model's root>`` next to
    their own ``--tb-top``, so they would fail for the exact reason the
    model opted out.
    """
    _flow_suite(graph_project)
    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )

    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.failures == []
    # Everything rooted at the opted-out model, in one place: the model,
    # both of its testbenches, and every non-simulation run over it. The
    # DUT-rooted ones (`tb_cocotb`, whose `toplevel:` IS the model root,
    # and the synth/cdc/fpv runs that default their top to it) are
    # normally dropped as redundant with the DUT export — but there is no
    # DUT export here, so dropping them silently would understate what
    # the opt-out cost.
    assert design.skipped == [
        {"model": "blk_a", "reason": graph_build.GRAPH_OPT_OUT},
        {
            "testbench": "verif/blk_a#tb_cocotb",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "testbench": "verif/blk_a#tb_hdl",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "fpv/blk_a#blk_a_safety",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "fpv/blk_a_chk#chk_bmc",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "fpv/blk_a_chk#chk_prove",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_generic",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_lint",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
    ]
    assert _tb_calls(record) == [] and _run_calls(record) == []
    # blk_b is untouched by its neighbour's opt-out.
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record)] == ["blk_b"]

    graph = json.loads(build.graph_path.read_text())
    # The run's `targets` stitch goes with its export: an fpv checker top
    # lives in the flow's own filelist, so nothing else is ever going to
    # define `module:blk_a_chk`. Opting a model out must not *add* a
    # dangling target. The one survivor is the documented cocotb
    # `binds_to` hop from the binding tier (docs/known-issues.md).
    assert dangling_targets(graph) == ["module:blk_a"]
    assert all(
        link["type"] == "binds_to"
        for link in graph["links"]
        if link["target"] == "module:blk_a"
    )
    assert "module:blk_a_chk" not in _nodes(graph)


def _twin_testbench_suite(project: Path) -> None:
    """Two `testbenches:` entries in one suite that elaborate identically.

    Same model, same filelist, same explicit ``toplevel:`` — so the
    de-duplication key, which is exactly what the viewer is handed, is
    the same for both and only one export runs. The *names* differ, and
    each is a `tb:` node the config tier emitted.
    """
    (project / "verif" / "blk_a" / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        '  - name: "tb_alpha"\n'
        '    filelist: ["tb_top.sv"]\n'
        "    toplevel: tb_top\n"
        '  - name: "tb_beta"\n'
        '    filelist: ["tb_top.sv"]\n'
        "    toplevel: tb_top\n"
        "tests:\n"
        '  - name: "t_alpha"\n'
        '    desc: "alpha"\n'
        "    reglvl: 0\n"
        '    model: "blk_a"\n'
        '    model_path: "../../design/blk_a/models.yaml"\n'
        '    testbench: "tb_alpha"\n'
        '  - name: "t_beta"\n'
        '    desc: "beta"\n'
        "    reglvl: 0\n"
        '    model: "blk_a"\n'
        '    model_path: "../../design/blk_a/models.yaml"\n'
        '    testbench: "tb_beta"\n'
    )


def test_two_testbenches_collapsing_into_one_export_keep_both_names(
    graph_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The dedup key excludes the entry name, so both names must survive.

    One export, two `tb:` nodes — and each of them really does elaborate
    that hierarchy, so each is owed the observed stitch. Keeping only the
    first stranded the twin, exactly as it would strand a flow run's
    collapsed twin (which is why ``FlowRunTarget`` has kept every name
    since #385).

    ``FAKE_VIEW_TB_TOP`` makes the viewer report a top the config tier's
    ``toplevel:`` did NOT declare, so the observed stitch is
    distinguishable from the declared one and the assertion is about the
    export rather than about the YAML.
    """
    _twin_testbench_suite(graph_project)
    targets = graph_build.testbenches_from_suites(
        graph_project, graph_project / "verif"
    )
    assert [t.tb_names for t in targets] == [["tb_alpha", "tb_beta"]]
    assert targets[0].labels == ["verif/blk_a#tb_alpha", "verif/blk_a#tb_beta"]
    assert targets[0].node_ids == [
        "tb:verif/blk_a#tb_alpha",
        "tb:verif/blk_a#tb_beta",
    ]
    # ...and the first name still answers for the export itself.
    assert targets[0].tb_name == "tb_alpha"
    assert targets[0].label == "verif/blk_a#tb_alpha"

    monkeypatch.setenv("FAKE_VIEW_TB_TOP", "real_tb_top")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    # One export, not two: the dedup is intact.
    assert len(_tb_calls(record)) == 1
    graph = json.loads(build.graph_path.read_text())
    stitches = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "elaborates_as" and link["source"].startswith("tb:")
    }
    assert ("tb:verif/blk_a#tb_alpha", "module:real_tb_top") in stitches
    assert ("tb:verif/blk_a#tb_beta", "module:real_tb_top") in stitches
    assert not dangling_targets(graph)


def test_both_collapsed_testbenches_are_reported_when_their_model_opts_out(
    graph_project: Path, tmp_path: Path
):
    """Every opted-out testbench gets a row, collapsed twin included."""
    _twin_testbench_suite(graph_project)
    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    reported = [r["testbench"] for r in design.skipped if "testbench" in r]
    assert reported == ["verif/blk_a#tb_alpha", "verif/blk_a#tb_beta"]
    # And the envelope carries the same rows the sidecar does.
    meta = json.loads(build.meta_path.read_text())
    assert meta["tiers"][DESIGN_TIER]["skipped"] == design.skipped
    assert _tb_calls(record) == []


def test_a_stale_export_that_cannot_be_removed_is_fatal(
    graph_project: Path, tmp_path: Path
):
    """Failing to retract is worse than never having tried.

    The build would otherwise report the model as skipped while its old
    per-model hierarchy stayed on disk and readable — the exact state the
    retraction exists to prevent, now blessed by a green exit. An
    unwritable output directory is the kind of setup problem
    ``build_graph`` propagates rather than degrades.
    """
    if os.geteuid() == 0:  # pragma: no cover - depends on the runner
        pytest.skip("root ignores directory permissions")
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view), view_version="0.4.0", extract_enabled=False
    )
    build_graph(graph_project, **common)
    design_dir = graph_project / "artefacts" / "graph" / "design"
    assert (design_dir / "blk_a" / "graph.json").is_file()

    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    design_dir.chmod(0o555)  # removing a child needs write on the parent
    try:
        with pytest.raises(FatalRtlBuddyError) as excinfo:
            build_graph(graph_project, **common)
    finally:
        design_dir.chmod(0o755)
    message = str(excinfo.value)
    assert "blk_a" in message
    assert str(design_dir / "blk_a") in message
    assert "graph: false" in message
    # `rmtree` is not atomic: it removes what it can and then fails, so a
    # refused retraction leaves a *partial* tree behind. That is a second
    # reason it cannot be swallowed — the artefact directory is now in a
    # state no build produced.
    assert (design_dir / "blk_a").is_dir()
    # With the permission restored the same build succeeds and retracts.
    build_graph(graph_project, **common)
    assert not (design_dir / "blk_a").exists()


def test_the_unremovable_export_event_has_a_human_message_case():
    """Guidelines → Logging: an ERROR event needs its own case, or the
    log says less than the exception the user already saw."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "graph_build.stale_export_not_dropped",
        {
            "model": "blk_a",
            "path": "artefacts/graph/design/blk_a",
            "error": "Permission denied",
        },
    )
    assert msg != "graph build stale_export_not_dropped"
    assert "blk_a" in msg
    assert "artefacts/graph/design/blk_a" in msg
    assert "Permission denied" in msg


def test_opting_out_retracts_a_models_previously_written_export(
    graph_project: Path, tmp_path: Path
):
    """A design-tier export is durable, so an opt-out has to retract it.

    Nothing rewrites ``artefacts/graph/design/<model>/`` but a later
    export of that same model. So a model exported yesterday and marked
    ``graph: false`` today would leave a complete, readable hierarchy on
    disk while `graph-meta.json` and the merged graph both say it has
    none — and the extractor's cross-check reads those files directly.
    A stale export is a confident wrong answer, not a missing one.
    """
    _flow_suite(graph_project)
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view), view_version="0.4.0", extract_enabled=False
    )
    build_graph(graph_project, **common)

    design_dir = graph_project / "artefacts" / "graph" / "design"
    dut = design_dir / "blk_a" / "graph.json"
    assert dut.is_file()
    # The viewer's own provenance sidecar, and the TB- and run-rooted
    # exports that nest under the same model directory.
    sidecar = design_dir / "blk_a" / "graph-meta.json"
    sidecar.write_text('{"generator": {"tool": "rtl-buddy-view"}}')
    tb_export = design_dir / "blk_a" / "tb" / "tb_hdl" / "graph.json"
    run_export = design_dir / "blk_a" / "run" / "blk_a_chk" / "graph.json"
    assert tb_export.is_file() and run_export.is_file()

    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    build = build_graph(graph_project, **common)

    assert not (design_dir / "blk_a").exists()
    for path in (dut, sidecar, tb_export, run_export):
        assert not path.exists(), path
    # The neighbour is untouched — only the opted-out model's subtree goes.
    assert (design_dir / "blk_b" / "graph.json").is_file()
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "built"
    assert {r["model"] for r in design.skipped} == {"blk_a"}
    # Idempotent: a second build with the model still opted out is fine.
    assert build_graph(graph_project, force=True, **common).graph_path.is_file()
    assert not (design_dir / "blk_a").exists()


def test_a_dut_rooted_tb_and_run_are_reported_when_their_model_opts_out(
    graph_project: Path, tmp_path: Path
):
    """The dedup that hides a same-root testbench must not hide a skip.

    A cocotb testbench whose ``toplevel:`` IS the model root, and a synth
    or cdc run whose top defaults to it, are normally dropped before the
    tier ever sees them: the DUT export already covers that hierarchy, so
    re-elaborating it would only produce the same nodes twice. When the
    model opts out there is no DUT export to defer to, and dropping them
    silently would contradict the promise that everything rooted at an
    opted-out model is listed under ``skipped`` — and would understate
    the count in the tier's summary line.

    The other half is the same fixture without the opt-out: the dedup is
    unchanged there, so those items are neither exported nor skipped.
    """
    view, record = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view), view_version="0.4.0", extract_enabled=False
    )

    graphable = build_graph(graph_project, **common)
    design = next(t for t in graphable.tiers if t.tier == DESIGN_TIER)
    # `tb_cocotb` tops at blk_a, and so do the fpv/synth/cdc runs: one
    # DUT export covers all four, and none of them is a skip.
    assert design.skipped == []
    assert _tb_calls(record) == [
        argv for argv in _tb_calls(record) if "tb_hdl" in " ".join(argv)
    ]
    assert len(_dut_calls(record)) == 2
    assert _run_calls(record) == []

    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    second = tmp_path / "second"
    second.mkdir()
    view2, record2 = _fake_view(second)
    opted = build_graph(
        graph_project, force=True, **{**common, "view_executable": str(view2)}
    )
    design = next(t for t in opted.tiers if t.tier == DESIGN_TIER)
    reported = {
        record.get("testbench") or record.get("run") or record["model"]
        for record in design.skipped
    }
    # The DUT-rooted testbench and the model-topped runs, which the dedup
    # would otherwise have swallowed before the tier could report them.
    assert "verif/blk_a#tb_cocotb" in reported
    assert "impl/blk_a#blk_a_generic" in reported
    assert "impl/blk_a#blk_a_lint" in reported
    assert "fpv/blk_a#blk_a_safety" in reported
    # Reported, never exported: blk_b is the only thing the viewer saw.
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record2)] == ["blk_b"]
    assert _tb_calls(record2) == [] and _run_calls(record2) == []
    assert design.failures == []
    assert design.row_detail().endswith(f"{len(design.skipped)} skipped")


def test_every_model_opting_out_skips_the_tier_without_failing_it(
    graph_project: Path, tmp_path: Path
):
    for name in ("blk_a", "blk_b"):
        _rewrite_model(graph_project, name, "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "skipped"
    assert "opted out" in (design.detail or "")
    assert build.failed_tiers() == []
    assert _graph_calls(record) == []
    assert build.graph_path.is_file()

    graph = json.loads(build.graph_path.read_text())
    # No config->design stitch survives: `maps_to`, a run's `targets` at
    # the model's own root, and a cocotb testbench's declared
    # `elaborates_as` are all withdrawn, because the tier that would
    # define their target is not going to run.
    assert [
        link
        for link in graph["links"]
        if link["type"] in ("maps_to", "targets", "elaborates_as")
    ] == []
    # What is left is the binding tier's cocotb `test -> module:<DUT>`
    # hop, which is emitted from the merged graph and is dangling for the
    # same reason `--no-design` leaves it dangling (docs/known-issues.md).
    assert dangling_targets(graph) == ["module:blk_a"]
    assert all(
        link["type"] == "binds_to"
        for link in graph["links"]
        if link["target"] == "module:blk_a"
    )


def test_an_outdated_viewer_cannot_fail_a_fully_opted_out_design_tier(
    graph_project: Path, tmp_path: Path
):
    """The version gate must not decide a tier that never needs the viewer.

    A project whose design directory holds only library models has
    nothing to export, so an old (or gated) ``rtl-buddy-view`` is
    irrelevant to it and must not turn an opt-out into a failed tier and
    a non-zero exit.
    """
    for name in ("blk_a", "blk_b"):
        _rewrite_model(graph_project, name, "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.3.1",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "skipped"
    assert VIEW_GRAPH_MIN_VERSION not in (design.detail or "")
    assert build.failed_tiers() == []
    assert _argv_lines(record) == []
    # A graphable model still gets the upgrade hint, so the gate is not
    # simply switched off.
    _rewrite_model(graph_project, "blk_b", "    graph: true\n")
    gated = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.3.1",
        extract_enabled=False,
        force=True,
    )
    gated_design = next(t for t in gated.tiers if t.tier == DESIGN_TIER)
    assert gated_design.status == "failed"
    assert VIEW_GRAPH_MIN_VERSION in gated_design.detail


def test_a_shared_top_does_not_leak_one_models_opt_out_onto_another(
    graph_project: Path, tmp_path: Path
):
    """Two models rooted at the same module, one opted out.

    The opt-out is a fact about a model and its testbenches, not about a
    module name: keying it on the top would silently strip the graphable
    model's testbench of its declared ``elaborates_as`` edge.
    """
    _rewrite_model(graph_project, "blk_a", "    graph: false\n")
    # blk_b now roots at blk_a's top. Both models are tested from the
    # *same* suite, each by a cocotb testbench declaring that shared
    # `toplevel:` — a per-suite set of opted-out top names cannot tell
    # the two testbenches apart.
    _rewrite_model(graph_project, "blk_b", '    top: "blk_a"\n')
    (graph_project / "verif" / "blk_a" / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        '  - name: "tb_cocotb"\n'
        "    filelist: []\n"
        "    toplevel: blk_a\n"
        "    cocotb:\n"
        "      module: cocotb_blk_a\n"
        '  - name: "tb_b_cocotb"\n'
        "    filelist: []\n"
        "    toplevel: blk_a\n"
        "    cocotb:\n"
        "      module: cocotb_blk_a\n"
        "tests:\n"
        '  - name: "t_cocotb"\n'
        '    desc: "blk_a cocotb test"\n'
        "    reglvl: 0\n"
        '    model: "blk_a"\n'
        '    model_path: "../../design/blk_a/models.yaml"\n'
        '    testbench: "tb_cocotb"\n'
        '  - name: "t_b"\n'
        '    desc: "blk_b cocotb test"\n'
        "    reglvl: 0\n"
        '    model: "blk_b"\n'
        '    model_path: "../../design/blk_b/models.yaml"\n'
        '    testbench: "tb_b_cocotb"\n'
    )
    view, _ = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    graph = json.loads(build.graph_path.read_text())
    stitches = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "elaborates_as"
    }
    # blk_b's testbench keeps its edge — blk_b is graphable and exports
    # `module:blk_a`; blk_a's own cocotb testbench loses its edge.
    assert ("tb:verif/blk_a#tb_b_cocotb", "module:blk_a") in stitches
    assert ("tb:verif/blk_a#tb_cocotb", "module:blk_a") not in stitches
    assert dangling_targets(graph) == []


def test_two_graphable_models_sharing_a_top_are_refused_before_any_export(
    graph_project: Path, tmp_path: Path
):
    """`module:<top>` is a global id, so one top cannot mean two designs.

    DUT ids are the one thing suite qualification never rewrites — they
    are the weld a TB or run export merges onto — so two such exports do
    not stay apart: the merge keeps the first node's attributes and
    unions both link sets, and the graph ends up claiming one module
    instantiates both designs. Nothing downstream can spot that, so the
    build refuses the input instead of writing it.
    """
    _rewrite_model(graph_project, "blk_b", '    top: "blk_a"\n')
    view, record = _fake_view(tmp_path)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        build_graph(
            graph_project,
            view_executable=str(view),
            view_version="0.4.0",
            extract_enabled=False,
        )
    message = str(excinfo.value)
    # Both models, both models.yaml paths, the shared top, and both ways out.
    assert "blk_a" in message and "blk_b" in message
    assert "design/blk_a/models.yaml" in message
    assert "design/blk_b/models.yaml" in message
    assert "graph: false" in message and "top:" in message
    # Refused *before* any export: nothing was handed to the viewer and
    # no half-written graph is left behind.
    assert _argv_lines(record) == []
    assert not (graph_project / "artefacts" / "graph" / "graph.json").is_file()


def test_two_selected_models_sharing_a_name_are_refused_before_any_export(
    graph_project: Path, tmp_path: Path
):
    """Every per-model artefact path is keyed on the model *name*.

    Two ``models.yaml`` files may each declare a `blk_a`: the loader only
    rejects duplicates within one file, and everything else keeps them
    apart (``_model_key`` is realpath-qualified, and so are their
    ``model:`` node ids). So both are planned, both run, and the second
    export overwrites the first in ``artefacts/graph/design/blk_a/`` and
    ``artefacts/hier/blk_a/`` — while the tier reports two models built.
    Distinct ``top:`` values do not help: the paths still collide.
    """
    dupe = graph_project / "design" / "blk_dupe"
    dupe.mkdir()
    (dupe / "blk_a.sv").write_text("module blk_a_alt (input logic clk);\nendmodule\n")
    (dupe / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "blk_a"\n'
        '    desc: "a second block calling itself blk_a"\n'
        '    filelist: ["blk_a.sv"]\n'
        '    top: "blk_a_alt"\n'
    )
    view, record = _fake_view(tmp_path)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        build_graph(
            graph_project,
            view_executable=str(view),
            view_version="0.4.0",
            extract_enabled=False,
        )
    message = str(excinfo.value)
    assert "design/blk_a/models.yaml" in message
    assert "design/blk_dupe/models.yaml" in message
    # It names the paths that would have collided, and both ways out.
    assert "artefacts/graph/design/blk_a/" in message
    assert "artefacts/hier/blk_a/" in message
    assert "Rename one of them" in message
    assert _argv_lines(record) == []
    assert not (graph_project / "artefacts" / "graph" / "graph.json").is_file()


def test_graph_false_does_not_excuse_a_shared_name(graph_project: Path, tmp_path: Path):
    """Opting out is not a way out of a name collision.

    A model name is how every selector spells a model — ``--model NAME``,
    a test's ``model:``, a back-pointer — and none of them can say which
    of two entries is meant. An opted-out duplicate would shadow the
    graphable one in a name-keyed lookup, silently, and afterwards the
    survivor looks like the only one there ever was. So the name half of
    the refusal covers every model in scope, opted out or not; only the
    *top* half is graphable-only.
    """
    dupe = graph_project / "design" / "blk_dupe"
    dupe.mkdir()
    (dupe / "blk_a.sv").write_text("module blk_a_alt (input logic clk);\nendmodule\n")
    (dupe / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "blk_a"\n'
        '    desc: "a second block calling itself blk_a"\n'
        '    filelist: ["blk_a.sv"]\n'
        '    top: "blk_a_alt"\n'
        "    graph: false\n"
    )
    view, record = _fake_view(tmp_path)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        build_graph(
            graph_project,
            view_executable=str(view),
            view_version="0.4.0",
            extract_enabled=False,
        )
    message = str(excinfo.value)
    assert "design/blk_a/models.yaml" in message
    assert "design/blk_dupe/models.yaml" in message
    # And it says so, rather than pointing at the knob that does not help.
    assert "Rename one of them" in message
    assert "`graph: false` does not resolve a name collision" in message
    assert _argv_lines(record) == []


def test_the_model_selector_does_not_silently_pick_between_two_of_a_name(
    graph_project: Path, tmp_path: Path
):
    """``--model blk_a`` with two `blk_a` entries must not choose one.

    The selector resolved names first-found-wins, so an opted-out entry
    that happened to be discovered first shadowed the graphable one and
    the build exported nothing while reporting a clean skip. It now
    returns every match and lets the collision refusal speak.
    """
    dupe = graph_project / "design" / "blk_dupe"
    dupe.mkdir()
    (dupe / "blk_a.sv").write_text("module blk_a_alt (input logic clk);\nendmodule\n")
    (dupe / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "blk_a"\n'
        '    desc: "a second block calling itself blk_a"\n'
        '    filelist: ["blk_a.sv"]\n'
        '    top: "blk_a_alt"\n'
        "    graph: false\n"
    )
    view, _ = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["graph", "build", "--tool", str(view), "--no-extract", "--model", "blk_a"],
    )
    assert result.exit_code != 0, result.output
    combined = result.output + str(result.exception or "")
    assert "design/blk_a/models.yaml" in combined
    assert "design/blk_dupe/models.yaml" in combined


def test_a_duplicate_name_is_reported_before_a_duplicate_top(
    graph_project: Path, tmp_path: Path
):
    """When both collisions hold, "rename one model" fixes both."""
    dupe = graph_project / "design" / "blk_dupe"
    dupe.mkdir()
    (dupe / "blk_a.sv").write_text("module blk_a (input logic clk);\nendmodule\n")
    (dupe / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "blk_a"\n'
        '    desc: "same name, same top"\n'
        '    filelist: ["blk_a.sv"]\n'
    )
    view, _ = _fake_view(tmp_path)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        build_graph(
            graph_project,
            view_executable=str(view),
            view_version="0.4.0",
            extract_enabled=False,
        )
    assert "Rename one" in str(excinfo.value)


def test_a_shared_top_is_allowed_when_one_of_the_two_models_opts_out(
    graph_project: Path, tmp_path: Path
):
    """A `graph: false` model is never exported, so it cannot collide.

    This is the documented way out of the refusal above, so it has to
    actually work — and the surviving model must still export normally.
    """
    _rewrite_model(graph_project, "blk_b", '    top: "blk_a"\n')
    _rewrite_model(graph_project, "blk_b", "    graph: false\n")
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    design = next(t for t in build.tiers if t.tier == DESIGN_TIER)
    assert design.status == "built"
    assert design.failures == []
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record)] == ["blk_a"]
    graph = json.loads(build.graph_path.read_text())
    assert _nodes(graph)["module:blk_a"]["file"] == "design/blk_a/blk_a.sv"
    assert dangling_targets(graph) == []


def test_the_duplicate_top_refusal_ignores_models_out_of_scope(
    graph_project: Path, tmp_path: Path
):
    """`--model` / `-c` narrow the tier, so they narrow the check too.

    Only what would actually be exported can collide; refusing on a
    model the user excluded would make the selector unusable.
    """
    _rewrite_model(graph_project, "blk_b", '    top: "blk_a"\n')
    only_a = [
        m
        for m in graph_build.models_from_design_tree(graph_project / "design")
        if m.name == "blk_a"
    ]
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        models=only_a,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    assert [argv[argv.index("--top") + 1] for argv in _dut_calls(record)] == ["blk_a"]
    assert next(t for t in build.tiers if t.tier == DESIGN_TIER).status == "built"


def test_narrowing_an_all_opted_out_build_refreshes_the_skipped_list(
    graph_project: Path, tmp_path: Path
):
    """A selector that only moves `skipped` must still invalidate the cache.

    An all-opted-out design tier hashes nothing, so `--model` moves no
    input and the fingerprint used to match — handing back a
    `graph-meta.json` whose `skipped` list still described the wider,
    previous invocation. The sidecar is part of what the build promises,
    so what narrowed it is part of the fingerprint.
    """
    for name in ("blk_a", "blk_b"):
        _rewrite_model(graph_project, name, "    graph: false\n")
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view), view_version="0.4.0", extract_enabled=False
    )

    wide = build_graph(graph_project, **common)
    wide_skipped = json.loads(wide.meta_path.read_text())["tiers"][DESIGN_TIER][
        "skipped"
    ]
    assert wide_skipped == [
        {"model": "blk_a", "reason": graph_build.GRAPH_OPT_OUT},
        {"model": "blk_b", "reason": graph_build.GRAPH_OPT_OUT},
        {
            "testbench": "verif/blk_a#tb_cocotb",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "testbench": "verif/blk_a#tb_hdl",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "fpv/blk_a#blk_a_safety",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_generic",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_lint",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
    ]

    only_a = [
        m
        for m in graph_build.models_from_design_tree(graph_project / "design")
        if m.name == "blk_a"
    ]
    narrow = build_graph(graph_project, models=only_a, **common)
    assert narrow.unchanged is False
    assert narrow.fingerprint != wide.fingerprint
    # The sidecar on disk now describes *this* invocation: blk_b is gone
    # from it, and only blk_a's own records remain.
    narrow_skipped = json.loads(narrow.meta_path.read_text())["tiers"][DESIGN_TIER][
        "skipped"
    ]
    assert narrow_skipped == [
        {"model": "blk_a", "reason": graph_build.GRAPH_OPT_OUT},
        {
            "testbench": "verif/blk_a#tb_cocotb",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "testbench": "verif/blk_a#tb_hdl",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "fpv/blk_a#blk_a_safety",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_generic",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
        {
            "run": "impl/blk_a#blk_a_lint",
            "model": "blk_a",
            "reason": graph_build.GRAPH_OPT_OUT,
        },
    ]
    assert narrow_skipped != wide_skipped

    # ...and re-running the narrowed build really is a no-op again.
    assert build_graph(graph_project, models=only_a, **common).unchanged is True


def _out_of_tree_model(project: Path, *, top: str) -> None:
    """A models.yaml outside ``design/``, reached only via a suite.

    The shape a `--regression` selection can produce: a test's
    ``model_path:`` may point anywhere, and the config tier only walks
    ``--design-dir`` for models.yaml — so this file is hashed by nothing.
    """
    vendor = project / "vendor" / "pp"
    vendor.mkdir(parents=True, exist_ok=True)
    (vendor / "pp.sv").write_text("module pp_top (input logic clk);\nendmodule\n")
    (vendor / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        '  - name: "pp_axi"\n'
        '    desc: "vendored collection"\n'
        '    filelist: ["pp.sv"]\n'
        f'    top: "{top}"\n'
    )
    suite = project / "verif" / "vendor"
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        '  - name: "tb_vendor"\n'
        "    filelist: []\n"
        "tests:\n"
        '  - name: "t_vendor"\n'
        '    desc: "vendor smoke"\n'
        "    reglvl: 0\n"
        '    model: "pp_axi"\n'
        '    model_path: "../../vendor/pp/models.yaml"\n'
        '    testbench: "tb_vendor"\n'
    )
    reg = project / "regression.yaml"
    if "verif/vendor/tests.yaml" not in reg.read_text():
        reg.write_text(reg.read_text() + "  - verif/vendor/tests.yaml\n")


def test_editing_top_alone_reroots_a_model_no_tier_hashes(
    graph_project: Path, tmp_path: Path
):
    """`top:` is part of a selected model's fingerprint identity.

    A models.yaml under ``--design-dir`` is hashed by the config tier, so
    editing it invalidates the cache whatever changed. One reached only
    through a test's ``model_path:`` is hashed by nothing — the design
    tier hashes the model's *sources*, and `top:` is not one of them. So
    re-rooting such a model moved no input, the fingerprint matched, and
    the build served a cached graph rooted at the old module.
    """
    _out_of_tree_model(graph_project, top="pp_top")
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
        tb=False,
        flow_tops=False,
    )

    def _build() -> "graph_build.GraphBuild":
        models = graph_build.models_from_regression(graph_project / "regression.yaml")
        return build_graph(graph_project, models=models, **common)

    first = _build()
    assert "module:pp_top" in _nodes(json.loads(first.graph_path.read_text()))
    # The premise: nothing hashes that file, so only the selection can
    # notice the edit.
    hashed = {entry["path"] for report in first.tiers for entry in report.inputs}
    assert "vendor/pp/models.yaml" not in hashed
    # ...and an untouched re-run is still a no-op.
    assert _build().unchanged is True

    models_yaml = graph_project / "vendor" / "pp" / "models.yaml"
    models_yaml.write_text(models_yaml.read_text().replace("pp_top", "pp_alt"))

    second = _build()
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint
    nodes = _nodes(json.loads(second.graph_path.read_text()))
    assert "module:pp_alt" in nodes and "module:pp_top" not in nodes
    assert _build().unchanged is True


def test_opting_an_unhashed_model_out_also_moves_the_fingerprint(
    graph_project: Path, tmp_path: Path
):
    """The `graph:` flag rides in the same identity.

    Membership would in fact catch it — the model leaves the exported set
    and gains a skip record — but the declaration is what changed, so it
    is pinned on the declaration.
    """
    _out_of_tree_model(graph_project, top="pp_top")
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
        tb=False,
        flow_tops=False,
    )

    def _build() -> "graph_build.GraphBuild":
        models = graph_build.models_from_regression(graph_project / "regression.yaml")
        return build_graph(graph_project, models=models, **common)

    first = _build()
    models_yaml = graph_project / "vendor" / "pp" / "models.yaml"
    models_yaml.write_text(models_yaml.read_text() + "    graph: false\n")

    second = _build()
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint
    design = next(t for t in second.tiers if t.tier == DESIGN_TIER)
    assert design.skipped == [{"model": "pp_axi", "reason": graph_build.GRAPH_OPT_OUT}]
    assert "module:pp_top" not in _nodes(json.loads(second.graph_path.read_text()))


def test_tier_flags_are_part_of_the_fingerprint_even_with_nothing_to_hash(
    graph_project: Path, tmp_path: Path
):
    """`--no-tb` / `--no-flow-tops` / `--no-design` change the sidecar too.

    On a project with design-tier inputs the flags already move the input
    hashes. On an all-opted-out one they move nothing, which is exactly
    when the stale sidecar was reachable.
    """
    for name in ("blk_a", "blk_b"):
        _rewrite_model(graph_project, name, "    graph: false\n")
    view, _ = _fake_view(tmp_path)
    common = dict(
        view_executable=str(view), view_version="0.4.0", extract_enabled=False
    )
    base = build_graph(graph_project, **common).fingerprint
    assert build_graph(graph_project, tb=False, **common).fingerprint != base
    assert build_graph(graph_project, flow_tops=False, **common).fingerprint != base
    assert build_graph(graph_project, design=False, **common).fingerprint != base
    # `--no-design` and a fully opted-out tier produce the same graph but
    # not the same report, so they must not share a fingerprint.
    off = build_graph(graph_project, design=False, **common)
    off_design = next(t for t in off.tiers if t.tier == DESIGN_TIER)
    assert off_design.detail == "disabled (--no-design)"


def test_model_top_override_roots_the_export_and_the_config_stitch(
    graph_project: Path, tmp_path: Path
):
    _rewrite_model(graph_project, "blk_b", '    top: "blk_b_core"\n')
    view, record = _fake_view(tmp_path)
    build = build_graph(
        graph_project,
        view_executable=str(view),
        view_version="0.4.0",
        extract_enabled=False,
    )
    assert sorted(argv[argv.index("--top") + 1] for argv in _dut_calls(record)) == [
        "blk_a",
        "blk_b_core",
    ]
    graph = json.loads(build.graph_path.read_text())
    nodes = _nodes(graph)
    assert "module:blk_b_core" in nodes and "module:blk_b" not in nodes
    # The config tier's stitch follows the override, so the merged graph
    # resolves instead of dangling on a module that does not exist.
    assert {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "maps_to"
    } == {
        ("model:design/blk_a/models.yaml#blk_a", "module:blk_a"),
        ("model:design/blk_b/models.yaml#blk_b", "module:blk_b_core"),
    }
    assert dangling_targets(graph) == []


def test_model_top_override_still_dedupes_a_model_topped_flow_run(
    graph_project: Path,
):
    """The fixture's `blk_a_safety` run tops at its model.

    Its `get_top()` follows the model override, so the DUT export still
    covers it and it must not become a second, run-rooted export.
    """
    _rewrite_model(graph_project, "blk_a", '    top: "blk_a_core"\n')
    assert graph_build.flow_runs_from_regressions(graph_project) == []


def test_cli_envelope_and_summary_report_opted_out_models(
    graph_project: Path, tmp_path: Path
):
    _rewrite_model(graph_project, "blk_b", "    graph: false\n")
    view, _ = _fake_view(tmp_path)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "graph",
            "build",
            "--tool",
            str(view),
            "--no-extract",
            "--strict",
        ],
    )
    # `--strict` promotes real per-item failures; an opt-out is not one.
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    design = next(t for t in envelope["payload"]["tiers"] if t["tier"] == DESIGN_TIER)
    assert design["skipped"] == [
        {"model": "blk_b", "reason": graph_build.GRAPH_OPT_OUT}
    ]
    assert "failures" not in design

    human = runner.invoke(
        rb.app, ["graph", "build", "--tool", str(view), "--no-extract", "--force"]
    )
    assert human.exit_code == 0, human.output
    assert "1 skipped" in human.output


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
    build = build_graph(graph_project, extract_enabled=False)
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


def test_the_duplicate_design_top_event_has_a_human_message_case():
    """Guidelines → Logging: an ERROR event needs its own case, or
    `rtl_buddy.log` renders `graph build duplicate_design_top` with none
    of the fields that say which models to go and edit."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "graph_build.duplicate_design_top",
        {
            "top": "blk_a",
            "models": "blk_a, blk_b",
            "paths": "design/blk_a/models.yaml, design/blk_b/models.yaml",
        },
    )
    assert msg != "graph build duplicate_design_top"
    assert "blk_a, blk_b" in msg
    assert "design/blk_b/models.yaml" in msg
    assert "graph: false" in msg


def test_the_duplicate_design_model_event_has_a_human_message_case():
    """Guidelines → Logging: the ERROR event has to name the two files,
    or `rtl_buddy.log` says less than the exception does."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "graph_build.duplicate_design_model",
        {
            "model": "blk_a",
            "paths": "design/blk_a/models.yaml, design/blk_dupe/models.yaml",
        },
    )
    assert msg != "graph build duplicate_design_model"
    assert "blk_a" in msg
    assert "design/blk_dupe/models.yaml" in msg
    assert "rename one of them" in msg
    assert "does not resolve a name collision" in msg


def test_the_new_warning_events_have_human_message_cases():
    """Guidelines → Logging: every WARNING/ERROR event gets a dedicated case,
    otherwise the fallback renders `graph build run_export_failed` with none
    of `run`, `top`, `returncode` or `log` — the fields that make the failure
    actionable."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "graph_build.run_export_failed",
        {
            "run": "fpv/blk_a_chk#chk_bmc",
            "top": "blk_a_chk",
            "returncode": 3,
            "log": "/p/run.log",
        },
    )
    for token in ("fpv/blk_a_chk#chk_bmc", "blk_a_chk", "3", "/p/run.log", "targets"):
        assert token in msg, msg

    msg = _human_message(
        "spec_trace.fpv_reg_load_failed",
        {"path": "/p/fpv_regression.yaml", "error": "bad filetype"},
    )
    assert "/p/fpv_regression.yaml" in msg and "bad filetype" in msg
