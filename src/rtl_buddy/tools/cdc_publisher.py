"""Publish ``rb cdc`` JSON-report findings to the rtl-buddy-hub.

When a hub is running for the current project, every ``rb cdc``
run pushes its violation list as a ``diagnostics_set`` event so the
SPA's on-canvas badge layer (rtl-buddy-view#82) and nvim's
``rtlbuddy`` diagnostics namespace (rtl-buddy-nvim main) light up
the findings immediately — no manual ``rb hub send diagnose``
copy-paste.

The publisher is best-effort by design: missing hub, no live PID,
connect failure, or a malformed JSON payload all silently no-op
with a debug-level log line. ``rb cdc`` is a tool for CI as well
as interactive use; failing the analysis because a sidecar UI is
unreachable would be the wrong tradeoff.

Source-key convention:

    ``rb-cdc:<analysis_name>``

…one cache slot per analysis. Re-running an analysis after a fix
naturally replaces (or clears) just that slot, so a project with
several analyses doesn't have one fix wiping all the others.

Wire mapping (rtl-buddy-cdc JSON → ``diagnostics_set`` items):

    rule_id        → code
    severity       → severity                 (verbatim — same enum)
    message        → message
    instance_path  → instance_path            (list-of-segments → "." join)
    location.file  → file
    location.start_line  → line
    location.start_column → col (default 1)

Items missing ``location.file`` are dropped — the wire requires
``file`` non-empty and the SPA can't anchor them to a node anyway.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..hub.client import HubClient, HubClientError, HubUnavailable
from ..hub.protocol import Origin
from ..logging_utils import log_event


logger = logging.getLogger(__name__)


def _wire_item(v: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one rtl-buddy-cdc violation entry into a
    ``diagnostics_set`` item dict, or ``None`` if the entry is too
    incomplete to be useful (missing file or message)."""

    severity = v.get("severity")
    message = v.get("message")
    if not isinstance(severity, str) or severity not in {
        "error",
        "warning",
        "info",
        "hint",
    }:
        return None
    if not isinstance(message, str) or not message:
        return None

    loc = v.get("location")
    if not isinstance(loc, dict):
        return None
    file = loc.get("file")
    if not isinstance(file, str) or not file:
        return None
    line = loc.get("start_line")
    if not isinstance(line, int) or line < 1:
        return None

    item: dict[str, Any] = {
        "file": file,
        "line": line,
        "severity": severity,
        "message": message,
    }
    col = loc.get("start_column")
    if isinstance(col, int) and col >= 1:
        item["col"] = col
    end_line = loc.get("end_line")
    if isinstance(end_line, int) and end_line >= 1:
        item["end_line"] = end_line
    end_col = loc.get("end_column")
    if isinstance(end_col, int) and end_col >= 1:
        item["end_col"] = end_col

    rule_id = v.get("rule_id")
    if isinstance(rule_id, str) and rule_id:
        item["code"] = rule_id

    instance_path = v.get("instance_path")
    if (
        isinstance(instance_path, list)
        and instance_path
        and all(isinstance(p, str) and p for p in instance_path)
    ):
        item["instance_path"] = ".".join(instance_path)

    return item


def build_items_from_cdc_report(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a parsed rtl-buddy-cdc JSON report and return the wire
    items for its ``violations`` list. Suppressed and baseline-
    carryover findings are intentionally excluded — they don't drive
    the exit code and a noisy badge layer hurts more than it helps.
    """
    violations = payload.get("violations")
    if not isinstance(violations, list):
        return []
    items: list[dict[str, Any]] = []
    for v in violations:
        if not isinstance(v, dict):
            continue
        wire = _wire_item(v)
        if wire is not None:
            items.append(wire)
    return items


def publish_cdc_report(
    *,
    analysis_name: str,
    json_report_path: str | Path,
    project_root: Path | None = None,
) -> bool:
    """Read the JSON report at ``json_report_path`` and push its
    violations to the running hub as a ``diagnostics_set`` event.

    Returns ``True`` when something was published, ``False`` when the
    hub is unavailable or the report can't be parsed. Never raises —
    the call site treats this as a best-effort side effect.

    ``analysis_name`` is the rtl-buddy-cdc analysis name (e.g.
    ``ip_dma_lint``); the wire ``source`` is ``rb-cdc:<analysis_name>``
    so concurrent analyses cache independently.
    """

    import json as _json

    path = Path(json_report_path)
    if not path.is_file():
        log_event(
            logger,
            logging.DEBUG,
            "cdc.publish.no_report",
            path=str(path),
            analysis=analysis_name,
        )
        return False
    try:
        payload = _json.loads(path.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        log_event(
            logger,
            logging.DEBUG,
            "cdc.publish.bad_report",
            path=str(path),
            analysis=analysis_name,
            error=str(exc),
        )
        return False

    items = build_items_from_cdc_report(payload)

    source = f"rb-cdc:{analysis_name}"
    try:
        client = HubClient.connect(
            project_root=project_root, origin=Origin.CLI, client_version="rb-cdc"
        )
    except HubUnavailable:
        log_event(
            logger,
            logging.DEBUG,
            "cdc.publish.no_hub",
            analysis=analysis_name,
        )
        return False
    except HubClientError as exc:
        log_event(
            logger,
            logging.DEBUG,
            "cdc.publish.connect_failed",
            analysis=analysis_name,
            error=str(exc),
        )
        return False

    try:
        client.emit("diagnostics_set", {"source": source, "items": items})
    finally:
        client.close()

    log_event(
        logger,
        logging.INFO,
        "cdc.publish.ok",
        analysis=analysis_name,
        source=source,
        items=len(items),
    )
    return True


__all__ = [
    "build_items_from_cdc_report",
    "publish_cdc_report",
]
