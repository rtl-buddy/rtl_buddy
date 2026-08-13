# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub's landing page (rtl-buddy/rtl_buddy#398).

``GET /`` used to be the schematic SPA, which made it the hub's
whole identity: nothing told a user the graph pane existed, and the next
app would have been just as invisible. ``/`` is now a landing that names
the *tasks* ("explore the design", "navigate the knowledge graph") and
routes to the app that does each one; the SPA moved to ``/view``, and
to ``/sch`` in #423 when the page routes became the apps' short names.

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

#: Route serving the schematic SPA (``/`` before #398, ``/view`` before
#: #423). The page route is the app's **short** name, so the three apps
#: read as one set — ``/sch``, ``/gph``, ``/cov`` — and the URL matches
#: the chip every app switcher shows.
#:
#: This is a PAGE route only. The hub-protocol origin stays ``view``, as
#: do ``/view.json`` and every other data route: the wire contract is
#: protocol v1 and renaming a page does not get to touch it.
VIEW_PAGE_ROUTE = "/sch"

#: The pre-#423 spelling, answered with a 307 to :data:`VIEW_PAGE_ROUTE`.
LEGACY_VIEW_PAGE_ROUTE = "/view"


@dataclass(frozen=True, slots=True)
class AppCard:
    """One task-oriented card, and one entry in every app switcher.

    Two names, because the two surfaces read differently. ``name`` is the
    app's **long** name (``rtl-buddy-schematic``) and only the landing's
    cards and the docs use it; ``short`` is the chrome label (``sch``)
    that every switcher, peer strip and ``send →`` button carries. The
    long name introduces the app once; the short one is what a person
    then navigates by.

    ``origin`` is the hub :class:`~rtl_buddy.hub.protocol.Origin` the app
    registers as — the join that lets the landing say "already open", and
    a WIRE value: it stays ``view`` for the schematic (and ``graph`` for
    ``rtl-buddy-gph``) until protocol v2, which is exactly why the
    display names live here rather than being read off the origin.
    ``status`` is ``"live"`` for an app this build ships and ``"planned"``
    for one that is announced but not routable yet.
    """

    id: str
    name: str
    short: str
    task: str
    why: str
    route: str
    origin: str
    status: str = "live"


#: The hub's apps, in the order they appear on the landing and in every
#: app switcher. Every one of them is routable; a card is *muted* when
#: the app has nothing to show yet, carrying the command that would give
#: it something, rather than disappearing.
#:
#: This tuple is the one place the family's display names are written
#: down on the server side — the panes carry their own copy of the
#: origin→short-name map, since a pane is a self-contained single file.
#: Renaming an app is an edit here plus that map in each pane.
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

    A ``planned`` card is never advertised as available — the page gates
    routability on ``status``, so the two fields must not contradict.
    No shipped card is planned any more: the cov card went live with the
    pane (rtl-buddy/rtl_buddy#400), with ``cov_available`` as its
    data-presence half, the same shape as ``graph_present``.
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
    from . import graph_page

    root = Path(project_root)
    # Same coordinate the /graph.json route serves from: the landing can
    # never advertise a graph the pane would 404 on (drift guard — the
    # predicate is derived, not re-spelled).
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
    "LEGACY_VIEW_PAGE_ROUTE",
    "AppCard",
    "build_state_payload",
    "graph_state",
    "render_landing_html",
]
