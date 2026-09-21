# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Readers for the raw synth and power tool output (#558).

Two formats, one job each: turn a file a tool wrote into the rows
:mod:`rtl_buddy.phys.model` assembles. They are separated from the model
so a backend that later grows a third producer — a vendor synthesiser's
report, a post-route power run — adds a reader here and nothing else.

Both are deliberately forgiving. A row that does not parse is dropped
rather than raising: these files are read *after* a flow has already
succeeded, and half a model is strictly better than failing a synthesis
over a column that moved.
"""

from __future__ import annotations

import json
import re


def parse_stat_json(text: str) -> list[dict]:
    """Per-module rows from Yosys' ``stat -json`` dump.

    Yosys writes a ``modules`` map keyed by the RTLIL name — ``\\top``,
    with the leading backslash that marks a public identifier — each
    carrying ``num_cells`` and, when ``stat`` was given a Liberty,
    ``area``. The sibling ``design`` block is the whole-design roll-up
    and is *not* returned: the model's totals come from the same log
    scrape the flow already reports, precisely so the two can be
    compared.

    A caveat worth knowing before reading the numbers: ``num_cells``
    counts a submodule *instance* as one cell, while ``area`` already
    includes that submodule's area. The two columns are therefore not on
    the same footing for a non-flattened design, and a consumer summing
    ``area_um2`` across modules will double-count. This function reports
    what Yosys reports; deciding what to do about it is the consumer's
    call, which is the same rule the model applies to hierarchy roll-up.

    :returns: ``[{"module", "cell_count", "area_um2"}]`` sorted by
        module name. ``area_um2`` is ``None`` when no Liberty was used.
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


#: One instance row of OpenSTA's `report_power -instances`::
#:
#:     2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_
#:
#: Four powers in watts, then the instance path — which is the last
#: field rather than the first, so the columns cannot be anchored from
#: the left and the name is whatever remains. Verilog identifiers carry
#: no spaces, so "the rest of the line" is unambiguous.
_INSTANCE_ROW_RE = re.compile(
    r"^\s*"
    r"([-\d.eE+]+)\s+"  # internal
    r"([-\d.eE+]+)\s+"  # switching
    r"([-\d.eE+]+)\s+"  # leakage
    r"([-\d.eE+]+)\s+"  # total
    r"(\S+)\s*$"
)

#: Watts as OpenSTA reports them, microwatts as the model records them.
_W_TO_UW = 1e6


def parse_instance_power(text: str, cells: dict | None = None) -> list[dict]:
    """Per-instance rows from OpenSTA's ``report_power -instances`` text.

    The report is one line per leaf instance and carries no module
    column, so the liberty cell each instance is an instance *of* comes
    from ``cells`` — the sidecar map the generated Tcl writes alongside
    (:func:`parse_instance_cells`). An instance missing from the map
    gets ``module: None`` rather than being dropped: the power numbers
    are the point, and the map is the half more likely to be absent.

    Rows are returned sorted by instance path. OpenSTA emits them
    descending by total power, which is a useful default for a human
    reading the report and a useless one for diffing two models.

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
    """The ``<instance path> <liberty cell>`` sidecar, as a mapping.

    Written by the generated Tcl's hierarchy walk because
    ``report_power`` prints the path and nothing else, and joining a
    power row back to a module is the whole point of recording it.
    """
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
