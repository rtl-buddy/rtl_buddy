# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Hub-served design-knowledge-graph pane: the payload behind ``GET /graph.json`` and the page at ``GET /gph``.

:func:`build_graph_payload` joins ``graph.json`` with ``results-overlay.json`` in memory, so ``graph.json`` on disk stays hash-stable. :func:`render_graph_html` returns one self-contained HTML document with no CDN or build step. The page registers as hub peer ``origin=graph`` and emits ``selection_changed`` and ``open_source`` envelopes; ``rb hub send graph-focus <node>`` drives it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..graph.config_tier import (
    ELABORATES_AS,
    FLOW_CDC,
    FLOW_LINT,
    FLOW_FPGA,
    FLOW_FPV,
    FLOW_SIM,
    FLOW_SYNTH,
    GRAPH_JSON_NAME,
    MAPS_TO,
)
from ..graph.build import QUALIFIER_SEP
from ..graph.merge import rel_path
from ..graph.coverage import (
    STATUS_DECLARED_ONLY,
    STATUS_EXERCISED,
    STATUS_OBSERVED_UNDECLARED,
    annotate_coverage,
    coverage_block,
)
from ..graph.query import GraphQueryError, load_context
from ..graph.results import annotate_graph
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


#: Version of the ``graph.hub`` block in the ``GET /graph.json`` body, bumped on
#: incompatible changes. Independent of the graph's own ``schema_version``.
PAGE_SCHEMA_VERSION = 3

#: Route serving the merged graph + overlay join.
GRAPH_JSON_ROUTE = "/graph.json"

#: Route serving the interactive page, named like ``/sch`` and ``/cov``.
GRAPH_PAGE_ROUTE = "/gph"

#: Old page route, answered with a 307 to :data:`GRAPH_PAGE_ROUTE`.
LEGACY_GRAPH_PAGE_ROUTE = "/graph"

#: Left-to-right column order on the page.
#:
#: Columns are not tiers: they are what a person looks for (the spec, the
#: design, one column per verification flow). A node no rule places lands in
#: ``other``.
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

#: The config tier's ``flow`` stamp -> its column. FPGA shares ``syn-config``
#: with synthesis and style lint shares ``cdc-config``, so rarely used flows
#: do not add mostly empty columns.
FLOW_COLUMNS = {
    FLOW_SIM: "test-config",
    FLOW_SYNTH: "syn-config",
    FLOW_FPV: "formal-config",
    FLOW_CDC: "cdc-config",
    FLOW_LINT: "cdc-config",
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

    A suite claimed by two regressions resolves in
    :data:`~rtl_buddy.graph.config_tier.FLOW_SOURCES` order.
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

    ``rb graph build`` exports the design tier once per model and once per
    testbench, and a node does not say which export it came from. Rules,
    cheapest first:

    1. ``qualified_by`` (set on any id two files claimed) is the suite directory.
    2. A module that a ``tb:`` node ``elaborates_as`` is that testbench's root,
       unless a ``model:`` node ``maps_to`` it too (cocotb and SystemC
       testbenches top at the DUT). The edge type tells the source kind.
    3. An ``inst:<root>/<path>`` id embeds its root, so every instance under a
       testbench root belongs to that testbench.

    Ports and parameters follow their ``owner`` module. Any other module under
    a testbench root stays in the design column: a DUT reached only through
    its testbench is design, not test plumbing.
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

    # Second pass: a port's `owner` is a bare module name, so a suite-qualified
    # module is found by prefix, and only when exactly one claims the name.
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

    Computed server-side because it needs graph-wide joins. Never written into
    ``graph.json``: the layout is presentation and must not churn the built graph.
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
            # `maps_to` is an identity: a model sits beside the design it aliases.
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
    """``graph.json`` plus the results overlay, joined, as one JSON body.

    The result is NetworkX node-link JSON with four additions: per-node
    ``results`` (:func:`~rtl_buddy.graph.results.annotate_graph`), per-node
    ``coverage`` (:func:`~rtl_buddy.graph.coverage.annotate_coverage`), per-node
    ``category`` (:func:`categorize_nodes`), and a ``graph.hub`` block with what
    the page header needs: source paths, counts, the overlay summary, the
    coverage header and per-tier and per-column counts. The joins are the ones
    the query verbs use and run in memory only.

    Raises :class:`~rtl_buddy.graph.query.GraphQueryError` when there is no
    graph; its message names ``rb graph build``.
    """

    ctx = load_context(
        project_root,
        graph_path=graph_path,
        overlay_path=overlay_path,
        with_results=True,
    )
    payload = ctx.graph
    annotated = annotate_graph(payload, ctx.overlay)
    covered = annotate_coverage(payload, ctx.overlay)
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
            "with_coverage": covered,
        },
        "tiers": dict(sorted(tiers.items())),
        "types": dict(sorted(types.items())),
        # Ordered, not sorted: the page renders its legend from it.
        "columns": list(COLUMN_ORDER),
        "categories": {name: columns.get(name, 0) for name in COLUMN_ORDER},
        "overlay_summary": (ctx.overlay or {}).get("summary"),
        # Header only: the per-node map is already on the nodes. The
        # undeclared list has no node, so it rides along.
        "coverage": _coverage_header(coverage_block(ctx.overlay)),
        "item_statuses": [
            STATUS_EXERCISED,
            STATUS_DECLARED_ONLY,
            STATUS_OBSERVED_UNDECLARED,
        ],
    }
    return payload


def _coverage_header(block: dict | None) -> dict | None:
    """The coverage block without its per-node map."""
    if block is None:
        return None
    return {key: value for key, value in block.items() if key != "nodes"}


def graph_payload_bytes(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /graph.json``.

    A missing graph gives a 404 with a JSON ``error`` naming the command that
    builds one.
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


def graph_json_path(project_root: str | os.PathLike) -> Path:
    """Where ``graph.json`` lives for this root; every presence check derives from it."""

    from ..graph.config_tier import default_graph_dir

    return default_graph_dir(project_root) / GRAPH_JSON_NAME


def graph_files_present(project_root: str | os.PathLike) -> bool:
    """Whether ``artefacts/graph/graph.json`` exists for this root.

    Cheap enough to call per request; decides whether the index page links ``/gph``.
    """

    return graph_json_path(project_root).is_file()


def render_graph_html(
    *,
    hub_addr: str,
    graph_url: str = GRAPH_JSON_ROUTE,
    phys_url: str | None = None,
) -> bytes:
    """The ``GET /gph`` document, with the hub address injected.

    Everything is inline: no CDN, web font or external stylesheet.

    ``phys_url`` is the physical model's data route
    (:data:`~rtl_buddy.hub.phys_page.PHYS_JSON_ROUTE`) when the project has
    one; the heat overlay reads module rows through it. It is injected rather
    than carried in ``graph.hub`` so the pane and the landing card share one
    presence probe, and because ``/graph.json`` is a 404 on a project with no
    graph. ``None`` omits the global and the page mutes the heat control.
    """

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_GRAPH_URL__ = {graph_url!r};"
    )
    if phys_url is not None:
        preamble += f"\nwindow.__RTL_BUDDY_PHY_URL__ = {phys_url!r};"
    return GRAPH_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _graph_page_template() -> str:
    """Read the page template that ships beside this module."""

    return (Path(__file__).parent / "graph_page.html").read_text(encoding="utf-8")


GRAPH_PAGE_HTML: str = _graph_page_template()
"""The page source, loaded once at import from the sibling ``graph_page.html``."""


__all__ = [
    "COLUMN_ORDER",
    "FALLBACK_FLOW_COLUMN",
    "FLOW_COLUMNS",
    "FLOW_TYPES",
    "GRAPH_JSON_ROUTE",
    "GRAPH_PAGE_HTML",
    "GRAPH_PAGE_ROUTE",
    "LEGACY_GRAPH_PAGE_ROUTE",
    "PAGE_SCHEMA_VERSION",
    "SPEC_TYPES",
    "build_graph_payload",
    "categorize_nodes",
    "graph_files_present",
    "graph_json_path",
    "graph_payload_bytes",
    "render_graph_html",
]
