"""Multi-corner OpenROAD/OpenSTA Tcl generation and log parsing for `rb pnr` and `rb power`.

A platform with two or more `corners:` is analysed in one OpenROAD session, so repair and the final timing reports see every corner. The first listed corner is the primary: CTS characterises buffer and wire delays there.

Per-corner numbers have no user command, so the generated Tcl calls `sta::` functions, using the OpenSTA 3.0 names (`find_scene`, `worst_slack_scene`) when present and the earlier ones (`find_corner`, `worst_slack_corner`) otherwise.

`rb pnr` gives each corner an SDC mode of its own (`scenes_tcl`) when OpenSTA can switch between modes (`MODES_PROBE_TCL`); otherwise, and in `rb power`, `define_corners` puts every corner in one mode (`liberty_tcl`).

A macro Liberty (`lib-paths`) entry is one path, timed at every corner, or a mapping of corner to path, each file timed at its corner only.
"""

import math
import re

from ..config.lib_paths import LibPath, corner_lib_file

#: Tcl proc `rb_find_corner`: resolves a corner name to the OpenSTA `Scene` (3.0) or `Corner` object.
FIND_CORNER_PROC = """\
proc rb_find_corner {name} {
  if {[info commands ::sta::find_scene] ne ""} {
    return [sta::find_scene $name]
  }
  return [sta::find_corner $name]
}"""


def liberty_tcl(
    corner_libs: dict[str, list[str]], macro_libs: list[LibPath]
) -> list[str]:
    """Return Tcl that defines the corners and reads each Liberty into its corner.

    `corner_libs` maps corner to its Liberty files, primary first; that order makes the first corner the command corner. Macro Liberty is read as `macro_corner_liberty_tcl` reads it.
    """
    lines = [f"define_corners {' '.join(corner_libs)}"]
    lines.extend(
        f"read_liberty -corner {c} {lib}"
        for c, libs in corner_libs.items()
        for lib in libs
    )
    lines.extend(macro_corner_liberty_tcl(list(corner_libs), macro_libs))
    return lines


def macro_corner_liberty_tcl(
    corners: list[str], macro_libs: list[LibPath]
) -> list[str]:
    """Return Tcl that reads each macro Liberty entry into the `define_corners` corners: a path into every corner, a per-corner mapping each corner's file into that corner only.

    OpenSTA warns `STA-1140 library ... already exists` on the second read of a path, which is harmless.
    """
    return [
        f"read_liberty -corner {c} {corner_lib_file(entry, c)}"
        for entry in macro_libs
        for c in corners
    ]


def macro_mode_liberty_tcl(macro_libs: list[LibPath]) -> list[str]:
    """Return Tcl that reads each macro Liberty file once, for `scenes_tcl` to assign to the scenes: a path, or each distinct file of a per-corner mapping.

    `define_scene -liberty` names Liberty already read; it does not read a file. A scene's Liberty is fixed when the scene is defined, so every file is read before it.
    """
    return [
        f"read_liberty {lib}"
        for entry in macro_libs
        for lib in (
            [entry] if isinstance(entry, str) else dict.fromkeys(entry.values())
        )
    ]


#: Prints `RB_STA_MODES:yes` when OpenSTA can define scenes and switch back to an SDC mode it made earlier, which the per-corner modes of `scenes_tcl` need; OpenROAD 26Q2's `set_mode` stays on the newest mode.
MODES_PROBE_TCL = """\
set rb_ok 0
catch {
  set_mode rb_a
  set_mode rb_b
  set_mode rb_a
  set rb_ok [expr {[info commands define_scene] ne "" && [sta::cmd_mode_name] eq "rb_a"}]
}
if {$rb_ok} { puts RB_STA_MODES:yes } else { puts RB_STA_MODES:no }
exit
"""

#: What `MODES_PROBE_TCL` prints when modes work, and when they do not.
MODES_PROBE_YES = "RB_STA_MODES:yes"
MODES_PROBE_NO = "RB_STA_MODES:no"


def mode_liberty_tcl(corner_libs: dict[str, list[str]]) -> list[str]:
    """Return Tcl that reads each corner's Liberty once, primary first, for `scenes_tcl` to assign to its corner's scene."""
    paths = dict.fromkeys(lib for libs in corner_libs.values() for lib in libs)
    return [f"read_liberty {lib}" for lib in paths]


def scenes_tcl(
    corner_libs: dict[str, list[str]], macro_libs: list[LibPath]
) -> list[str]:
    """Return Tcl that makes one SDC mode and one scene per corner, both named after the corner, after every Liberty is read.

    Each scene times its corner's Liberty and each macro Liberty entry's file for that corner: a path in every scene, a per-corner mapping's file in its corner's scene only. The primary corner's mode is left the command mode, the one an SDC command without a mode, CTS and `write_sdc` use.
    """
    lines = []
    for corner, libs in corner_libs.items():
        macros = [corner_lib_file(entry, corner) for entry in macro_libs]
        lines.append(f"set_mode {corner}")
        lines.append(
            f"define_scene {corner} -mode {corner} "
            f"-liberty {{{' '.join([*libs, *macros])}}}"
        )
    lines.append(f"set_mode {next(iter(corner_libs))}")
    return lines


def in_each_mode(corners: list[str], command: str) -> str:
    """Return Tcl that runs `command` in each corner's SDC mode, then makes the primary corner's mode current again."""
    return "".join(f"set_mode {c}\n{command}\n" for c in corners) + (
        f"set_mode {corners[0]}\n"
    )


#: Lines `timing_report_tcl` prints per corner: the `report_worst_slack` and `report_tns` format behind a `corner <name>` prefix.
_CORNER_TIMING_RE = re.compile(
    r"^corner (\S+) (worst slack max|worst slack min|tns max|tns min) ([-\d.]+)\s*$",
    re.MULTILINE,
)

_TIMING_FIELDS = {
    "worst slack max": "wns_setup_ps",
    "worst slack min": "wns_hold_ps",
    "tns max": "tns_ps",
    "tns min": "tns_hold_ps",
}


def timing_report_tcl(corners: list[str]) -> str:
    """Return Tcl that prints each corner's worst setup and hold slack and setup and hold TNS."""
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
            "    set tns_hold [sta::total_negative_slack_scene_cmd $corner min]",
            "  } else {",
            "    set setup [sta::worst_slack_corner $corner max]",
            "    set hold [sta::worst_slack_corner $corner min]",
            "    set tns [sta::total_negative_slack_corner_cmd $corner max]",
            "    set tns_hold [sta::total_negative_slack_corner_cmd $corner min]",
            "  }",
            "  set digits $::sta_report_default_digits",
            '  puts "corner $name worst slack max [sta::format_time $setup $digits]"',
            '  puts "corner $name worst slack min [sta::format_time $hold $digits]"',
            '  puts "corner $name tns max [sta::format_time $tns $digits]"',
            '  puts "corner $name tns min [sta::format_time $tns_hold $digits]"',
            "}",
            # catch: a failure here must lose only that corner's rows, not the routed database.
            *(
                f"if {{[catch {{rb_report_corner_timing {c}}} rb_err]}} "
                f'{{ puts "rb: per-corner timing for {c} unavailable: $rb_err" }}'
                for c in corners
            ),
        ]
    )


def parse_corner_timing(
    log_text: str, corners: list[str], ps_per_unit: float
) -> dict[str, dict]:
    """Parse per-corner `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps` from `pnr.log` text.

    The log prints in the Liberty `time_unit`, `ps_per_unit` picoseconds. Every configured corner gets an entry, in config order. A value that is missing or not a finite number is omitted.
    """
    found: dict[str, dict] = {c: {} for c in corners}
    for m in _CORNER_TIMING_RE.finditer(log_text):
        corner, kind, value = m.groups()
        if corner not in found:
            continue
        try:
            slack = float(value)
        except ValueError:
            continue
        if math.isfinite(slack):
            found[corner][_TIMING_FIELDS[kind]] = slack * ps_per_unit
    return found


_CORNER_INF_RE = re.compile(
    r"^corner (\S+) worst slack (max|min) INF\s*$", re.MULTILINE
)


def parse_corner_unconstrained(log_text: str, corners: list[str]) -> dict[str, set]:
    """Return each of `corners` with the sides, `setup` and `hold`, whose per-corner worst slack `log_text` prints as INF: no path of that kind is constrained at that corner."""
    found: dict[str, set] = {c: set() for c in corners}
    for m in _CORNER_INF_RE.finditer(log_text):
        corner, kind = m.groups()
        if corner in found:
            found[corner].add("setup" if kind == "max" else "hold")
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
