# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub's landing page (rtl-buddy/rtl_buddy#398).

``GET /`` used to be the view SPA, which made the schematic the hub's
whole identity: nothing told a user the graph pane existed, and the next
app would have been just as invisible. ``/`` is now a landing that names
the *tasks* ("explore the design", "navigate the knowledge graph") and
routes to the app that does each one; the SPA moved to ``/view``.

Two halves, same split as :mod:`~rtl_buddy.hub.graph_page`:

* :func:`render_landing_html` — the page at ``GET /``, one self-contained
  document whose only external references are same-origin hub routes
  (``/hub/theme.css``, ``/hub/assets/*``, ``/hub/state.json``).
* :func:`build_state_payload` — the body of ``GET /hub/state.json``, the
  page's whole data source: which apps this hub can actually serve right
  now, which of them already has a tab attached, and the project state a
  person needs to know they are looking at the right hub.

The page is deliberately **not** a hub peer. The hub allows one client
per origin and a second ``hello`` supersedes the first, so a tab that
only lists the apps must never hold an origin — it would be the thing
that evicted the app you had open. It polls the JSON instead, and that
same rule is why each card carries an "already open" warning *before*
the click rather than an apology after it.
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

#: Route serving the view SPA (``/`` before #398).
VIEW_PAGE_ROUTE = "/view"


@dataclass(frozen=True, slots=True)
class AppCard:
    """One task-oriented card, and one entry in every app switcher.

    ``origin`` is the hub :class:`~rtl_buddy.hub.protocol.Origin` the app
    registers as — the join that lets the landing say "already open".
    ``status`` is ``"live"`` for an app this build ships and ``"planned"``
    for one that is announced but not routable yet.
    """

    id: str
    name: str
    task: str
    why: str
    route: str
    origin: str
    status: str = "live"


#: The hub's apps, in the order they appear on the landing and in every
#: app switcher. Coverage is listed while it is still being built (#400)
#: rather than hidden: the landing is where a user learns what the hub
#: can do, and a greyed card with a "coming soon" badge says that more
#: honestly than an app that appears one release later with no warning.
APPS: tuple[AppCard, ...] = (
    AppCard(
        id="view",
        name="design view",
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
        name="graph",
        task="Navigate the knowledge graph",
        why=(
            "Spec, design, suites and tests as one picture — click a node to "
            "select it in the design view and open it in your editor."
        ),
        route="/graph",
        origin="graph",
    ),
    AppCard(
        id="cov",
        name="coverage",
        task="Inspect coverage",
        why="Line, branch and toggle coverage per module, test and run.",
        route="/cov",
        origin="cov",
        status="planned",
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
    now: float | None = None,
) -> dict:
    """Body for ``GET /hub/state.json``.

    Every input is passed in rather than read off a server object so the
    advertisement rules — which card is live, which is greyed, which says
    "already open" — are testable without an event loop.

    Availability follows data presence, the same rule
    ``_has_graph_json`` already applies to the SPA's graph global. An
    unavailable app keeps its card (muted, with the command that makes
    it available) instead of vanishing: "the graph pane exists and you
    have not built a graph" is the useful message, and it is the one a
    hidden card cannot deliver.
    """

    now = time.time() if now is None else now
    connected = {str(p) for p in peers if str(p) != "cli"}

    apps: list[dict] = []
    for card in APPS:
        note: str | None = None
        if card.status == "planned":
            available = cov_available and card.id == "cov"
            if not available:
                note = "lands with `rb cov` (rtl-buddy/rtl_buddy#400)"
        elif card.id == "view":
            available = view_available
            note = view_note
        elif card.id == "graph":
            available = graph_present
            note = None if graph_present else "run `rb graph build` first"
        else:  # pragma: no cover - defensive; every card is handled above
            available = True
        apps.append(
            {
                "id": card.id,
                "name": card.name,
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
    }


def render_landing_html(*, hub_addr: str) -> bytes:
    """The ``GET /`` document, with the hub address injected.

    The injection mirrors every other hub page's (``%HUB_INJECTION%`` →
    ``window.__RTL_BUDDY_HUB__``) even though this page does not open a
    WebSocket: a page served by the hub should be able to say which hub,
    and DevTools is the first place anyone looks.
    """

    preamble = f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};"
    return LANDING_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def graph_state(
    project_root: str | os.PathLike | None,
) -> tuple[bool, str | None, float | None]:
    """``(present, project-relative path, mtime)`` for this root's graph.

    Freshness, not just existence: "built 3 days ago" is the difference
    between a graph the landing should send you to and one that predates
    the branch you are on.
    """

    if project_root is None:
        return False, None, None
    from ..graph.config_tier import GRAPH_JSON_NAME, default_graph_dir

    root = Path(project_root)
    path = default_graph_dir(project_root) / GRAPH_JSON_NAME
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
"""The page source, loaded once at import — same rule as the graph pane."""


# Re-exported so a pane importing the landing does not also have to know
# where the token sheet lives.
THEME_CSS_ROUTE = theme.THEME_CSS_ROUTE


__all__ = [
    "APPS",
    "LANDING_PAGE_HTML",
    "LANDING_PAGE_ROUTE",
    "STATE_JSON_ROUTE",
    "THEME_CSS_ROUTE",
    "VIEW_PAGE_ROUTE",
    "AppCard",
    "build_state_payload",
    "graph_state",
    "render_landing_html",
]
