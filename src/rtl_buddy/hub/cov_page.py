# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub-served coverage pane (rtl-buddy/rtl_buddy#400).

Coverage has always been produced and then thrown away: four scalars
survived into ``--machine``, an LCOV HTML tree survived as a path, and
"which test covered this line" needed a Coverview archive somebody had
remembered to package. #399 turned the artefacts into a model
(:mod:`rtl_buddy.cov.model`) and a manifest
(:mod:`rtl_buddy.cov.manifest`); this module is the browser end of it,
cast in the same mould as :mod:`~rtl_buddy.hub.graph_page`:

* :func:`build_cov_payload` — the run's model + manifest as one JSON
  body at ``GET /cov.json``, assembled by
  :func:`rtl_buddy.cov.query.detail_payload`, i.e. **the same builder
  the CLI verbs use**. The pane and ``rb cov summary`` can therefore
  disagree about presentation but never about numbers.
* :func:`read_source_lines` — ``GET /cov/source?path=…``, the file text
  the annotation renders against. Points carry line numbers, not
  source, and a payload that inlined every covered file would be tens
  of megabytes on a real design; this is the one lazy edge.
* :func:`render_cov_html` — the page at ``GET /cov``. One
  self-contained document: no CDN, no bundler, no build step, for the
  same reason the graph pane has none — the hub is routinely run where
  there is no route off localhost.

The page is a hub *peer* registering as ``origin=cov``
(:class:`~rtl_buddy.hub.protocol.Origin`), so it is open alongside the
schematic and the graph rather than evicting them. Clicking a cold line
emits ``source_focused`` (which the hub's resolver augments into a
``selection_changed`` for the design view — the pane knows files and
modules, not instance paths, so deriving the instance on the hub is the
only honest way to reach the schematic) and, on request, ``open_source``
to the editor. ``rb hub send cov-focus <target>`` drives it from the
other direction, replayed on connect so it works before the tab is open.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..cov import manifest as manifest_mod
from ..cov import query as cov_query
from ..cov.raw import METRICS
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


#: Bumped when the ``GET /cov.json`` envelope changes incompatibly.
#: Independent of the coverage model's own ``schema_version`` — this
#: versions the ``hub`` block the pane reads, not the point vocabulary.
PAGE_SCHEMA_VERSION = 1

#: Route serving the run's coverage model.
COV_JSON_ROUTE = "/cov.json"

#: Route serving the interactive page.
COV_PAGE_ROUTE = "/cov"

#: Route serving one annotated file's source text.
COV_SOURCE_ROUTE = "/cov/source"

#: Largest source file the pane will fetch, in bytes. A coverage model
#: can name a generated file that is megabytes of one line; refusing it
#: with a reason beats hanging the tab.
MAX_SOURCE_BYTES = 4 * 1024 * 1024

#: How long :func:`cov_data_present` trusts a previous answer, seconds.
#: Unlike the graph's single ``stat``, discovery is a bounded walk of the
#: project tree (coverage artefacts land wherever the command ran), and
#: the landing page polls its state several times a minute.
PRESENCE_TTL_SECONDS = 5.0

_presence_cache: dict[str, tuple[float, bool]] = {}


def build_cov_payload(
    project_root: str | os.PathLike,
    *,
    cov_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> dict:
    """The newest run's coverage model + manifest, as one JSON body.

    The body is :func:`rtl_buddy.cov.query.detail_payload` — run block,
    totals, counts, per-test rows, per-file rows *with their points*,
    module index, ``artefacts`` and any SVA ``covers`` — plus a ``hub``
    block carrying what the page needs to render its chrome without a
    second round-trip (schema version, the routes it fetches from, the
    metric order, and the manifest/model paths).

    Raises :class:`~rtl_buddy.cov.query.CovQueryError` when there is no
    coverage to serve; its message already names the command that
    produces some, which is the actionable half of the 404.
    """

    ctx = cov_query.load_context(project_root, cov_dir=cov_dir, manifest=manifest)
    payload = cov_query.detail_payload(ctx)
    payload["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "model": manifest_mod.project_relative(ctx.model_path, ctx.project_root),
        "model_schema_version": ctx.model.get("schema_version"),
        "generator": ctx.model.get("generator"),
        # Ordered, not sorted: this IS the left-to-right metric order the
        # dashboard and the per-file table render in.
        "metrics": list(METRICS),
        "source_route": COV_SOURCE_ROUTE,
    }
    return payload


def cov_payload_bytes(
    project_root: str | os.PathLike,
    *,
    cov_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /cov.json``.

    No coverage is a 404 with a JSON ``error`` naming the command that
    makes some — the same shape ``GET /graph.json`` returns — rather
    than an exception escaping into the websockets layer's opaque
    failure body.
    """

    try:
        payload = build_cov_payload(project_root, cov_dir=cov_dir, manifest=manifest)
    except cov_query.CovQueryError as exc:
        log_event(
            logger,
            logging.WARNING,
            "hub.cov_page.unavailable",
            error=str(exc),
        )
        return 404, json.dumps({"error": str(exc)}).encode("utf-8")
    return 200, json.dumps(payload).encode("utf-8")


def read_source_lines(
    project_root: str | os.PathLike, requested: str
) -> tuple[int, bytes]:
    """``(status, body)`` for ``GET /cov/source?path=<project-relative>``.

    ``path`` is resolved **under the project root and nowhere else**.
    The coverage model's paths are project-relative by construction, but
    this route takes its argument from a query string, so containment is
    checked rather than assumed: a pane is a browser tab, and a browser
    tab is reachable by anything that can reach the port.

    The body is ``{"path", "lines"}`` — the file split into lines, with
    line ``N`` at index ``N-1``, which is the shape the annotation
    renders against.
    """

    root = Path(project_root).resolve()
    if not requested:
        return 400, json.dumps({"error": "cov: source needs a ?path="}).encode("utf-8")
    candidate = Path(requested)
    target = (root / candidate).resolve() if not candidate.is_absolute() else candidate
    try:
        target.relative_to(root)
    except ValueError:
        return 403, json.dumps(
            {"error": f"cov: {requested} is outside the project root"}
        ).encode("utf-8")
    if not target.is_file():
        return 404, json.dumps({"error": f"cov: no source file at {requested}"}).encode(
            "utf-8"
        )
    try:
        size = target.stat().st_size
        if size > MAX_SOURCE_BYTES:
            return 413, json.dumps(
                {
                    "error": (
                        f"cov: {requested} is {size} bytes, over the "
                        f"{MAX_SOURCE_BYTES}-byte annotation limit"
                    )
                }
            ).encode("utf-8")
        # Simulator-recorded sources are occasionally latin-1 or worse;
        # a mojibake line beats a pane that refuses to open the file.
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return 404, json.dumps(
            {"error": f"cov: cannot read {requested}: {exc}"}
        ).encode("utf-8")
    return 200, json.dumps({"path": requested, "lines": text.splitlines()}).encode(
        "utf-8"
    )


def cov_data_present(
    project_root: str | os.PathLike | None, *, ttl: float = PRESENCE_TTL_SECONDS
) -> bool:
    """Whether any run under this root left a coverage manifest.

    Cached for :data:`PRESENCE_TTL_SECONDS`: the answer is used by the
    landing page's poll and by the SPA injection, both of which ask
    repeatedly, and unlike the graph's fixed path this one costs a walk.
    A TTL rather than an invalidation hook because nothing in the hub
    observes the filesystem — a coverage run finished in another
    terminal must show up on its own, and five seconds late is not late.
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


def render_cov_html(*, hub_addr: str, cov_url: str = COV_JSON_ROUTE) -> bytes:
    """The ``GET /cov`` document, with the hub address injected.

    Everything is inline. The page must work on a machine with no route
    off localhost, so there is no CDN reference, no web font and no
    external stylesheet anywhere in it — only same-origin hub routes.
    """

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_COV_URL__ = {cov_url!r};\n"
        f"window.__RTL_BUDDY_COV_SOURCE_URL__ = {COV_SOURCE_ROUTE!r};"
    )
    return COV_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _cov_page_template() -> str:
    """Read the page template that ships beside this module."""

    return (Path(__file__).parent / "cov_page.html").read_text(encoding="utf-8")


COV_PAGE_HTML: str = _cov_page_template()
"""The page source, loaded once at import — same rule as the graph pane.

Kept in a sibling ``.html`` file rather than a Python string so an editor
treats it as HTML and the JS inside it stays reviewable; the wheel ships
it via hatchling's package data (it lives under ``src/rtl_buddy/``)."""


__all__ = [
    "COV_JSON_ROUTE",
    "COV_PAGE_HTML",
    "COV_PAGE_ROUTE",
    "COV_SOURCE_ROUTE",
    "MAX_SOURCE_BYTES",
    "PAGE_SCHEMA_VERSION",
    "PRESENCE_TTL_SECONDS",
    "build_cov_payload",
    "cov_data_present",
    "cov_payload_bytes",
    "read_source_lines",
    "render_cov_html",
]
