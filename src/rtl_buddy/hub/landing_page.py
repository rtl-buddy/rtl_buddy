# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub's landing page: the task-oriented index served at ``GET /``.

:func:`render_landing_html` renders the page, whose only external references are same-origin hub routes. :func:`build_state_payload` builds ``GET /hub/state.json``, the page's whole data source: which apps can be served, which already have a tab, and the project state. The page is deliberately not a hub peer, because a second ``hello`` for an origin would evict the app you had open; it polls the JSON instead.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import theme


#: Route serving the landing page.
LANDING_PAGE_ROUTE = "/"

#: Route serving the landing page's live state.
STATE_JSON_ROUTE = "/hub/state.json"

#: Route serving the schematic SPA, named like ``/gph``, ``/cov`` and ``/phy``.
#:
#: A page route only: the hub-protocol origin stays ``view``, as do
#: ``/view.json`` and the other data routes.
VIEW_PAGE_ROUTE = "/sch"

#: Old page route, answered with a 307 to :data:`VIEW_PAGE_ROUTE`.
LEGACY_VIEW_PAGE_ROUTE = "/view"


@dataclass(frozen=True, slots=True)
class AppCard:
    """One task-oriented card, and one entry in every app switcher.

    ``name`` is the long name (``rtl-buddy-schematic``) used on the landing
    cards and in docs; ``short`` is the chrome label (``sch``) used by
    switchers, peer strips and ``send →`` buttons. ``origin`` is the hub
    :class:`~rtl_buddy.hub.protocol.Origin` the app registers as, a wire value
    that stays ``view`` for the schematic and ``graph`` for the graph pane
    until protocol v2. ``status`` is ``"live"`` or ``"planned"`` (announced,
    not routable).
    """

    id: str
    name: str
    short: str
    task: str
    why: str
    route: str
    origin: str
    status: str = "live"


#: The hub's apps, in landing and switcher order. A card whose app has nothing
#: to show is muted with the command that fills it, not hidden.
#:
#: This tuple holds the server-side display names; each pane carries its own
#: origin -> short-name map, so a rename edits both.
APPS: tuple[AppCard, ...] = (
    AppCard(
        id="view",
        name="rtl-buddy-schematic",
        short="sch",
        task="Explore the design",
        why=(
            "Schematic hierarchy for a model or testbench, cross-highlighted "
            "with the waveform and your editor."
        ),
        route=VIEW_PAGE_ROUTE,
        origin="view",
    ),
    AppCard(
        id="graph",
        name="rtl-buddy-graph",
        short="gph",
        task="Navigate the knowledge graph",
        why=(
            "Spec, design, suites and tests as one picture — click a node to "
            "select it in the schematic and open it in your editor."
        ),
        route="/gph",
        origin="graph",
    ),
    AppCard(
        id="cov",
        name="rtl-buddy-coverage",
        short="cov",
        task="Inspect coverage",
        why=(
            "Line, branch and toggle coverage per module, test and run — "
            "annotated on the source, with the tests behind every point."
        ),
        route="/cov",
        origin="cov",
    ),
    AppCard(
        id="phys",
        name="rtl-buddy-phys",
        short="phy",
        task="Weigh area and power",
        why=(
            "Cells and area per module, leakage and dynamic power per "
            "instance — the synthesis and power runs' own numbers, ranked."
        ),
        route="/phy",
        origin="phys",
    ),
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def build_state_payload(
    *,
    hub_addr: str,
    server_version: str | None = None,
    project_root: Path | None = None,
    active_model: str | None = None,
    active_test: str | None = None,
    peers: Sequence[str] = (),
    view_available: bool = True,
    view_note: str | None = None,
    graph_present: bool = False,
    graph_path: str | None = None,
    graph_mtime: float | None = None,
    cov_available: bool = False,
    phys_available: bool = False,
    now: float | None = None,
) -> dict:
    """Body for ``GET /hub/state.json``.

    Inputs are passed in rather than read off a server object so the
    advertisement rules are testable without an event loop. An app is available
    when its data exists; an unavailable app keeps a muted card naming the
    command that fixes it. A ``planned`` card is never available.
    """

    now = time.time() if now is None else now
    connected = {str(p) for p in peers if str(p) != "cli"}

    apps: list[dict] = []
    for card in APPS:
        note: str | None = None
        if card.status == "planned":  # pragma: no cover - every shipped card is live
            available = False
        elif card.id == "cov":
            available = cov_available
            note = (
                None
                if cov_available
                else "run a coverage flag (`rb regression --coverage-merge`) first"
            )
        elif card.id == "view":
            available = view_available
            note = view_note
        elif card.id == "graph":
            available = graph_present
            note = None if graph_present else "run `rb graph build` first"
        elif card.id == "phys":
            available = phys_available
            # Either command alone gives the pane something to render.
            note = None if phys_available else "run `rb synth` or `rb power` first"
        else:  # pragma: no cover - defensive; every card is handled above
            available = True
        apps.append(
            {
                "id": card.id,
                "name": card.name,
                "short": card.short,
                "task": card.task,
                "why": card.why,
                "route": card.route,
                "origin": card.origin,
                "status": card.status,
                "available": bool(available),
                "note": note,
                "open": card.origin in connected,
            }
        )

    # The page's empty-state test reads this, so physical artefacts count as built.
    # A block, to match `graph`.
    phys: dict = {"present": bool(phys_available)}

    graph: dict = {"present": bool(graph_present), "path": graph_path}
    if graph_present and graph_mtime is not None:
        graph["built_at"] = _iso(graph_mtime)
        graph["age_seconds"] = max(0.0, round(now - graph_mtime, 1))
    else:
        graph["built_at"] = None
        graph["age_seconds"] = None

    return {
        "hub": {
            "addr": hub_addr,
            "server_version": server_version,
            "project_root": str(project_root) if project_root is not None else None,
            "active_model": active_model,
            "active_test": active_test,
        },
        "peers": sorted(connected),
        "apps": apps,
        "graph": graph,
        "phys": phys,
    }


def render_landing_html(*, hub_addr: str) -> bytes:
    """The ``GET /`` document, with the hub address injected.

    ``window.__RTL_BUDDY_HUB__`` is set as on every hub page, though this page
    opens no WebSocket.
    """

    preamble = f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};"
    return LANDING_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def graph_state(
    project_root: str | os.PathLike | None,
) -> tuple[bool, str | None, float | None]:
    """``(present, project-relative path, mtime)`` for this root's graph.

    The mtime lets the page show how old the graph is.
    """

    if project_root is None:
        return False, None, None
    from . import graph_page

    root = Path(project_root)
    # Derived from the route's own path, so the landing never advertises a
    # graph the pane would 404 on.
    path = graph_page.graph_json_path(project_root)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False, None, None
    try:
        rel = str(path.relative_to(root))
    except ValueError:  # pragma: no cover - graph dir is always under the root
        rel = str(path)
    return True, rel, mtime


def _landing_page_template() -> str:
    """Read the page template that ships beside this module."""

    return (Path(__file__).parent / "landing_page.html").read_text(encoding="utf-8")


LANDING_PAGE_HTML: str = _landing_page_template()
"""The page source, loaded once at import."""


# Re-exported for panes that import the landing.
THEME_CSS_ROUTE = theme.THEME_CSS_ROUTE


__all__ = [
    "APPS",
    "LANDING_PAGE_HTML",
    "LANDING_PAGE_ROUTE",
    "STATE_JSON_ROUTE",
    "THEME_CSS_ROUTE",
    "VIEW_PAGE_ROUTE",
    "LEGACY_VIEW_PAGE_ROUTE",
    "AppCard",
    "build_state_payload",
    "graph_state",
    "render_landing_html",
]
