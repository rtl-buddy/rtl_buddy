"""Multi-corner OpenROAD/OpenSTA Tcl and log parsing shared by `rb pnr` and
`rb power` (#104, #105).

A `cfg-pnr-platforms` entry with two or more `corners:` is analysed in ONE
OpenROAD session rather than one session per corner: OpenSTA holds every
corner at once (`define_corners`, then `read_liberty -corner`), so the
optimisations that run inside the P&R flow see all of them — `repair_design`,
`repair_timing` and the hold repair iterate every corner — and the final
`report_worst_slack` / `report_tns` / `report_checks` are already the worst
across corners. Fanning out N sessions would instead route N different
layouts, none of which is signed off at the other corners.

What does *not* see every corner is CTS: OpenROAD characterises its buffer
and wire delays at the command corner, which `define_corners` makes the
first one listed. That is why the first entry of `corners:` is the
*primary* corner.

`define_corners` is deprecated in OpenSTA 3.0 in favour of `define_scene`,
but it is the spelling every OpenROAD from the flow's 25Q1 minimum to 26Q2
accepts, and it still defines the same scenes. The per-corner numbers have
no user command in either release (`report_worst_slack` takes no corner),
so the generated Tcl reaches for the `sta::` functions behind them, under
their 3.0 names (`find_scene`, `worst_slack_scene`, ...) when present and
their pre-3.0 ones (`find_corner`, `worst_slack_corner`, ...) otherwise.
"""

import math
import re

#: Defines `rb_find_corner`, which resolves a corner name to the OpenSTA
#: object the per-corner `sta::` functions take — a `Scene` from OpenSTA
#: 3.0, a `Corner` before it.
FIND_CORNER_PROC = """\
proc rb_find_corner {name} {
  if {[info commands ::sta::find_scene] ne ""} {
    return [sta::find_scene $name]
  }
  return [sta::find_corner $name]
}"""


def liberty_tcl(corner_libs: dict[str, str], macro_libs: list[str]) -> list[str]:
    """`define_corners` plus one `read_liberty -corner` per corner and library.

    ``corner_libs`` is the platform's corner → Liberty map, primary first;
    it becomes the corner order OpenSTA uses, which is what makes the
    primary the command corner.

    A hard macro usually ships one Liberty, not one per corner, and it is
    read into *every* corner: OpenSTA binds a library to the corner it was
    read for, and a cell with no library at some corner has no timing or
    power there at all. This is what ORFS does with a single-corner macro
    too. OpenSTA warns `STA-1140 library ... already exists` from the
    second read on; the warning is expected and harmless — each read is
    bound to its own corner.
    """
    lines = [f"define_corners {' '.join(corner_libs)}"]
    lines.extend(f"read_liberty -corner {c} {lib}" for c, lib in corner_libs.items())
    for lib in macro_libs:
        lines.extend(f"read_liberty -corner {c} {lib}" for c in corner_libs)
    return lines


# ---------------------------------------------------------------------------
# rb pnr — per-corner timing
# ---------------------------------------------------------------------------

#: The lines `timing_report_tcl` makes OpenROAD print, one set per corner.
#: They mirror `report_worst_slack` / `report_tns`' own spelling behind a
#: `corner <name>` prefix, in the same default digits, so the worst of them
#: is the number the global report prints.
_CORNER_TIMING_RE = re.compile(
    r"^corner (\S+) (worst slack max|worst slack min|tns max) ([-\d.]+)\s*$",
    re.MULTILINE,
)

_TIMING_FIELDS = {
    "worst slack max": "wns_setup_ps",
    "worst slack min": "wns_hold_ps",
    "tns max": "tns_ps",
}


def timing_report_tcl(corners: list[str]) -> str:
    """Tcl printing each corner's worst setup / hold slack and setup TNS."""
    return "\n".join(
        [
            "",
            'puts ">>> Per-corner timing"',
            FIND_CORNER_PROC,
            "proc rb_report_corner_timing {name} {",
            "  set corner [rb_find_corner $name]",
            '  if {[info commands ::sta::worst_slack_scene] ne ""} {',
            "    set setup [sta::worst_slack_scene $corner max]",
            "    set hold [sta::worst_slack_scene $corner min]",
            "    set tns [sta::total_negative_slack_scene_cmd $corner max]",
            "  } else {",
            "    set setup [sta::worst_slack_corner $corner max]",
            "    set hold [sta::worst_slack_corner $corner min]",
            "    set tns [sta::total_negative_slack_corner_cmd $corner max]",
            "  }",
            "  set digits $::sta_report_default_digits",
            '  puts "corner $name worst slack max [sta::format_time $setup $digits]"',
            '  puts "corner $name worst slack min [sta::format_time $hold $digits]"',
            '  puts "corner $name tns max [sta::format_time $tns $digits]"',
            "}",
            # Report-only, and it sits ahead of the writes: an error here
            # (an OpenSTA without either API spelling, say) costs that
            # corner's rows, not the routed database (#104, #105).
            *(
                f"if {{[catch {{rb_report_corner_timing {c}}} rb_err]}} "
                f'{{ puts "rb: per-corner timing for {c} unavailable: $rb_err" }}'
                for c in corners
            ),
        ]
    )


def parse_corner_timing(log_text: str, corners: list[str]) -> dict[str, dict]:
    """Per-corner `{wns_setup_ps, wns_hold_ps, tns_ps}` from a `pnr.log`.

    Every configured corner gets an entry, in config order; a value the log
    does not carry (or carries as something other than a finite number) is
    left out, the way the scalar fields leave out what they cannot parse.
    """
    found: dict[str, dict] = {c: {} for c in corners}
    for m in _CORNER_TIMING_RE.finditer(log_text):
        corner, kind, value = m.groups()
        if corner not in found:
            continue
        try:
            ns = float(value)
        except ValueError:
            continue
        if math.isfinite(ns):
            found[corner][_TIMING_FIELDS[kind]] = ns * 1000.0
    return found


def worst_corner(per_corner: dict[str, dict], key: str, *, highest=False) -> str | None:
    """The corner with the lowest (or ``highest``) ``key``, first on a tie.

    ``None`` when no corner carries the field.
    """
    candidates = [(c, v[key]) for c, v in per_corner.items() if key in v]
    if not candidates:
        return None
    pick = max if highest else min
    return pick(candidates, key=lambda cv: cv[1])[0]


# ---------------------------------------------------------------------------
# rb power — per-corner totals, and the corner that drives the budget
# ---------------------------------------------------------------------------

#: Printed by the power script with the corner it picked as the worst — the
#: highest design total — before it writes `power.rpt` and the per-instance
#: breakdown at that corner.
POWER_CORNER_MARKER = "RB_POWER_CORNER"

_POWER_CORNER_RE = re.compile(rf"^{POWER_CORNER_MARKER} (\S+)\s*$", re.MULTILINE)


def power_report_tcl(
    corners: list[str], report_path_for, worst_report: str
) -> list[str]:
    """Per-corner `report_power`, then the worst corner's report and choice.

    ``report_path_for(corner)`` names each corner's report. The worst corner
    is chosen in Tcl, on `sta::design_power`'s design total, so the design
    report at ``worst_report`` and the per-instance breakdown after it are
    both of the corner whose totals the run reports — the one that drives a
    power budget. Sets `rb_power_corner` for the per-instance block.

    `report_power -corner` is spelled with the pre-3.0 flag, which OpenSTA
    3.0 still accepts as an alias of `-scene`.
    """
    lines = [f"report_power -corner {c} > {report_path_for(c)}" for c in corners]
    lines.extend(
        [
            FIND_CORNER_PROC,
            'set rb_power_corner ""',
            'set rb_power_worst ""',
            f"foreach rb_corner {{{' '.join(corners)}}} {{",
            "  set rb_total [lindex [sta::design_power [rb_find_corner $rb_corner]] 3]",
            '  if {$rb_power_worst eq "" || $rb_total > $rb_power_worst} {',
            "    set rb_power_worst $rb_total",
            "    set rb_power_corner $rb_corner",
            "  }",
            "}",
            f'puts "{POWER_CORNER_MARKER} $rb_power_corner"',
            f"report_power -corner $rb_power_corner > {worst_report}",
        ]
    )
    return lines


def parse_power_corner(log_text: str) -> str | None:
    """The corner the power script chose as worst, or ``None``."""
    m = _POWER_CORNER_RE.search(log_text)
    return m.group(1) if m else None
