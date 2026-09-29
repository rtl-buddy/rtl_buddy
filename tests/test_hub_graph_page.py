"""Tests for the hub-served design-knowledge-graph pane: the ``/graph.json`` join,
column bucketing, the offline ``/graph`` page, ``graph_focus``, module-level
schematic sync, physical heat and the version label.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import graph_page, phys_page, theme
from rtl_buddy.hub.protocol import (
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    new_id,
)
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.viewer_http import ViewerServer, render_index_html
from rtl_buddy.phys.manifest import build_manifest, write_manifest
from rtl_buddy.phys.model import build_synth_model, write_model


_GRAPH = {
    "directed": True,
    "multigraph": True,
    "graph": {
        "schema_version": 1,
        "generator": {"tool": "rtl_buddy", "version": "0.0.0+test", "tier": "merged"},
        "project_root_rel": ".",
    },
    "nodes": [
        {
            "id": "module:fifo",
            "type": "module",
            "label": "fifo",
            "tier": "design",
            "file": "design/fifo/src/fifo.sv",
            "line": 3,
        },
        {
            "id": "inst:fifo/fifo.u_wr",
            "type": "instance",
            "label": "u_wr",
            "tier": "design",
            "file": "design/fifo/src/fifo.sv",
            "line": 9,
        },
        {
            "id": "test:verif/fifo#smoke",
            "type": "test",
            "label": "smoke",
            "tier": "config",
            "file": "verif/fifo/tests.yaml",
            "line": 4,
        },
        {
            "id": "test:verif/fifo#burst",
            "type": "test",
            "label": "burst",
            "tier": "config",
            "file": "verif/fifo/tests.yaml",
            "line": 12,
        },
        {
            "id": "py:verif/fifo/cocotb_fifo.py",
            "type": "python_module",
            "label": "cocotb_fifo",
            "tier": "binding",
            "file": "verif/fifo/cocotb_fifo.py",
        },
    ],
    "links": [
        {
            "source": "inst:fifo/fifo.u_wr",
            "target": "module:fifo_wr",
            "type": "instance_of",
            "confidence": "EXTRACTED",
        },
        {
            "source": "test:verif/fifo#smoke",
            "target": "py:verif/fifo/cocotb_fifo.py",
            "type": "binds_to",
            "confidence": "EXTRACTED",
        },
    ],
}

_OVERLAY = {
    "rtl-buddy-filetype": "graph_results_overlay",
    "schema_version": 1,
    "summary": {"tests": 2, "statuses": {"PASS": 1, "FAIL": 1}},
    "tests": {
        "test:verif/fifo#smoke": {
            "id": "test:verif/fifo#smoke",
            "suite": "verif/fifo",
            "test": "smoke",
            "status": "PASS",
            "source": "envelope",
        },
        "test:verif/fifo#burst": {
            "id": "test:verif/fifo#burst",
            "suite": "verif/fifo",
            "test": "burst",
            "status": "FAIL",
            "desc": "checker mismatch at 120 ns",
            "source": "envelope",
        },
    },
}


@pytest.fixture
def built_graph(tmp_path: Path) -> Path:
    """A project root with ``artefacts/graph/{graph,results-overlay}.json``."""

    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(_GRAPH), encoding="utf-8")
    (out / "results-overlay.json").write_text(json.dumps(_OVERLAY), encoding="utf-8")
    return tmp_path


def _node(node_id: str, node_type: str, tier: str, **attrs) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "label": node_id.split(":", 1)[-1],
        "tier": tier,
        **attrs,
    }


def _link(source: str, target: str, link_type: str) -> dict:
    return {
        "source": source,
        "target": target,
        "type": link_type,
        "confidence": "EXTRACTED",
    }


# Every column, exercised once. The ``fpv`` testbench hierarchy is synthetic; the rule
# follows the suite's ``flow``, not its config file.
_BUCKET_GRAPH = {
    "directed": True,
    "multigraph": True,
    "graph": {"schema_version": 1, "project_root_rel": "."},
    "nodes": [
        _node("spec:fifo", "spec_block", "config"),
        _node("covitem:fifo#F-COV-1", "coverage_item", "config"),
        _node("doc:spec/fifo/README.md", "spec_doc", "config"),
        _node("golden:spec/fifo/fifo_model.py", "golden_model", "config"),
        _node("model:design/fifo/models.yaml#fifo", "model", "config"),
        _node("module:fifo", "module", "design"),
        _node("inst:fifo/fifo.u_wr", "instance", "design"),
        _node("port:fifo.clk", "port", "design", owner="fifo"),
        _node("suite:verif/fifo", "suite", "config", flow="sim"),
        _node("test:verif/fifo#smoke", "test", "config", flow="sim"),
        _node("tb:verif/fifo#tb_top", "testbench", "config", flow="sim"),
        _node(
            "module:tb_top@verif/fifo", "module", "design", qualified_by="verif/fifo"
        ),
        _node(
            "inst:tb_top/tb_top.u_dut@verif/fifo",
            "instance",
            "design",
            qualified_by="verif/fifo",
        ),
        # Not qualified, so it reaches its column through its owner.
        _node("param:tb_top.WIDTH", "parameter", "design", owner="tb_top"),
        _node("suite:synth/fifo", "suite", "config", flow="synth"),
        _node("test:synth/fifo#generic", "test", "config", flow="synth"),
        _node("suite:fpv/fifo", "suite", "config", flow="fpv"),
        _node("test:fpv/fifo#safety", "test", "config", flow="fpv"),
        _node("tb:fpv/fifo#fv_top", "testbench", "config", flow="fpv"),
        _node("module:fifo_fv_top", "module", "design"),
        _node("inst:fifo_fv_top/fifo_fv_top.u_dut", "instance", "design"),
        _node("suite:lint/cdc", "suite", "config", flow="cdc"),
        _node("test:lint/cdc#fifo_lint", "test", "config", flow="cdc"),
        _node("suite:fpga/fifo", "suite", "config", flow="fpga"),
        _node("test:fpga/fifo#a35t", "test", "config", flow="fpga"),
        # cocotb wins over its suite's flow
        _node("test:verif/fifo#cocotb", "test", "config", flow="sim", cocotb=True),
        _node(
            "tb:verif/fifo#tb_cocotb", "testbench", "config", flow="sim", cocotb=True
        ),
        _node("py:verif/fifo/cocotb_fifo.py", "python_module", "binding"),
        # render-don't-drop: an unknown flow, an unknown type, no tier
        _node("suite:verif/odd", "suite", "config", flow="teleportation"),
        _node("weird:thing", "sorcery", "config"),
        {"id": "orphan:1", "type": "mystery", "label": "orphan"},
    ],
    "links": [
        _link("model:design/fifo/models.yaml#fifo", "module:fifo", "maps_to"),
        _link("tb:verif/fifo#tb_top", "module:tb_top@verif/fifo", "elaborates_as"),
        _link("tb:fpv/fifo#fv_top", "module:fifo_fv_top", "elaborates_as"),
        # A cocotb testbench tops at the DUT itself; that must not pull the DUT
        # hierarchy into a flow column.
        _link("tb:verif/fifo#tb_cocotb", "module:fifo", "elaborates_as"),
        # A non-simulation run's ``top:`` is not a testbench-ownership signal: the
        # module a synth run names is still design.
        _link("test:synth/fifo#generic", "module:fifo", "targets"),
    ],
}


@pytest.fixture
def bucket_graph(tmp_path: Path) -> Path:
    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(_BUCKET_GRAPH), encoding="utf-8")
    return tmp_path


def _columns(project_root: Path) -> dict[str, str]:
    payload = graph_page.build_graph_payload(project_root)
    return {n["id"]: n["category"] for n in payload["nodes"]}


def test_every_node_lands_in_exactly_one_declared_column(bucket_graph: Path):
    columns = _columns(bucket_graph)
    assert len(columns) == len(_BUCKET_GRAPH["nodes"])
    assert set(columns.values()) <= set(graph_page.COLUMN_ORDER)


def test_spec_types_and_models_bucket_by_type(bucket_graph: Path):
    columns = _columns(bucket_graph)
    for node_id in (
        "spec:fifo",
        "covitem:fifo#F-COV-1",
        "doc:spec/fifo/README.md",
        "golden:spec/fifo/fifo_model.py",
    ):
        assert columns[node_id] == "spec"
    # A model aliases its module, so it sits beside the design rather than in the
    # suite's flow column.
    assert columns["model:design/fifo/models.yaml#fifo"] == "design"


def test_flow_stamp_picks_the_config_column(bucket_graph: Path):
    columns = _columns(bucket_graph)
    assert columns["suite:verif/fifo"] == "test-config"
    assert columns["test:synth/fifo#generic"] == "syn-config"
    assert columns["test:fpv/fifo#safety"] == "formal-config"
    assert columns["test:lint/cdc#fifo_lint"] == "cdc-config"
    # FPGA implementation shares the synthesis column.
    assert columns["test:fpga/fifo#a35t"] == "syn-config"


def test_cocotb_wins_over_the_suites_flow(bucket_graph: Path):
    columns = _columns(bucket_graph)
    assert columns["test:verif/fifo#cocotb"] == "test-cocotb"
    assert columns["tb:verif/fifo#tb_cocotb"] == "test-cocotb"
    assert columns["py:verif/fifo/cocotb_fifo.py"] == "test-cocotb"
    assert columns["suite:verif/fifo"] == "test-config"


def test_dut_hierarchy_stays_in_the_design_column(bucket_graph: Path):
    columns = _columns(bucket_graph)
    # ``module:fifo`` is targeted by ``maps_to``, ``elaborates_as`` and ``targets`` at
    # once; the model's claim wins.
    assert columns["module:fifo"] == "design"
    assert columns["inst:fifo/fifo.u_wr"] == "design"
    assert columns["port:fifo.clk"] == "design"


def test_testbench_hierarchy_follows_its_suites_flow(bucket_graph: Path):
    """A testbench is not the design: both halves of a ``rb graph build`` are ``tier:
    design``, so the owning suite decides.
    """

    columns = _columns(bucket_graph)
    assert columns["module:tb_top@verif/fifo"] == "test-config"
    assert columns["inst:tb_top/tb_top.u_dut@verif/fifo"] == "test-config"
    # Reached through its ``owner``, whose id is suite-qualified.
    assert columns["param:tb_top.WIDTH"] == "test-config"
    assert columns["module:fifo_fv_top"] == "formal-config"
    assert columns["inst:fifo_fv_top/fifo_fv_top.u_dut"] == "formal-config"


def test_unplaceable_nodes_land_in_other_rather_than_vanish(bucket_graph: Path):
    columns = _columns(bucket_graph)
    # An unknown flow is still a suite and keeps the default flow column.
    assert columns["suite:verif/odd"] == graph_page.FALLBACK_FLOW_COLUMN
    assert columns["weird:thing"] == "other"
    assert columns["orphan:1"] == "other"


def test_hub_block_carries_the_column_order_and_counts(bucket_graph: Path):
    hub = graph_page.build_graph_payload(bucket_graph)["graph"]["hub"]
    assert hub["columns"] == list(graph_page.COLUMN_ORDER)
    assert set(hub["categories"]) == set(graph_page.COLUMN_ORDER)
    assert sum(hub["categories"].values()) == len(_BUCKET_GRAPH["nodes"])
    assert hub["categories"]["cdc-config"] == 2
    assert hub["categories"]["design"] == 4


def test_category_is_served_only_and_never_written_to_disk(bucket_graph: Path):
    """`category` is a presentation choice; graph.json must not churn."""

    graph_file = bucket_graph / "artefacts" / "graph" / "graph.json"
    before = graph_file.read_bytes()
    graph_page.build_graph_payload(bucket_graph)
    assert graph_file.read_bytes() == before
    assert all("category" not in n for n in json.loads(before)["nodes"])


def test_payload_joins_overlay_onto_test_nodes(built_graph: Path):
    payload = graph_page.build_graph_payload(built_graph)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["test:verif/fifo#smoke"]["results"]["status"] == "PASS"
    assert by_id["test:verif/fifo#burst"]["results"]["status"] == "FAIL"
    # Non-test nodes get no entry; the overlay is keyed by test node id.
    assert "results" not in by_id["module:fifo"]


def test_payload_never_writes_graph_json_back(built_graph: Path):
    """The join is in-memory; graph.json stays byte-identical on disk."""

    graph_file = built_graph / "artefacts" / "graph" / "graph.json"
    before = graph_file.read_bytes()
    graph_page.build_graph_payload(built_graph)
    graph_page.build_graph_payload(built_graph)
    assert graph_file.read_bytes() == before


def test_payload_hub_block_describes_the_render(built_graph: Path):
    hub = graph_page.build_graph_payload(built_graph)["graph"]["hub"]
    assert hub["schema_version"] == graph_page.PAGE_SCHEMA_VERSION
    assert hub["graph_path"] == "artefacts/graph/graph.json"
    assert hub["overlay_path"] == "artefacts/graph/results-overlay.json"
    # Counts are of the served body: a dangling link target is a link but not a node.
    assert hub["counts"]["nodes"] == len(_GRAPH["nodes"])
    assert hub["counts"]["links"] == len(_GRAPH["links"])
    assert hub["counts"]["with_results"] == 2
    assert hub["tiers"]["design"] == 2
    assert hub["tiers"]["config"] == 2
    assert hub["tiers"]["binding"] == 1
    # Tiers still count what the build produced; columns are the layout.
    assert hub["columns"] == list(graph_page.COLUMN_ORDER)
    assert sum(hub["categories"].values()) == len(_GRAPH["nodes"])
    assert hub["overlay_summary"]["statuses"] == {"PASS": 1, "FAIL": 1}


def test_payload_without_overlay_is_still_served(tmp_path: Path):
    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(_GRAPH), encoding="utf-8")
    hub = graph_page.build_graph_payload(tmp_path)["graph"]["hub"]
    assert hub["overlay_path"] is None
    assert hub["counts"]["with_results"] == 0


def test_payload_bytes_404_names_the_build_command(tmp_path: Path):
    status, body = graph_page.graph_payload_bytes(tmp_path)
    assert status == 404
    assert "rb graph build" in json.loads(body)["error"]


def test_graph_files_present(tmp_path: Path, built_graph: Path):
    assert graph_page.graph_files_present(built_graph) is True
    unbuilt = tmp_path / "no-graph-here"
    unbuilt.mkdir()
    assert graph_page.graph_files_present(unbuilt) is False


def test_page_injects_hub_address():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_GRAPH_URL__ = '/graph.json'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """Every non-anchor ``src``/``href`` is a same-origin absolute path; no CDN, remote
    font or import.
    """

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<script src=" not in body
    assert "@import" not in body
    for host in ("cdn.", "unpkg", "jsdelivr", "googleapis", "//fonts"):
        assert host not in body
    for attr in ("href=", "src="):
        for chunk in body.split(attr)[1:]:
            quote = chunk[0]
            value = chunk[1:].split(quote)[0] if quote in "\"'" else chunk.split()[0]
            assert value.startswith("/"), f"{attr}{value}"
    # Only the SVG namespace may be an absolute URL.
    for scheme in ("https://", "http://"):
        for chunk in body.split(scheme)[1:]:
            authority = chunk.split("'")[0].split('"')[0].split(" ")[0]
            assert authority.startswith("www.w3.org"), authority


def test_page_links_the_shared_token_sheet_with_a_fallback():
    """The sheet is a link, but a 404 on it must not blank the page."""

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<link rel="stylesheet" href="/hub/theme.css">' in body
    assert theme.FAVICON_16 in body and theme.FAVICON_32 in body
    # Inline fallback: the tokens the pane cannot render without.
    for token in ("--bg:", "--panel:", "--fg:", "--accent:", "--col-design:"):
        assert token in body, token
    # Light default: the first surface value is the light one.
    assert "--bg:          #f8fafc;" in body
    # The fallback comes before the link, or it out-ranks the sheet at equal specificity
    # and breaks ``prefers-color-scheme: dark``.
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_page_carries_the_pieces_the_issue_asks_for():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    for column in graph_page.COLUMN_ORDER:
        assert f"'{column}'" in body, column
        assert f"--col-{column}:" in body, column
    for token in ("design", "config", "binding", "PASS", "FAIL"):
        assert token in body
    assert "selection_changed" in body
    assert "open_source" in body
    assert "graph_focus" in body
    assert "'graph'" in body  # registers under its own origin


def _page_js() -> str:
    """The page's inline script — the last ``<script>`` in the body."""

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
    """Pure helpers sliced out of the page between markers and run in bare ``node``;
    nothing between the markers may touch the DOM.
    """

    match = re.search(rf"// >>> {marker}\n(.*?)// <<< {marker}", _page_js(), re.S)
    assert match, f"the {marker} markers moved"
    return match.group(1)


def _node_eval(script: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the dev machine
        pytest.skip("node not installed")
    done = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_a_model_node_is_named_by_its_maps_to_stitch():
    """On project-tier graphs the module a ``model:`` node roots at is its ``maps_to``
    target, not its ``#<name>`` fragment.
    """

    out = _node_eval(
        _marked_js("module-name")
        + """
        var links = [
          { type: 'maps_to', source: 'model:design/common/models.yaml#ip_async_fifo',
            target: 'module:ip_async_fifo' },
          // `top: axi_xbar` — the name and the root module differ.
          { type: 'maps_to', source: 'model:design/vendor/models.yaml#pp_axi',
            target: 'module:axi_xbar' },
          // A root whose id had to be suite-qualified: the qualifier
          // disambiguates the id, it is not part of the module's name.
          { type: 'maps_to', source: 'model:design/x/models.yaml#alias',
            target: 'module:real_top@verif/x' },
          // Right type, wrong shape — neither may be mistaken for a root.
          { type: 'maps_to', source: 'model:design/x/models.yaml#empty',
            target: 'module:' },
          { type: 'maps_to', source: 'model:design/x/models.yaml#weird',
            target: 'inst:real_top/real_top' },
          // Some other edge off the same model.
          { type: 'specified_by', source: 'model:design/common/models.yaml#ip_async_fifo',
            target: 'block:fifo' },
          null
        ];
        function outOf(id) {
          return links.filter(function (l) { return l && l.source === id; });
        }
        var ids = [
          'model:design/common/models.yaml#ip_async_fifo',
          'model:design/vendor/models.yaml#pp_axi',
          'model:design/x/models.yaml#alias',
          'model:design/x/models.yaml#empty',
          'model:design/x/models.yaml#weird',
          // `graph: false` — no stitch at all (its `file` is irrelevant
          // here: a models.yaml is not a design coordinate either).
          'model:design/vendor/models.yaml#apb_intf'
        ];
        console.log(JSON.stringify(ids.map(function (id) {
          return moduleNameFor({ id: id, type: 'model' }, outOf(id));
        })));
        // No links handed over at all, and a non-array second argument
        // (`nodes.map(moduleNameFor)` passes the index).
        console.log(JSON.stringify([
          moduleNameFor({ id: ids[0], type: 'model' }),
          moduleNameFor({ id: ids[0], type: 'model' }, 0),
          moduleNameFor({ id: ids[0], type: 'model' }, [])
        ]));
        """
    )
    resolved, degenerate = out.strip().splitlines()
    assert json.loads(resolved) == [
        "ip_async_fifo",
        # The top, not the model name.
        "axi_xbar",
        "real_top",
        # `module:` with nothing after it is not a module name.
        None,
        # `maps_to` at a non-module target is not a root.
        None,
        # An opted-out model has no stitch, so no coordinate.
        None,
    ]
    assert json.loads(degenerate) == [None, None, None]


def test_the_served_payload_carries_the_stitch_the_page_resolves_through(
    tmp_path: Path,
):
    """Bucketing leaves ``maps_to`` links unrewritten, and an opted-out model reaches
    the page with no stitch.
    """

    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {"schema_version": 1, "generator": {"tier": "merged"}},
        "nodes": [
            {
                "id": "module:axi_xbar",
                "type": "module",
                "label": "axi_xbar",
                "tier": "design",
                "file": "design/vendor/xbar.sv",
                "line": 1,
            },
            # `top: axi_xbar` — name and root module differ.
            {
                "id": "model:design/vendor/models.yaml#pp_axi",
                "type": "model",
                "label": "pp_axi",
                "tier": "config",
                "file": "design/vendor/models.yaml",
            },
            # `graph: false` — node present, no stitch, flagged.
            {
                "id": "model:design/vendor/models.yaml#apb_intf",
                "type": "model",
                "label": "apb_intf",
                "tier": "config",
                "graph": False,
                "file": "design/vendor/models.yaml",
            },
        ],
        "links": [
            {
                "source": "model:design/vendor/models.yaml#pp_axi",
                "target": "module:axi_xbar",
                "type": "maps_to",
                "confidence": "DECLARED",
            },
        ],
    }
    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    payload = graph_page.build_graph_payload(tmp_path)
    stitches = {
        (link["source"], link["target"])
        for link in payload["links"]
        if link.get("type") == "maps_to"
    }
    assert stitches == {("model:design/vendor/models.yaml#pp_axi", "module:axi_xbar")}
    served = {n["id"]: n for n in payload["nodes"]}
    # The opted-out model is still a node, since cross-references point at it.
    assert served["model:design/vendor/models.yaml#apb_intf"]["graph"] is False
    assert not any(
        link["source"] == "model:design/vendor/models.yaml#apb_intf"
        for link in payload["links"]
    )


def test_a_module_node_falls_back_to_its_own_id():
    """A ``module:`` node with no ``instance_of`` link is named by its id minus the
    prefix.
    """

    out = _node_eval(
        _marked_js("module-name")
        + """
        var ids = ['module:fifo', 'module:axi__lite__W8', 'module:', 'fifo'];
        console.log(JSON.stringify(ids.map(function (id) {
          return moduleNameFor({ id: id, type: 'module' });
        })));
        """
    )
    assert json.loads(out) == [
        "fifo",
        # Verbatim: both ends speak the source vocabulary, so no elaboration suffix is
        # stripped.
        "axi__lite__W8",
        None,
        # Not a `module:` id, so not a name we can vouch for.
        None,
    ]


def test_nothing_else_names_a_module():
    """Tests, suites, coverage items and testbenches are not design coordinates."""

    out = _node_eval(
        _marked_js("module-name")
        + """
        var nodes = [
          { id: 'test:verif/fifo#smoke', type: 'test' },
          { id: 'suite:verif/fifo', type: 'suite' },
          { id: 'covitem:spec/fifo#REQ-1', type: 'covitem' },
          { id: 'tb:verif/fifo/tb_fifo.sv', type: 'testbench' },
          { id: 'py:verif/fifo/cocotb_fifo.py', type: 'python_module' },
          { id: 'inst:fifo/fifo.u_wr', type: 'instance' }
        ];
        console.log(JSON.stringify(nodes.map(function (n) {
          return moduleNameFor(n, []);
        })));
        console.log(JSON.stringify([moduleNameFor(null, []),
                                    moduleNameFor(undefined, []),
                                    moduleNameFor({ type: 'module' }, [])]));
        """
    )
    typed, nullish = out.strip().splitlines()
    assert json.loads(typed) == [None] * 6
    assert json.loads(nullish) == [None, None, None]


def test_a_node_without_an_instance_still_syncs_the_schematic():
    """With only config and binding tiers built, a click falls back to ``graph_focus
    {node: 'module:<name>'}``.

    The click and the inspector's ``send → sch`` share ``viewTargetFor``; asserted on
    the source because it closes over the DOM.
    """

    js = _page_js()
    derivation = js.split("function viewTargetFor(n, out) {")[1].split("\n  }")[0]
    # The instance path still wins…
    assert "var ip = instancePathFor(n);" in derivation
    assert "type: 'selection_changed', payload: { instance_path: ip }," in derivation
    # The module name is the fallback, not a second send; a ``model:`` node's module
    # comes off its ``maps_to`` link.
    assert "var mod = moduleNameFor(n, out);" in derivation
    assert "function viewTargetFor(n, out) {" in js
    assert "type: 'graph_focus', payload: { node: 'module:' + mod }," in derivation
    assert derivation.index("instancePathFor") < derivation.index("moduleNameFor")
    assert "note: 'focus module:' + mod + ' in the schematic'" in derivation
    # One emit per click, off the one derivation.
    click = js.split("if (els.optSelect.checked) {")[1].split("\n    }")[0]
    assert "var view = viewTargetFor(n, state.out[n.id]);" in click
    assert "var sent = !!view && emit(view.type, view.payload);" in click
    # The cross-model warning is armed only for a delivered instance path,
    # and cleared for everything else — including a send that never left.
    assert "if (sent && view.ip) { maybeWarnCrossModel(view.ip); }" in click
    assert "else { setCrossModel(null); }" in click
    # Self-echo is harmless: inbound graph_focus ignores our own origin.
    assert "case 'graph_focus':\n        if (env.origin === 'graph') { break; }" in js


def test_a_models_roots_come_off_the_maps_to_stitch():
    """Active-model roots come from the payload: a ``model:`` node's ``maps_to`` target.
    The model name is a fallback only when the graph has no stitch.
    """

    out = _node_eval(
        _marked_js("active-model")
        + """
        var links = [
          { type: 'maps_to', source: 'model:design/common/models.yaml#fifo',
            target: 'module:fifo' },
          { type: 'maps_to', source: 'model:design/cdc/models.yaml#cdc',
            target: 'module:cdc' },
          // A model whose top module is named something else entirely,
          // and whose module id had to be suite-qualified.
          { type: 'maps_to', source: 'model:design/x/models.yaml#alias',
            target: 'module:real_top@verif/x' },
          // ...and a second model that really is rooted at `alias`. The
          // whole point: `alias` is NOT the first model's elaboration.
          { type: 'maps_to', source: 'model:design/y/models.yaml#other',
            target: 'module:alias' },
          // A model with two stitches keeps both.
          { type: 'maps_to', source: 'model:design/z/models.yaml#twin',
            target: 'module:twin_a' },
          { type: 'maps_to', source: 'model:design/z/models.yaml#twin',
            target: 'module:twin_b' },
          // Not a model->module stitch: the right type, the wrong source
          // prefix — a `tb:` node's roots are not a model's.
          { type: 'maps_to', source: 'tb:verif/x#tb', target: 'module:tb_top' },
          { type: 'instance_of', source: 'inst:fifo/fifo', target: 'module:fifo' },
          // A graphable model whose `top:` IS the opted-out model's
          // name. Legal: opted-out models are excluded from the build's
          // top-collision check, so nothing stops `apb_intf` being some
          // other model's root module.
          { type: 'maps_to', source: 'model:design/w/models.yaml#wrapper',
            target: 'module:apb_intf' },
          null
        ];
        // The config tier emits a `model:` node for every model it reads,
        // including the ones that opted out of the design tier.
        var nodes = [
          { id: 'model:design/common/models.yaml#fifo', type: 'model' },
          { id: 'model:design/cdc/models.yaml#cdc', type: 'model' },
          { id: 'model:design/x/models.yaml#alias', type: 'model' },
          { id: 'model:design/y/models.yaml#other', type: 'model' },
          { id: 'model:design/z/models.yaml#twin', type: 'model' },
          { id: 'model:design/w/models.yaml#wrapper', type: 'model' },
          // `graph: false`: a node, and no stitch, by design.
          { id: 'model:design/v/models.yaml#apb_intf', type: 'model',
            graph: false },
          // Not a model node, and not a name to match on.
          { id: 'module:apb_intf', type: 'module' },
          { id: 'tb:verif/x#tb', type: 'testbench' },
          null
        ];
        var names = ['fifo', 'alias', 'twin', 'apb_intf', 'nope',
                     '', null, undefined];
        names.forEach(function (m) {
          console.log(JSON.stringify(
            Object.keys(activeModelRoots(links, m, nodes)).sort()));
        });
        console.log(JSON.stringify(Object.keys(
          activeModelRoots(null, 'fifo', null))));
        """
    )
    assert [json.loads(line) for line in out.strip().splitlines()] == [
        ["fifo"],
        # Only the module it maps to; ``alias`` is another model's root.
        ["real_top"],
        ["twin_a", "twin_b"],
        # The opted-out model has no stitch and so no roots; seeding its name would
        # claim another model's elaboration.
        [],
        # A model with no node in the payload falls back to the naming convention.
        ["nope"],
        # No active model, no roots, no preference.
        [],
        [],
        [],
        # No payload at all is the same answer as an unbuilt config tier.
        ["fifo"],
    ]


def test_the_active_models_instance_wins_over_a_shallower_stranger():
    """A candidate rooted in the active model beats a shallower one from any other
    model.
    """

    out = _node_eval(
        _marked_js("active-model")
        + """
        var links = [{ type: 'maps_to',
                       source: 'model:design/common/models.yaml#fifo',
                       target: 'module:fifo' }];
        var roots = activeModelRoots(links, 'fifo');
        console.log(shallowestInstancePath(
          ['cdc.u_sync', 'fifo.u_top.u_deep.u_x'], roots));
        // …and inside the active model the shallowest still wins.
        console.log(shallowestInstancePath(
          ['fifo.u_top.u_deep.u_x', 'fifo.u_x', 'cdc.u_sync'], roots));
        // Ties keep the first candidate — link order, as before.
        console.log(shallowestInstancePath(['fifo.u_a', 'fifo.u_b'], roots));
        """
    )
    assert out.strip().splitlines() == [
        "fifo.u_top.u_deep.u_x",
        "fifo.u_x",
        "fifo.u_a",
    ]


def test_a_suite_qualified_root_still_counts_as_the_active_models_own():
    """``@<suite>`` qualifiers are stripped from both halves of the identity; otherwise
    root-scope instances of two models tie on depth and ownership cannot break the
    tie.
    """

    out = _node_eval(
        _marked_js("active-model")
        + """
        var links = [{ type: 'maps_to',
                       source: 'model:design/common/models.yaml#fifo',
                       target: 'module:fifo@verif/x' }];
        var roots = activeModelRoots(links, 'fifo');
        // Same depth; the active model's own elaboration must win despite
        // carrying the suite qualifier.
        console.log(shallowestInstancePath(['cdc', 'fifo@verif/x'], roots));
        // The ordinary case, where the qualifier lands on the LAST
        // component, was already clean and stays that way.
        console.log(shallowestInstancePath(
          ['cdc.u_sync', 'fifo.u_top.u_x@verif/x'], roots));
        """
    )
    assert out.strip().splitlines() == ["fifo@verif/x", "fifo.u_top.u_x@verif/x"]


def test_a_module_the_active_model_never_instantiates_still_resolves():
    """A vendor block outside the active model is still a valid landing, and an unknown
    active model behaves as no preference.
    """

    out = _node_eval(
        _marked_js("active-model")
        + """
        var links = [{ type: 'maps_to',
                       source: 'model:design/common/models.yaml#fifo',
                       target: 'module:fifo' }];
        var paths = ['cdc.u_a.u_sync', 'cdc.u_sync'];
        // Active model known, but it instantiates the module nowhere:
        // the all-models shallowest answer stands.
        console.log(shallowestInstancePath(paths, activeModelRoots(links, 'fifo')));
        // Active model unknown: the same shallowest answer, unchanged.
        console.log(shallowestInstancePath(paths, activeModelRoots(links, null)));
        console.log(shallowestInstancePath(paths, null));
        // Nothing to resolve at all is still null, not a crash.
        console.log(JSON.stringify([
          shallowestInstancePath([], activeModelRoots(links, 'fifo')),
          shallowestInstancePath(null, null),
          shallowestInstancePath(['', null, 7], activeModelRoots(links, 'fifo'))
        ]));
        """
    )
    lines = out.strip().splitlines()
    assert lines[:3] == ["cdc.u_sync"] * 3
    assert json.loads(lines[3]) == [None, None, None]


def test_a_root_is_translated_back_to_a_selectable_model():
    """``modelForRoot`` inverts ``activeModelRoots``: a root is a module name, and the
    switch request needs the declaring model's name.
    """

    out = _node_eval(
        _marked_js("active-model")
        + """
        var links = [
          { type: 'maps_to', source: 'model:design/common/models.yaml#fifo',
            target: 'module:fifo' },
          // `top: axi_xbar` — the root is not a selectable model name.
          { type: 'maps_to', source: 'model:design/vendor/models.yaml#pp_axi',
            target: 'module:axi_xbar' },
          // Suite-qualified on the stitch side; the lookup key is not.
          { type: 'maps_to', source: 'model:design/x/models.yaml#alias',
            target: 'module:real_top@verif/x' },
          // First stitch wins when two models top at one module — link
          // order, the same tie-break the ranking uses.
          { type: 'maps_to', source: 'model:design/y/models.yaml#twin_a',
            target: 'module:shared_top' },
          { type: 'maps_to', source: 'model:design/y/models.yaml#twin_b',
            target: 'module:shared_top' },
          // Right type, wrong shapes — none of these names a model.
          { type: 'maps_to', source: 'tb:verif/x#tb', target: 'module:tb_top' },
          { type: 'maps_to', source: 'model:design/x/models.yaml',
            target: 'module:unfragmented' },
          { type: 'instance_of', source: 'inst:cdc/cdc', target: 'module:cdc' },
          null
        ];
        var roots = ['fifo', 'axi_xbar', 'real_top', 'shared_top', 'tb_top',
                     'unfragmented', 'cdc', '', null, undefined];
        console.log(JSON.stringify(roots.map(function (r) {
          return modelForRoot(links, r);
        })));
        // No payload to read a stitch off: the convention is all there is.
        console.log(JSON.stringify([modelForRoot(null, 'fifo'),
                                    modelForRoot([], 'fifo')]));
        """
    )
    translated, degenerate = out.strip().splitlines()
    assert json.loads(translated) == [
        "fifo",
        # The declaring model, not the module.
        "pp_axi",
        "alias",
        "twin_a",
        # A ``tb:`` stitch or a ``model:`` id without ``#<name>`` declares no model; the
        # module name stands.
        "tb_top",
        "unfragmented",
        "cdc",
        None,
        None,
        None,
    ]
    assert json.loads(degenerate) == ["fifo", "fifo"]


def test_the_cross_model_warning_compares_roots_not_the_model_name():
    """The cross-model warning compares de-qualified roots with the mapped roots and
    offers the declaring model; asserted on the source since it closes over pane
    state.
    """

    js = _page_js()
    warn = js.split("function maybeWarnCrossModel(ip) {")[1].split("\n  }")[0]
    # The root is de-qualified and compared with the mapped roots, never with the model
    # name.
    assert "var root = rootComponent(ip);" in warn
    assert "root === activeModel" not in warn
    # Only "the hub has no active model" is decided synchronously; cached roots never
    # suppress, since the cache can lag the schematic.
    assert "if (!activeModel) { setCrossModel(null); return; }" in warn
    assert "currentModelRoots()[root]) { setCrossModel(null); return; }" not in warn
    # Reconfirm always, and use the freshly confirmed model's roots.
    assert "activeModel = reply.payload.active_model;" in warn
    assert "if (activeModel && !currentModelRoots()[root])" in warn
    # An unconfirmable reply disarms the warning.
    assert "if (reply.kind !== 'response') { setCrossModel(null); return; }" in warn
    # A foreign root is translated back to a selectable model before the switch target
    # is built; the button and note name that model.
    assert "var target = modelForRoot(state.links, root);" in warn
    assert "setCrossModel({ model: target, ip: ip });" in warn
    assert "originLabel('view') + ' → ' + target" in warn
    # The switch still asks the hub for that model.
    fix = js.split("els.fixView.addEventListener('click', function () {")[1]
    assert "fetch('/view.json?model=' + encodeURIComponent(target.model))" in fix


def _cross_model_js() -> str:
    """``maybeWarnCrossModel`` is lifted whole and run against stubs for
    ``activeModel``, ``state``, ``request``, ``note`` and ``setCrossModel``.

    ``currentModelRoots`` is stood in uncached; its memoisation keys are asserted
    elsewhere.
    """

    body = _page_js().split("function maybeWarnCrossModel(ip) {")[1].split("\n  }")[0]
    return (
        _marked_js("active-model")
        + "function currentModelRoots() {\n"
        + "  return activeModelRoots(state.links, activeModel, state.nodes);\n"
        + "}\n"
        + "function maybeWarnCrossModel(ip) {"
        + body
        + "\n}\n"
    )


def test_a_stale_active_model_cannot_suppress_the_cross_model_warning():
    """The cached model never decides on its own, since the cache can be behind the
    schematic; every selection reconfirms first.

    The scenarios select ``bar.u_x``: ``bar`` is model ``foo``'s ``top:``, and
    ``cdc`` is a second model rooted at itself.
    """

    out = _node_eval(
        _cross_model_js()
        + """
        var LINKS = [
          // `top: bar` — the root is not the model's name.
          { type: 'maps_to', source: 'model:design/a/models.yaml#foo',
            target: 'module:bar' },
          { type: 'maps_to', source: 'model:design/b/models.yaml#cdc',
            target: 'module:cdc' }
        ];
        var NODES = [
          { id: 'model:design/a/models.yaml#foo', type: 'model' },
          { id: 'model:design/b/models.yaml#cdc', type: 'model' }
        ];
        var state, activeModel, crossModel, notes, requests, reply;
        function setCrossModel(t) { crossModel = t; }
        function note(text, level) { notes.push([text, level]); }
        function originLabel() { return 'view'; }
        function request(type, payload, onReply) {
          requests.push(type);
          onReply(reply);
        }
        function scenario(cached, replyEnv) {
          state = { links: LINKS, nodes: NODES };
          activeModel = cached; crossModel = null; notes = []; requests = [];
          reply = replyEnv;
          maybeWarnCrossModel('bar.u_x');
          return {
            requests: requests, cross: crossModel, notes: notes,
            active: activeModel
          };
        }
        function confirm(model) {
          return { kind: 'response', payload: { active_model: model } };
        }
        console.log(JSON.stringify([
          // 1. The regression: the cache says `foo`, whose roots contain
          //    `bar`, but the schematic is really on `cdc`.
          scenario('foo', confirm('cdc')),
          // 2. The cache was right — reconfirmed, then suppressed.
          scenario('foo', confirm('foo')),
          // 3. Stale the other way: the cache accuses, the hub exonerates.
          scenario('cdc', confirm('foo')),
          // 4. A genuinely foreign root, cache and hub agreeing.
          scenario('cdc', confirm('cdc')),
          // 5. The hub has no active model any more.
          scenario('foo', confirm(null)),
          // 6. Unconfirmable: no accusation, and no stale arming left.
          scenario('foo', { kind: 'error', payload: {} }),
          // 7. No cached model at all: decided synchronously, no request.
          scenario(null, confirm('cdc'))
        ], null, 0));
        """
    )
    stale, right, exonerated, foreign, none, unconfirmable, unknown = json.loads(out)

    # 1. Reconfirmed: the warning fires against ``foo``, the model declaring ``bar`` as
    # its top; the hub cannot activate ``bar``.
    assert stale["requests"] == ["state_snapshot"]
    assert stale["active"] == "cdc"
    assert stale["cross"] == {"model": "foo", "ip": "bar.u_x"}
    assert len(stale["notes"]) == 1 and stale["notes"][0][1] == "warn"
    assert "(cdc)" in stale["notes"][0][0]
    assert "view → foo" in stale["notes"][0][0]

    # 2. The suppressed case still reconfirms — and stays quiet.
    assert right["requests"] == ["state_snapshot"]
    assert right["cross"] is None and right["notes"] == []

    # 3. …and a stale cache cannot invent a warning either.
    assert exonerated["requests"] == ["state_snapshot"]
    assert exonerated["active"] == "foo"
    assert exonerated["cross"] is None and exonerated["notes"] == []

    # 4. The ordinary cross-model case is unchanged.
    assert foreign["cross"] == {"model": "foo", "ip": "bar.u_x"}
    assert len(foreign["notes"]) == 1

    # 5. No active model to be outside of: nothing to warn about.
    assert none["requests"] == ["state_snapshot"]
    assert none["cross"] is None and none["notes"] == []

    # 6. An error envelope disarms rather than accusing on a guess.
    assert unconfirmable["cross"] is None and unconfirmable["notes"] == []
    assert unconfirmable["active"] == "foo"  # the cache is left alone

    # 7. The one synchronous short-circuit, and the only silent one.
    assert unknown["requests"] == []
    assert unknown["cross"] is None and unknown["notes"] == []


def test_the_module_branch_resolves_through_the_active_model_preference():
    """``instancePathFor`` collects every instance and ranks them against the active
    model's roots; asserted on the source since it closes over the DOM.
    """

    js = _page_js()
    branch = js.split("if (n.type === 'module') {")[1].split("\n    }")[0]
    assert "if (l.type !== 'instance_of') { return; }" in branch
    assert "var p = instancePathFor(state.byId[l.source]);" in branch
    # Every candidate is collected and the choice is made once.
    assert "if (p) { paths.push(p); }" in branch
    assert "return shallowestInstancePath(paths, currentModelRoots());" in branch
    # The roots are the active model's, over the payload on screen.
    roots = js.split("function currentModelRoots() {")[1].split("\n  }")[0]
    assert "activeModelRoots(state.links, activeModel, state.nodes)" in roots
    assert "modelRootsCache.model !== activeModel" in roots
    assert "modelRootsCache.links !== state.links" in roots


def test_the_cached_active_model_follows_the_schematic():
    """The cached model is kept current from ``view_changed`` broadcasts and the
    ``state_snapshot`` requested at welcome.
    """

    js = _page_js()
    assert "case 'view_changed':" in js
    changed = js.split("case 'view_changed':")[1].split("break;")[0]
    assert "activeModel = env.payload.model;" in changed
    # The welcome-time read and the cross-model reconfirm are both present.
    assert "function refreshActiveModel() {" in js
    assert "activeModel = reply.payload.active_model;" in js


def test_the_sync_toggle_advertises_the_module_fallback():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    label = [line for line in body.splitlines() if 'id="opt-select"' in line]
    assert label, "the sync schematic checkbox moved"
    tooltip = body.split('<input type="checkbox" id="opt-select"')[0].split(
        '<label class="chk" title="'
    )[-1]
    assert "highlight all instances of their module" in tooltip


def _cov_target_js() -> str:
    """``covTargetFor`` builds on ``moduleNameFor``, so its block is sliced on top of
    the one defining it.
    """

    return _marked_js("module-name") + _marked_js("cov-target")


def test_a_graph_node_maps_onto_a_coverage_target():
    """Each ``cov_focus.target`` branch (``test:``, ``module:``, ``file:``) names
    something the cov pane's ``applyFocus`` resolves.
    """

    out = _node_eval(
        _cov_target_js()
        + """
        // A model's module comes off its `maps_to` stitch (moduleNameFor),
        // so the cov pane lands on the same module the schematic does —
        // including when a models.yaml `top:` renamed it.
        var links = [
          { type: 'maps_to', source: 'model:design/common/models.yaml#ip_async_fifo',
            target: 'module:ip_async_fifo' },
          { type: 'maps_to', source: 'model:design/vendor/models.yaml#pp_axi',
            target: 'module:axi_xbar' }
        ];
        function outOf(n) {
          return links.filter(function (l) { return n && l.source === n.id; });
        }
        var nodes = [
          // A test: the run's per-test attribution, qualified by suite
          // the way the schema's own example spells it.
          { id: 'test:verif/fifo#smoke', type: 'test', label: 'smoke',
            file: 'verif/fifo/tests.yaml', line: 4 },
          { id: 'model:design/common/models.yaml#ip_async_fifo', type: 'model' },
          // `top: axi_xbar`: the cov target follows the stitch, not the name.
          { id: 'model:design/vendor/models.yaml#pp_axi', type: 'model' },
          // `graph: false`: no stitch — and it carries a `file` the way
          // every served config-tier model node does (its models.yaml).
          // The file fallback must not fire on it: a YAML file is not a
          // coverage coordinate, so the button would read as live and
          // focus a file with no coverage.
          { id: 'model:design/vendor/models.yaml#apb_intf', type: 'model',
            file: 'design/vendor/models.yaml', line: 7 },
          // A module node has a `file` too — the module is the better
          // answer, so it must win.
          { id: 'module:fifo', type: 'module',
            file: 'design/fifo/src/fifo.sv', line: 3 },
          // A spec coverage item: its block read as a module, with the
          // cover column up and the item id as the point name.
          { id: 'covitem:fifo#REQ-1', type: 'coverage_item', label: 'REQ-1',
            block: 'fifo', file: 'spec/fifo/spec.yaml', line: 9 },
          // Anything else with a file: the file, at its line. Paths are
          // project-root-relative on both sides, so they cross verbatim.
          { id: 'inst:fifo/fifo.u_wr', type: 'instance',
            file: 'design/fifo/src/fifo.sv', line: 9 },
          { id: 'tb:verif/fifo#tb_fifo', type: 'testbench',
            file: 'verif/fifo/tb_fifo.sv' },
          // No test, no module, no file — nothing to point cov at.
          { id: 'suite:verif/fifo', type: 'suite' },
          { id: 'test:', type: 'test' },
          { id: 'covitem:fifo#REQ-2', type: 'coverage_item', label: 'REQ-2' }
        ];
        console.log(JSON.stringify(nodes.map(function (n) {
          return covTargetFor(n, outOf(n));
        })));
        console.log(JSON.stringify([covTargetFor(null, []),
                                    covTargetFor(undefined, [])]));
        """
    )
    mapped, nullish = out.strip().splitlines()
    assert json.loads(mapped) == [
        {"target": "test:verif/fifo#smoke"},
        {"target": "module:ip_async_fifo"},
        # The top the model roots at, not its name.
        {"target": "module:axi_xbar"},
        # An opted-out model has no ``maps_to`` and so no coverage target.
        None,
        {"target": "module:fifo"},
        {"target": "module:fifo", "metric": "cover", "item": "REQ-1"},
        {"target": "file:design/fifo/src/fifo.sv", "line": 9},
        # No line on the node means no line on the wire; 0 is not legal.
        {"target": "file:verif/fifo/tb_fifo.sv"},
        None,
        None,
        # A coverage item with no block names no module.
        None,
    ]
    assert json.loads(nullish) == [None, None]


def test_the_inspector_offers_send_and_open_for_every_sibling_app():
    """The action row sits above the identity so a node with many edges cannot push it
    out of sight.
    """

    js = _page_js()
    assert "els.inspector.appendChild(actionsEl);" in js
    inspector = js.split("function renderInspector(n) {")[1]
    assert inspector.index("actionsEl = renderActions(n);") < inspector.index(
        "row(dl, 'id', n.id);"
    )
    apps = js.split("var APPS = [")[1].split("\n  ];")[0]
    # The vocabulary each app is addressed in, and its route (as in the header
    # switcher).
    assert "origin: 'view', prose: 'the schematic'," in apps
    assert "origin: 'cov', prose: 'the coverage pane'," in apps
    assert "targetFor: viewTargetFor," in apps
    assert "send: function (t) { return emit(t.type, t.payload); }," in apps
    assert "targetFor: covTargetFor," in apps
    assert "send: function (t) { return emit('cov_focus', t); }," in apps
    # One send control per app, from the one target derivation.
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "var t = app.targetFor(n, state.out[n.id]);" in row
    assert "'send → ' + originLabel(app.origin)," in row
    assert "function () { sendTo(app, n); }" in row
    assert "'open ' + " not in row
    assert "window.open(" not in row
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    for route in ("/sch", "/cov", "/phy"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body


def test_send_ignores_the_sync_checkbox():
    """The checkbox governs only what a click broadcasts; an explicit ``send → sch`` is
    not gated on it.
    """

    js = _page_js()
    send = js.split("function sendTo(app, n) {")[1].split("\n  }")[0]
    assert "els.optSelect" not in send
    assert "var t = app.targetFor(n, state.out[n.id]);" in send
    assert (
        "if (!app.send(t)) { note('hub not connected', 'error'); return false; }"
        in (send)
    )
    # The same "nothing to send" wording as the click path.
    assert "if (!t) { note(app.why, 'warn'); return false; }" in send


def test_a_send_is_dark_when_its_app_is_not_connected():
    """``send`` needs an open tab, so the tooltip points to opening the app when there
    is none.
    """

    js = _page_js()
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "var live = hasPeer(app.origin);" in row
    assert "!t || !live," in row
    assert "app.prose + ' is not connected — open it from the header links'" in row
    # The peer list is kept and the row repaints when it changes.
    assert "function hasPeer(origin) { return peers.indexOf(origin) >= 0; }" in js
    assert "peers = next;" in js
    assert "if (changed) { refreshActions(); }" in js


def test_the_action_row_has_no_open_buttons():
    """The row has only send controls; open links live in the header switcher, and
    ``_replay_cached_state`` lands a late-opened tab on the current focus.
    """

    js = _page_js()
    assert "function openWith(" not in js
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "window.open(" not in row
    # The header switcher still carries the open links.
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    for route in ("/sch", "/cov", "/phy"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body


def test_the_tooltips_own_up_to_the_broadcast():
    """A focus event is a broadcast: an instance path aimed at the schematic also moves
    the coverage pane.
    """

    js = _page_js()
    apps = js.split("var APPS = [")[1].split("\n  ];")[0]
    assert "This is a hub broadcast" in apps
    assert "cov_focus is read by the coverage pane only." in apps
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert row.count("app.overlap") == 1


# Registration: one client per origin, a polite hello first, one takeover retry on
# refusal, and a way back after being superseded. Mirrors the schematic SPA's flow.


def test_the_first_hello_is_polite():
    """A hello is polite (no ``takeover``) by default, so a second tab does not start an
    eviction war.
    """

    out = _node_eval(
        _marked_js("hello-payload")
        + """
        console.log(JSON.stringify(
          [helloPayload(false), helloPayload(true), helloPayload(undefined)]));
        """
    )
    polite, takeover, unset = json.loads(out)
    assert polite == {
        "client": "graph",
        "version": "1.0.0",
        "capabilities": ["graph_focus"],
    }
    # Omitted, not ``false``: the wire carries only what the tab asks for.
    assert "takeover" not in polite
    assert unset == polite
    assert takeover["takeover"] is True

    js = _page_js()
    # Every hello comes from one helper, flagged by the state a refusal sets.
    assert "payload: helloPayload(pendingTakeover)" in js
    assert "ws.addEventListener('open', sendHello);" in js
    assert "var pendingTakeover = false;" in js
    assert "takeover: true" not in js


def test_an_occupied_slot_is_retried_once_with_takeover():
    """A refusal is answered with exactly one takeover hello."""

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'not_connected' && !pendingTakeover &&" in handler
    assert "/already registered/i.test(payload.message || '')" in handler
    assert "pendingTakeover = true;" in handler
    assert "sendHello();" in handler
    # Cleared on welcome, so a later reconnect starts polite again.
    welcome = js.split("case 'welcome':")[1].split("break;")[0]
    assert "pendingTakeover = false;" in welcome
    # A registration error the handler dealt with stays out of the message area.
    assert (
        "if (env.kind === 'error' && env.payload && !handleHubError(env.payload)) {"
        in js
    )


def test_superseded_stops_reconnecting_and_offers_the_slot_back():
    """Being superseded by a newer tab is not retried; the strip says so and offers the
    way back.
    """

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'superseded'" in handler
    assert "superseded = true;" in handler
    assert "showSuperseded();" in handler
    assert "var superseded = false;" in js
    # Disarmed at the timer and at the close that follows the eviction, which would
    # otherwise repaint the strip.
    sched = js.split("function scheduleReconnect() {")[1].split("\n  }")[0]
    assert "if (superseded) { return; }" in sched
    close = js.split("ws.addEventListener('close', function () {")[1].split(
        "\n    });"
    )[0]
    assert close.index("if (superseded) { return; }") < close.index(
        "scheduleReconnect();"
    )
    # A distinct strip state: offline dot, its own wording, clickable.
    show = js.split("function showSuperseded() {")[1].split("\n  }")[0]
    assert "els.wsDot.className = 'dot offline';" in show
    assert "els.wsStatus.textContent = SUPERSEDED_TEXT;" in show
    assert "els.wsStatus.className = 'take-back';" in show
    assert "els.wsStatus.setAttribute('role', 'button');" in show
    assert "another graph tab took this connection — click to take back" in js
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert ".take-back {" in body
    assert "cursor: pointer;" in body.split(".take-back {")[1].split("}")[0]


def test_taking_the_slot_back_hellos_with_takeover():
    """Reclaiming the slot sends a takeover hello; a polite one would be refused."""

    js = _page_js()
    back = js.split("function takeBack() {")[1].split("\n  }")[0]
    assert "if (!superseded) { return; }" in back
    assert "superseded = false;" in back
    assert "pendingTakeover = true;" in back
    # Backoff starts over: this is a fresh, deliberate connection.
    assert "retryMs = 500;" in back
    assert "connect();" in back
    # The status word is the control while superseded; ``takeBack`` no-ops otherwise, so
    # the listener is bound once.
    assert "els.wsStatus.addEventListener('click', takeBack);" in js


def test_an_ordinary_drop_still_reconnects():
    """A hub restart or network drop is not treated as a supersede."""

    js = _page_js()
    close = js.split("ws.addEventListener('close', function () {")[1].split(
        "\n    });"
    )[0]
    assert "els.wsDot.className = 'dot offline';" in close
    assert "els.wsStatus.textContent = 'offline';" in close
    assert "els.wsStatus.title = 'lost /ws — retrying';" in close
    assert "scheduleReconnect();" in close
    sched = js.split("function scheduleReconnect() {")[1].split("\n  }")[0]
    assert "setTimeout(connect, retryMs);" in sched
    assert "retryMs = Math.min(retryMs * 2, 10000);" in sched


def test_page_javascript_parses(tmp_path: Path):
    """The inline script parses; a syntax error blanks the tab silently."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the dev machine
        pytest.skip("node not installed")
    script = tmp_path / "graph_page.js"
    script.write_text(_page_js(), encoding="utf-8")
    done = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


# Same cases as tests/test_hub_cov_page.py and the SPA's buildInfo.js, word for word, so
# one drifting turns one suite red.


def test_a_dev_build_is_labelled_with_its_git_sha():
    """The label is the ``g``-prefixed run of the local segment (the git SHA); the build
    date does not reach it.
    """

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel('6.26.2.dev13+g3f5b890e3.d20260806'),
          versionLabel('6.26.2.dev13+g3f5b890e3'),
          versionLabel('6.26.2.dev1+g0abcdef12.d20260101.dirty'),
          versionLabel('6.26.2.dev13+d20260806.g3f5b890e3')
        ]));
        """
    )
    assert json.loads(out) == [
        "6.26.2.dev13 @ 3f5b890e3",
        "6.26.2.dev13 @ 3f5b890e3",
        "6.26.2.dev1 @ 0abcdef12",
        # The run is found wherever it sits in the local segment.
        "6.26.2.dev13 @ 3f5b890e3",
    ]


def test_a_release_is_labelled_by_its_version_alone():
    """A tagged build has no local segment and shows no ``@``."""

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify(
          ['6.26.2', '6.26.2.dev13', '0.0.0'].map(versionLabel)));
        """
    )
    assert json.loads(out) == ["6.26.2", "6.26.2.dev13", "0.0.0"]


def test_a_local_segment_without_a_sha_still_labels_the_version():
    """A version with no SHA shows the base without ``@``."""

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel('1.0+local'),
          versionLabel('1.0+d20260806'),
          versionLabel('1.0+gitlab'),
          versionLabel('1.0+'),
          versionLabel('1.0+gabc')
        ]));
        """
    )
    assert json.loads(out) == [
        "1.0",
        "1.0",
        # `gitlab` starts with a g but `itlab` is not hex — no SHA here.
        "1.0",
        "1.0",
        # Fewer than 4 hex digits is not a SHA; pinned in lockstep with the SPA copy.
        "1.0",
    ]


def test_no_version_means_no_label_at_all():
    """A welcome without ``server_version`` renders nothing, not ``undefined``."""

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel(''), versionLabel(undefined), versionLabel(null),
          versionLabel('+g3f5b890e3')
        ]));
        """
    )
    # A local-segment-only version names no release, so there is no label.
    assert json.loads(out) == [None, None, None, None]


def test_the_footer_carries_the_version_and_every_welcome_rewrites_it():
    """The label sits after the peers span and is re-read on every welcome."""

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<span id="hub-version" class="muted"></span>' in body
    # After the peers span, before the flexible gap.
    peers = body.index('<span id="peers"')
    version = body.index('<span id="hub-version"')
    assert peers < version < body.index('<span class="grow"></span>', peers)

    js = _page_js()
    assert "setHubVersion(env.payload && env.payload.server_version);" in js
    assert "els.hubVersion.textContent = label ? 'rtl-buddy ' + label : '';" in js
    assert "els.hubVersion.title = full;" in js


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest_asyncio.fixture
async def hub_and_viewer(
    built_graph: Path,
) -> AsyncIterator[tuple[HubServer, ViewerServer]]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())

    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=built_graph,
        hub_server=hub,
    )
    await viewer.start()
    viewer_task = asyncio.create_task(viewer.serve_forever())
    try:
        yield hub, viewer
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (viewer_task, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_http_graph_page_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/graph"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert f"{viewer.hub_host}:{viewer.hub_port}".encode("utf-8") in body
    assert b"rtl-buddy-gph" in body


@pytest.mark.asyncio
async def test_http_graph_json_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    payload = json.loads(body)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["test:verif/fifo#burst"]["results"]["status"] == "FAIL"
    assert payload["graph"]["hub"]["counts"]["with_results"] == 2


@pytest.mark.asyncio
async def test_http_index_advertises_the_graph_url(hub_and_viewer):
    """A hub with a built graph sets ``__RTL_BUDDY_GRAPH_URL__``."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert b"window.__RTL_BUDDY_GRAPH_URL__ = '/graph.json'" in body


def test_index_omits_graph_url_without_a_graph():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:1")
    assert b"__RTL_BUDDY_GRAPH_URL__" not in body


@pytest.mark.asyncio
async def test_http_graph_json_404_without_a_built_graph(tmp_path: Path):
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host, hub_port=hub_port, http_port=0, project_root=tmp_path
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(_http_get, url)
        assert excinfo.value.code == 404
        assert "rb graph build" in json.loads(excinfo.value.read())["error"]

        # The page is still 200; its empty state says to run ``rb graph build``.
        page_status, _h, _b = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{viewer.http_port}/graph"
        )
        assert page_status == 200
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_http_graph_json_400_without_project_root():
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(_http_get, url)
        assert excinfo.value.code == 400
        assert "project_root" in json.loads(excinfo.value.read())["error"]
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


def test_graph_focus_envelope_validates():
    env = Envelope(
        origin=Origin.CLI,
        kind=Kind.EVENT,
        type="graph_focus",
        id=new_id(),
        payload={"node": "test:verif/fifo#smoke"},
    )
    assert decode(encode(env).encode("utf-8")).payload == {
        "node": "test:verif/fifo#smoke"
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"node": ""}, {"node": "module:fifo", "extra": 1}],
)
def test_graph_focus_rejects_malformed_payloads(payload: dict):
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload=payload,
            )
        )


def test_graph_origin_is_its_own_peer_slot():
    """The pane registers as ``graph``, not ``view``: the hub allows one client per
    origin and both apps must be open at once.
    """

    assert Origin.GRAPH.value == "graph"
    env = Envelope(
        origin=Origin.GRAPH,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={"client": "graph", "version": "1.0.0", "capabilities": []},
    )
    assert decode(encode(env).encode("utf-8")).origin is Origin.GRAPH


class _Peer:
    """Minimal TCP peer, same shape as ``test_hub_send_and_snapshot``."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "_Peer":
        r, w = await asyncio.open_connection(host, port)
        return cls(r, w)

    async def send(self, env: Envelope) -> None:
        self.writer.write(encode(env).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def recv(self, *, timeout: float = 2.0) -> Envelope:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return decode(line)

    async def hello(self, origin: Origin) -> Envelope:
        await self.send(
            Envelope(
                origin=origin,
                kind=Kind.REQUEST,
                type="hello",
                id=new_id(),
                payload={
                    "client": origin.value,
                    "version": "0.1",
                    "capabilities": [],
                },
            )
        )
        return await self.recv()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest_asyncio.fixture
async def bare_hub() -> AsyncIterator[HubServer]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    await hub.start()
    task = asyncio.create_task(hub.serve_forever())
    try:
        yield hub
    finally:
        await hub.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_graph_focus_broadcasts_to_the_pane(bare_hub: HubServer):
    pane = await _Peer.connect(bare_hub.host, bare_hub.port)
    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        assert (await pane.hello(Origin.GRAPH)).type == "welcome"
        assert (await driver.hello(Origin.CLI)).type == "welcome"
        # The pane sees `peer_joined` for the CLI first.
        assert (await pane.recv()).type == "peer_joined"

        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload={"node": "module:fifo"},
            )
        )
        env = await pane.recv()
        assert env.type == "graph_focus"
        assert env.origin is Origin.CLI
        assert env.payload == {"node": "module:fifo"}
    finally:
        await pane.close()
        await driver.close()


@pytest.mark.asyncio
async def test_graph_focus_is_replayed_to_a_late_pane(bare_hub: HubServer):
    """``rb hub send graph-focus`` before the tab is open still lands."""

    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        await driver.hello(Origin.CLI)
        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload={"node": "covitem:fifo#FIFO-COV-1"},
            )
        )
        await asyncio.sleep(0.1)
        assert bare_hub.state.graph_focus is not None
        assert bare_hub.state.graph_focus.node == "covitem:fifo#FIFO-COV-1"

        pane = await _Peer.connect(bare_hub.host, bare_hub.port)
        try:
            assert (await pane.hello(Origin.GRAPH)).type == "welcome"
            replayed = await pane.recv()
            assert replayed.type == "graph_focus"
            assert replayed.payload == {"node": "covitem:fifo#FIFO-COV-1"}
        finally:
            await pane.close()
    finally:
        await driver.close()


# Display names differ from wire origins; the ``Origin`` enum keeps ``view``, ``graph``
# and ``cov``.


def test_the_origin_label_map_renames_only_the_display():
    """``view`` and ``graph`` have distinct display names; other origins, including
    unknown ones, pass through unchanged.
    """

    out = _node_eval(
        _marked_js("origin-labels")
        + """
        var origins = ['view', 'graph', 'cov', 'phys', 'wave', 'src', 'cli',
                       'notebook', 'quantum'];
        console.log(JSON.stringify(origins.map(originLabel)));
        console.log(JSON.stringify([originLabel(null), originLabel(undefined),
                                    originLabel('')]));
        console.log(JSON.stringify(originLabel('toString')));
        """
    )
    labelled, nullish, inherited = out.strip().splitlines()
    assert json.loads(labelled) == [
        "sch",
        "gph",
        "cov",
        "phy",
        "wave",
        "src",
        "cli",
        "notebook",
        "quantum",
    ]
    assert json.loads(nullish) == ["", "", ""]
    # `hasOwnProperty`, not `in`: an origin that collides with a name on
    # Object.prototype must still come back as itself.
    assert json.loads(inherited) == "toString"


def test_every_rendered_origin_goes_through_the_map():
    js = _page_js()
    assert "var ORIGIN_LABELS = { view: 'sch', graph: 'gph', phys: 'phy' };" in js
    # the peer strip
    assert "list.map(originLabel).join(', ')" in js
    # the header switcher's sibling links
    assert "originLabel(links[i].getAttribute('data-origin'))" in js
    # the cross-app buttons and the cross-model button
    assert "'send → ' + originLabel(app.origin)," in js
    assert "els.fixView.textContent = originLabel('view') + ' → ' + target.model;" in js


def test_the_header_switcher_links_every_sibling_pane():
    """``/phy`` is in the header switcher beside the other panes, addressed by its wire
    origin.
    """

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    switcher = body.split('<nav class="switcher"')[1].split("</nav>")[0]
    for route, origin in (("/sch", "view"), ("/cov", "cov"), ("/phy", "phys")):
        assert (
            f'<a href="{route}" target="_blank" rel="noopener" data-origin="{origin}"'
            in switcher
        )


def test_the_rename_did_not_leak_into_the_wire():
    """The hub still speaks ``view``, ``graph`` and ``cov`` on the wire and
    ``/view.json`` is the data route; only page routes such as ``/sch`` carry short
    names.
    """

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    assert "origin: 'graph', kind: 'request', type: 'hello'," in js
    assert "origin: 'view'," in js  # the APPS entry addresses the wire value
    assert "origin: 'cov'," in js
    # The page link is the short name...
    assert 'href="/sch"' in body
    assert 'href="/phy"' in body
    assert 'href="/view"' not in body
    # ...and the data route it fetches from is emphatically not.
    assert "fetch('/view.json?model=' + encodeURIComponent(target.model))" in js


_PANE_FILES = ("graph_page.html", "cov_page.html", "landing_page.html")


def _pane_bytes(name: str) -> bytes:
    return (Path(graph_page.__file__).parent / name).read_bytes()


@pytest.mark.parametrize("name", _PANE_FILES)
def test_a_pane_is_a_text_file(name: str):
    """No NUL byte in a pane: it makes ``rg`` and ``grep`` treat the file as binary and
    skip it.
    """

    assert b"\x00" not in _pane_bytes(name)


def test_the_peer_list_is_diffed_identically_in_both_panes():
    """The ``setPeers`` comparison in each pane uses the same separator; asserted on the
    code because the prose comments differ per pane.
    """

    def code(name: str) -> list[str]:
        body = _pane_bytes(name).decode("utf-8")
        block = body.split("function setPeers(list) {")[1].split("\n  }")[0]
        return [
            line for line in block.splitlines() if not line.strip().startswith("//")
        ]

    graph, cov = code("graph_page.html"), code("cov_page.html")
    assert graph == cov, "the two setPeers copies drifted"
    assert "    var changed = next.join('') !== peers.join('');" in graph


# Physical heat: ``module:`` nodes are painted from ``GET /phy.json``. The wiring, the
# pure join rules (run in ``node``) and the token-based ramp are tested; the painting is
# checked manually.
#
# Manual check: with graph and synth artefacts, run ``rb hub start --serve-viewer``,
# open ``/gph`` and tick ``heat``.


def test_the_page_advertises_the_physical_data_route():
    """The phys route is injected like the landing page's, from one presence probe."""

    body = graph_page.render_graph_html(
        hub_addr="127.0.0.1:1", phys_url=phys_page.PHYS_JSON_ROUTE
    ).decode("utf-8")
    assert "window.__RTL_BUDDY_PHY_URL__ = '/phy.json'" in body
    # The run coordinate is the /phy pane's, spelled the same way.
    assert f"'{phys_page.PHYS_DIR_PARAM}=' + encodeURIComponent(dir)" in body


def test_no_manifest_mutes_the_heat_control_with_the_landing_wording():
    """With no phys route injected, the control is muted with the landing card's phys
    note.
    """

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # The page still reads the global, so only the assignment must be absent.
    assert "window.__RTL_BUDDY_PHY_URL__ = " not in body
    assert "run `rb synth` or `rb power` first" in body
    # Muted, not hidden, so something explains the absence of heat.
    assert "if (!PHY_URL) { mutePhysControl(HEAT_ABSENT); }" in body
    assert "els.optHeat.disabled = true;" in body
    # Muting means only "nothing to show"; a refused run switch is handled by
    # ``physLoadStep``.
    assert "function physLoadStep(shown, result) {" in body


def test_the_metric_switcher_mirrors_the_phys_pane():
    """The metrics, their order and the ``phys_focus.metric`` enum agree; payload order
    wins when present.
    """

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    metrics = ", ".join(f"'{metric}'" for metric in phys_page.METRICS)
    assert f"var HEAT_METRICS = [{metrics}];" in body
    assert "if (Array.isArray(hub.metrics) && hub.metrics.length)" in body
    assert 'metric <select id="heat-metric"></select>' in body
    assert '<input type="checkbox" id="opt-heat"> heat' in body
    # The ramp is the shared one: tokens, not a page-local palette.
    for token in ("--heat-h:", "--heat-s:", "--heat-l0:", "--heat-l1:", "--heat-none:"):
        assert token in body, token
    assert (
        "fill: hsl(var(--heat-h), var(--heat-s),\n"
        "        calc(var(--heat-l0) + (var(--heat-l1) - var(--heat-l0)) * var(--f)));"
    ) in body


# The join rules: ``phys-focus`` is evaluated on top of ``heat-join``.


def _heat_js() -> str:
    return _marked_js("heat-join")


def _heat_focus_js() -> str:
    return _marked_js("heat-join") + _marked_js("phys-focus")


# One design three levels deep, exported twice: rooted at the top and through a
# testbench.
_HEAT_LINKS = """
var links = [
  { type: 'instance_of', source: 'inst:blk/blk', target: 'module:blk' },
  { type: 'instance_of', source: 'inst:blk/blk.u_sub', target: 'module:sub' },
  { type: 'instance_of', source: 'inst:blk/blk.u_sub.u_leaf', target: 'module:tiny' },
  { type: 'instance_of', source: 'inst:tb/tb.u_dut', target: 'module:blk' },
  { type: 'instance_of', source: 'inst:tb/tb.u_dut.u_sub', target: 'module:sub' },
  { type: 'instantiates', source: 'module:blk', target: 'module:sub' }
];
"""

# The rows of the tests/test_hub_phys_page.py fixture, so both panes are asserted over
# one model.
_HEAT_PAYLOAD = """
var payload = {
  top: 'blk',
  modules: [
    { module: 'blk', cell_count: 120, area_um2: 480.5 },
    { module: 'sub', cell_count: 40, area_um2: 96.0 },
    { module: 'tiny', cell_count: 2, area_um2: null }
  ],
  instances: [
    { instance_path: 'u_sub/_64_', module: 'sub',
      leakage_uw: 0.079, internal_uw: 2.28, switching_uw: 0.0675, total_uw: 2.42 },
    { instance_path: 'u_sub/u_leaf/_12_', module: 'tiny',
      leakage_uw: 0.001, internal_uw: 0.5, switching_uw: 0.25, total_uw: 0.751 },
    { instance_path: '_7_', module: 'DFF_X1',
      leakage_uw: 0.01, internal_uw: 1.0, switching_uw: null, total_uw: 1.01 }
  ]
};
"""


def test_power_rolls_up_to_the_enclosing_module():
    """A leaf's power belongs to the module whose body instantiates it (own-module, not
    hierarchical); a leaf in the top's body is the top's.
    """

    out = _node_eval(
        _heat_js()
        + _HEAT_LINKS
        + _HEAT_PAYLOAD
        + """
        var join = joinPhys(links, payload);
        console.log(JSON.stringify({
          rooted: join.rooted,
          index: join.index,
          blk: join.modules.blk,
          sub: join.modules.sub,
          tiny: join.modules.tiny
        }));
        """
    )
    got = json.loads(out)
    assert got["rooted"] is True
    # Only the design top's own elaboration answers the rows; the testbench export
    # reaches the same instance another way.
    assert got["index"] == {"": "blk", "u_sub": "sub", "u_sub.u_leaf": "tiny"}
    assert got["sub"]["total"] == 2.42
    assert (got["sub"]["instances"], got["sub"]["leaves"]) == (1, 1)
    assert got["tiny"]["total"] == 0.751
    # The top's own body: one leaf, and nothing from below it.
    assert got["blk"]["total"] == 1.01
    assert (got["blk"]["instances"], got["blk"]["leaves"]) == (1, 1)


def test_the_power_half_is_never_joined_by_module_name():
    """The power ``module`` column is a Liberty cell, so it is not name-joined onto RTL
    modules: ``DFF_X1``, as both a cell and an RTL module, gets its synthesis row and
    no power.
    """

    out = _node_eval(
        _heat_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:blk/blk', target: 'module:blk' },
          { type: 'instance_of', source: 'inst:blk/blk.u_sub', target: 'module:DFF_X1' }
        ];
        var payload = {
          top: 'blk',
          modules: [{ module: 'DFF_X1', cell_count: 9, area_um2: 12.5 }],
          instances: [
            { instance_path: 'u_sub/_1_', module: 'DFF_X1', total_uw: 1 },
            { instance_path: 'u_sub/_2_', module: 'DFF_X1', total_uw: 2 },
            { instance_path: '_3_', module: 'DFF_X1', total_uw: 4 }
          ]
        };
        var join = joinPhys(links, payload);
        console.log(JSON.stringify(join.modules));
        """
    )
    got = json.loads(out)
    # The RTL module of that name gets its own synthesis row plus only the power of the
    # leaves inside it (1 + 2).
    assert got["DFF_X1"]["cells"] == 9
    assert got["DFF_X1"]["area"] == 12.5
    assert got["DFF_X1"]["total"] == 3
    # Two leaf rows, one instantiation.
    assert (got["DFF_X1"]["instances"], got["DFF_X1"]["leaves"]) == (1, 2)
    assert got["blk"]["total"] == 4
    assert got["blk"]["cells"] is None


def test_rows_under_a_top_the_graph_does_not_carry_are_not_attributed():
    """With nothing rooted at the model's top (a wrapper the graph was not built for, or
    ``--model``), leaf power is unattributable and the pane says so.
    """

    out = _node_eval(
        _heat_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:tb/tb.u_dut', target: 'module:blk' }
        ];
        var payload = {
          top: 'blk', modules: [{ module: 'blk', cell_count: 7, area_um2: null }],
          instances: [{ instance_path: 'u_sub/_1_', module: 'sub', total_uw: 3 }]
        };
        var join = joinPhys(links, payload);
        console.log(JSON.stringify({
          rooted: join.rooted, index: join.index,
          unattributed: join.unattributed, attributed: join.attributed,
          modules: join.modules
        }));
        """
    )
    got = json.loads(out)
    assert got["rooted"] is False
    assert got["index"] == {}
    assert got["unattributed"] == 1 and got["attributed"] == 0
    # The synthesis half still joins — it needs no hierarchy at all.
    assert got["modules"]["blk"]["cells"] == 7
    assert got["modules"]["blk"]["total"] is None


def test_an_unmeasured_metric_is_null_and_not_zero():
    """``null`` means "not measured": no ``area_um2`` for an unmapped run, and
    ``dynamic`` is null only when neither half was measured.
    """

    out = _node_eval(
        _heat_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:blk/blk', target: 'module:blk' }
        ];
        var payload = {
          top: 'blk', modules: [{ module: 'blk', cell_count: 3, area_um2: null }],
          instances: [{ instance_path: '_1_', module: 'X', internal_uw: 0.5 }]
        };
        var join = joinPhys(links, payload);
        var found = join.modules.blk;
        console.log(JSON.stringify(['cells', 'area', 'leakage', 'dynamic', 'total']
          .map(function (m) { return heatValueOf(found, m); })));
        console.log(JSON.stringify([
          heatMaxOf(['blk'], join, 'area'),
          heatMaxOf(['blk'], join, 'cells'),
          heatMaxOf(['nowhere'], join, 'cells'),
          initialHeatMetric(payload),
          initialHeatMetric({ modules: payload.modules })
        ]));
        """
    )
    values, scales = out.strip().splitlines()
    # cells measured, area not, leakage not, dynamic from one half only,
    # total not.
    assert json.loads(values) == [3, None, None, 0.5, None]
    assert json.loads(scales) == [
        # A ramp over a column nobody measured has no top end, so every
        # node paints as "not measured" instead of as the maximum.
        None,
        3,
        # A module the graph carries and the model does not: no value,
        # and it must not become a zero that scales the ramp.
        None,
        # Total power is what a reader ticking this is chasing…
        "total",
        # …unless the run produced no power half, which would open the
        # overlay on an empty column and read as broken.
        "cells",
    ]


def test_a_leaf_that_is_also_an_rtl_instance_pays_its_parent():
    """Attribution is to a proper ancestor, never the row's own path, so a macro's power
    counts in its parent's body.
    """

    out = _node_eval(
        _heat_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:blk/blk', target: 'module:blk' },
          { type: 'instance_of', source: 'inst:blk/blk.u_ram', target: 'module:sram' }
        ];
        var payload = {
          top: 'blk', modules: [],
          instances: [{ instance_path: 'u_ram', module: 'SRAM_1024', total_uw: 9 }]
        };
        var join = joinPhys(links, payload);
        console.log(JSON.stringify(join.modules));
        """
    )
    got = json.loads(out)
    assert got["blk"]["total"] == 9
    assert "sram" not in got


def test_a_model_row_is_levelled_before_it_is_resolved():
    """OpenSTA's ``/`` and the graph's ``.`` are normalised to levels, except inside an
    escaped identifier.
    """

    out = _node_eval(
        _heat_js()
        + r"""
        console.log(JSON.stringify([
          levelPath('u_sub/u_leaf/_12_'),
          levelPath('u_sub.u_leaf._12_'),
          levelPath('\\gen[0].u_x /_64_'),
          levelPath(null)
        ]));
        console.log(JSON.stringify([
          instanceCoord('inst:blk/blk.u_sub'),
          instanceCoord('inst:blk/blk'),
          instanceCoord('inst:tb/tb.u_dut@verif/fifo'),
          instanceCoord('module:blk')
        ]));
        """
    )
    paths, coords = out.strip().splitlines()
    assert json.loads(paths) == [
        "u_sub.u_leaf._12_",
        "u_sub.u_leaf._12_",
        # One level, name intact, and the terminating space dropped so
        # the two spellings compare equal.
        "\\gen[0].u_x._64_",
        "",
    ]
    assert json.loads(coords) == [
        {"root": "blk", "path": "u_sub"},
        # The root scope's own node: the empty path, which is a real
        # answer (a leaf in the top's body is the top module's).
        {"root": "blk", "path": ""},
        # The suite qualifier disambiguates an id; it is not part of an
        # instance's name.
        {"root": "tb", "path": "u_dut"},
        None,
    ]


def test_an_inbound_phys_focus_lands_on_the_node_that_owns_it():
    """``phys_focus`` targets are modules by name or instances by path; a leaf resolves
    to its owning module, and an instance node wins when the path names one.
    """

    out = _node_eval(
        _heat_focus_js()
        + _HEAT_LINKS
        + _HEAT_PAYLOAD
        + """
        var join = joinPhys(links, payload);
        var byId = {
          'module:blk': 1, 'module:sub': 1, 'module:tiny': 1,
          'inst:blk/blk': 1, 'inst:blk/blk.u_sub': 1
        };
        var targets = [
          'module:sub', 'module:nothing',
          'instance:u_sub/_64_', 'u_sub/_64_',
          'instance:u_sub', 'blk.u_sub/_64_', '_7_', ''
        ];
        console.log(JSON.stringify(targets.map(function (t) {
          return physFocusNodes(t, byId, join.index, join.top);
        })));
        """
    )
    assert json.loads(out) == [
        ["module:sub"],
        # A soft miss, like `graph_focus`: the pane reports it and keeps
        # its focus.
        [],
        # The leaf's owner, both prefixed and bare (an unprefixed target
        # is an instance path).
        ["module:sub"],
        ["module:sub"],
        # A path that IS an instance node selects the node itself.
        ["inst:blk/blk.u_sub"],
        # Rooted at the top, which is how the /phy pane broadcasts a
        # path: the reading that names a real enclosing instance wins
        # over the one that falls through to the top.
        ["module:sub"],
        # A leaf in the top's own body.
        ["module:blk"],
        [],
    ]


def test_the_focus_handler_emits_nothing_and_keeps_the_metric_hint():
    """Inbound focus adds no wire type; the metric hint applies even when the target
    misses.
    """

    js = _page_js()
    assert "case 'phys_focus':" in js
    handler = js.split("function applyPhysFocus(payload, deferred) {")[1].split(
        "\n  }"
    )[0]
    assert "if (metric) { setHeatMetric(String(metric)); }" in handler
    assert handler.index("setHeatMetric") < handler.index("physFocusNodes")
    assert "focusById(ids[0], { emit: false, announce: false });" in handler
    assert "emit(" not in handler
    # The overlay is a fill and a switcher; it adds nothing to what the
    # pane says on the wire.
    assert js.count("emit('selection_changed'") == 1


def test_the_roll_up_counts_instantiations_and_rows_apart():
    """Cells and area are the module definition's, counted once; power sums over every
    instantiation. Both ride on the entry.
    """

    out = _node_eval(
        _heat_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:blk/blk', target: 'module:blk' },
          { type: 'instance_of', source: 'inst:blk/blk.u_a', target: 'module:fifo' },
          { type: 'instance_of', source: 'inst:blk/blk.u_b', target: 'module:fifo' },
          { type: 'instance_of', source: 'inst:blk/blk.u_c', target: 'module:fifo' }
        ];
        var payload = {
          top: 'blk',
          modules: [{ module: 'fifo', cell_count: 40, area_um2: 96 }],
          instances: [
            { instance_path: 'u_a/_1_', module: 'DFF_X1', total_uw: 1 },
            { instance_path: 'u_a/_2_', module: 'DFF_X1', total_uw: 1 },
            { instance_path: 'u_b/_1_', module: 'DFF_X1', total_uw: 1 },
            { instance_path: 'u_c/_1_', module: 'DFF_X1', total_uw: 1 }
          ]
        };
        var join = joinPhys(links, payload);
        console.log(JSON.stringify(join.modules.fifo));
        """
    )
    got = json.loads(out)
    # One definition's cells and area…
    assert (got["cells"], got["area"]) == (40, 96)
    # …and three instantiations' power, off four leaf rows.
    assert got["total"] == 4
    assert (got["instances"], got["leaves"]) == (3, 4)


def test_a_dotted_suite_qualifier_does_not_become_a_path_level():
    """The suite qualifier is read from the last ``@`` of the whole id, so a dotted
    suite path such as ``verif/fifo.v2`` is one qualifier, not a path level.
    """

    out = _node_eval(
        _heat_js()
        + """
        console.log(JSON.stringify([
          instanceCoord('inst:blk/blk.u_sub@verif/fifo.v2'),
          instanceCoord('inst:blk/blk@verif/fifo.v2'),
          instanceCoord('inst:blk/blk.u_sub@verif/fifo'),
          instanceCoord('inst:blk/blk.u_sub')
        ]));
        console.log(JSON.stringify([
          stripQualifier('module:sub@verif/fifo.v2'),
          stripQualifier('module:sub'),
          stripQualifier(null)
        ]));
        """
    )
    coords, stripped = out.strip().splitlines()
    assert json.loads(coords) == [
        {"root": "blk", "path": "u_sub"},
        {"root": "blk", "path": ""},
        {"root": "blk", "path": "u_sub"},
        {"root": "blk", "path": "u_sub"},
    ]
    assert json.loads(stripped) == ["module:sub", "module:sub", ""]


def test_a_suite_qualified_module_is_one_module_everywhere():
    """A qualifier disambiguates an id, not a module: the join reads exports under the
    bare name and a focus highlights every export.
    """

    out = _node_eval(
        _heat_focus_js()
        + """
        var links = [
          { type: 'instance_of', source: 'inst:blk/blk@verif/a',
            target: 'module:blk@verif/a' },
          { type: 'instance_of', source: 'inst:blk/blk.u_sub@verif/a',
            target: 'module:sub@verif/a' }
        ];
        var payload = {
          top: 'blk',
          modules: [{ module: 'sub', cell_count: 40, area_um2: 96 }],
          instances: [{ instance_path: 'u_sub/_1_', module: 'DFF_X1', total_uw: 7 }]
        };
        var join = joinPhys(links, payload);
        console.log(JSON.stringify({ index: join.index, sub: join.modules.sub }));
        var byId = {
          'module:sub@verif/a': 1, 'module:sub@verif/b': 1, 'module:blk@verif/a': 1
        };
        console.log(JSON.stringify([
          physFocusNodes('module:sub', byId, join.index, join.top),
          physFocusNodes('u_sub/_1_', byId, join.index, join.top)
        ]));
        """
    )
    joined, focused = out.strip().splitlines()
    got = json.loads(joined)
    # The qualifier is off both halves of the identity, so the row and
    # the roll-up meet under the name Yosys counted.
    assert got["index"] == {"": "blk", "u_sub": "sub"}
    assert (got["sub"]["cells"], got["sub"]["total"]) == (40, 7)
    # Every export of the module, for a module target and for the
    # owning-module fallback of an instance target alike.
    assert json.loads(focused) == [
        ["module:sub@verif/a", "module:sub@verif/b"],
        ["module:sub@verif/a", "module:sub@verif/b"],
    ]


def _heat_load_js() -> str:
    return _marked_js("heat-load-step")


def test_a_refused_run_switch_keeps_the_model_on_screen():
    """A refused ``?dir=`` switch keeps the payload, its run and the overlay; the run is
    committed only when a body arrives.
    """

    out = _node_eval(
        _heat_load_js()
        + """
        // A → refused B → the reader ticks the overlay again.
        var shown = { phys: null, dir: null };
        var trace = [];
        var step = function (result) {
          var next = physLoadStep(shown, result);
          shown = { phys: next.phys, dir: next.dir };
          trace.push({
            phys: next.phys, dir: next.dir, refetch: next.refetch,
            level: next.level, message: next.message
          });
        };
        step({ ok: true, requested: 'art/a', body: { top: 'A' } });
        step({ ok: false, requested: 'art/b', body: null,
               message: 'phys: no phys-manifest.json in art/b' });
        console.log(JSON.stringify(trace));
        """
    )
    first, refused = json.loads(out)
    assert first["phys"] == {"top": "A"}
    assert first["dir"] == "art/a"
    # The refusal: same payload, same run, nothing to refetch, and the
    # reason in the status line as an error.
    assert refused["phys"] == {"top": "A"}
    assert refused["dir"] == "art/a"
    assert refused["refetch"] is False
    assert refused["level"] == "error"
    assert "art/b" in refused["message"]

    # The applying half, which is DOM-coupled: the run is committed
    # inside the success branch only, the picker is re-rendered onto the
    # run still shown, and nothing is muted.
    js = _page_js()
    apply = js.split("function applyPhysLoad(generation, result) {")[1].split("\n  }")[
        0
    ]
    assert "state.phys = next.phys;" in apply
    assert "if (result.ok) { ingestPhys(next.phys); return; }" in apply
    assert "renderHeatControls();" in apply
    # A stale response from a superseded fetch commits nothing.
    assert "if (generation !== physGeneration) { return; }" in apply
    # And re-ticking the control re-paints rather than re-fetching,
    # because the payload never left.
    assert "if (els.optHeat.checked && !state.phys) { loadPhys(); return; }" in js
    # The picker hands its value to the FETCH and never to the state: a
    # switch that is refused leaves the selected run where it was.
    assert "loadPhys(els.heatRun.value || null);" in js
    assert "state.physDir = els.heatRun.value" not in js


def test_a_bad_dir_on_the_first_load_falls_back_to_the_newest_run():
    """A missing ``?dir=`` run falls back to the newest and reports the requested run as
    not found; with no run requested the control is muted.
    """

    out = _node_eval(
        _heat_load_js()
        + """
        console.log(JSON.stringify([
          physLoadStep({ phys: null, dir: null },
            { ok: false, requested: 'art/gone', body: null,
              message: 'phys: no phys-manifest.json in art/gone' }),
          physLoadStep({ phys: null, dir: null },
            { ok: false, requested: null, body: null,
              message: 'phys: no physical artefacts; run `rb synth`' })
        ]));
        """
    )
    pinned, nothing = json.loads(out)
    assert pinned["refetch"] is True
    assert pinned["phys"] is None and pinned["dir"] is None
    assert "art/gone" in pinned["message"] and "newest" in pinned["message"]
    assert pinned["level"] == "warn"
    # Nothing requested, nothing held: the muted control, with the
    # server's own wording.
    assert nothing["refetch"] is False
    assert nothing["phys"] is None
    assert "rb synth" in nothing["message"]

    js = _page_js()
    apply = js.split("function applyPhysLoad(generation, result) {")[1].split("\n  }")[
        0
    ]
    assert "physPendingNote = next.message;" in apply
    assert "loadPhys(null);" in apply
    assert "mutePhys(next.message);" in apply
    # The fallback's warning is said by the ingest that follows it: a
    # note written before that fetch is overwritten by its own
    # "loading…" line.
    assert (
        "if (physPendingNote) { note(physPendingNote, 'warn'); physPendingNote = null; }"
        in js
    )


def test_a_focus_that_beats_the_model_turns_the_overlay_on():
    """An inbound ``phys_focus`` ticks the overlay, fetches the model if unread and
    applies the focus when the body lands.

    The second pass never defers again, and a held focus is drained even on failure.
    """

    js = _page_js()
    handler = js.split("function applyPhysFocus(payload, deferred) {")[1].split(
        "\n  }"
    )[0]
    assert "if (!deferred && PHY_URL && !state.phys) {" in handler
    assert "physPendingFocus = payload;" in handler
    assert "els.optHeat.checked = true;" in handler
    assert "loadPhys();" in handler
    # Drained on the ingest that answers it, and on the failure that
    # does not.
    drain = js.split("function drainPhysFocus() {")[1].split("\n  }")[0]
    assert "applyPhysFocus(held, true);" in drain
    assert "physPendingFocus = null;" in drain
    ingest = js.split("function ingestPhys(payload) {")[1].split("\n  }")[0]
    assert "drainPhysFocus();" in ingest
    mute = js.split("function mutePhys(message) {")[1].split("\n  }")[0]
    assert "drainPhysFocus();" in mute
    # Several nodes for one module are highlighted together, and any
    # later activation clears the set.
    assert "state.focusIds = ids;" in handler
    activate = js.split("function activate(n, opts) {")[1].split("\n  }")[0]
    assert "state.focusIds = null;" in activate
    classes = js.split("function applyClasses() {")[1].split("\n  }")[0]
    assert "var dim = hidden || !matches(n) || (near && !near[id] && !also);" in classes


def test_the_run_being_shown_is_offered_even_when_the_listing_headed_it_off():
    """A run beyond ``phys_page.RUNS_LIMIT`` gets a listing entry synthesised from the
    payload header at the top, so the selector matches the tints.
    """

    out = _node_eval(
        _marked_js("heat-run-url")
        + """
        var payload = {
          run: 'older', top: 'blk', manifest: 'verif/blk/artefacts/older/m.json',
          artefacts: { phys_dir: 'verif/blk/artefacts/older' },
          power_mode: 'dynamic', power_activity: { label: 'saif csr_smoke' }
        };
        var block = { count: 9, runs: [
          { run: 'newest', top: 'blk', phys_dir: 'verif/blk/artefacts/newest',
            manifest: 'verif/blk/artefacts/newest/m.json', newest: true }
        ] };
        var entries = heatRunEntries(payload, block);
        console.log(JSON.stringify(entries.map(function (e) {
          return [heatRunValue(e), heatRunLabel(e, payload.manifest)];
        })));
        // A listing that already carries the shown run is left alone.
        var listed = heatRunEntries(payload, { count: 1, runs: [
          { run: 'older', phys_dir: 'verif/blk/artefacts/older',
            manifest: payload.manifest }
        ] });
        console.log(JSON.stringify(listed.length));
        """
    )
    entries, listed = out.strip().splitlines()
    assert json.loads(entries) == [
        # The synthesised entry, first, marked as the one being shown…
        [
            "verif/blk/artefacts/older",
            "older · blk · dynamic (saif csr_smoke) [shown]",
        ],
        # …and the newest, which still selects the bare route so a
        # reload goes on following discovery.
        ["", "newest · blk [newest — follows]"],
    ]
    assert json.loads(listed) == 1

    # The hover is rebuilt, not extended: this runs on every render.
    js = _page_js()
    picker = js.split("function renderHeatRuns() {")[1].split("\n  }")[0]
    assert "els.heatRunWrap.title = heatRunTitle;" in picker
    assert picker.index("els.heatRunWrap.title = heatRunTitle;") < picker.index(
        "entries.forEach"
    )
    # A graph reload recomputes the join, so the controls it labels
    # follow it.
    reloaded = js.split("if (state.phys) {")[1].split("\n    }")[0]
    assert "state.heat = joinPhys(state.links, state.phys);" in reloaded
    assert "renderHeatControls();" in reloaded


def _write_phys_run(root: Path) -> Path:
    """One run's physical artefacts written with the real producers, so the fixture
    cannot drift from their shape.
    """

    phys_dir = root / "verif" / "blk" / "artefacts" / "both"
    phys_dir.mkdir(parents=True, exist_ok=True)
    model = build_synth_model(
        top="fifo",
        modules=[{"module": "fifo", "cell_count": 120, "area_um2": 480.5}],
        area_um2=480.5,
        gate_count=120,
        netlist_sha256="0" * 64,
    )
    model_path = write_model(model, phys_dir)
    write_manifest(
        build_manifest(
            project_root=root,
            phys_dir=phys_dir,
            command="synth",
            run="both",
            top="fifo",
            model_path=model_path,
            totals=model["totals"],
            synth={
                "backend": "yosys",
                "run": "both",
                "stats": phys_dir / "synth_stat.json",
                "netlist": phys_dir / "synth_netlist.v",
                "log": phys_dir / "synth.log",
            },
        ),
        phys_dir,
    )
    return phys_dir


@pytest.mark.asyncio
async def test_http_graph_page_advertises_the_model_it_can_paint_with(
    hub_and_viewer, built_graph: Path
):
    """The pane's phys URL keys on the same manifest probe as the landing card."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}{graph_page.GRAPH_PAGE_ROUTE}"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    # The fixture project has a graph and no physical artefacts.
    assert b"window.__RTL_BUDDY_PHY_URL__ = " not in body
    assert "run `rb synth` or `rb power` first".encode("utf-8") in body

    _write_phys_run(built_graph)
    phys_page._presence_cache.clear()  # noqa: SLF001 - a 5s TTL, cleared for the assert
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert b"window.__RTL_BUDDY_PHY_URL__ = '/phy.json'" in body
    # And the route it now points at is live.
    status, _headers, phys_body = await asyncio.to_thread(
        _http_get, f"http://127.0.0.1:{viewer.http_port}{phys_page.PHYS_JSON_ROUTE}"
    )
    assert status == 200
    payload = json.loads(phys_body)
    assert payload["hub"]["metrics"] == list(phys_page.METRICS)
    assert [row["module"] for row in payload["modules"]] == ["fifo"]
