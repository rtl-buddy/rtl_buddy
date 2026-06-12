"""Ledger layout primitives for ``rb xplr`` — no CLI surface.

The ledger is one directory per experiment under the project's artefact
tree::

    artefacts/xplr/<exp-id>/record.json

``record.json`` is the canonical P0 experiment record (see
:mod:`rtl_buddy.xplr.schema`); later phases add per-experiment flow
artifacts next to it. Experiment ids auto-increment: ``exp-0001``,
``exp-0002``, ... — :func:`next_id` scans existing directory names so
ids stay unique even if a run died before writing its record.

All functions take the ledger root :class:`~pathlib.Path` explicitly;
:func:`ledger_root` derives it from an
:class:`~rtl_buddy.exec_context.ExecutionContext` using the standard
``artifact_dir`` convention.
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
RECORD_FILENAME = "record.json"
# Non-experiment dirs that legitimately live under the ledger root: the
# default cfg-xplr worktree-root is artefacts/xplr/worktrees/ (P2).
RESERVED_DIRNAMES = ("worktrees",)

_AUTO_ID_RE = re.compile(r"^exp-(\d{4,})$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ledger_root(exec_ctx: ExecutionContext) -> Path:
    """Return the ledger root (``artefacts/xplr``) for an execution context."""

    return exec_ctx.artifact_dir(LEDGER_DIRNAME)


def record_path(root: Path, exp_id: str) -> Path:
    """Return ``<root>/<exp_id>/record.json`` (validates the id shape)."""

    _check_id(exp_id)
    return root / exp_id / RECORD_FILENAME


def next_id(root: Path) -> str:
    """Return the next auto-increment experiment id (``exp-NNNN``).

    Scans every entry under ``root`` matching ``exp-NNNN`` — with or
    without a ``record.json`` — so an experiment directory created by a
    crashed run still reserves its number.
    """

    highest = 0
    if root.is_dir():
        for entry in root.iterdir():
            match = _AUTO_ID_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"exp-{highest + 1:04d}"


def write_record(root: Path, record: ExperimentRecord) -> Path:
    """Validate + write ``record`` to ``<root>/<id>/record.json`` atomically.

    The canonical serialization is written to a same-directory temp file
    then ``os.replace``d into place, so readers never observe a partial
    record. Returns the record path.
    """

    path = record_path(root, record.id)
    text = dumps_record(record)
    # dumps_record serializes without validating; re-parse so a record
    # mutated into an invalid state fails loudly before touching disk.
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
    """Load + validate ``<root>/<exp_id>/record.json``.

    Raises :class:`FatalRtlBuddyError` if the record is missing,
    malformed JSON, or fails schema validation.
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
    """Return every valid record under ``root``, sorted by experiment id.

    Experiment directories without a ``record.json`` (e.g. from a
    crashed run) are skipped with a warning rather than failing the
    whole listing; an invalid record still raises, since it means the
    ledger contract was broken.
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
