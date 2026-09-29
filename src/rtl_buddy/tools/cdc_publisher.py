"""Publish ``rb cdc`` JSON-report violations to a running hub as a ``diagnostics_set`` event.

Publishing is best-effort: a missing hub, a connect failure or an unreadable report is
logged at debug level and never fails the analysis. The event source is
``rb-cdc:<analysis_name>``, so each analysis keeps its own diagnostics slot.

Report fields map to items as: ``rule_id`` -> ``code``, ``severity``, ``message``,
``instance_path`` (segments joined with ``.``), ``location.file`` -> ``file``,
``location.start_line`` -> ``line``, ``location.start_column`` -> ``col``.
Violations without a file, line, valid severity or message are dropped.
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
    """Translate one violation into a ``diagnostics_set`` item, or ``None`` if required fields are missing."""

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
    """Return the items for a parsed report's ``violations`` list.

    Suppressed and baseline-carryover findings are not in that list and are not published.
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
    """Push the report's violations to the running hub.

    Returns ``True`` when published, ``False`` when the hub is unavailable or the
    report cannot be read. Never raises.
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
