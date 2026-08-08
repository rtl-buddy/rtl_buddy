"""Tests for the hub-served design-knowledge-graph pane (#382).

Six surfaces, in the order a user meets them:

1. ``GET /graph.json`` — ``artefacts/graph/graph.json`` joined with
   ``artefacts/graph/results-overlay.json`` in memory. The join must not
   touch ``graph.json`` on disk: hash stability across regressions is
   the whole reason the overlay is a separate file (#379).
2. Column bucketing — every node lands in exactly one of the eight
   :data:`~rtl_buddy.hub.graph_page.COLUMN_ORDER` columns, computed
   server-side because two of its inputs are graph-wide joins. This is
   also where the ``category`` stamp is pinned as *served only*: writing
   it back to disk would be the churn #379 exists to prevent.
3. ``GET /graph`` — one self-contained HTML document. The offline rule
   is asserted structurally (no external ``src``/``href``, no CDN host),
   because "it worked on my laptop" is exactly the failure mode a hub
   running on an air-gapped build machine hits.
4. ``graph_focus`` — the wire type behind ``rb hub send graph-focus``:
   schema-valid, broadcast to peers, and replayed to a peer that
   connects after the fact.
5. Module-level design-view sync — the fallback for a node that has no
   instance path, which on a project-tier graph is every node. Its pure
   helper is sliced out of the page between markers and exercised in
   ``node``, the same convention ``tests/test_hub_cov_page.py`` uses.
6. The version label in the status strip — one wording of "which build
   am I looking at" shared with the coverage pane and the view SPA, so
   its cases are asserted identically in all three.
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

from rtl_buddy.hub import graph_page, theme
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


# ---------------------------------------------------------------------------
# fixtures — a minimal built graph on disk
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# column bucketing
#
# One graph per rule, rather than one big one: the interesting cases are
# the design-tier nodes that came from a *testbench* elaboration, and the
# only way to state what should happen to them is to name them.
# ---------------------------------------------------------------------------


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


#: Every column, exercised once. The `fpv` testbench hierarchy is
#: synthetic — today only `tests.yaml` suites produce `tb:` nodes with a
#: hierarchy — but the rule is driven by the suite's `flow`, not by which
#: config file it came from, and that is the property worth pinning.
_BUCKET_GRAPH = {
    "directed": True,
    "multigraph": True,
    "graph": {"schema_version": 1, "project_root_rel": "."},
    "nodes": [
        # spec
        _node("spec:fifo", "spec_block", "config"),
        _node("covitem:fifo#F-COV-1", "coverage_item", "config"),
        _node("doc:spec/fifo/README.md", "spec_doc", "config"),
        _node("golden:spec/fifo/fifo_model.py", "golden_model", "config"),
        # design — the DUT hierarchy, plus the model that aliases it
        _node("model:design/fifo/models.yaml#fifo", "model", "config"),
        _node("module:fifo", "module", "design"),
        _node("inst:fifo/fifo.u_wr", "instance", "design"),
        _node("port:fifo.clk", "port", "design", owner="fifo"),
        # sim suite + its SystemVerilog testbench hierarchy
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
        # Not qualified (no other file declares a `tb_top` parameter of
        # this name), so it has to reach its column through its owner.
        _node("param:tb_top.WIDTH", "parameter", "design", owner="tb_top"),
        # synth
        _node("suite:synth/fifo", "suite", "config", flow="synth"),
        _node("test:synth/fifo#generic", "test", "config", flow="synth"),
        # fpv, with a testbench hierarchy of its own
        _node("suite:fpv/fifo", "suite", "config", flow="fpv"),
        _node("test:fpv/fifo#safety", "test", "config", flow="fpv"),
        _node("tb:fpv/fifo#fv_top", "testbench", "config", flow="fpv"),
        _node("module:fifo_fv_top", "module", "design"),
        _node("inst:fifo_fv_top/fifo_fv_top.u_dut", "instance", "design"),
        # cdc, and fpga sharing the synthesis column
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
        # A cocotb testbench tops at the DUT itself; that must not drag
        # the DUT's whole hierarchy into a flow column.
        _link("tb:verif/fifo#tb_cocotb", "module:fifo", "elaborates_as"),
        # A non-simulation run's `top:`. Same target namespace, third
        # verb — and, like today, not a testbench-ownership signal: the
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
    # A model *is* its module under another name, so it sits beside the
    # design it aliases rather than in the suite's flow column.
    assert columns["model:design/fifo/models.yaml#fifo"] == "design"


def test_flow_stamp_picks_the_config_column(bucket_graph: Path):
    columns = _columns(bucket_graph)
    assert columns["suite:verif/fifo"] == "test-config"
    assert columns["test:synth/fifo#generic"] == "syn-config"
    assert columns["test:fpv/fifo#safety"] == "formal-config"
    assert columns["test:lint/cdc#fifo_lint"] == "cdc-config"
    # FPGA implementation is the synthesis flow carried further, and
    # shares its column rather than earning a near-always-empty one.
    assert columns["test:fpga/fifo#a35t"] == "syn-config"


def test_cocotb_wins_over_the_suites_flow(bucket_graph: Path):
    columns = _columns(bucket_graph)
    assert columns["test:verif/fifo#cocotb"] == "test-cocotb"
    assert columns["tb:verif/fifo#tb_cocotb"] == "test-cocotb"
    assert columns["py:verif/fifo/cocotb_fifo.py"] == "test-cocotb"
    # Its suite is still a simulation suite.
    assert columns["suite:verif/fifo"] == "test-config"


def test_dut_hierarchy_stays_in_the_design_column(bucket_graph: Path):
    columns = _columns(bucket_graph)
    # `module:fifo` is pointed at by all three config->design verbs at
    # once — a model's `maps_to`, a cocotb testbench's `elaborates_as`
    # and a synth run's `targets`. The model's claim wins, which is the
    # rule that keeps a DUT out of a flow column.
    assert columns["module:fifo"] == "design"
    assert columns["inst:fifo/fifo.u_wr"] == "design"
    assert columns["port:fifo.clk"] == "design"


def test_testbench_hierarchy_follows_its_suites_flow(bucket_graph: Path):
    """The point of the split: a testbench is not the design.

    Both halves of a `rb graph build` are `tier: design`, so the tier
    cannot say which is which; the suite that owns the elaboration can.
    """

    columns = _columns(bucket_graph)
    assert columns["module:tb_top@verif/fifo"] == "test-config"
    assert columns["inst:tb_top/tb_top.u_dut@verif/fifo"] == "test-config"
    # Reached through its `owner`, whose id had to be suite-qualified.
    assert columns["param:tb_top.WIDTH"] == "test-config"
    # An fpv suite's testbench hierarchy lands in the formal column.
    assert columns["module:fifo_fv_top"] == "formal-config"
    assert columns["inst:fifo_fv_top/fifo_fv_top.u_dut"] == "formal-config"


def test_unplaceable_nodes_land_in_other_rather_than_vanish(bucket_graph: Path):
    columns = _columns(bucket_graph)
    # An unknown flow is still a suite: it keeps the default flow column
    # rather than being exiled, because "sim" is what a suite is.
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


# ---------------------------------------------------------------------------
# build_graph_payload
# ---------------------------------------------------------------------------


def test_payload_joins_overlay_onto_test_nodes(built_graph: Path):
    payload = graph_page.build_graph_payload(built_graph)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["test:verif/fifo#smoke"]["results"]["status"] == "PASS"
    assert by_id["test:verif/fifo#burst"]["results"]["status"] == "FAIL"
    # Non-test nodes get no entry — the overlay is keyed by test node id.
    assert "results" not in by_id["module:fifo"]


def test_payload_never_writes_graph_json_back(built_graph: Path):
    """The join is in-memory; graph.json stays byte-identical.

    This is the #379 invariant restated at the hub: a pane that wrote
    its annotations back would make graph.json churn on every
    regression and break the build fingerprint's no-op re-run.
    """

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
    # Counts are of the served body, so a dangling link target
    # (``module:fifo_wr``, which no tier exported) is a link but not a
    # node — the page materialises it client-side.
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


# ---------------------------------------------------------------------------
# render_graph_html — the offline rule
# ---------------------------------------------------------------------------


def test_page_injects_hub_address():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_GRAPH_URL__ = '/graph.json'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """No CDN, no remote font, no import, no off-machine reference.

    Since #398 the pane links the hub's own token sheet, so "no external
    stylesheet" narrowed to what it always meant: every ``src``/``href``
    that is not a page anchor must be a **same-origin absolute path**,
    served by this same hub process. A hub on a machine with no route
    off localhost still renders the pane completely.
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
    # The only absolute URLs may be the SVG namespace; nothing may point
    # off the machine.
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
    # Light default (#398): the first surface value is the light one.
    assert "--bg:          #f8fafc;" in body
    # ...and the fallback comes BEFORE the link, or it would out-rank the
    # sheet at equal specificity and kill prefers-color-scheme: dark.
    # tests/test_hub_theme.py checks this properly, for every hub page.
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_page_carries_the_pieces_the_issue_asks_for():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # every column, a colour for each, pass/fail badges, the envelopes
    for column in graph_page.COLUMN_ORDER:
        assert f"'{column}'" in body, column
        assert f"--col-{column}:" in body, column
    for token in ("design", "config", "binding", "PASS", "FAIL"):
        assert token in body
    assert "selection_changed" in body
    assert "open_source" in body
    assert "graph_focus" in body
    assert "'graph'" in body  # registers under its own origin


# ---------------------------------------------------------------------------
# module-level design-view sync
# ---------------------------------------------------------------------------


def _page_js() -> str:
    """The page's inline script — the last ``<script>`` in the body."""

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
    """One block of pure helpers, sliced out of the page by its markers.

    Same convention as ``cov_page.html``: nothing between the markers
    may touch the DOM or close over page state, which is exactly what
    evaluating them in bare ``node`` enforces.
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


def test_a_model_node_is_named_after_its_top_module():
    """Project-tier graphs — everything you have before a design tier is
    built — put the design content on ``model:`` nodes, and a model is
    named after the module it wraps. The fragment after ``#`` is that
    name; a model id without one names nothing."""

    out = _node_eval(
        _marked_js("module-name")
        + """
        var ids = [
          'model:design/common/models.yaml#ip_async_fifo',
          'model:design/cdc/models.yaml#ip_cdc_handshake',
          'model:design/common/models.yaml#',
          'model:design/common/models.yaml',
          'model:a/b.yaml#deep#er'
        ];
        console.log(JSON.stringify(ids.map(function (id) {
          return moduleNameFor({ id: id, type: 'model' });
        })));
        """
    )
    assert json.loads(out) == [
        "ip_async_fifo",
        "ip_cdc_handshake",
        # An empty fragment is not a module name.
        None,
        # No fragment at all, likewise.
        None,
        # Everything after the FIRST `#` — a name may not contain one,
        # but splitting on the last would silently truncate a weird id.
        "deep#er",
    ]


def test_a_module_node_falls_back_to_its_own_id():
    """``moduleNameFor`` only sees a ``module:`` node when
    ``instancePathFor`` found no ``instance_of`` link to follow, and
    then the id minus its prefix is the name."""

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
        # Verbatim: both ends of this wire speak the SOURCE vocabulary,
        # so there is no elaboration suffix to strip and stripping one
        # would corrupt a real name. (cov_page.html's `baseModuleName`
        # exists because coverage speaks the simulator's names instead.)
        "axi__lite__W8",
        None,
        # Not a `module:` id, so not a name we can vouch for.
        None,
    ]


def test_nothing_else_names_a_module():
    """Tests, suites, coverage items and testbenches are not design
    coordinates. Naming one would move the design view on a click that
    had nothing to do with the design."""

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
        console.log(JSON.stringify(nodes.map(moduleNameFor)));
        console.log(JSON.stringify([moduleNameFor(null), moduleNameFor(undefined),
                                    moduleNameFor({ type: 'module' })]));
        """
    )
    typed, nullish = out.strip().splitlines()
    assert json.loads(typed) == [None] * 6
    assert json.loads(nullish) == [None, None, None]


def test_a_node_without_an_instance_still_syncs_the_design_view():
    """The bug this closes: with only config and binding tiers built,
    ``instancePathFor`` returns null for every node and the click
    broadcast nothing at all. The fallback emits the wire type the cov
    pane already sends, ``graph_focus {node: 'module:<name>'}``.

    The choice lives in ``viewTargetFor`` — the click path and the
    inspector's explicit ``send → design view`` button must take it
    identically, and duplicating it is how they stop being identical.
    Both it and ``activate`` close over the page's DOM (``state.inc``,
    ``els``), so this is asserted on the source rather than in ``node``.
    """

    js = _page_js()
    derivation = js.split("function viewTargetFor(n) {")[1].split("\n  }")[0]
    # The instance path still wins…
    assert "var ip = instancePathFor(n);" in derivation
    assert "type: 'selection_changed', payload: { instance_path: ip }," in derivation
    # …and the module name is the fallback, not a second send.
    assert "var mod = moduleNameFor(n);" in derivation
    assert "type: 'graph_focus', payload: { node: 'module:' + mod }," in derivation
    assert derivation.index("instancePathFor") < derivation.index("moduleNameFor")
    assert "note: 'focus module:' + mod + ' in design view'" in derivation
    # One emit per click, off the one derivation.
    click = js.split("if (els.optSelect.checked) {")[1].split("\n    }")[0]
    assert "var view = viewTargetFor(n);" in click
    assert "var sent = !!view && emit(view.type, view.payload);" in click
    # The cross-model warning is armed only for a delivered instance path,
    # and cleared for everything else — including a send that never left.
    assert "if (sent && view.ip) { maybeWarnCrossModel(view.ip); }" in click
    assert "else { setCrossModel(null); }" in click
    # Self-echo is harmless: inbound graph_focus ignores our own origin.
    assert "case 'graph_focus':\n        if (env.origin === 'graph') { break; }" in js


def test_the_sync_toggle_advertises_the_module_fallback():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    label = [line for line in body.splitlines() if 'id="opt-select"' in line]
    assert label, "the sync design view checkbox moved"
    tooltip = body.split('<input type="checkbox" id="opt-select"')[0].split(
        '<label class="chk" title="'
    )[-1]
    assert "highlight all instances of their module" in tooltip


# ---------------------------------------------------------------------------
# cross-app send / open
#
# Two controls per sibling app: `send → X` puts the selection on the tab
# already open, `open X ↗` emits the same envelope and then opens the tab,
# which lands focused because ``HubServer._replay_cached_state`` unicasts
# the cached focus slots to every peer as it registers.
# ---------------------------------------------------------------------------


def _cov_target_js() -> str:
    """``covTargetFor`` builds on ``moduleNameFor``, so it is sliced on
    top of the block that defines it — the same way the cov pane's lens
    helpers are sliced on top of ``module-names``."""

    return _marked_js("module-name") + _marked_js("cov-target")


def test_a_graph_node_maps_onto_a_coverage_target():
    """``cov_focus.target`` is prefixed — ``test:``, ``module:`` or
    ``file:`` — and each branch has to name something the cov pane's
    ``applyFocus`` can actually resolve, not merely something the schema
    accepts."""

    out = _node_eval(
        _cov_target_js()
        + """
        var nodes = [
          // A test: the run's per-test attribution, qualified by suite
          // the way the schema's own example spells it.
          { id: 'test:verif/fifo#smoke', type: 'test', label: 'smoke',
            file: 'verif/fifo/tests.yaml', line: 4 },
          // A model is named after its top module (moduleNameFor).
          { id: 'model:design/common/models.yaml#ip_async_fifo', type: 'model' },
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
        console.log(JSON.stringify(nodes.map(covTargetFor)));
        console.log(JSON.stringify([covTargetFor(null), covTargetFor(undefined)]));
        """
    )
    mapped, nullish = out.strip().splitlines()
    assert json.loads(mapped) == [
        {"target": "test:verif/fifo#smoke"},
        {"target": "module:ip_async_fifo"},
        {"target": "module:fifo"},
        {"target": "module:fifo", "metric": "cover", "item": "REQ-1"},
        {"target": "file:design/fifo/src/fifo.sv", "line": 9},
        # No line on the node, no line on the wire — the field is
        # optional and 0 is not a legal one.
        {"target": "file:verif/fifo/tb_fifo.sv"},
        None,
        None,
        # A coverage item with no block names no module.
        None,
    ]
    assert json.loads(nullish) == [None, None]


def test_the_inspector_offers_send_and_open_for_every_sibling_app():
    """Two controls per app, and the row sits above the identity: it is
    about what you can do with the selection, and a node with fifty edges
    must not push it out of sight."""

    js = _page_js()
    assert "els.inspector.appendChild(actionsEl);" in js
    inspector = js.split("function renderInspector(n) {")[1]
    assert inspector.index("actionsEl = renderActions(n);") < inspector.index(
        "row(dl, 'id', n.id);"
    )
    apps = js.split("var APPS = [")[1].split("\n  ];")[0]
    # The vocabulary each app is addressed in, and the route it opens on
    # — the same routes the header switcher links to.
    assert "origin: 'view', name: 'view'," in apps
    assert "origin: 'cov', name: 'coverage'," in apps
    assert "targetFor: viewTargetFor," in apps
    assert "send: function (t) { return emit(t.type, t.payload); }," in apps
    assert "targetFor: covTargetFor," in apps
    assert "send: function (t) { return emit('cov_focus', t); }," in apps
    # One send control per app, off the one target derivation. No open-↗
    # variant: opening an app fresh is the header switcher's job.
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "var t = app.targetFor(n);" in row
    assert "'send → ' + app.name," in row
    assert "function () { sendTo(app, n); }" in row
    assert "'open ' + app.name" not in row
    assert "window.open(" not in row
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    for route in ("/view", "/cov"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body


def test_send_ignores_the_sync_checkbox():
    """The checkbox governs what a CLICK broadcasts. An explicit
    ``send → view`` is the user asking for it in so many words,
    so it must not be gated on a toggle they never touched."""

    js = _page_js()
    send = js.split("function sendTo(app, n) {")[1].split("\n  }")[0]
    assert "els.optSelect" not in send
    assert "var t = app.targetFor(n);" in send
    assert (
        "if (!app.send(t)) { note('hub not connected', 'error'); return false; }"
        in (send)
    )
    # …and the same "nothing to send" wording the click path uses.
    assert "if (!t) { note(app.why, 'warn'); return false; }" in send


def test_a_send_is_dark_when_its_app_is_not_connected():
    """`send` pushes to a tab that is already open; with no such tab the
    envelope goes nowhere visible. `open` is the answer then, and the
    tooltip says so rather than leaving a dead button."""

    js = _page_js()
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "var live = hasPeer(app.origin);" in row
    assert "!t || !live," in row
    assert "app.name + ' is not connected — open it from the header links'" in row
    # The peer list is kept, not merely printed, and the row repaints
    # when it moves.
    assert "function hasPeer(origin) { return peers.indexOf(origin) >= 0; }" in js
    assert "peers = next;" in js
    assert "if (changed) { refreshActions(); }" in js


def test_the_action_row_has_no_open_buttons():
    """``open <app> ↗`` was redundant with the header switcher's links
    and is gone; the row is sends-only. The header keeps the open links
    (target=_blank), and the hub's ``_replay_cached_state`` still lands a
    late-opened tab on the current focus — send first, then open from the
    header, arrives the same way the old combined button did."""

    js = _page_js()
    assert "function openWith(" not in js
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert "window.open(" not in row
    # The header switcher still carries the open links.
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    for route in ("/view", "/cov"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body


def test_the_tooltips_own_up_to_the_broadcast():
    """A focus event is a broadcast, not a point-to-point send: an
    instance path aimed at the design view also moves the coverage pane,
    which resolves instance paths onto modules of its own accord."""

    js = _page_js()
    apps = js.split("var APPS = [")[1].split("\n  ];")[0]
    assert "This is a hub broadcast" in apps
    assert "cov_focus is read by the coverage pane only." in apps
    row = js.split("function renderActions(n) {")[1].split("\n  }")[0]
    assert row.count("app.overlap") == 1


# ---------------------------------------------------------------------------
# hub registration: the polite hello, one takeover retry, and the way back
#
# One client per origin. ``HubServer._run_handshake`` (hub/server.py)
# refuses a hello for an occupied slot with ``not_connected`` /
# ``"<client> client already registered"`` unless the hello sets
# ``takeover``, and sends the tab it evicts ``superseded`` /
# ``"<client> client replaced by a newer registration"`` before closing
# its socket. This pane used to send ``takeover: true`` on EVERY hello
# and reconnect from every close, so two tabs of it evicted each other
# every ~500 ms. The flow below is the view SPA's, mirrored: its
# ``_pendingTakeover`` / ``superseded`` handling in
# ``viewer/src/composables/useHub.js`` (rtl-buddy-view).
# ---------------------------------------------------------------------------


def test_the_first_hello_is_polite():
    """The common case is no other graph tab open, and a polite hello
    wins that outright. Asking for a takeover unconditionally is what
    turned a second tab into an eviction war."""

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
    # Omitted, not `false`: the hub reads a missing field the same way,
    # and the wire carries only what the tab is actually asking for.
    assert "takeover" not in polite
    assert unset == polite
    assert takeover["takeover"] is True

    js = _page_js()
    # Every hello on the wire comes from that helper, flagged only by the
    # state a refusal sets — there is no `takeover: true` literal left.
    assert "payload: helloPayload(pendingTakeover)" in js
    assert "ws.addEventListener('open', sendHello);" in js
    assert "var pendingTakeover = false;" in js
    assert "takeover: true" not in js


def test_an_occupied_slot_is_retried_once_with_takeover():
    """A stale tab must not be able to block this one forever, so the
    refusal is answered with exactly one takeover hello — once, because
    looping on it would be the old war with an extra round-trip."""

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'not_connected' && !pendingTakeover &&" in handler
    assert "/already registered/i.test(payload.message || '')" in handler
    assert "pendingTakeover = true;" in handler
    assert "sendHello();" in handler
    # Cleared on welcome, so a later reconnect starts polite again.
    welcome = js.split("case 'welcome':")[1].split("break;")[0]
    assert "pendingTakeover = false;" in welcome
    # A registration error the handler dealt with stays out of the
    # message area — one event, one surface.
    assert (
        "if (env.kind === 'error' && env.payload && !handleHubError(env.payload)) {"
        in js
    )


def test_superseded_stops_reconnecting_and_offers_the_slot_back():
    """Losing the slot to a NEWER tab is the one drop worth not retrying:
    reconnecting would evict the tab the user just opened. The strip says
    so in its own words and is the way back."""

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'superseded'" in handler
    assert "superseded = true;" in handler
    assert "showSuperseded();" in handler
    assert "var superseded = false;" in js
    # Disarmed at the timer AND at the close that follows the eviction,
    # which would otherwise repaint the strip over the affordance.
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
    """The other tab still holds the slot, so the hello that reclaims it
    is the one hello that MUST ask for a takeover — a polite one would be
    refused and the tab would go straight back to offline."""

    js = _page_js()
    back = js.split("function takeBack() {")[1].split("\n  }")[0]
    assert "if (!superseded) { return; }" in back
    assert "superseded = false;" in back
    assert "pendingTakeover = true;" in back
    # Backoff starts over: this is a fresh, deliberate connection.
    assert "retryMs = 500;" in back
    assert "connect();" in back
    # The status word IS the control while superseded; `takeBack` no-ops
    # in every other state, so the listener is bound once.
    assert "els.wsStatus.addEventListener('click', takeBack);" in js


def test_an_ordinary_drop_still_reconnects():
    """A hub restart or a flaky network is not a supersede, and nothing
    about the fix may change what those look like."""

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
    """A page that ships a syntax error renders a blank tab and says
    nothing about why, so the parse is worth a test of its own."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the dev machine
        pytest.skip("node not installed")
    script = tmp_path / "graph_page.js"
    script.write_text(_page_js(), encoding="utf-8")
    done = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------
# the hub version label
#
# The same contract in three places — this pane, cov_page.html, and the
# view SPA's ``viewer/src/buildInfo.js`` — so the cases below are the
# cases ``tests/test_hub_cov_page.py`` asserts, deliberately word for
# word. If one of the three drifts, exactly one of these suites goes red.
# ---------------------------------------------------------------------------


def test_a_dev_build_is_labelled_with_its_git_sha():
    """``server_version`` is setuptools-scm's, and on anything built past
    a tag the ``g``-prefixed run in the local segment IS the git SHA.
    The ``.dYYYYMMDD`` beside it is a build date the SHA already
    implies, so it does not reach the label."""

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
        # Order inside the local segment is not ours to assume: the run
        # is found wherever it sits, not only at the front.
        "6.26.2.dev13 @ 3f5b890e3",
    ]


def test_a_release_is_labelled_by_its_version_alone():
    """A tagged build has no local segment and so no SHA to show —
    ``6.26.2`` is the whole truth about it, and a bare ``@`` with
    nothing after it would only look broken."""

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify(
          ['6.26.2', '6.26.2.dev13', '0.0.0'].map(versionLabel)));
        """
    )
    assert json.loads(out) == ["6.26.2", "6.26.2.dev13", "0.0.0"]


def test_a_local_segment_without_a_sha_still_labels_the_version():
    """``1.0+local`` is a legal version; it simply names no build. The
    base is still worth showing, so a missing SHA drops the ``@`` and
    nothing else."""

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
        # fewer than 4 hex digits is not a SHA — pinned in lockstep with
        # the SPA copy (rtl-buddy-view viewer/src/buildInfo.js).
        "1.0",
    ]


def test_no_version_means_no_label_at_all():
    """A welcome without ``server_version`` (an older hub, or a payload
    that lost the field) renders nothing rather than the word
    ``undefined`` in the status strip."""

    out = _node_eval(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel(''), versionLabel(undefined), versionLabel(null),
          versionLabel('+g3f5b890e3')
        ]));
        """
    )
    # A version that is nothing but a local segment names no release,
    # so there is no label to hang the SHA off.
    assert json.loads(out) == [None, None, None, None]


def test_the_footer_carries_the_version_and_every_welcome_rewrites_it():
    """The label lives beside the peers it shares a tier with, and is
    re-read on every welcome: a reconnect can land on a hub restarted
    on a newer build."""

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


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


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
    assert b"rtl-buddy-graph" in body


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

        # The page itself is still 200 — its empty state is the better
        # place to say "run rb graph build" than a blank browser tab.
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


# ---------------------------------------------------------------------------
# graph_focus — the wire type
# ---------------------------------------------------------------------------


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
    """The pane must not share ``view`` with the SPA.

    The hub allows one client per origin, and the acceptance criteria
    require both open at once ("clicking a module node selects it in the
    design view"), so a shared slot would make them evict each other.
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
