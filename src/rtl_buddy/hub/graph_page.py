# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub-served design-knowledge-graph pane (#382).

Graphify ships a static per-directory ``graph.html``; the hub can do
better, because it is the process that already owns view↔wave↔src
coordinate resolution and speaks ``selection_changed`` / ``open_source``
to every connected peer. This module is the two halves of that pane:

* :func:`build_graph_payload` — ``artefacts/graph/graph.json`` joined
  with ``artefacts/graph/results-overlay.json`` **in memory**, served at
  ``GET /graph.json``. The join is :func:`~rtl_buddy.graph.results.annotate_graph`,
  i.e. exactly the one the query verbs use, so the picture and the
  answers can never disagree. ``graph.json`` on disk is never written —
  that is what keeps it hash-stable across regressions (#379).
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

from ..graph.config_tier import GRAPH_JSON_NAME
from ..graph.merge import rel_path
from ..graph.query import GraphQueryError, load_context
from ..graph.results import annotate_graph
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


#: Bumped when the ``GET /graph.json`` envelope changes incompatibly.
#: Independent of the graph's own ``schema_version`` — this versions the
#: ``graph.hub`` block the pane reads, not the node/edge vocabulary.
PAGE_SCHEMA_VERSION = 1

#: Route serving the merged graph + overlay join.
GRAPH_JSON_ROUTE = "/graph.json"

#: Route serving the interactive page.
GRAPH_PAGE_ROUTE = "/graph"

#: Tier order the page lays out left-to-right — the same order
#: :data:`rtl_buddy.graph.merge.TIER_ORDER` sorts by, so the picture
#: reads in the direction the build produced it. A tier this build does
#: not know about lands in a trailing "other" column rather than being
#: dropped: an unknown tier is still a tier the user wants to see.
COLUMN_ORDER = ("design", "config", "binding")


def build_graph_payload(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
) -> dict:
    """``graph.json`` + the results overlay, joined, as one JSON body.

    The returned object is still NetworkX node-link JSON — the pane
    consumes the same envelope every other graph consumer does — with
    two additions: each node that has a result carries it under
    ``results`` (:func:`~rtl_buddy.graph.results.annotate_graph`), and
    ``graph.hub`` carries what the page needs to render a header
    without a second round-trip (where the two files were read from,
    node/link counts, the overlay's summary, per-tier counts).

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

    tiers: dict[str, int] = {}
    types: dict[str, int] = {}
    for node in payload.get("nodes") or []:
        tier = node.get("tier")
        tiers[str(tier) if tier else "other"] = (
            tiers.get(str(tier) if tier else "other", 0) + 1
        )
        node_type = node.get("type")
        if node_type:
            types[str(node_type)] = types.get(str(node_type), 0) + 1

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
    "GRAPH_JSON_ROUTE",
    "GRAPH_PAGE_HTML",
    "GRAPH_PAGE_ROUTE",
    "PAGE_SCHEMA_VERSION",
    "build_graph_payload",
    "graph_files_present",
    "graph_payload_bytes",
    "render_graph_html",
]
