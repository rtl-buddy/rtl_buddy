"""Parse the Vivado reports written by the flow in :mod:`.fpga_vivado_flow`.

Covers ``report_utilization``, ``report_timing_summary``, ``report_power``, ``report_drc``
and ``report_methodology``. Each parser takes the report text and raises :class:`ValueError`
when the report's anchor section is missing.
"""

from __future__ import annotations

import re


def _num(cell: str) -> int | float | None:
    """Parse a table cell to ``int`` or ``float``; ``None`` for blanks, ``NA`` and dashes.

    A leading ``<`` is dropped, so ``<0.01`` parses as 0.01.
    """
    cell = cell.strip().lstrip("<")
    if not cell or cell in {"-", "_", "---", "NA", "n/a"}:
        return None
    if re.fullmatch(r"-?\d+", cell):
        return int(cell)
    try:
        return float(cell)
    except ValueError:
        return None


def _split_table_row(line: str) -> list[str]:
    """Split one ``| a | b |`` ASCII-table row into stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _iter_ascii_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Return ``(header_cells, data_rows)`` for each ``+---+`` ASCII table.

    Data rows lie between the second and last separator; a table without them has zero rows.
    """
    tables: list[tuple[list[str], list[list[str]]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not re.fullmatch(r"\+[-+]+\+", lines[i].strip()):
            i += 1
            continue
        if i + 2 >= len(lines) or not lines[i + 1].lstrip().startswith("|"):
            i += 1
            continue
        header = _split_table_row(lines[i + 1])
        if not re.fullmatch(r"\+[-+]+\+", lines[i + 2].strip()):
            i += 2
            continue
        rows: list[list[str]] = []
        j = i + 3
        while j < len(lines):
            stripped = lines[j].strip()
            if re.fullmatch(r"\+[-+]+\+", stripped):
                break
            if not stripped.startswith("|"):
                break
            rows.append(_split_table_row(lines[j]))
            j += 1
        tables.append((header, rows))
        i = j + 1
    return tables


# UltraScale+ reports say "CLB LUTs"/"CLB Registers"; 7-series says "Slice LUTs"/"Slice Registers".
_RESOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "lut": ("CLB LUTs", "Slice LUTs"),
    "ff": ("CLB Registers", "Slice Registers"),
    "bram": ("Block RAM Tile",),
    "dsp": ("DSPs",),
}


def parse_utilization(text: str) -> dict:
    """Parse a ``report_utilization`` report.

    Returns::

        {
          "resources": {site_type: {"used", "fixed", "available",
                                    "util_pct"}, ...},
          "lut": {...} | None,   # canonical aliases into "resources"
          "ff": {...} | None,
          "bram": {...} | None,
          "dsp": {...} | None,
        }

    Every row of every ``Site Type`` table is captured; the first occurrence of a
    repeated site type wins. Blank cells are ``None``.

    Raises:
      ValueError: if the text is not a Vivado utilization report.
    """
    if "Utilization Design Information" not in text:
        raise ValueError("not a Vivado utilization report")

    resources: dict[str, dict] = {}
    for header, rows in _iter_ascii_tables(text):
        if not header or header[0] != "Site Type":
            continue
        col_index = {name: idx for idx, name in enumerate(header)}

        def _cell(row: list[str], column: str) -> int | float | None:
            idx = col_index.get(column)
            if idx is None or idx >= len(row):
                return None
            return _num(row[idx])

        for row in rows:
            if not row or not row[0]:
                continue
            site_type = row[0]
            resources.setdefault(
                site_type,
                {
                    "used": _cell(row, "Used"),
                    "fixed": _cell(row, "Fixed"),
                    "available": _cell(row, "Available"),
                    "util_pct": _cell(row, "Util%"),
                },
            )

    result: dict = {"resources": resources}
    for alias, site_types in _RESOURCE_ALIASES.items():
        result[alias] = next(
            (resources[st] for st in site_types if st in resources), None
        )
    return result


# Column order of the "Design Timing Summary" and "Intra Clock Table" rows.
_TIMING_FIELDS: tuple[str, ...] = (
    "wns_ns",
    "tns_ns",
    "tns_failing_endpoints",
    "tns_total_endpoints",
    "whs_ns",
    "ths_ns",
    "ths_failing_endpoints",
    "ths_total_endpoints",
    "wpws_ns",
    "tpws_ns",
    "tpws_failing_endpoints",
    "tpws_total_endpoints",
)


def _timing_values(tokens: list[str]) -> dict:
    values: dict = dict.fromkeys(_TIMING_FIELDS)
    for field, token in zip(_TIMING_FIELDS, tokens):
        values[field] = _num(token)
    return values


# Each path in "Timing Details" opens with `Slack (VIOLATED) : -0.882ns` and is followed by `Key: value` lines.
_PATH_SLACK_RE = re.compile(r"^Slack \((VIOLATED|MET)\)\s*:\s*(-?[\d.]+)ns")
_PATH_FIELD_RE = re.compile(
    r"^(Source|Destination|Path Group|Path Type|Requirement|"
    r"Data Path Delay|Logic Levels):\s+(\S.*)$"
)


def _parse_detail_paths(lines: list[str]) -> list[dict]:
    """Return one dict per ``Slack (VIOLATED|MET)`` block in the "Timing Details" section.

    ``path_type`` distinguishes Setup from Hold paths.
    """
    paths: list[dict] = []
    current: dict | None = None
    for line in lines:
        stripped = line.strip()
        m = _PATH_SLACK_RE.match(stripped)
        if m:
            current = {
                "slack_ns": _num(m.group(2)),
                "met": m.group(1) == "MET",
                "source": None,
                "destination": None,
                "path_group": None,
                "path_type": None,
                "requirement_ns": None,
                "data_path_delay_ns": None,
                "logic_levels": None,
            }
            paths.append(current)
            continue
        if current is None:
            continue
        m = _PATH_FIELD_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        match key:
            case "Source" if current["source"] is None:
                current["source"] = value
            case "Destination" if current["destination"] is None:
                current["destination"] = value
            case "Path Group" if current["path_group"] is None:
                current["path_group"] = value
            case "Path Type" if current["path_type"] is None:
                # "Setup (Max at Slow Process Corner)" -> "Setup"
                current["path_type"] = value.split()[0]
            case "Requirement" if current["requirement_ns"] is None:
                current["requirement_ns"] = _num(value.split("ns")[0])
            case "Data Path Delay" if current["data_path_delay_ns"] is None:
                current["data_path_delay_ns"] = _num(value.split("ns")[0])
            case "Logic Levels" if current["logic_levels"] is None:
                current["logic_levels"] = _num(value.split()[0])
    return paths


def parse_timing_summary(text: str) -> dict:
    """Parse a ``report_timing_summary`` report.

    Returns the "Design Timing Summary" numbers (WNS/TNS/WHS/THS/WPWS/TPWS in ns plus
    failing and total endpoint counts), the "Intra Clock Table" rows under ``"clocks"``
    and ``"timing_met"``. ``timing_met`` follows Vivado's verdict line, or WNS and WHS
    being non-negative when the line is absent.

    ``failing_endpoints`` is the setup plus hold failing-endpoint count. ``failing_paths``
    lists the ``Slack (VIOLATED)`` blocks from "Timing Details" as dicts with ``slack_ns``,
    ``source``, ``destination``, ``path_group``, ``path_type``, ``requirement_ns``,
    ``data_path_delay_ns``, ``logic_levels`` and ``met``.

    Raises:
      ValueError: if the text has no "Design Timing Summary" section.
    """
    if "Design Timing Summary" not in text:
        raise ValueError("not a Vivado timing summary report")

    lines = text.splitlines()

    summary: dict | None = None
    for i, line in enumerate(lines):
        if not line.strip().startswith("WNS(ns)"):
            continue
        # The values row is two lines below the header (dashed underline between).
        if i + 2 < len(lines):
            tokens = lines[i + 2].split()
            if tokens:
                summary = _timing_values(tokens)
                break
    if summary is None:
        raise ValueError("Vivado timing summary has no headline values row")

    clocks: list[dict] = []
    try:
        intra_at = next(
            i for i, line in enumerate(lines) if "| Intra Clock Table" in line
        )
    except StopIteration:
        intra_at = None
    if intra_at is not None:
        header_at = next(
            (
                i
                for i in range(intra_at + 1, len(lines))
                if lines[i].strip().startswith("Clock") and "WNS(ns)" in lines[i]
            ),
            None,
        )
        if header_at is not None:
            for line in lines[header_at + 2 :]:
                stripped = line.strip()
                if not stripped:
                    break
                tokens = stripped.split()
                if len(tokens) < 2:
                    break
                clocks.append({"clock": tokens[0], **_timing_values(tokens[1:])})

    if "Timing constraints are not met." in text:
        timing_met = False
    elif "All user specified timing constraints are met." in text:
        timing_met = True
    else:
        wns = summary["wns_ns"]
        whs = summary["whs_ns"]
        timing_met = (wns is None or wns >= 0) and (whs is None or whs >= 0)

    endpoint_counts = [
        summary["tns_failing_endpoints"],
        summary["ths_failing_endpoints"],
    ]
    known = [c for c in endpoint_counts if c is not None]
    failing_endpoints = int(sum(known)) if known else None

    failing_paths = [p for p in _parse_detail_paths(lines) if not p["met"]]

    return {
        **summary,
        "timing_met": timing_met,
        "clocks": clocks,
        "failing_endpoints": failing_endpoints,
        "failing_paths": failing_paths,
    }


_POWER_SUMMARY_KEYS: dict[str, str] = {
    "total_on_chip_w": r"Total On-Chip Power \(W\)",
    "dynamic_w": r"Dynamic \(W\)",
    "static_w": r"Device Static \(W\)",
    "junction_temp_c": r"Junction Temperature \(C\)",
}


def parse_power(text: str) -> dict:
    """Parse a ``report_power`` report.

    Returns total on-chip, dynamic and device-static power in watts, the junction
    temperature in Celsius and the confidence level string. Non-numeric values
    (``NA``, ``Unspecified*``) are ``None``.

    Raises:
      ValueError: if the text is not a Vivado power report.
    """
    if "Power Report" not in text and "Total On-Chip Power" not in text:
        raise ValueError("not a Vivado power report")

    result: dict = dict.fromkeys(_POWER_SUMMARY_KEYS)
    result["confidence_level"] = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_table_row(line)
        if len(cells) != 2:
            continue
        key_cell, value_cell = cells
        for field, pattern in _POWER_SUMMARY_KEYS.items():
            if result[field] is None and re.fullmatch(pattern, key_cell):
                result[field] = _num(value_cell)
        if result["confidence_level"] is None and key_cell == "Confidence Level":
            result["confidence_level"] = value_cell
    return result


_DRC_SEVERITIES = ("Advisory", "Warning", "Critical Warning", "Error", "Fatal")
_DRC_DETAIL_RE = re.compile(r"^([\w-]+#\d+)\s+(" + "|".join(_DRC_SEVERITIES) + r")\s*$")


def _parse_rule_report(text: str) -> tuple[int, dict[str, int], list[dict]]:
    """Parse the layout shared by ``report_drc`` and ``report_methodology``.

    Returns ``(total, by_severity, entries)``. ``by_severity`` comes from the REPORT
    SUMMARY table, or from the REPORT DETAILS entries when the table is absent.
    """
    total = 0
    m = re.search(r"Violations found:\s*(\d+)", text)
    if m:
        total = int(m.group(1))

    by_severity: dict[str, int] = {}
    for header, rows in _iter_ascii_tables(text):
        if header[:2] != ["Rule", "Severity"]:
            continue
        for row in rows:
            if len(row) < 4:
                continue
            count = _num(row[3])
            if count is None:
                continue
            by_severity[row[1]] = by_severity.get(row[1], 0) + int(count)

    # A detail entry is "<RULE>#<n> <Severity>" followed by a description line.
    entries: list[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _DRC_DETAIL_RE.match(line.strip())
        if not m:
            continue
        description = lines[i + 1].strip() if i + 1 < len(lines) else ""
        entries.append(
            {"id": m.group(1), "severity": m.group(2), "description": description}
        )

    if not by_severity and entries:
        for entry in entries:
            severity = entry["severity"]
            by_severity[severity] = by_severity.get(severity, 0) + 1
    if not total:
        total = sum(by_severity.values())
    return total, by_severity, entries


def parse_drc(text: str) -> dict:
    """Parse a ``report_drc`` report.

    Returns::

        {
          "total_violations": int,
          "by_severity": {"Critical Warning": 2, "Warning": 1, ...},
          "violations": [{"id", "severity", "description"}, ...],
        }

    ``violations`` has one entry per REPORT DETAILS item. A clean report gives zero
    counts and an empty list.

    Raises:
      ValueError: if the text is not a Vivado DRC report.
    """
    if "Report DRC" not in text and "REPORT SUMMARY" not in text:
        raise ValueError("not a Vivado DRC report")

    total, by_severity, violations = _parse_rule_report(text)
    return {
        "total_violations": total,
        "by_severity": by_severity,
        "violations": violations,
    }


def parse_methodology(text: str) -> dict:
    """Parse a ``report_methodology`` report.

    Returns::

        {
          "total_warnings": int,
          "by_severity": {"Warning": 49, ...},
          "warnings": [{"id", "severity", "description"}, ...],
        }

    Rule ids and severities are Vivado's, verbatim. A clean report gives zero counts
    and an empty list.

    Raises:
      ValueError: if the text is not a Vivado methodology report.
    """
    if "Report Methodology" not in text:
        raise ValueError("not a Vivado methodology report")

    total, by_severity, warnings = _parse_rule_report(text)
    return {
        "total_warnings": total,
        "by_severity": by_severity,
        "warnings": warnings,
    }
