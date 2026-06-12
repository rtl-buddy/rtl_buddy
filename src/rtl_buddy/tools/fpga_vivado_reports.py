"""Parsers for Vivado post-route report files (``rb fpga``).

Pure text -> dict parsing for the reports the batch flow in
:mod:`.fpga_vivado_flow` emits: ``report_utilization``,
``report_timing_summary``, ``report_power``, ``report_drc``,
``report_methodology``. No subprocess code lives here — the P1 backend
reads the ``.rpt`` files from ``artefacts/<run>/`` and feeds the text
through these functions.

The contract is tested against real, sanitized Vivado 2022.1.2 reports
under ``tests/fixtures/fpga/`` (part ``xczu7ev-ffvc1156-2-e``). Each
parser tolerates the standard report headers (``Copyright ... | Tool
Version ...``) and raises :class:`ValueError` when the text does not
contain the report's anchor section — garbage in, exception out.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Shared helpers


def _num(cell: str) -> int | float | None:
    """Parse a numeric table cell.

    Returns an ``int`` for integer-looking cells, a ``float`` otherwise
    (e.g. ``0.5`` Block RAM tiles), and ``None`` for blanks / ``NA`` /
    dashes. Vivado prints ``<0.01`` for sub-resolution utilization — the
    ``<`` is dropped, so the value parses as its printed bound (0.01).
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
    """Yield ``(header_cells, data_rows)`` for each ``+---+`` table.

    Vivado tables are::

        +------+------+
        | Head | Head |
        +------+------+
        | data | data |
        +------+------+

    Rows between the second and final separator are data rows. Tables
    without a data section (e.g. empty Black Boxes tables) yield zero
    rows.
    """
    tables: list[tuple[list[str], list[list[str]]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not re.fullmatch(r"\+[-+]+\+", lines[i].strip()):
            i += 1
            continue
        # Separator found: next line should be the header row.
        if i + 2 >= len(lines) or not lines[i + 1].lstrip().startswith("|"):
            i += 1
            continue
        header = _split_table_row(lines[i + 1])
        if not re.fullmatch(r"\+[-+]+\+", lines[i + 2].strip()):
            # A header-only table (no second separator) — skip.
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


# ---------------------------------------------------------------------------
# report_utilization


# Canonical resource aliases. UltraScale+ reports say "CLB LUTs" /
# "CLB Registers"; 7-series says "Slice LUTs" / "Slice Registers".
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

    Every row of every ``Site Type`` table is captured (first occurrence
    wins when a site type repeats across tables, so the headline "CLB
    Logic" numbers take precedence over the "CLB Logic Distribution"
    breakdown). Blank cells parse as ``None``.

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


# ---------------------------------------------------------------------------
# report_timing_summary


# Field order of the "Design Timing Summary" (and "Intra Clock Table")
# numeric columns in a Vivado 2022.1 report.
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


# Path detail blocks in the "Timing Details" section. Each path opens
# with `Slack (VIOLATED) :        -0.882ns  (...)` followed by indented
# `Key:                  value` lines until the location table.
_PATH_SLACK_RE = re.compile(r"^Slack \((VIOLATED|MET)\)\s*:\s*(-?[\d.]+)ns")
_PATH_FIELD_RE = re.compile(
    r"^(Source|Destination|Path Group|Path Type|Requirement|"
    r"Data Path Delay|Logic Levels):\s+(\S.*)$"
)


def _parse_detail_paths(lines: list[str]) -> list[dict]:
    """Extract the per-path blocks from the "Timing Details" section.

    Returns one dict per ``Slack (VIOLATED|MET)`` block (Max *and* Min
    Delay Paths — ``path_type`` distinguishes Setup from Hold).
    Continuation lines (the parenthesized cell/clock annotations under
    ``Source:`` / ``Destination:``) don't match the field pattern and
    are skipped; the location-delay table never matches either, so the
    scan is safe to run over the whole report tail.
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

    Returns the "Design Timing Summary" numbers (WNS/TNS/WHS/THS/WPWS/
    TPWS in ns plus failing/total endpoint counts), the per-clock rows
    of the "Intra Clock Table" under ``"clocks"``, and ``"timing_met"``
    derived from Vivado's own verdict line ("All user specified timing
    constraints are met." / "Timing constraints are not met."), falling
    back to a non-negative WNS/WHS check when neither line is present.

    For the timing-closure loop two derived keys are included:
    ``failing_endpoints`` (setup + hold endpoints with negative slack,
    from the headline TNS/THS counts) and ``failing_paths`` — the
    ``Slack (VIOLATED)`` path blocks from the report's "Timing Details"
    section as ``{"slack_ns", "source", "destination", "path_group",
    "path_type", "requirement_ns", "data_path_delay_ns",
    "logic_levels", "met"}`` dicts (the report carries the single worst
    path per clock pair by default).

    Raises:
      ValueError: if the text has no "Design Timing Summary" section.
    """
    if "Design Timing Summary" not in text:
        raise ValueError("not a Vivado timing summary report")

    lines = text.splitlines()

    # --- headline numbers ------------------------------------------------
    summary: dict | None = None
    for i, line in enumerate(lines):
        if not line.strip().startswith("WNS(ns)"):
            continue
        # Header row -> dashed underline -> values row.
        if i + 2 < len(lines):
            tokens = lines[i + 2].split()
            if tokens:
                summary = _timing_values(tokens)
                break
    if summary is None:
        raise ValueError("Vivado timing summary has no headline values row")

    # --- per-clock rows ---------------------------------------------------
    clocks: list[dict] = []
    try:
        intra_at = next(
            i for i, line in enumerate(lines) if "| Intra Clock Table" in line
        )
    except StopIteration:
        intra_at = None
    if intra_at is not None:
        # Layout: "Clock  WNS(ns) ..." column header, a dashed underline
        # row, then one row per clock until a blank line.
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

    # --- verdict ----------------------------------------------------------
    if "Timing constraints are not met." in text:
        timing_met = False
    elif "All user specified timing constraints are met." in text:
        timing_met = True
    else:
        wns = summary["wns_ns"]
        whs = summary["whs_ns"]
        timing_met = (wns is None or wns >= 0) and (whs is None or whs >= 0)

    # --- timing-closure loop fields ----------------------------------------
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


# ---------------------------------------------------------------------------
# report_power


_POWER_SUMMARY_KEYS: dict[str, str] = {
    "total_on_chip_w": r"Total On-Chip Power \(W\)",
    "dynamic_w": r"Dynamic \(W\)",
    "static_w": r"Device Static \(W\)",
    "junction_temp_c": r"Junction Temperature \(C\)",
}


def parse_power(text: str) -> dict:
    """Parse a ``report_power`` report.

    Returns total on-chip / dynamic / device-static power in watts, the
    junction temperature in Celsius, and the overall confidence level
    string from the report's Summary table. Non-numeric values
    (``NA`` / ``Unspecified*``) parse as ``None``.

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


# ---------------------------------------------------------------------------
# report_drc


_DRC_SEVERITIES = ("Advisory", "Warning", "Critical Warning", "Error", "Fatal")
_DRC_DETAIL_RE = re.compile(r"^([\w-]+#\d+)\s+(" + "|".join(_DRC_SEVERITIES) + r")\s*$")


def _parse_rule_report(text: str) -> tuple[int, dict[str, int], list[dict]]:
    """Shared machinery for the DRC-shaped rule reports.

    ``report_drc`` and ``report_methodology`` share one layout: a
    ``Violations found: N`` headline, a REPORT SUMMARY table
    (Rule | Severity | Description | Violations) and REPORT DETAILS
    entries (``NSTD-1#1``-style ids). Returns
    ``(total, by_severity, entries)`` with the summary table aggregated
    by severity, falling back to the details when the table is absent.
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

    # Details: "<RULE>#<n> <Severity>" followed by a description line.
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

    ``violations`` lists the REPORT DETAILS entries (``NSTD-1#1``-style
    ids, one entry per violation instance); ``by_severity`` aggregates
    the REPORT SUMMARY rule table, falling back to the details when the
    summary table is absent. A clean report yields zero counts and an
    empty list.

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


# ---------------------------------------------------------------------------
# report_methodology


def parse_methodology(text: str) -> dict:
    """Parse a ``report_methodology`` report.

    Returns::

        {
          "total_warnings": int,
          "by_severity": {"Warning": 49, ...},
          "warnings": [{"id", "severity", "description"}, ...],
        }

    A methodology report shares the DRC report layout (REPORT SUMMARY
    rule table + ``TIMING-18#1``-style REPORT DETAILS entries), so the
    same machinery applies. The vendor's rule ids and severities are
    surfaced verbatim — informational, not adopted as rtl_buddy's own
    taxonomy. A clean report yields zero counts and an empty list.

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
