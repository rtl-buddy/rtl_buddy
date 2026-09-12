# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub-served synth+power pane (rtl-buddy/rtl_buddy#558).

Area and power used to survive a run as four scalars and a log path.
Phase 1 of #558 turned the artefacts into a model
(:mod:`rtl_buddy.phys.model`) and a manifest
(:mod:`rtl_buddy.phys.manifest`), phase 2 put read verbs over them
(:mod:`rtl_buddy.phys.query`); this module is the browser end, cast in
the same mould as :mod:`~rtl_buddy.hub.cov_page`:

* :func:`build_phys_payload` — the run's model + manifest as one JSON
  body at ``GET /phy.json``, assembled by
  :func:`rtl_buddy.phys.query.summary_payload`, i.e. **the same builder
  ``rb phys summary`` uses**. The pane and the CLI can therefore
  disagree about presentation but never about numbers.
* :func:`phys_data_present` — the landing card's and the SPA global's
  data-presence gate, cached like the coverage one because discovery is
  a walk of the project tree.
* :func:`render_phys_html` — the page at ``GET /phy``. One
  self-contained document: no CDN, no bundler, no build step, for the
  same reason the other panes have none — the hub is routinely run
  where there is no route off localhost.

**The payload, and why it has no second copy of the rows.**
``summary_payload(ctx, limit=0)`` is the whole body. At ``limit=0`` the
two rankings stop truncating, so ``modules`` and ``instances`` are the
model's own halves in ranked order — a permutation of every row, not a
head of one. A payload that carried the rankings *and* a raw copy of
the same rows would be twice the bytes of the largest thing here (a
real design's instance half is the megabytes in this document) to say
the same thing twice, so the ranking IS the raw data and the pane
re-sorts client-side. What the pane needs beyond the rows —
``totals``, ``counts``, ``halves``, ``missing_halves``, ``artefacts``
and the run header — the summary builder already carries, including the
totals-versus-sum comparison the model exists to make possible.

A half the run did not produce comes back as an EMPTY ranking, because
the rankings sort over ``model[half] or []``; ``halves`` and
``missing_halves`` are what distinguish "no synthesis ran here" from "a
synthesis ran and the design has no cells". The pane reads those, not
the row count, for its missing-half banner.

The page is a hub *peer* registering as ``origin=phys``
(:class:`~rtl_buddy.hub.protocol.Origin`), so it is open alongside the
schematic, the graph and the coverage pane rather than evicting them.
Clicking a module emits ``graph_focus {node: 'module:<name>'}`` — the
envelope the coverage pane's module chips already emit, which both the
graph pane and the schematic SPA resolve — and clicking an instance
emits ``selection_changed``, because an instance path *is* the
schematic's own coordinate and needs no resolver in between.
``rb hub send phys-focus <target>`` drives it from the other direction,
replayed on connect so it works before the tab is open.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..logging_utils import log_event
from ..phys import manifest as manifest_mod
from ..phys import query as phys_query


logger = logging.getLogger(__name__)


#: Bumped when the ``GET /phy.json`` envelope changes incompatibly.
#: Independent of the physical model's own ``schema_version`` — this
#: versions the ``hub`` block the pane reads, not the row vocabulary.
PAGE_SCHEMA_VERSION = 1

#: Route serving the run's physical model.
PHYS_JSON_ROUTE = "/phy.json"

#: Route serving the interactive page.
PHYS_PAGE_ROUTE = "/phy"

#: The metric switcher, left to right, and the ``phys_focus.metric``
#: enum verbatim (rtl-buddy-sch ``schemas/hub-protocol-v1.json``).
#:
#: Four of the five are columns of the model. ``dynamic`` is not: it is
#: internal + switching, summed by the pane, because that is the number
#: a power reviewer actually compares against leakage and no producer
#: writes it down. It is in the *wire* enum for the same reason it is on
#: this switcher — a sender pointing at "the dynamic-power view" should
#: not have to know which half of the sum the model stores.
METRICS = ("cells", "area", "leakage", "dynamic", "total")

#: How long :func:`phys_data_present` trusts a previous answer, seconds.
#: The same TTL, for the same reason, as the coverage pane's: discovery
#: is a bounded walk of the project tree (physical artefacts land in
#: whatever ``artefacts/<run>/`` the run was named into — there is not
#: even a directory-name shortcut), and the landing page polls its state
#: several times a minute.
PRESENCE_TTL_SECONDS = 5.0

_presence_cache: dict[str, tuple[float, bool]] = {}


def build_phys_payload(
    project_root: str | os.PathLike,
    *,
    phys_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> dict:
    """The newest run's physical model + manifest, as one JSON body.

    The body is :func:`rtl_buddy.phys.query.summary_payload` at
    ``limit=0`` — run block, totals, counts, halves, the *complete*
    module and instance rankings and the artefact block — plus a ``hub``
    block carrying what the page needs to render its chrome without a
    second round-trip (schema version, the metric order, the power
    columns, and the model path).

    ``limit=0`` rather than the CLI's ten: a terminal that printed every
    leaf instance would be a terminal nobody reads to the end, which is
    why the verb truncates; a table that cannot show the row you are
    looking for is just broken, which is why the pane does not.

    Raises :class:`~rtl_buddy.phys.query.PhysQueryError` when there is
    nothing to serve; its message already names the commands that
    produce some, which is the actionable half of the 404.
    """

    ctx = phys_query.load_context(project_root, phys_dir=phys_dir, manifest=manifest)
    payload = phys_query.summary_payload(ctx, limit=0)
    payload["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "model": manifest_mod.project_relative(ctx.model_path, ctx.project_root),
        "model_schema_version": ctx.model.get("schema_version"),
        "generator": ctx.model.get("generator"),
        # Ordered, not sorted: this IS the left-to-right order of the
        # pane's metric switcher.
        "metrics": list(METRICS),
        # Likewise the left-to-right order of the instance table's four
        # power columns — total first, then what it decomposes into.
        "power_columns": list(phys_query.POWER_COLUMNS),
    }
    return payload


def phys_payload_bytes(
    project_root: str | os.PathLike,
    *,
    phys_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /phy.json``.

    No physical artefacts is a 404 with a JSON ``error`` naming the
    commands that make some — the shape ``GET /cov.json`` and ``GET
    /graph.json`` both return — rather than an exception escaping into
    the websockets layer's opaque failure body.
    """

    try:
        payload = build_phys_payload(project_root, phys_dir=phys_dir, manifest=manifest)
    except phys_query.PhysQueryError as exc:
        log_event(
            logger,
            logging.WARNING,
            "hub.phys_page.unavailable",
            error=str(exc),
        )
        return 404, json.dumps({"error": str(exc)}).encode("utf-8")
    return 200, json.dumps(payload).encode("utf-8")


def phys_data_present(
    project_root: str | os.PathLike | None, *, ttl: float = PRESENCE_TTL_SECONDS
) -> bool:
    """Whether any run under this root left a physical manifest.

    Cached for :data:`PRESENCE_TTL_SECONDS`: the answer is used by the
    landing page's poll and by the SPA injection, both of which ask
    repeatedly, and the lookup costs a walk. A TTL rather than an
    invalidation hook because nothing in the hub observes the
    filesystem — a synthesis finished in another terminal must show up
    on its own, and five seconds late is not late.
    """

    if project_root is None:
        return False
    key = str(project_root)
    now = time.monotonic()
    cached = _presence_cache.get(key)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]
    present = bool(manifest_mod.discover_manifests(project_root))
    _presence_cache[key] = (now, present)
    return present


def render_phys_html(*, hub_addr: str, phys_url: str = PHYS_JSON_ROUTE) -> bytes:
    """The ``GET /phy`` document, with the hub address injected.

    Everything is inline. The page must work on a machine with no route
    off localhost, so there is no CDN reference, no web font and no
    external stylesheet anywhere in it — only same-origin hub routes.
    """

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_PHY_URL__ = {phys_url!r};"
    )
    return PHYS_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _phys_page_template() -> str:
    """Read the page template that ships beside this module."""

    return (Path(__file__).parent / "phys_page.html").read_text(encoding="utf-8")


PHYS_PAGE_HTML: str = _phys_page_template()
"""The page source, loaded once at import — same rule as the other panes.

Kept in a sibling ``.html`` file rather than a Python string so an editor
treats it as HTML and the JS inside it stays reviewable; the wheel ships
it via hatchling's package data (it lives under ``src/rtl_buddy/``)."""


__all__ = [
    "METRICS",
    "PAGE_SCHEMA_VERSION",
    "PHYS_JSON_ROUTE",
    "PHYS_PAGE_HTML",
    "PHYS_PAGE_ROUTE",
    "PRESENCE_TTL_SECONDS",
    "build_phys_payload",
    "phys_data_present",
    "phys_payload_bytes",
    "render_phys_html",
]
