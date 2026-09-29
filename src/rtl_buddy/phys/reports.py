# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Readers for Yosys ``stat -json`` and OpenSTA power reports, producing rows for :mod:`rtl_buddy.phys.model`.

Rows that do not parse are dropped rather than raising.
"""

from __future__ import annotations

import json
import re


def parse_stat_json(text: str) -> list[dict]:
    """Per-module rows from Yosys' ``stat -json`` dump.

    Reads the ``modules`` map, whose keys carry RTLIL's leading backslash. The
    whole-design ``design`` block is not returned. In a non-flattened design
    ``num_cells`` counts a submodule instance as one cell but ``area`` includes
    the submodule, so summing ``area_um2`` across modules double-counts.

    :returns: ``[{"module", "cell_count", "area_um2"}]`` sorted by module name.
        ``area_um2`` is ``None`` when ``stat`` had no Liberty.
    """
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return []
    modules = doc.get("modules") if isinstance(doc, dict) else None
    if not isinstance(modules, dict):
        return []

    rows = []
    for raw_name, stats in modules.items():
        if not isinstance(stats, dict):
            continue
        rows.append(
            {
                "module": _rtlil_name(raw_name),
                "cell_count": _as_int(stats.get("num_cells")),
                "area_um2": _as_float(stats.get("area")),
            }
        )
    return sorted(rows, key=lambda row: row["module"])


def _rtlil_name(name: str) -> str:
    """Strip RTLIL's public-identifier marker: ``\\top`` -> ``top``."""
    text = str(name)
    return text[1:] if text.startswith("\\") else text


#: One instance row of OpenSTA's `report_power -instances`: four powers in
#: watts, then the instance path.
#:
#:     2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_
_INSTANCE_ROW_RE = re.compile(
    r"^\s*"
    r"([-\d.eE+]+)\s+"  # internal
    r"([-\d.eE+]+)\s+"  # switching
    r"([-\d.eE+]+)\s+"  # leakage
    r"([-\d.eE+]+)\s+"  # total
    r"(\S+)\s*$"
)

_W_TO_UW = 1e6


def parse_instance_power(text: str, cells: dict | None = None) -> list[dict]:
    """Per-instance rows from OpenSTA's ``report_power -instances`` text.

    ``cells`` maps instance path to liberty cell (:func:`parse_instance_cells`);
    an instance missing from it gets ``module: None``. Rows are sorted by
    instance path.

    :returns: ``[{"instance_path", "module", "leakage_uw",
        "internal_uw", "switching_uw", "total_uw"}]``, powers in µW.
    """
    cells = cells or {}
    rows = []
    for line in text.splitlines():
        match = _INSTANCE_ROW_RE.match(line)
        if match is None:
            continue
        try:
            internal, switching, leakage, total = (
                float(match.group(index)) for index in (1, 2, 3, 4)
            )
        except ValueError:  # pragma: no cover - the regex admits no such text
            continue
        path = match.group(5)
        rows.append(
            {
                "instance_path": path,
                "module": cells.get(path),
                "leakage_uw": leakage * _W_TO_UW,
                "internal_uw": internal * _W_TO_UW,
                "switching_uw": switching * _W_TO_UW,
                "total_uw": total * _W_TO_UW,
            }
        )
    return sorted(rows, key=lambda row: row["instance_path"])


def parse_instance_cells(text: str) -> dict:
    """The ``<instance path> <liberty cell>`` sidecar written by the generated Tcl, as a mapping."""
    cells = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        cells[fields[0]] = fields[1]
    return cells


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
