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

* :func:`build_phys_payload` — one run's model + manifest as one JSON
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

**Which run, and who chooses it (#568).** A project holds one artefact
directory per partition, per corner and per power mode, and the pane
used to show whichever manifest happened to be newest at reload — with
no way back to the one you were reading. ``?dir=<project-relative
phys_dir>`` now selects a run; bare ``/phy.json`` still serves the
newest, so nothing about the default changed. The value is validated
against the project root before anything is read
(:func:`contained_phys_dir`) — the query string is an untrusted input
and a browser tab is reachable by anything that can reach the port.

The two are a pair, and the pane picks between them: the entry marked
``newest`` selects the *bare* route rather than that run's directory,
because "the run that is newest right now" and "whichever run is
newest" are different choices and only the second one keeps working.
Pinning the newest run's directory the moment a reader chose it would
mean the run they picked *because* it was the latest is the one they
stop seeing new results for. An explicitly chosen older run does pin.

The menu the reader picks from rides in the body as ``runs``, the
``rb phys runs`` payload verbatim. One request, because a menu fetched
separately is empty for a round-trip and absent when that request is
the one that fails; and cheap, because that listing reads a manifest per
run and no model at all.

**Focus routing is hub-local.** ``phys_focus`` is unchanged on the wire:
it carries a target and a metric and says nothing about a run, and the
pane applies it to whichever run it is displaying. A sender addresses
"the physical pane"; the pane addresses one run at a time; the reader is
the one who chose it. A run field on the wire would let a sender move a
view its user is working in, and would cost the lockstep schema bump the
protocol reserves for changes that earn one.

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

#: The query parameter that selects one run at :data:`PHYS_JSON_ROUTE`
#: (#568). Its value is a run's project-relative ``phys_dir``, exactly as
#: the ``runs`` block below spells it, so the pane hands back what it was
#: given rather than composing a path of its own.
PHYS_DIR_PARAM = "dir"

#: How many runs the payload's ``runs`` block carries. The pane needs a
#: menu, not an inventory: a dropdown is scrolled, not searched, and a
#: project with more runs than this has an ordering — newest first — that
#: puts the ones anybody is switching between at the top. The block
#: carries the untruncated count beside the list, so the pane can say
#: that it is a head. `rb phys runs` is where the rest are.
RUNS_LIMIT = 50

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
    runs_limit: int | None = RUNS_LIMIT,
) -> dict:
    """The newest run's physical model + manifest, as one JSON body.

    The body is :func:`rtl_buddy.phys.query.summary_payload` at
    ``limit=0`` — run block, totals, counts, halves, the *complete*
    module and instance rankings and the artefact block — plus a ``hub``
    block carrying what the page needs to render its chrome without a
    second round-trip (schema version, the metric order, the power
    columns, the model path, and the publication token the page
    compares reloads by).

    ``limit=0`` rather than the CLI's ten: a terminal that printed every
    leaf instance would be a terminal nobody reads to the end, which is
    why the verb truncates; a table that cannot show the row you are
    looking for is just broken, which is why the pane does not.

    The body also carries a ``runs`` block — the ``rb phys runs``
    payload verbatim, headed at :data:`RUNS_LIMIT` (#568). It rides here
    rather than behind a second endpoint because the pane needs it on
    every load: the run selector has to be populated before a reader can
    choose, and a menu fetched separately is a menu that is empty for a
    round-trip and missing entirely when that request is the one that
    fails. It costs one small read per run and no model, which is what
    the manifests' identity blocks were put there for.

    Raises :class:`~rtl_buddy.phys.query.PhysQueryError` when there is
    nothing to serve; its message already names the commands that
    produce some, which is the actionable half of the 404.
    """

    runs = phys_query.runs_payload(project_root, limit=runs_limit)
    if phys_dir is None and manifest is None and runs["runs"]:
        # The newest entry is the run this route has always served by
        # default, and the listing has just walked the tree to find it.
        # Handing it over rather than letting `load_context` discover it
        # again is one project walk per request instead of two; an empty
        # listing still falls through, so a project with no artefacts
        # gets the refusal that names the commands.
        phys_dir = os.path.join(str(project_root), runs["runs"][0]["phys_dir"])
    ctx = phys_query.load_context(project_root, phys_dir=phys_dir, manifest=manifest)
    payload = phys_query.summary_payload(ctx, limit=0)
    payload["runs"] = runs
    payload["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "model": manifest_mod.project_relative(ctx.model_path, ctx.project_root),
        "model_schema_version": ctx.model.get("schema_version"),
        "generator": ctx.model.get("generator"),
        # Which publish wrote the documents this body was read from. It
        # is what the page compares reloads by (`modelIdentity`), and it
        # is the only field that can tell a *re*publication at the same
        # path under the same top — a revision switch, a rerun — from a
        # re-read of the model already on screen; without it the pane
        # keeps a lens and a selection aimed at rows that have been
        # replaced whenever the names happen to coincide. Read off the
        # model rather than the manifest because the rows in this body
        # are the model's, so a pair caught mid-rewrite is named by the
        # half the reader is actually looking at. `None` for a document
        # written before publications were stamped, which the page falls
        # back from to the manifest path and the top.
        "publication": ctx.model.get("publication"),
        # Ordered, not sorted: this IS the left-to-right order of the
        # pane's metric switcher.
        "metrics": list(METRICS),
        # Likewise the left-to-right order of the instance table's four
        # power columns — total first, then what it decomposes into.
        "power_columns": list(phys_query.POWER_COLUMNS),
    }
    return payload


def contained_phys_dir(project_root: str | os.PathLike, requested: str):
    """The absolute artefact directory a ``?dir=`` names, or ``None``.

    The same question ``GET /cov/source`` asks of its ``?path=``, and for
    the same reason: the argument comes off a query string and a browser
    tab is reachable by anything that can reach the port. ``None`` is the
    refusal, which the caller turns into a 403.

    The containment test is made on the **logical** path — absolute-ised,
    so ``..`` collapses lexically and ``<root>/../secret`` is refused,
    but with no symlink resolved — and only falls back to the resolved
    pair. That is the one place this diverges from the coverage route,
    and it follows the physical layer's own rule
    (:func:`rtl_buddy.phys.manifest.project_relative`,
    :func:`~rtl_buddy.phys.manifest.discover_manifests`): a suite whose
    ``artefacts/`` is a symlink to scratch storage is a supported and
    documented setup, discovery walks into it, and `rb phys runs` lists
    the runs it finds there — so resolving first would 403 exactly the
    directories the selector had just offered. The resolved comparison is
    still tried second, for the reverse arrangement: a path handed in
    through a link the project root is not reached through.

    **The divergence is bounded by discovery's own boundary.** A logical
    path that stays under the root may have crossed a link on the way,
    and not every such link is one of these: an ``artefacts`` link to
    scratch is the supported layout, while a ``vendor/`` link or a link
    to ``$HOME`` is an unrelated tree wearing a project-relative name.
    Discovery already draws that line —
    :func:`rtl_buddy.phys.manifest.may_follow_link`, an ``artefacts``
    component on the path below the root, and no link that circles back
    over the root — and the route asks it the same question rather than
    inventing a second answer. The predicate is imported, not restated;
    two spellings of one boundary drift, and the drift anyone finds
    first is the one where this is the looser of the two.

    **The question is asked of every link crossed, not of the endpoint.**
    Asking it once, of the requested path as a whole, reads the rule off
    the wrong path: ``may_follow_link`` looks for an ``artefacts``
    component, and a component of that name below a link satisfies it
    just as well as the link's own position does. So ``vendor ->
    /srv/other`` with ``?dir=vendor/artefacts/run`` passed — the
    ``artefacts`` in the answer belongs to the tree on the far side of
    the link, not to the project — and the route served a manifest and a
    model from outside the project, which is precisely what the boundary
    exists to prevent. The walk below asks the question of each prefix
    that *is* a link, about that link's own position, which is what
    discovery does as it descends. A link is followable only where the
    artefacts layout sanctions the link itself.

    That makes the grant exactly the layout the selector offers: every
    route discovery reports is one it walked, so every route it reports
    passes this check by construction, and a route it refused is refused
    here too. A run ``rb phys runs`` would not list is a run this cannot
    serve.

    What is read at the end of it is a ``phys-manifest.json`` in that
    directory and the model it names, not an arbitrary file.
    """
    logical_root = Path(os.path.abspath(str(project_root)))
    logical = Path(os.path.abspath(os.path.join(logical_root, str(requested))))
    root_real = os.path.realpath(str(project_root))
    if _under(logical, logical_root):
        return logical if _links_crossed_are_followable(logical, logical_root) else None
    # Logically outside, but resolving back in: a path handed in through
    # a link the project root is not reached through. Its components are
    # not project-relative, so there is no layout position to ask about —
    # what makes it containable is that it lands inside the root.
    if _under(Path(os.path.realpath(logical)), Path(root_real)):
        return logical
    return None


def _links_crossed_are_followable(logical: Path, logical_root: Path) -> bool:
    """Whether every link on the way down to ``logical`` is one to follow.

    Walked from the project root outwards, one component at a time, so
    each link is judged on *its own* position below the root rather than
    on the position of the path that happens to end below it. That is
    the order :func:`~rtl_buddy.phys.manifest.discover_manifests`
    descends in, and asking the same predicate in the same order is what
    keeps the two from disagreeing.

    A component that is not a link costs an ``lstat`` and nothing else;
    a path with no links in it is admitted having asked nothing.
    """
    root_real = os.path.realpath(logical_root)
    prefix = logical_root
    walked: tuple[str, ...] = ()
    for part in Path(os.path.relpath(logical, logical_root)).parts:
        prefix = prefix / part
        walked += (part,)
        if os.path.islink(prefix) and not manifest_mod.may_follow_link(
            str(prefix), walked, root_real
        ):
            return False
    return True


def _under(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or sits below it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def phys_payload_bytes(
    project_root: str | os.PathLike,
    *,
    phys_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
    requested_dir: str | None = None,
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /phy.json``.

    No physical artefacts is a 404 with a JSON ``error`` naming the
    commands that make some — the shape ``GET /cov.json`` and ``GET
    /graph.json`` both return — rather than an exception escaping into
    the websockets layer's opaque failure body.

    ``requested_dir`` is the route's ``?dir=`` (#568): the
    project-relative artefact directory of one run, as the payload's
    ``runs`` block spells it. Absent, the newest manifest is served, and
    that stays the default — a reader who has selected nothing gets the
    run that finished last, exactly as before. Present, it is validated
    against the project root before anything is read
    (:func:`contained_phys_dir`): outside is ``403``, and a directory
    with no manifest in it is ``404``, which is the same answer the
    empty project gets and for the same reason — there is nothing there
    to serve.
    """

    if requested_dir:
        selected = contained_phys_dir(project_root, requested_dir)
        if selected is None:
            log_event(
                logger,
                logging.WARNING,
                "hub.phys_page.dir_outside_project",
                requested=requested_dir,
            )
            return 403, json.dumps(
                {
                    "error": (
                        f"phys: {requested_dir} is outside the project root, "
                        "or reached through a link the run listing does not "
                        "follow"
                    )
                }
            ).encode("utf-8")
        if not (selected / manifest_mod.MANIFEST_FILENAME).is_file():
            return 404, json.dumps(
                {
                    "error": (
                        f"phys: no {manifest_mod.MANIFEST_FILENAME} in "
                        f"{requested_dir}; `rb phys runs` lists the runs there are"
                    )
                }
            ).encode("utf-8")
        phys_dir = selected

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
    "PHYS_DIR_PARAM",
    "PHYS_JSON_ROUTE",
    "PHYS_PAGE_HTML",
    "PHYS_PAGE_ROUTE",
    "PRESENCE_TTL_SECONDS",
    "RUNS_LIMIT",
    "build_phys_payload",
    "contained_phys_dir",
    "phys_data_present",
    "phys_payload_bytes",
    "render_phys_html",
]
