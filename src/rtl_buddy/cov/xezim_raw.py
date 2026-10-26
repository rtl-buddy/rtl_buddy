# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Reader for xezim's coverage file (``xezim_cov.json``, kept per test as ``coverage.json``).

xezim writes one JSON document per run. The parts this reader uses:

- ``code_coverage.design_units[]``: one entry per module, instances already folded,
  with ``statements[] {file, line, kind, count}``, ``branches[] {file, line, kind,
  arms[] {line, arm, count}}`` and ``toggles[] {signal, width, rise[], fall[]}``.
  ``code_coverage`` is absent when the run had no ``--code-coverage``.
- ``assertions[] {file, line, span_start, kind, pass, fail}``: ``kind == "cover"``
  entries are cover points, ``pass`` their hit count. The file carries no label.

Records have the shape :func:`rtl_buddy.cov.raw.parse_raw_records` returns, so the
coverage model treats both simulators alike:

- line: one point per statement. xezim has no columns, so ``column`` is the
  statement's ordinal among the design unit's statements on the same line and
  ``name`` is its kind (``always_ff``, ``statement``, ...).
- branch: one point per arm, on the arm's line; ``column`` is the ordinal among the
  design unit's arms on that line and ``name`` the arm label (``if``, ``else``, ...).
- toggle: one point per bit, ``name`` ``sig[i]``. The model has a single hit count,
  so a bit counts as hit only when it rose and fell (``hits = min(rise, fall)``).
  Toggles carry no source position: they are filed under the design unit's source
  file (its first statement's) at line 0, and dropped when the unit has no statements.
- cover: ``name`` is the statement label read back from the source line
  (``label: cover ...``), else ``cover@<line>``.

xezim has no expression coverage, so no expression records are produced.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ..logging_utils import log_event
from .raw import BRANCH, COVER, LINE, TOGGLE

logger = logging.getLogger(__name__)

#: Extension of a raw xezim coverage file; the model picks this reader by it.
XEZIM_RAW_SUFFIX = ".json"

_COVER_LABEL_RE = re.compile(r"^\s*(\w+)\s*:\s*cover\b")


def is_xezim_raw(path) -> bool:
    """Whether ``path`` names a xezim coverage file (by extension)."""
    return path is not None and str(path).endswith(XEZIM_RAW_SUFFIX)


def load_xezim_doc(path) -> dict | None:
    """Read a xezim coverage document, or None when it is unreadable or not an object."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def design_units(doc: dict) -> list[dict]:
    """The document's per-module entries; empty without code coverage."""
    code = doc.get("code_coverage") or {}
    return list(code.get("design_units") or [])


def unit_source_file(unit: dict) -> str | None:
    """A design unit's source file: its first statement's, else its first branch's."""
    for key in ("statements", "branches"):
        for item in unit.get(key) or []:
            if item.get("file"):
                return item["file"]
    return None


class _LabelReader:
    """Reads cover labels from source lines, caching each file once."""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self._lines: dict[str, list[str] | None] = {}

    def label(self, file: str | None, line: int | None) -> str | None:
        if not file or not line:
            return None
        lines = self._lines.get(file, False)
        if lines is False:
            path = file if os.path.isabs(file) else os.path.join(self.base_dir, file)
            try:
                lines = Path(path).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                lines = None
            self._lines[file] = lines
        if lines is None or line > len(lines):
            return None
        match = _COVER_LABEL_RE.match(lines[line - 1])
        return None if match is None else match.group(1)


def _record(metric, *, type_, name, file, line, column, module, hits):
    return {
        "metric": metric,
        "type": type_,
        "name": name,
        "file": file,
        "line": line,
        "column": column,
        "module": module,
        "hier": None,
        "hits": hits,
    }


def _cover_module(units: list[dict], file: str | None, line: int | None):
    """The design unit a cover at ``file:line`` most likely sits in, or None.

    The unit with a statement in ``file`` whose first such statement starts closest
    before ``line``; the only unit in that file when none starts before it.
    """
    starts = []
    for unit in units:
        lines = [
            s.get("line")
            for s in unit.get("statements") or []
            if s.get("file") == file and isinstance(s.get("line"), int)
        ]
        if lines:
            starts.append((min(lines), unit.get("design_unit")))
    if not starts:
        return None
    before = [entry for entry in starts if line is not None and entry[0] <= line]
    if before:
        return max(before)[1]
    return starts[0][1] if len(starts) == 1 else None


def records_from_doc(doc: dict, *, base_dir, raw_path=None) -> list[dict]:
    """Coverage records from a parsed xezim document; ``base_dir`` resolves relative source paths for label recovery."""
    records: list[dict] = []
    units = design_units(doc)
    for unit in units:
        module = unit.get("design_unit")
        line_ordinals: dict[tuple, int] = {}
        for stmt in unit.get("statements") or []:
            key = (stmt.get("file"), stmt.get("line"))
            column = line_ordinals.get(key, 0)
            line_ordinals[key] = column + 1
            records.append(
                _record(
                    LINE,
                    type_="line",
                    name=stmt.get("kind"),
                    file=stmt.get("file"),
                    line=stmt.get("line"),
                    column=column,
                    module=module,
                    hits=int(stmt.get("count") or 0),
                )
            )
        arm_ordinals: dict[tuple, int] = {}
        for branch in unit.get("branches") or []:
            file = branch.get("file")
            for arm in branch.get("arms") or []:
                line = arm.get("line", branch.get("line"))
                key = (file, line)
                column = arm_ordinals.get(key, 0)
                arm_ordinals[key] = column + 1
                records.append(
                    _record(
                        BRANCH,
                        type_="branch",
                        name=arm.get("arm"),
                        file=file,
                        line=line,
                        column=column,
                        module=module,
                        hits=int(arm.get("count") or 0),
                    )
                )
        toggles = unit.get("toggles") or []
        if not toggles:
            continue
        unit_file = unit_source_file(unit)
        if unit_file is None:
            log_event(
                logger,
                logging.DEBUG,
                "coverage.xezim.toggles_unplaced",
                raw_path=raw_path,
                module=module,
                signals=len(toggles),
            )
            continue
        for toggle in toggles:
            signal = toggle.get("signal")
            rise = toggle.get("rise") or []
            fall = toggle.get("fall") or []
            width = int(toggle.get("width") or max(len(rise), len(fall)))
            for bit in range(width):
                up = rise[bit] if bit < len(rise) else 0
                down = fall[bit] if bit < len(fall) else 0
                records.append(
                    _record(
                        TOGGLE,
                        type_="toggle",
                        name=f"{signal}[{bit}]",
                        file=unit_file,
                        line=0,
                        column=None,
                        module=module,
                        hits=int(min(up, down)),
                    )
                )
    labels = _LabelReader(base_dir)
    for assertion in doc.get("assertions") or []:
        if assertion.get("kind") != "cover":
            continue
        file = assertion.get("file")
        line = assertion.get("line")
        name = labels.label(file, line) or f"cover@{line}"
        records.append(
            _record(
                COVER,
                type_="cover",
                name=name,
                file=file,
                line=line,
                column=None,
                module=_cover_module(units, file, line),
                hits=int(assertion.get("pass") or 0),
            )
        )
    return records


def parse_xezim_records(raw_path, *, metrics=None) -> list[dict] | None:
    """Parse every coverage point out of a xezim coverage file.

    Returns one dict per point, ``{metric, type, name, file, line, column, module,
    hier, hits}`` as :func:`rtl_buddy.cov.raw.parse_raw_records` does, or None when the
    file cannot be read. ``metrics`` restricts the result to those canonical names.
    """
    doc = load_xezim_doc(raw_path)
    if doc is None:
        return None
    records = records_from_doc(
        doc, base_dir=os.path.dirname(os.path.abspath(raw_path)), raw_path=raw_path
    )
    if metrics is not None:
        wanted = set(metrics)
        records = [r for r in records if r["metric"] in wanted]
    return records
