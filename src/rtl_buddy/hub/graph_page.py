# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub-served design-knowledge-graph pane (#382).

A static per-directory ``graph.html`` would be the lazy answer; the hub can do
better, because it is the process that already owns view↔wave↔src
coordinate resolution and speaks ``selection_changed`` / ``open_source``
to every connected peer. This module is the two halves of that pane:

* :func:`build_graph_payload` — ``artefacts/graph/graph.json`` joined
  with ``artefacts/graph/results-overlay.json`` **in memory**, served at
  ``GET /graph.json``. The join is :func:`~rtl_buddy.graph.results.annotate_graph`,
  i.e. exactly the one the query verbs use, so the picture and the
  answers can never disagree. It also stamps each node with the
  ``category`` column it renders in (:func:`categorize_nodes`).
  ``graph.json`` on disk is never written — that is what keeps it
  hash-stable across regressions (#379), and the same rule is why
  ``category`` exists only in the served body.
* :func:`render_graph_html` — the page at ``GET /graph``, a single
  self-contained HTML document. No CDN, no bundler, no build step: the
  hub is frequently run on machines with no route to the internet, and
  a viewer that needs one is a viewer that does not open.

The page is a hub *peer*, registering as ``origin=graph`` (see
:class:`~rtl_buddy.hub.protocol.Origin`) so it can be open at the same
time as the schematic SPA rather than evicting it. Clicking a node
emits the same envelopes the SPA emits: ``selection_changed`` for
anything that resolves to a design-view instance path, ``open_source``
for anything that knows its file. ``rb hub send graph-focus <node>``
drives it from the other direction.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..graph.config_tier import (
    ELABORATES_AS,
    FLOW_CDC,
    FLOW_FPGA,
    FLOW_FPV,
    FLOW_SIM,
    FLOW_SYNTH,
    GRAPH_JSON_NAME,
    MAPS_TO,
)
from ..graph.build import QUALIFIER_SEP
from ..graph.merge import rel_path
from ..graph.query import GraphQueryError, load_context
from ..graph.results import annotate_graph
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


#: Bumped when the ``GET /graph.json`` envelope changes incompatibly.
#: Independent of the graph's own ``schema_version`` — this versions the
#: ``graph.hub`` block the pane reads, not the node/edge vocabulary.
PAGE_SCHEMA_VERSION = 2

#: Route serving the merged graph + overlay join.
GRAPH_JSON_ROUTE = "/graph.json"

#: Route serving the interactive page.
GRAPH_PAGE_ROUTE = "/graph"

#: Left-to-right column order on the page.
#:
#: Columns are **not** tiers. A tier says which tool produced a node,
#: which is a fact about the build, not about the design: `design` and
#: `config` between them hold the spec, the DUT, four different flows'
#: suites and every testbench hierarchy, so a three-column picture put
#: two thirds of the graph in one stripe. Columns are what a person is
#: looking for — the spec on the left, the design in the middle, and one
#: column per verification flow on the right. A node the rules below
#: cannot place lands in the trailing ``other`` column rather than being
#: dropped.
COLUMN_ORDER = (
    "spec",
    "design",
    "test-config",
    "syn-config",
    "formal-config",
    "cdc-config",
    "test-cocotb",
    "other",
)

#: The config tier's ``flow`` stamp -> the column it lands in. FPGA
#: implementation shares ``syn-config`` with synthesis: it *is* the
#: synthesis flow carried through place-and-route, it never has its own
#: suites in a project that does not do FPGA work, and a column that is
#: empty in almost every project is a column that costs more than it says.
FLOW_COLUMNS = {
    FLOW_SIM: "test-config",
    FLOW_SYNTH: "syn-config",
    FLOW_FPV: "formal-config",
    FLOW_CDC: "cdc-config",
    FLOW_FPGA: "syn-config",
}

#: Config-tier node types that describe *intent* rather than a flow.
SPEC_TYPES = frozenset({"spec_block", "coverage_item", "spec_doc", "golden_model"})

#: Config-tier node types the ``flow`` stamp applies to.
FLOW_TYPES = frozenset({"suite", "test", "testbench"})

#: Column for a flow-stamped node whose flow this build does not know.
FALLBACK_FLOW_COLUMN = "test-config"


def _flow_column(flow: object) -> str | None:
    """Column for a ``flow`` attribute (a string, or a list when shared).

    A suite claimed by two regressions is resolved in
    :data:`~rtl_buddy.graph.config_tier.FLOW_SOURCES` order — the order the
    stamp itself was built in, so the answer does not depend on which
    consumer asks.
    """

    values = [flow] if isinstance(flow, str) else flow
    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        column = FLOW_COLUMNS.get(str(value))
        if column:
            return column
    return None


def _tb_hierarchy_suites(nodes: list[dict], links: list[dict]) -> dict[str, str]:
    """Design-tier node id -> the suite whose testbench elaboration owns it.

    ``rb graph build`` exports the design tier twice: once rooted at each
    model, and once per testbench rooted at its top (#377). Both halves are
    ``tier: design``, and the whole point of the second half is that its
    ``module:<dut>`` is *the same node* as the first half's — so "which
    export did this node come from?" is not a question a tier tells you.

    Three rules answer it without re-running anything, cheapest first:

    1. ``qualified_by`` — set by the build on any id two files claimed, and
       its value already *is* the suite directory.
    2. A module a ``tb:`` node ``elaborates_as`` is that testbench's root,
       unless a ``model:`` node ``maps_to`` it too: a cocotb or SystemC
       testbench tops at the DUT itself, and a module a ``models.yaml``
       declares is design whatever else elaborates it. The two stitches are
       separate edge types (#376), so the source kind is read off the edge
       rather than off the id prefix.
    3. An ``inst:<root>/<path>`` id embeds the root it was reached from, so
       every instance under a testbench root belongs to that testbench.

    Ports and parameters follow their ``owner`` module.

    A *module* node under a testbench root that is neither of those is left
    in the design column on purpose. Modules are the weld between the two
    exports, so nothing in the merged graph says which one produced them —
    "every instance of it is a testbench instance" would be the only test
    available, and it is wrong for exactly the case that matters: a DUT no
    ``models.yaml`` declares, reached only through the testbench that
    instantiates it, is design and not test plumbing. Under-claiming here
    costs a driver module the right column; over-claiming would file a
    vendor IP block under someone's testbench.
    """

    by_id = {n["id"]: n for n in nodes if n.get("id")}
    model_targets: set[str] = set()
    tb_roots: dict[str, str] = {}
    for link in links:
        source, target = link.get("source"), link.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if link.get("type") == MAPS_TO and source.startswith("model:"):
            model_targets.add(target)
    for link in links:
        source, target = link.get("source"), link.get("target")
        if link.get("type") != ELABORATES_AS or not isinstance(source, str):
            continue
        if not source.startswith("tb:") or not isinstance(target, str):
            continue
        if not target.startswith("module:") or target in model_targets:
            continue
        tb_roots.setdefault(target, source[len("tb:") :].split("#", 1)[0])

    owned: dict[str, str] = {}
    for node in nodes:
        node_id, tier = node.get("id"), node.get("tier")
        if not node_id or tier != "design":
            continue
        qualifier = node.get("qualified_by")
        if isinstance(qualifier, str) and qualifier:
            owned[node_id] = qualifier
        elif node_id in tb_roots:
            owned[node_id] = tb_roots[node_id]
        elif node_id.startswith("inst:"):
            base, _, qual = node_id.partition(QUALIFIER_SEP)
            root = base[len("inst:") :].split("/", 1)[0]
            suite = tb_roots.get(f"module:{root}") or (
                tb_roots.get(f"module:{root}{QUALIFIER_SEP}{qual}") if qual else None
            )
            if suite:
                owned[node_id] = suite

    # Ports and parameters need their module's answer, so they run second.
    # A port's `owner` is the bare module *name*, so when that name had to
    # be suite-qualified the lookup has to find the qualified id — and only
    # when exactly one claims the name, or the port would inherit an
    # arbitrary suite.
    for node in nodes:
        node_id, owner = node.get("id"), node.get("owner")
        if not node_id or node_id in owned or node.get("tier") != "design":
            continue
        if node.get("type") not in ("port", "parameter") or not owner:
            continue
        module = f"module:{owner}"
        if module not in by_id:
            prefix = f"{module}{QUALIFIER_SEP}"
            candidates = [i for i in by_id if i.startswith(prefix)]
            if len(candidates) != 1:
                continue
            module = candidates[0]
        if module in owned:
            owned[node_id] = owned[module]
    return owned


def categorize_nodes(payload: dict) -> dict[str, str]:
    """Node id -> :data:`COLUMN_ORDER` column, for one served payload.

    Computed here rather than in the page because two of the inputs are
    graph-wide joins the browser would have to redo on every render (which
    design-tier nodes belong to a testbench elaboration, and which flow the
    suite that owns them runs), and because a rule that is wrong is easier
    to see in a test than in a picture.

    Deliberately **not** written into ``graph.json``: the column layout is a
    presentation choice that must never make the built graph churn.
    """

    nodes = [n for n in (payload.get("nodes") or []) if n.get("id")]
    links = payload.get("links") or []
    tb_suites = _tb_hierarchy_suites(nodes, links)
    suite_flows = {
        n["id"][len("suite:") :]: n.get("flow")
        for n in nodes
        if n.get("type") == "suite" and str(n["id"]).startswith("suite:")
    }

    categories: dict[str, str] = {}
    for node in nodes:
        node_id, tier, node_type = node["id"], node.get("tier"), node.get("type")
        if node_type in SPEC_TYPES:
            categories[node_id] = "spec"
        elif node_type == "model":
            # A model *is* its module under another name — the `maps_to`
            # stitch is an identity, so it belongs beside the design it
            # aliases rather than in a flow column.
            categories[node_id] = "design"
        elif tier == "binding":
            categories[node_id] = "test-cocotb"
        elif tier == "config":
            if node.get("cocotb") and node_type in ("test", "testbench"):
                categories[node_id] = "test-cocotb"
            elif node_type in FLOW_TYPES:
                categories[node_id] = (
                    _flow_column(node.get("flow")) or FALLBACK_FLOW_COLUMN
                )
            else:
                categories[node_id] = "other"
        elif tier == "design":
            suite = tb_suites.get(node_id)
            if suite is None:
                categories[node_id] = "design"
            else:
                categories[node_id] = (
                    _flow_column(suite_flows.get(suite)) or FALLBACK_FLOW_COLUMN
                )
        else:
            categories[node_id] = "other"
    return categories


def build_graph_payload(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
) -> dict:
    """``graph.json`` + the results overlay, joined, as one JSON body.

    The returned object is still NetworkX node-link JSON — the pane
    consumes the same envelope every other graph consumer does — with
    three additions: each node that has a result carries it under
    ``results`` (:func:`~rtl_buddy.graph.results.annotate_graph`), each
    node carries the ``category`` column it renders in
    (:func:`categorize_nodes`), and ``graph.hub`` carries what the page
    needs to render a header without a second round-trip (where the two
    files were read from, node/link counts, the overlay's summary,
    per-tier and per-column counts).

    Raises :class:`~rtl_buddy.graph.query.GraphQueryError` when there is
    no graph to serve — its message already names ``rb graph build``,
    which is the actionable half of the 404 the caller will render.
    """

    ctx = load_context(
        project_root,
        graph_path=graph_path,
        overlay_path=overlay_path,
        with_results=True,
    )
    payload = ctx.graph
    annotated = annotate_graph(payload, ctx.overlay)
    categories = categorize_nodes(payload)

    tiers: dict[str, int] = {}
    types: dict[str, int] = {}
    columns: dict[str, int] = {name: 0 for name in COLUMN_ORDER}
    for node in payload.get("nodes") or []:
        tier = node.get("tier")
        tiers[str(tier) if tier else "other"] = (
            tiers.get(str(tier) if tier else "other", 0) + 1
        )
        node_type = node.get("type")
        if node_type:
            types[str(node_type)] = types.get(str(node_type), 0) + 1
        column = categories.get(node.get("id"), "other")
        node["category"] = column
        columns[column] = columns.get(column, 0) + 1

    graph_attrs = payload.get("graph")
    if not isinstance(graph_attrs, dict):
        graph_attrs = {}
        payload["graph"] = graph_attrs
    graph_attrs["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "graph_path": rel_path(ctx.project_root, ctx.graph_path),
        "overlay_path": (
            rel_path(ctx.project_root, ctx.overlay_path)
            if ctx.overlay_path is not None and ctx.overlay is not None
            else None
        ),
        "counts": {
            "nodes": len(payload.get("nodes") or []),
            "links": len(payload.get("links") or []),
            "with_results": annotated,
        },
        "tiers": dict(sorted(tiers.items())),
        "types": dict(sorted(types.items())),
        # Ordered, not sorted: this IS the left-to-right layout, and the
        # page renders its legend straight from it.
        "columns": list(COLUMN_ORDER),
        "categories": {name: columns.get(name, 0) for name in COLUMN_ORDER},
        "overlay_summary": (ctx.overlay or {}).get("summary"),
    }
    return payload


def graph_payload_bytes(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /graph.json``.

    A missing graph is a 404 with a JSON ``error`` naming the command
    that makes one, not an exception escaping into the websockets
    layer's opaque failure body — the same shape ``/api/axi-profile/
    notebook`` uses for its errors.
    """

    try:
        payload = build_graph_payload(
            project_root, graph_path=graph_path, overlay_path=overlay_path
        )
    except GraphQueryError as exc:
        log_event(
            logger,
            logging.WARNING,
            "hub.graph_page.unavailable",
            error=str(exc),
        )
        return 404, json.dumps({"error": str(exc)}).encode("utf-8")
    return 200, json.dumps(payload).encode("utf-8")


def graph_files_present(project_root: str | os.PathLike) -> bool:
    """Whether ``artefacts/graph/graph.json`` exists for this root.

    Cheap enough to call per request; used to decide whether the index
    page advertises the ``/graph`` link.
    """

    from ..graph.config_tier import default_graph_dir

    return (default_graph_dir(project_root) / GRAPH_JSON_NAME).is_file()


def render_graph_html(*, hub_addr: str, graph_url: str = GRAPH_JSON_ROUTE) -> bytes:
    """The ``GET /graph`` document, with the hub address injected.

    Everything is inline. The page must work on a machine with no route
    off localhost, so there is no CDN reference, no web font and no
    external stylesheet anywhere in it.
    """

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_GRAPH_URL__ = {graph_url!r};"
    )
    return GRAPH_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _graph_page_template() -> str:
    """Read the page template that ships beside this module."""

    return (Path(__file__).parent / "graph_page.html").read_text(encoding="utf-8")


GRAPH_PAGE_HTML: str = _graph_page_template()
"""The page source, loaded once at import.

Kept in a sibling ``.html`` file rather than a Python string so an
editor treats it as HTML and the JS inside it stays reviewable; the
wheel ships it via hatchling's package data (it lives under
``src/rtl_buddy/``)."""


__all__ = [
    "COLUMN_ORDER",
    "FALLBACK_FLOW_COLUMN",
    "FLOW_COLUMNS",
    "FLOW_TYPES",
    "GRAPH_JSON_ROUTE",
    "GRAPH_PAGE_HTML",
    "GRAPH_PAGE_ROUTE",
    "PAGE_SCHEMA_VERSION",
    "SPEC_TYPES",
    "build_graph_payload",
    "categorize_nodes",
    "graph_files_present",
    "graph_payload_bytes",
    "render_graph_html",
]
