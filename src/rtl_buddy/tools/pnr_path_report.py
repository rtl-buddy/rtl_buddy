"""The worst-path and fanout reports of `rb pnr`'s final reports: `worst_paths.rpt` and `fanout.rpt`.

The final reports print one tagged line per path group's worst setup path and per high-fanout net (`report_tcl`); rb parses them from `pnr.log` (`parse`), resolves their instance and net names against the synthesis netlist the run read (`NetlistNames`), writes the two reports and returns the result fields (`write_reports`).

A pin's name is resolved when the netlist keeps it, tried in this order: a top-level port; the pin of an instance the netlist names itself (a macro or a block); the net that names the pin, when the netlist declares it under a name of its own; or an instance with a `src` attribute, named by its scope and source lines. The net that names a startpoint or a fanout driver is the one its pin drives; an endpoint's is its register's single output, or else the endpoint pin's own net. Names Yosys made up (`_123_`, anything with `$`) and cells P&R added are not in the netlist under their own name, so they stay raw and are marked unresolved.

The Tcl reads what it can per pin and per candidate net: a cell's register outputs and asynchronous pins come from its Liberty timing arcs, cached per cell; the fanout scan reads two counts per net and classifies only the nets that can rank.
"""

import os
import re
from dataclasses import dataclass, field

from . import block_params

#: Printed once per path group by the final reports: the group's worst setup path at the setup-critical corner.
WORST_PATH_TAG = "RB-WORST-PATH:"
#: Printed once per listed net by the final reports: its fanout, then each net and its driver pin, back through buffers and inverters.
FANOUT_TAG = "RB-FANOUT:"
#: The report files, next to `electrical.rpt`.
WORST_PATHS_REPORT = "worst_paths.rpt"
FANOUT_REPORT = "fanout.rpt"
#: How many buffers and inverters a driver is traced back through.
_DRIVER_HOPS = 16

_UNAVAILABLE_RE = re.compile(
    r"^rb: (worst path|fanout) report unavailable: (.*)$", re.M
)

_PROCS = """\
proc rb_cell_info {cell} {
  global rb_cell_info
  set name [$cell name]
  if {![info exists rb_cell_info($name)]} {
    set outputs {}
    set async {}
    foreach arcs [$cell timing_arc_sets] {
      if {[catch {set to [[$arcs to] name]; set from [[$arcs from] name]}]} continue
      switch -- [$arcs role] {
        "Reg Clk to Q" - "Latch En to Q" { lappend outputs $to }
        "recovery" - "removal" { lappend async $to }
        "Reg Set/Clr" { lappend async $from }
      }
    }
    set rb_cell_info($name) [list [lsort -unique $outputs] [lsort -unique $async]]
  }
  return $rb_cell_info($name)
}
proc rb_cell_kind {cell} {
  if {$cell eq "NULL"} { return cell }
  if {[$cell is_inverter]} { return inv }
  if {[$cell is_buffer]} { return buf }
  return cell
}
proc rb_point_net {pin} {
  if {[$pin is_top_level_port]} { return "" }
  set named $pin
  if {![$pin is_driver]} {
    set inst [$pin instance]
    set cell [$inst liberty_cell]
    if {$cell ne "NULL"} {
      set outputs [lindex [rb_cell_info $cell] 0]
      if {[llength $outputs] == 1} {
        set out [$inst find_pin [lindex $outputs 0]]
        if {$out ne "NULL" && ![sta::is_clock $out]} { set named $out }
      }
    }
  }
  set net [$named net]
  return [expr {$net eq "NULL" ? "" : [get_full_name $net]}]
}
proc rb_worst_path {group corner path_end} {
  set path [$path_end path]
  set start [[$path start_path] pin]
  set end [$path_end pin]
  set skip {}
  foreach pin [list $start $end] {
    if {![$pin is_top_level_port]} { lappend skip [get_full_name [$pin instance]] }
  }
  set seen [dict create]
  set buffers 0
  set reached 0
  foreach pin [$path pins] {
    if {$pin eq $start} { set reached 1; break }
    if {[$pin is_top_level_port] || [$pin is_hierarchical]} continue
    set inst [$pin instance]
    set name [get_full_name $inst]
    if {$name in $skip || [dict exists $seen $name]} continue
    dict set seen $name 1
    if {[rb_cell_kind [$inst liberty_cell]] ne "cell"} { incr buffers }
  }
  set levels [expr {$reached ? [dict size $seen] : "-"}]
  puts "{tag}\\t$group\\t$corner\\t[$path_end slack]\\t$levels\\t$buffers\\t[get_full_name $start]\\t[rb_point_net $start]\\t[get_full_name $end]\\t[rb_point_net $end]"
}
proc rb_net_driver {net} {
  foreach it [$net getITerms] {
    if {[$it getIoType] eq "OUTPUT"} {
      set inst [$it getInst]
      set kind [rb_cell_kind [sta::find_liberty_cell [[$inst getMaster] getName]]]
      return [list "[$inst getName]/[[$it getMTerm] getName]" $kind $inst]
    }
  }
  foreach bt [$net getBTerms] {
    if {[$bt getIoType] eq "INPUT"} { return [list [$bt getName] port ""] }
  }
  return {}
}
proc rb_driver_input {inst} {
  foreach it [$inst getITerms] {
    if {[$it getIoType] eq "INPUT" && [$it getSigType] eq "SIGNAL"} { return [$it getNet] }
  }
  return ""
}
proc rb_sinks {net} {
  set sinks {}
  foreach it [$net getITerms] {
    if {[$it getIoType] ne "OUTPUT"} { lappend sinks $it }
  }
  return $sinks
}
proc rb_is_reset_tree {net depth} {
  return [lindex [rb_reset_search $net $depth] 0]
}
proc rb_reset_search {net depth} {
  global rb_reset_tree
  if {[info exists rb_reset_tree($net)]} { return [list $rb_reset_tree($net) 0] }
  set limited 0
  foreach it [rb_sinks $net] {
    set inst [$it getInst]
    set cell [sta::find_liberty_cell [[$inst getMaster] getName]]
    if {$cell eq "NULL"} continue
    if {[[$it getMTerm] getName] in [lindex [rb_cell_info $cell] 1]} {
      set rb_reset_tree($net) 1
      return {1 0}
    }
    if {[rb_cell_kind $cell] eq "cell"} continue
    if {$depth >= {hops}} { set limited 1; continue }
    foreach out [$inst getITerms] {
      if {[$out getIoType] ne "OUTPUT" || [$out getNet] eq "NULL"} continue
      lassign [rb_reset_search [$out getNet] [expr {$depth + 1}]] found cut
      if {$found} {
        set rb_reset_tree($net) 1
        return {1 0}
      }
      if {$cut} { set limited 1 }
    }
  }
  if {!$limited} { set rb_reset_tree($net) 0 }
  return [list 0 $limited]
}
proc rb_fanout_order {a b} {
  set diff [expr {[lindex $b 0] - [lindex $a 0]}]
  if {$diff} { return $diff }
  return [string compare [lindex $a 1] [lindex $b 1]]
}
proc rb_fanout_candidates {keep} {
  set floor 0
  set candidates {}
  set truncated 0
  foreach net [[ord::get_db_block] getNets] {
    set bound [expr {[$net getITermCount] + [$net getBTermCount]}]
    if {$bound < $floor} continue
    lappend candidates [list $bound $net]
    if {[llength $candidates] >= 2 * $keep} {
      set candidates [lrange [lsort -integer -decreasing -index 0 $candidates] 0 [expr {$keep - 1}]]
      set floor [lindex $candidates end 0]
      set truncated 1
    }
  }
  return [list $truncated [lsort -integer -decreasing -index 0 $candidates]]
}
proc rb_fanout_nets {count} {
  set clock [dict create]
  foreach net [sta::find_all_clk_nets] { dict set clock $net 1 }
  for {set keep [expr {8 * $count}]} {1} {set keep [expr {8 * $keep}]} {
    lassign [rb_fanout_candidates $keep] truncated candidates
    set top {}
    foreach candidate $candidates {
      lassign $candidate bound net
      if {[llength $top] >= $count && $bound < [lindex $top end 0]} break
      if {[$net getSigType] in {POWER GROUND CLOCK} || [dict exists $clock $net]} continue
      set fanout [expr {[llength [rb_sinks $net]] + [llength [$net getBTerms]]}]
      foreach bt [$net getBTerms] {
        if {[$bt getIoType] eq "INPUT"} { incr fanout -1 }
      }
      if {$fanout == 0 || [rb_is_reset_tree $net 0]} continue
      lappend top [list $fanout [$net getName] $net]
      set top [lrange [lsort -command rb_fanout_order $top] 0 [expr {$count - 1}]]
    }
    if {[llength $top] >= $count || !$truncated} break
  }
  foreach entry $top {
    lassign $entry fanout name net
    set line "{fanout_tag}\\t$fanout"
    for {set hop 0} {$net ne "" && $hop <= {hops}} {incr hop} {
      append line "\\t[$net getName]"
      set driver [rb_net_driver $net]
      if {$driver eq ""} { append line "\\t-\\t-"; break }
      lassign $driver pin kind inst
      append line "\\t$pin\\t$kind"
      set net [expr {$kind in {buf inv} ? [rb_driver_input $inst] : ""}]
    }
    puts $line
  }
}
"""


def report_tcl(corners: list[str], fanout_nets: int, modes: bool = False) -> str:
    """Tcl for the final reports that prints each path group's worst setup path and the `fanout_nets` signal nets of highest fanout, as tagged lines.

    On a multi-corner platform (`corners`) the paths are found at the setup-critical corner, the one with the worst setup slack, using `rb_find_corner` from the per-corner timing reports before it. With `modes`, each corner's SDC mode is named after it: its path groups are listed and searched in that mode, and the current mode is restored after. A failure loses its report, never the routed database: rb then reports it unavailable.
    """
    procs = (
        _PROCS.replace("{tag}", WORST_PATH_TAG)
        .replace("{fanout_tag}", FANOUT_TAG)
        .replace("{hops}", str(_DRIVER_HOPS))
    )
    if corners:
        corner = "\n".join(
            [
                'set rb_path_corner ""',
                "foreach rb_name {" + " ".join(corners) + "} {",
                "  set rb_scene [rb_find_corner $rb_name]",
                '  if {[info commands ::sta::worst_slack_scene] ne ""} {',
                "    set rb_slack [sta::worst_slack_scene $rb_scene max]",
                "  } else {",
                "    set rb_slack [sta::worst_slack_corner $rb_scene max]",
                "  }",
                '  if {$rb_path_corner eq "" || $rb_slack < $rb_path_slack} {',
                "    set rb_path_corner $rb_name",
                "    set rb_path_slack $rb_slack",
                "  }",
                "}",
                "set rb_corner_args [list -corner $rb_path_corner]",
            ]
        )
    else:
        corner = 'set rb_path_corner "-"\nset rb_corner_args {}'
    search = (
        "foreach rb_group [sta::path_group_names] {\n"
        "  foreach rb_end [find_timing_paths -path_delay max "
        "-path_group [list $rb_group] {*}$rb_corner_args] {\n"
        "    rb_worst_path $rb_group $rb_path_corner $rb_end\n"
        "  }\n"
        "}"
    )
    if corners and modes:
        search = (
            "set rb_was_mode [sta::cmd_mode_name]\n"
            "set_mode $rb_path_corner\n"
            "try {\n"
            + "".join(f"  {line}\n" for line in search.splitlines())
            + "} finally {\n"
            "  set_mode $rb_was_mode\n"
            "}"
        )
    search = "".join(f"  {line}\n" for line in search.splitlines())
    return (
        "# Each path group's worst setup path and the signal nets of highest fanout;\n"
        f"# rb resolves their names into {WORST_PATHS_REPORT} and {FANOUT_REPORT}.\n"
        + procs
        + "if {[catch {\n"
        + "".join(f"  {line}\n" for line in corner.splitlines())
        + search
        + '} rb_err]} { puts "rb: worst path report unavailable: $rb_err" }\n'
        + "if {[catch {\n"
        + f"  rb_fanout_nets {fanout_nets}\n"
        + '} rb_err]} { puts "rb: fanout report unavailable: $rb_err" }\n'
    )


@dataclass
class WorstPath:
    group: str
    corner: str | None
    slack_ps: float | None
    levels: int | None
    buffers: int
    startpoint: str
    # The net that names each end: the start pin's own, and the endpoint register's output or the endpoint pin's own.
    start_net: str
    endpoint: str
    end_net: str


@dataclass
class Driver:
    # The net this hop's pin drives.
    net: str
    # The driver pin, None when the net has no driver.
    pin: str | None
    # `buf`, `inv`, `cell` or `port`.
    kind: str


@dataclass
class FanoutNet:
    fanout: int
    net: str
    # From the listed net back through buffers and inverters.
    drivers: list[Driver]


@dataclass
class Reports:
    paths: list[WorstPath] = field(default_factory=list)
    nets: list[FanoutNet] = field(default_factory=list)
    # Why a report has no entries, by `worst path` or `fanout`, when the final reports said.
    errors: dict[str, str] = field(default_factory=dict)


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse(text: str) -> Reports:
    """Parse the tagged lines of `report_tcl` from the final reports' part of `pnr.log`."""
    reports = Reports()
    for line in text.splitlines():
        fields = line.split("\t")
        if fields[0] == WORST_PATH_TAG and len(fields) == 10:
            _, group, corner, slack, levels, buffers, start, snet, end, enet = fields
            seconds = _float(slack)
            reports.paths.append(
                WorstPath(
                    group=group,
                    corner=None if corner == "-" else corner,
                    slack_ps=(
                        seconds * 1e12
                        if seconds is not None and abs(seconds) < 1e20
                        else None
                    ),
                    levels=int(levels) if levels.isdigit() else None,
                    buffers=int(buffers) if buffers.isdigit() else 0,
                    startpoint=start,
                    start_net=snet,
                    endpoint=end,
                    end_net=enet,
                )
            )
        elif fields[0] == FANOUT_TAG and len(fields) >= 5 and fields[1].isdigit():
            hops = fields[2:]
            drivers = [
                Driver(
                    net=hops[i],
                    pin=None if hops[i + 1] == "-" else hops[i + 1],
                    kind=hops[i + 2] if i + 2 < len(hops) else "cell",
                )
                for i in range(0, len(hops) - 1, 3)
            ]
            reports.nets.append(
                FanoutNet(fanout=int(fields[1]), net=hops[0], drivers=drivers)
            )
    for m in _UNAVAILABLE_RE.finditer(text):
        reports.errors[m.group(1)] = m.group(2).strip()
    # A report that failed part way is incomplete, so none of its entries count.
    if "worst path" in reports.errors:
        reports.paths = []
    if "fanout" in reports.errors:
        reports.nets = []
    return reports


def split_name(name: str) -> list[str]:
    """Split an OpenROAD hierarchical name on its unescaped `/` and drop the escapes from each part."""
    parts = re.split(r"(?<!\\)/", name)
    return [re.sub(r"\\(.)", r"\1", part) for part in parts]


def _is_tool_name(name: str) -> bool:
    """Whether `name` is one Yosys made up rather than kept from the RTL."""
    return "$" in name or re.fullmatch(r"_\d+_(\[\d+\])?", name) is not None


class NetlistNames:
    """The instances and declarations of a structural Verilog netlist that a set of hierarchical names refer to.

    `block_params.NetlistReader` reads the netlist once and tokenises only the statements of the instances named by a part of one of `names`.
    """

    def __init__(self, path: str | None, top: str | None, names: list[str]):
        self.top = top
        self._reader: block_params.NetlistReader | None = None
        self._cells: dict[tuple[str, str], block_params.NetlistCell] = {}
        if not (path and top and os.path.isfile(path)):
            return
        wanted = {part for name in names for part in split_name(name)}
        with open(path, encoding="utf-8", errors="replace") as f:
            self._reader = block_params.NetlistReader(f.read())
        for cell in self._reader.cells(wanted):
            self._cells.setdefault((cell.module, cell.name), cell)

    def _scope(self, parts: list[str]) -> str | None:
        """The module the instance path `parts` leads into, or None when a part is not an instance there."""
        module = self.top if self._reader else None
        for part in parts:
            cell = self._cells.get((module, part)) if module else None
            if cell is None:
                return None
            module = cell.cell_type
        return module

    def cell(self, parts: list[str]) -> block_params.NetlistCell | None:
        """The instance at hierarchical path `parts`, or None when the netlist has no such instance."""
        scope = self._scope(parts[:-1])
        return self._cells.get((scope, parts[-1])) if scope else None

    def has_net(self, parts: list[str]) -> bool:
        """Whether the netlist declares the net at hierarchical path `parts`, a bit of a bus included."""
        scope = self._scope(parts[:-1])
        if scope is None:
            return False
        declared = self._reader.declared(scope)
        leaf = parts[-1]
        return leaf in declared or re.sub(r"\[\d+\]$", "", leaf) in declared


@dataclass
class Resolved:
    raw: str
    rtl: str | None
    src: str | None = None

    @property
    def resolved(self) -> bool:
        return self.rtl is not None

    @property
    def name(self) -> str:
        return self.rtl if self.rtl is not None else self.raw


def _src_location(src: str | None) -> str | None:
    """`file:line` for each location of a Yosys `src` attribute, without columns."""
    if not src:
        return None
    locations = [re.sub(r":(\d+)\.\d+(-\d+\.\d+)?$", r":\1", s) for s in src.split("|")]
    return ", ".join(dict.fromkeys(locations))


def resolve_pin(netlist: NetlistNames, pin: str, net: str) -> Resolved:
    """Resolve pin `pin` to an RTL name, `net` being the net that names it (see the module docstring)."""
    parts = split_name(pin)
    if len(parts) == 1:
        return Resolved(raw=pin, rtl=parts[0])
    inst = parts[:-1]
    cell = netlist.cell(inst)
    src = _src_location(cell.src) if cell else None
    if cell is not None and not _is_tool_name(inst[-1]):
        return Resolved(raw=pin, rtl=".".join(parts), src=src)
    if net:
        net_parts = split_name(net)
        if not _is_tool_name(net_parts[-1]) and netlist.has_net(net_parts):
            return Resolved(raw=pin, rtl=".".join(net_parts), src=src)
    if src:
        where = ", ".join(os.path.basename(s) for s in src.split(", "))
        scope = ".".join(inst[:-1])
        return Resolved(raw=pin, rtl=f"{scope} ({where})" if scope else where, src=src)
    return Resolved(raw=pin, rtl=None)


def resolve_driver(netlist: NetlistNames, net: FanoutNet) -> tuple[Resolved, str]:
    """Resolve the driver of `net`, through the buffers and inverters before it: the first driver that resolves and a note of what lies between, `""` for none; the net's own driver, unresolved, when none does."""
    first = None
    for hop, driver in enumerate(net.drivers):
        if driver.pin is None:
            break
        resolved = resolve_pin(netlist, driver.pin, driver.net)
        if resolved.resolved:
            between = net.drivers[:hop]
            inverters = sum(d.kind == "inv" for d in between)
            if not between:
                return resolved, ""
            note = f"behind {hop} buffer(s) or inverter(s)"
            return resolved, (f"inverted, {note}" if inverters % 2 else note)
        first = first or resolved
    return first or Resolved(raw="-", rtl=None), ""


def _ps(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "unavailable"


def _point(resolved: Resolved) -> str:
    detail = [resolved.raw] if resolved.name != resolved.raw else []
    if resolved.src:
        detail.append(f"src {resolved.src}")
    text = resolved.name + (f" [{'; '.join(detail)}]" if detail else "")
    return text if resolved.resolved else f"{text} (unresolved)"


def _unavailable(reports: Reports, key: str, empty: str) -> str:
    return f"unavailable: {reports.errors.get(key, empty)}"


def write_reports(
    artefact_dir: str,
    reports: Reports,
    netlist_path: str | None,
    top: str | None,
    fanout_nets: int,
) -> dict:
    """Resolve the names in `reports`, write `worst_paths.rpt` and `fanout.rpt` into `artefact_dir`, and return the result fields.

    `worst_path_*` describe the worst of the groups' paths and `max_net_fanout` the first listed net; a value that could not be computed is left out of the fields and reported unavailable in its file.
    """
    names = [p.startpoint for p in reports.paths] + [p.endpoint for p in reports.paths]
    names += [n for p in reports.paths for n in (p.start_net, p.end_net) if n]
    names += [
        x for net in reports.nets for d in net.drivers for x in (d.net, d.pin) if x
    ]
    netlist = NetlistNames(netlist_path, top, names)

    fields: dict = {}
    lines = ["Worst setup path of each path group" + _corner_note(reports), ""]
    worst = None
    for path in reports.paths:
        start = resolve_pin(netlist, path.startpoint, path.start_net)
        end = resolve_pin(netlist, path.endpoint, path.end_net)
        if worst is None or _slack_key(path) < _slack_key(worst[0]):
            worst = (path, start, end)
        levels = (
            f"{path.levels} ({path.buffers} buffer(s) or inverter(s))"
            if path.levels is not None
            else "unavailable"
        )
        lines += [
            f"Path group: {path.group}",
            f"  Slack (ps):   {_ps(path.slack_ps)}",
            f"  Logic levels: {levels}",
            f"  Startpoint:   {_point(start)}",
            f"  Endpoint:     {_point(end)}",
            "",
        ]
    if worst is None:
        lines += [_unavailable(reports, "worst path", "no setup path was reported"), ""]
    else:
        path, start, end = worst
        if path.levels is not None:
            fields["worst_path_levels"] = path.levels
        fields["worst_path_startpoint"] = start.name
        fields["worst_path_endpoint"] = end.name
        fields["worst_path_resolved"] = start.resolved and end.resolved
    _write(os.path.join(artefact_dir, WORST_PATHS_REPORT), lines)

    lines = [
        f"Signal nets of highest fanout, at most {fanout_nets}; clock, reset-tree "
        "and supply nets excluded",
        "",
    ]
    if reports.nets:
        fields["max_net_fanout"] = reports.nets[0].fanout
        lines.append(f"{'Fanout':>8}  Net  <-  Driver")
        for net in reports.nets:
            driver, note = resolve_driver(netlist, net)
            via = f" ({note})" if note else ""
            lines.append(f"{net.fanout:>8}  {net.net}  <-  {_point(driver)}{via}")
    else:
        lines.append(_unavailable(reports, "fanout", "no signal net was reported"))
    lines.append("")
    _write(os.path.join(artefact_dir, FANOUT_REPORT), lines)
    return fields


def _corner_note(reports: Reports) -> str:
    corners = {p.corner for p in reports.paths if p.corner}
    return (
        f", at the setup-critical corner {corners.pop()}" if len(corners) == 1 else ""
    )


def _slack_key(path: WorstPath) -> float:
    return path.slack_ps if path.slack_ps is not None else float("inf")


def _write(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
