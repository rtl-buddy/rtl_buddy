# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Hub-served coverage pane: the ``GET /cov.json`` payload, ``GET /cov/source`` file text and the ``GET /cov`` page.

The page registers with the hub as ``origin=cov`` and is one self-contained document with no external resources.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..cov import manifest as manifest_mod
from ..cov import model as model_mod
from ..cov import query as cov_query
from ..cov.raw import METRICS
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


#: Version of the ``hub`` block in ``GET /cov.json``, separate from the coverage model's ``schema_version``.
PAGE_SCHEMA_VERSION = 1

#: Route serving the run's coverage model.
COV_JSON_ROUTE = "/cov.json"

#: Route serving the interactive page.
COV_PAGE_ROUTE = "/cov"

#: Route serving one annotated file's source text.
COV_SOURCE_ROUTE = "/cov/source"

#: Largest source file, in bytes, that ``/cov/source`` serves.
MAX_SOURCE_BYTES = 4 * 1024 * 1024

#: Seconds :func:`cov_data_present` reuses a previous answer; discovery walks the project tree.
PRESENCE_TTL_SECONDS = 5.0

_presence_cache: dict[str, tuple[float, bool]] = {}

#: ``model path -> ((mtime_ns, size), project-relative source paths)``; the model parse is cached per file stamp.
_file_set_cache: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


def build_cov_payload(
    project_root: str | os.PathLike,
    *,
    cov_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> dict:
    """Return the newest run's coverage model and manifest as one JSON-ready dict.

    It is :func:`rtl_buddy.cov.query.detail_payload` (the same data ``rb cov`` reports) plus a ``hub`` block with the schema version, metric order, model path and source route. Raises :class:`~rtl_buddy.cov.query.CovQueryError` when there is no coverage.
    """

    ctx = cov_query.load_context(project_root, cov_dir=cov_dir, manifest=manifest)
    payload = cov_query.detail_payload(ctx)
    payload["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "model": manifest_mod.project_relative(ctx.model_path, ctx.project_root),
        "model_schema_version": ctx.model.get("schema_version"),
        "generator": ctx.model.get("generator"),
        # Order is the left-to-right metric order of the page.
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
    """Return ``(status, body)`` for ``GET /cov.json``; no coverage gives a 404 with a JSON ``error``."""

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


def model_file_set(
    project_root: str | os.PathLike,
    *,
    cov_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
) -> frozenset[str]:
    """Return the project-relative source paths named by the newest run's model.

    These are the only files ``/cov/source`` serves. Empty when no readable coverage exists. The manifest is looked up on every call, and the model parse is cached by file stamp.
    """

    try:
        manifest_path = cov_query.resolve_manifest_path(
            project_root, cov_dir=cov_dir, manifest=manifest
        )
        document = manifest_mod.load_manifest(manifest_path)
        model_path = manifest_mod.resolve(manifest_path, document.get("model"))
        if model_path is None:
            return frozenset()
        stat = os.stat(model_path)
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _file_set_cache.get(model_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        model = model_mod.load_model(model_path)
    except (cov_query.CovQueryError, OSError, ValueError) as exc:
        log_event(
            logger,
            logging.DEBUG,
            "hub.cov_page.no_model_file_set",
            error=str(exc),
        )
        return frozenset()
    paths = frozenset(
        row["path"] for row in model.get("files") or [] if row.get("path")
    )
    _file_set_cache[model_path] = (stamp, paths)
    return paths


def read_source_lines(
    project_root: str | os.PathLike, requested: str
) -> tuple[int, bytes]:
    """Return ``(status, body)`` for ``GET /cov/source?path=<project-relative>``.

    The path must be listed by the coverage model and resolve under the project root; otherwise the status is 400, 403, 404 or 413 with a JSON ``error``. On success the body is ``{"path", "lines"}``, with line ``N`` at index ``N-1``.
    """

    root = Path(project_root).resolve()
    if not requested:
        return 400, json.dumps({"error": "cov: source needs a ?path="}).encode("utf-8")
    # Resolve before the containment check: relative_to is lexical, so an unresolved ``<root>/../x`` would pass.
    target = (root / Path(requested)).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError:
        return 403, json.dumps(
            {"error": f"cov: {requested} is outside the project root"}
        ).encode("utf-8")
    # Model paths are project-relative posix; compare in that form.
    if relative.as_posix() not in model_file_set(project_root):
        # Refused before touching the filesystem so existence of unlisted paths is not revealed.
        return 403, json.dumps(
            {"error": f"cov: {requested} is not in this run's coverage model"}
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
        # Sources may not be UTF-8; replace bad bytes rather than refuse the file.
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
    """Return whether any run under the root left a coverage manifest, cached for ``ttl`` seconds."""

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
    """Return the ``GET /cov`` document with the hub address and data routes injected."""

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_COV_URL__ = {cov_url!r};\n"
        f"window.__RTL_BUDDY_COV_SOURCE_URL__ = {COV_SOURCE_ROUTE!r};"
    )
    return COV_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _cov_page_template() -> str:
    """Read ``cov_page.html`` from beside this module."""

    return (Path(__file__).parent / "cov_page.html").read_text(encoding="utf-8")


COV_PAGE_HTML: str = _cov_page_template()
"""The page source, loaded once at import."""


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
    "model_file_set",
    "read_source_lines",
    "render_cov_html",
]
