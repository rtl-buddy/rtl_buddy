"""Multi-corner OpenROAD/OpenSTA Tcl generation and log parsing for `rb pnr` and `rb power`.

A platform with two or more `corners:` is analysed in one OpenROAD session, so repair and the final timing reports see every corner. The first listed corner is the primary: CTS characterises buffer and wire delays there.

Per-corner numbers have no user command, so the generated Tcl calls `sta::` functions, using the OpenSTA 3.0 names (`find_scene`, `worst_slack_scene`) when present and the earlier ones (`find_corner`, `worst_slack_corner`) otherwise. `define_corners` is used because every supported OpenROAD accepts it.
"""

import math
import re

#: Tcl proc `rb_find_corner`: resolves a corner name to the OpenSTA `Scene` (3.0) or `Corner` object.
FIND_CORNER_PROC = """\
proc rb_find_corner {name} {
  if {[info commands ::sta::find_scene] ne ""} {
    return [sta::find_scene $name]
  }
  return [sta::find_corner $name]
}"""


def liberty_tcl(corner_libs: dict[str, str], macro_libs: list[str]) -> list[str]:
    """Return Tcl that defines the corners and reads each Liberty into its corner.

    `corner_libs` maps corner to Liberty, primary first; that order makes the first corner the command corner. Each macro Liberty is read into every corner. OpenSTA warns `STA-1140 library ... already exists` on the second read, which is harmless.
    """
    lines = [f"define_corners {' '.join(corner_libs)}"]
    lines.extend(f"read_liberty -corner {c} {lib}" for c, lib in corner_libs.items())
    for lib in macro_libs:
        lines.extend(f"read_liberty -corner {c} {lib}" for c in corner_libs)
    return lines


#: Lines `timing_report_tcl` prints per corner: the `report_worst_slack` and `report_tns` format behind a `corner <name>` prefix.
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
    """Return Tcl that prints each corner's worst setup slack, worst hold slack and setup TNS."""
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
            # catch: a failure here must lose only that corner's rows, not the routed database.
            *(
                f"if {{[catch {{rb_report_corner_timing {c}}} rb_err]}} "
                f'{{ puts "rb: per-corner timing for {c} unavailable: $rb_err" }}'
                for c in corners
            ),
        ]
    )


def parse_corner_timing(log_text: str, corners: list[str]) -> dict[str, dict]:
    """Parse per-corner `wns_setup_ps`, `wns_hold_ps` and `tns_ps` from `pnr.log` text.

    Every configured corner gets an entry, in config order. A value that is missing or not a finite number is omitted.
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
    """Return the corner with the lowest (or `highest`) `key`, first on a tie, or None if no corner has it."""
    candidates = [(c, v[key]) for c, v in per_corner.items() if key in v]
    if not candidates:
        return None
    pick = max if highest else min
    return pick(candidates, key=lambda cv: cv[1])[0]


#: Printed by the power script with the corner of highest design total.
POWER_CORNER_MARKER = "RB_POWER_CORNER"

_POWER_CORNER_RE = re.compile(rf"^{POWER_CORNER_MARKER} (\S+)\s*$", re.MULTILINE)


def power_report_tcl(
    corners: list[str], report_path_for, worst_report: str
) -> list[str]:
    """Return Tcl that reports power per corner, then reports the highest-power corner to `worst_report`.

    `report_path_for(corner)` names each corner's report. The Tcl picks the worst corner by `sta::design_power` total and sets `rb_power_corner` for the per-instance block that follows. `-corner` is the pre-3.0 flag, which OpenSTA 3.0 accepts as an alias of `-scene`.
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
    """Return the corner the power script chose as worst, or None."""
    m = _POWER_CORNER_RE.search(log_text)
    return m.group(1) if m else None
