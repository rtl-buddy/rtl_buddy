# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Hub-served synth and power pane: the JSON body, the run-selection guard and the HTML page.

``GET /phy.json`` returns one run's model and manifest, built by the same :func:`rtl_buddy.phys.query.summary_payload` that ``rb phys summary`` uses. ``?dir=<project-relative phys_dir>`` selects a run; without it the newest run is served. ``GET /phy`` serves a self-contained page that registers as ``origin=phys``. Clicking a module emits ``graph_focus``, clicking an instance emits ``selection_changed``, and ``rb hub send phys-focus`` drives the page from the other side. ``phys_focus`` names no run; it applies to the run on screen.
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


#: Version of the ``hub`` block in ``GET /phy.json``, separate from the physical model's ``schema_version``.
PAGE_SCHEMA_VERSION = 1

#: Route serving the run's physical model.
PHYS_JSON_ROUTE = "/phy.json"

#: Query parameter selecting a run; its value is a ``phys_dir`` from the payload's ``runs`` block.
PHYS_DIR_PARAM = "dir"

#: Newest-first cap on the payload's ``runs`` block, which also carries the untruncated count.
RUNS_LIMIT = 50

#: Route serving the interactive page.
PHYS_PAGE_ROUTE = "/phy"

#: Metric switcher order, matching the ``phys_focus.metric`` enum. ``dynamic`` is internal plus switching power, summed by the page.
METRICS = ("cells", "area", "leakage", "dynamic", "total")

#: Seconds :func:`phys_data_present` reuses an answer, because discovery walks the project tree.
PRESENCE_TTL_SECONDS = 5.0

_presence_cache: dict[str, tuple[float, bool]] = {}


def build_phys_payload(
    project_root: str | os.PathLike,
    *,
    phys_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
    runs_limit: int | None = RUNS_LIMIT,
) -> dict:
    """Return one run's physical model and manifest as a JSON-ready dict.

    The dict is :func:`rtl_buddy.phys.query.summary_payload` at ``limit=0`` (complete module and instance rankings), plus a ``runs`` block (the ``rb phys runs`` payload capped at ``runs_limit``) and a ``hub`` block with the page's chrome data. With no ``phys_dir`` or ``manifest``, the newest run is used.

    Raises :class:`~rtl_buddy.phys.query.PhysQueryError` when there is nothing to serve; its message names the commands that produce artefacts.
    """

    runs = phys_query.runs_payload(project_root, limit=runs_limit)
    if phys_dir is None and manifest is None and runs["runs"]:
        # Reuses the listing's walk so load_context does not walk the tree again.
        phys_dir = os.path.join(str(project_root), runs["runs"][0]["phys_dir"])
    ctx = phys_query.load_context(project_root, phys_dir=phys_dir, manifest=manifest)
    payload = phys_query.summary_payload(ctx, limit=0)
    payload["runs"] = runs
    payload["hub"] = {
        "schema_version": PAGE_SCHEMA_VERSION,
        "model": manifest_mod.project_relative(ctx.model_path, ctx.project_root),
        "model_schema_version": ctx.model.get("schema_version"),
        "generator": ctx.model.get("generator"),
        # Identifies which publish wrote the model; the page compares it across reloads. None for unstamped documents.
        "publication": ctx.model.get("publication"),
        # Order is the metric switcher's left-to-right order.
        "metrics": list(METRICS),
        # Order is the instance table's power column order.
        "power_columns": list(phys_query.POWER_COLUMNS),
    }
    return payload


def contained_phys_dir(project_root: str | os.PathLike, requested: str):
    """Return the absolute artefact directory named by ``?dir=``, or ``None`` if it must be refused.

    A path is accepted when it stays under the project root lexically (``..`` collapsed, symlinks not resolved), or when it resolves to a location under the root. Every symlink crossed below the root must pass :func:`rtl_buddy.phys.manifest.may_follow_link`, the rule ``discover_manifests`` uses, so the route serves exactly the runs ``rb phys runs`` lists. A ``vendor/`` symlink to another tree is refused even when the requested path contains an ``artefacts`` component.
    """
    logical_root = Path(os.path.abspath(str(project_root)))
    logical = Path(os.path.abspath(os.path.join(logical_root, str(requested))))
    root_real = os.path.realpath(str(project_root))
    if _under(logical, logical_root):
        return logical if _links_crossed_are_followable(logical, logical_root) else None
    # Outside lexically but resolves back under the root: no layout position to check.
    if _under(Path(os.path.realpath(logical)), Path(root_real)):
        return logical
    return None


def _links_crossed_are_followable(logical: Path, logical_root: Path) -> bool:
    """Return whether every symlink from the root down to ``logical`` may be followed.

    Each link is judged at its own position below the root, in the order ``discover_manifests`` descends.
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
    """Return whether ``path`` is ``root`` or below it."""
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
    """Return ``(status, body)`` for ``GET /phy.json``.

    ``requested_dir`` is the route's ``?dir=``. A directory refused by :func:`contained_phys_dir` gives 403; a directory with no manifest, or a project with no physical artefacts, gives 404 with a JSON ``error`` naming what to run.
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
    """Return whether any run under the root left a physical manifest.

    The answer is cached for ``ttl`` seconds.
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
    """Return the ``GET /phy`` document with the hub address and JSON URL injected.

    The page loads nothing from outside the hub.
    """

    preamble = (
        f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};\n"
        f"window.__RTL_BUDDY_PHY_URL__ = {phys_url!r};"
    )
    return PHYS_PAGE_HTML.replace("%HUB_INJECTION%", preamble).encode("utf-8")


def _phys_page_template() -> str:
    """Read ``phys_page.html`` from beside this module."""

    return (Path(__file__).parent / "phys_page.html").read_text(encoding="utf-8")


PHYS_PAGE_HTML: str = _phys_page_template()
"""The page source, read once at import."""


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
