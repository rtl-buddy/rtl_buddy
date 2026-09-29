"""On-disk ledger layout for ``rb xplr``: one ``artefacts/xplr/<exp-id>/record.json`` per experiment.

Functions take the ledger root explicitly; :func:`ledger_root` derives it from an execution context.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..exec_context import ExecutionContext
from ..logging_utils import log_event
from .schema import ExperimentRecord, dumps_record, loads_record


logger = logging.getLogger(__name__)

LEDGER_DIRNAME = "xplr"
# Defined in `tools.artifact_paths` so the artefact-clearing helpers can protect it.
from ..tools.artifact_paths import (  # noqa: E402
    XPLR_RECORD_NAME as RECORD_FILENAME,
)

# Non-experiment directories under the ledger root.
RESERVED_DIRNAMES = ("worktrees",)

_AUTO_ID_RE = re.compile(r"^exp-(\d{4,})$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ledger_root(exec_ctx: ExecutionContext) -> Path:
    """Return the ledger root (``artefacts/xplr``)."""

    return exec_ctx.artifact_dir(LEDGER_DIRNAME)


def record_path(root: Path, exp_id: str) -> Path:
    """Return ``<root>/<exp_id>/record.json``; raises on an invalid id."""

    _check_id(exp_id)
    return root / exp_id / RECORD_FILENAME


def next_id(root: Path) -> str:
    """Return the next experiment id (``exp-NNNN``).

    Counts every ``exp-NNNN`` directory, with or without a record.
    """

    highest = 0
    if root.is_dir():
        for entry in root.iterdir():
            match = _AUTO_ID_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"exp-{highest + 1:04d}"


def write_record(root: Path, record: ExperimentRecord) -> Path:
    """Validate ``record`` and write it atomically to ``<root>/<id>/record.json``.

    Returns the record path.
    """

    path = record_path(root, record.id)
    text = dumps_record(record)
    # dumps_record does not validate; re-parse to reject an invalid record before writing.
    loads_record(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{RECORD_FILENAME}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    log_event(logger, logging.DEBUG, "xplr.record_written", id=record.id, path=path)
    return path


def read_record(root: Path, exp_id: str) -> ExperimentRecord:
    """Load and validate ``<root>/<exp_id>/record.json``.

    Raises :class:`FatalRtlBuddyError` if the record is missing, malformed or invalid.
    """

    path = record_path(root, exp_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FatalRtlBuddyError(
            f"experiment '{exp_id}' has no record at {path}"
        ) from None
    try:
        return loads_record(text)
    except FatalRtlBuddyError as exc:
        raise FatalRtlBuddyError(f"{path}: {exc}") from exc


def list_records(root: Path) -> list[ExperimentRecord]:
    """Return every record under ``root``, sorted by id.

    Directories without a ``record.json`` are skipped with a warning; an invalid record raises.
    """

    if not root.is_dir():
        return []
    records: list[ExperimentRecord] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name in RESERVED_DIRNAMES:
            continue
        if not (entry / RECORD_FILENAME).is_file():
            log_event(
                logger,
                logging.WARNING,
                "xplr.record_missing",
                id=entry.name,
                path=entry,
            )
            continue
        records.append(read_record(root, entry.name))
    return records


def _check_id(exp_id: str) -> None:
    if not _ID_RE.match(exp_id):
        raise FatalRtlBuddyError(
            f"invalid experiment id {exp_id!r}: must match {_ID_RE.pattern}"
        )
