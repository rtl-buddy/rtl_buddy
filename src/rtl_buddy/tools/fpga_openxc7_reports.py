"""Parser for nextpnr-xilinx log output (``rb fpga`` openxc7 backend).

Pure text -> dict parsing, parallel to :mod:`.fpga_vivado_reports`. The
openXC7 toolchain has no report files — utilization and timing both
come from the nextpnr log, which the backend captures to
``artefacts/<run>/nextpnr.log``:

* "Device utilisation:" sections (``Info: <BEL>: used/avail pct%``)
  give post-pack resource usage per bel type.
* "Max frequency for clock 'x': F MHz (PASS|FAIL at T MHz)" lines give
  the achieved Fmax against the constrained target per clock.
* "Critical path report for clock 'x'" sections carry the worst path's
  start (``Source <cell.port>``) and end (``Sink <cell.port>``).

The contract is tested against hand-built fixture logs under
``tests/fixtures/fpga/`` that follow nextpnr's documented output format
(nextpnr-xilinx and prjxray are not exercised live in CI). WNS is
derived per clock as ``1000/target_mhz - 1000/fmax_mhz`` (ns) since
nextpnr reports frequencies, not slack.
"""

from __future__ import annotations

import re

# Canonical resource aliases onto nextpnr-xilinx (xc7) bel types.
_BEL_ALIASES: dict[str, tuple[str, ...]] = {
    "lut": ("SLICE_LUTX",),
    "ff": ("SLICE_FFX",),
    "bram": ("RAMB18E1", "RAMB36E1"),
    "dsp": ("DSP48E1",),
}

_UTIL_HEADER = "Device utilisation:"
_UTIL_RE = re.compile(r"^Info:\s+([\w.\-]+):\s*(\d+)\s*/\s*(\d+)\s+(\d+)%\s*$")
# A met clock prints as Info:, a failed one as Warning:.
_FMAX_RE = re.compile(
    r"^(?:Info|Warning): Max frequency for clock\s+'([^']+)':\s*(-?[\d.]+)\s*MHz"
    r"\s*\((PASS|FAIL) at\s*(-?[\d.]+)\s*MHz\)"
)
_CRIT_HEADER_RE = re.compile(r"^Info: Critical path report for clock '([^']+)'")
_CRIT_SOURCE_RE = re.compile(r"^Info:\s+[\d.]+\s+[\d.]+\s+Source\s+(\S+)")
_CRIT_SINK_RE = re.compile(r"^Info:\s+(?:[\d.]+\s+[\d.]+\s+)?Sink\s+(\S+)")


def _slack_ns(fmax_mhz: float, target_mhz: float) -> float | None:
    """Worst negative slack in ns derived from achieved vs target Fmax."""
    if fmax_mhz <= 0 or target_mhz <= 0:
        return None
    return round(1000.0 / target_mhz - 1000.0 / fmax_mhz, 3)


def parse_nextpnr_log(text: str) -> dict:
    """Parse a nextpnr-xilinx log into utilization + timing metrics.

    Returns::

        {
          "bels": {bel_type: {"used", "available", "util_pct"}, ...},
          "lut": {...} | None,   # canonical aliases into "bels"
          "ff": {...} | None,
          "bram": {...} | None,
          "dsp": {...} | None,
          "clocks": [{"clock", "fmax_mhz", "target_mhz", "met",
                      "slack_ns", "source", "destination"}, ...],
          "fmax_mhz": float | None,   # of the worst-slack clock
          "wns_ns": float | None,
          "timing_met": bool | None,  # None without any Fmax line
          "failing_paths": [...],     # FAIL clocks' critical paths
        }

    nextpnr prints the utilisation section more than once (after pack
    and again before routing); the last occurrence wins. A log with no
    "Max frequency" lines (no clock constraint) yields ``timing_met``
    ``None`` and empty ``clocks``.

    Raises:
      ValueError: if the text has no "Device utilisation:" section.
    """
    if _UTIL_HEADER not in text:
        raise ValueError("not a nextpnr log (no 'Device utilisation:' section)")

    lines = text.splitlines()

    # --- utilization (last section wins) -----------------------------------
    bels: dict[str, dict] = {}
    in_util = False
    for line in lines:
        if _UTIL_HEADER in line:
            in_util = True
            continue
        if not in_util:
            continue
        m = _UTIL_RE.match(line.strip())
        if m:
            bels[m.group(1)] = {
                "used": int(m.group(2)),
                "available": int(m.group(3)),
                "util_pct": float(m.group(4)),
            }
        else:
            in_util = False

    # --- per-clock critical paths -------------------------------------------
    # Source/Sink of the worst path, keyed by clock; a path's Sink is
    # the last one printed within its section.
    crit: dict[str, dict] = {}
    current_clock: str | None = None
    for line in lines:
        stripped = line.strip()
        m = _CRIT_HEADER_RE.match(stripped)
        if m:
            current_clock = m.group(1)
            crit[current_clock] = {"source": None, "destination": None}
            continue
        if current_clock is None:
            continue
        m = _CRIT_SOURCE_RE.match(stripped)
        if m and crit[current_clock]["source"] is None:
            crit[current_clock]["source"] = m.group(1)
            continue
        m = _CRIT_SINK_RE.match(stripped)
        if m:
            crit[current_clock]["destination"] = m.group(1)
            continue
        if not stripped.startswith("Info:"):
            current_clock = None

    # --- per-clock Fmax verdicts --------------------------------------------
    clocks: list[dict] = []
    for line in lines:
        m = _FMAX_RE.match(line.strip())
        if not m:
            continue
        clock, fmax, verdict, target = (
            m.group(1),
            float(m.group(2)),
            m.group(3),
            float(m.group(4)),
        )
        endpoints = crit.get(clock, {"source": None, "destination": None})
        clocks.append(
            {
                "clock": clock,
                "fmax_mhz": fmax,
                "target_mhz": target,
                "met": verdict == "PASS",
                "slack_ns": _slack_ns(fmax, target),
                "source": endpoints["source"],
                "destination": endpoints["destination"],
            }
        )

    result: dict = {"bels": bels, "clocks": clocks}
    for alias, bel_types in _BEL_ALIASES.items():
        result[alias] = next((bels[bt] for bt in bel_types if bt in bels), None)

    worst = min(
        (c for c in clocks if c["slack_ns"] is not None),
        key=lambda c: c["slack_ns"],
        default=None,
    )
    result["fmax_mhz"] = worst["fmax_mhz"] if worst else None
    result["wns_ns"] = worst["slack_ns"] if worst else None
    result["timing_met"] = all(c["met"] for c in clocks) if clocks else None
    result["failing_paths"] = [
        {
            "clock": c["clock"],
            "slack_ns": c["slack_ns"],
            "fmax_mhz": c["fmax_mhz"],
            "target_mhz": c["target_mhz"],
            "source": c["source"],
            "destination": c["destination"],
        }
        for c in clocks
        if not c["met"]
    ]
    return result
