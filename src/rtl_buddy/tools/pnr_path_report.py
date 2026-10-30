"""The worst-path and fanout reports of `rb pnr`'s final reports: `worst_paths.rpt` and `fanout.rpt`.

The final reports print one tagged line per path group's worst setup path and per high-fanout net (`report_tcl`); rb parses them from `pnr.log` (`parse`), resolves their instance and net names against the synthesis netlist the run read (`NetlistNames`), writes the two reports and returns the result fields (`write_reports`).

A pin's name is resolved when the netlist keeps it, tried in this order: a top-level port; the net the pin drives, or a register's output net, when the netlist declares it under a name of its own; the pin of an instance the netlist names itself (a macro or a block); or an instance with a `src` attribute, named by its hierarchy and source lines. Names Yosys made up (`_123_`, anything with `$`) and cells P&R added are not in the netlist under their own name, so they stay raw and are marked unresolved.
"""

import os
import re
from dataclasses import dataclass, field

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
proc rb_point_nets {pin registers} {
  if {[$pin is_top_level_port]} { return "" }
  if {[$pin is_driver]} {
    return [expr {[$pin net] eq "NULL" ? "" : [get_full_name [$pin net]]}]
  }
  if {![dict exists $registers [get_full_name [$pin instance]]]} { return "" }
  set nets {}
  set it [[$pin instance] pin_iterator]
  while {[$it has_next]} {
    set p [$it next]
    if {[$p is_driver] && [$p net] ne "NULL"} { lappend nets [get_full_name [$p net]] }
  }
  $it finish
  return [join $nets " "]
}
proc rb_is_buffer {cell} {
  return [expr {$cell ne "NULL" && ([$cell is_buffer] || [$cell is_inverter])}]
}
proc rb_worst_path {group corner path_end registers} {
  set path [$path_end path]
  set start [[$path start_path] pin]
  set end [$path_end pin]
  set end_inst [expr {[$end is_top_level_port] ? "" : [get_full_name [$end instance]]}]
  set seen [dict create]
  set buffers 0
  set reached 0
  foreach pin [$path pins] {
    if {$pin eq $start} { set reached 1; break }
    if {[$pin is_top_level_port] || [$pin is_hierarchical]} continue
    set inst [$pin instance]
    set name [get_full_name $inst]
    if {$name eq $end_inst || [dict exists $seen $name]} continue
    dict set seen $name 1
    if {[rb_is_buffer [$inst liberty_cell]]} { incr buffers }
  }
  set levels [expr {$reached ? [dict size $seen] : "-"}]
  puts "{tag}\\t$group\\t$corner\\t[$path_end slack]\\t$levels\\t$buffers\\t[get_full_name $start]\\t[rb_point_nets $start $registers]\\t[get_full_name $end]\\t[rb_point_nets $end $registers]"
}
proc rb_net_driver {net} {
  foreach it [$net getITerms] {
    if {[$it getIoType] eq "OUTPUT"} {
      return [list "[[$it getInst] getName]/[[$it getMTerm] getName]" [$it getInst]]
    }
  }
  foreach bt [$net getBTerms] {
    if {[$bt getIoType] eq "INPUT"} { return [list [$bt getName] ""] }
  }
  return {}
}
proc rb_buffer_input {inst} {
  if {![rb_is_buffer [sta::find_liberty_cell [[$inst getMaster] getName]]]} { return "" }
  foreach it [$inst getITerms] {
    if {[$it getIoType] eq "INPUT" && [$it getSigType] eq "SIGNAL"} { return [$it getNet] }
  }
  return ""
}
proc rb_reset_tree_nets {} {
  set nets [dict create]
  set queue {}
  foreach pin [all_registers -async_pins] {
    if {[$pin net] ne "NULL"} { lappend queue [$pin net] }
  }
  while {[llength $queue]} {
    set net [lindex $queue end]
    set queue [lreplace $queue end end]
    set db [sta::sta_to_db_net $net]
    if {[dict exists $nets $db]} continue
    dict set nets $db 1
    set it [$net pin_iterator]
    while {[$it has_next]} {
      set pin [$it next]
      if {![$pin is_driver] || [$pin is_top_level_port]} continue
      set inst [$pin instance]
      if {![rb_is_buffer [$inst liberty_cell]]} continue
      set inputs [$inst pin_iterator]
      while {[$inputs has_next]} {
        set p [$inputs next]
        if {[$p is_load] && [$p net] ne "NULL"} { lappend queue [$p net] }
      }
      $inputs finish
    }
    $it finish
  }
  return $nets
}
proc rb_fanout_order {a b} {
  set diff [expr {[lindex $b 0] - [lindex $a 0]}]
  if {$diff} { return $diff }
  return [string compare [lindex $a 1] [lindex $b 1]]
}
proc rb_fanout_nets {count} {
  set excluded [rb_reset_tree_nets]
  foreach net [sta::find_all_clk_nets] { dict set excluded $net 1 }
  set candidates {}
  foreach net [[ord::get_db_block] getNets] {
    if {[$net getSigType] in {POWER GROUND CLOCK} || [dict exists $excluded $net]} continue
    lappend candidates [list [expr {[$net getITermCount] + [$net getBTermCount]}] $net]
  }
  set top {}
  foreach candidate [lsort -integer -decreasing -index 0 $candidates] {
    lassign $candidate bound net
    if {[llength $top] >= $count && $bound < [lindex $top end 0]} break
    set fanout 0
    foreach it [$net getITerms] {
      if {[$it getIoType] ne "OUTPUT"} { incr fanout }
    }
    foreach bt [$net getBTerms] {
      if {[$bt getIoType] ne "INPUT"} { incr fanout }
    }
    if {$fanout == 0} continue
    lappend top [list $fanout [$net getName] $net]
    set top [lrange [lsort -command rb_fanout_order $top] 0 [expr {$count - 1}]]
  }
  foreach entry $top {
    lassign $entry fanout name net
    set line "{fanout_tag}\\t$fanout"
    for {set hop 0} {$net ne "" && $hop <= {hops}} {incr hop} {
      append line "\\t[$net getName]"
      set driver [rb_net_driver $net]
      if {$driver eq ""} { append line "\\t-"; break }
      append line "\\t[lindex $driver 0]"
      set inst [lindex $driver 1]
      set net [expr {$inst eq "" ? "" : [rb_buffer_input $inst]}]
    }
    puts $line
  }
}
"""


def report_tcl(corners: list[str], fanout_nets: int) -> str:
    """Tcl for the final reports that prints each path group's worst setup path and the `fanout_nets` signal nets of highest fanout, as tagged lines.

    On a multi-corner platform (`corners`) the paths are found at the setup-critical corner, the one with the worst setup slack, using `rb_find_corner` from the per-corner timing reports before it. A failure loses its report, never the routed database: rb then reports it unavailable.
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
    return (
        "# Each path group's worst setup path and the signal nets of highest fanout;\n"
        f"# rb resolves their names into {WORST_PATHS_REPORT} and {FANOUT_REPORT}.\n"
        + procs
        + "if {[catch {\n"
        + "".join(f"  {line}\n" for line in corner.splitlines())
        + "  set rb_registers [dict create]\n"
        + "  foreach rb_inst [all_registers -cells] "
        + "{ dict set rb_registers [get_full_name $rb_inst] 1 }\n"
        + "  foreach rb_group [sta::path_group_names] {\n"
        + "    foreach rb_end [find_timing_paths -path_delay max "
        + "-path_group [list $rb_group] {*}$rb_corner_args] {\n"
        + "      rb_worst_path $rb_group $rb_path_corner $rb_end $rb_registers\n"
        + "    }\n"
        + "  }\n"
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
    start_nets: list[str]
    endpoint: str
    end_nets: list[str]


@dataclass
class FanoutNet:
    fanout: int
    net: str
    # (net, driver pin) from the listed net back through buffers and inverters; a pin is None where the net has no driver.
    drivers: list[tuple[str, str | None]]


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
            _, group, corner, slack, levels, buffers, start, snets, end, enets = fields
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
                    start_nets=snets.split(),
                    endpoint=end,
                    end_nets=enets.split(),
                )
            )
        elif fields[0] == FANOUT_TAG and len(fields) >= 4 and fields[1].isdigit():
            hops = fields[2:]
            drivers = [
                (
                    hops[i],
                    None if i + 1 >= len(hops) or hops[i + 1] == "-" else hops[i + 1],
                )
                for i in range(0, len(hops), 2)
            ]
            reports.nets.append(
                FanoutNet(fanout=int(fields[1]), net=hops[0], drivers=drivers)
            )
    for m in _UNAVAILABLE_RE.finditer(text):
        reports.errors[m.group(1)] = m.group(2).strip()
    return reports


def split_name(name: str) -> list[str]:
    """Split an OpenROAD hierarchical name on its unescaped `/` and drop the escapes from each part."""
    parts = re.split(r"(?<!\\)/", name)
    return [re.sub(r"\\(.)", r"\1", part) for part in parts]


def _is_tool_name(name: str) -> bool:
    """Whether `name` is one Yosys made up rather than kept from the RTL."""
    return "$" in name or re.fullmatch(r"_\d+_(\[\d+\])?", name) is not None


_IDENT = r"(\\\S+|[A-Za-z_][\w$]*)"
_MODULE_RE = re.compile(rf"^\s*module\s+{_IDENT}")
_ATTR_RE = re.compile(r"^\s*\(\*(.*)\*\)\s*$")
_SRC_RE = re.compile(r'\bsrc\s*=\s*"([^"]*)"')
_DECL_RE = re.compile(
    rf"^\s*(?:wire|reg|input|output|inout)\b(?:\s+signed)?\s*(?:\[[^\]]*\])?\s*{_IDENT}\s*;"
)
_CELL_RE = re.compile(rf"^\s*{_IDENT}\s+(?:#\(.*\)\s*)?{_IDENT}\s*\(")
_KEYWORDS = frozenset(
    {
        "module",
        "endmodule",
        "assign",
        "wire",
        "reg",
        "input",
        "output",
        "inout",
        "always",
        "initial",
        "parameter",
        "localparam",
        "function",
        "task",
    }
)


def _unescape(ident: str) -> str:
    return ident[1:] if ident.startswith("\\") else ident


@dataclass
class _Module:
    cells: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    wires: set[str] = field(default_factory=set)


class NetlistNames:
    """The instance and net names of a structural Verilog netlist that a set of hierarchical names refer to, read in one pass.

    Only the cells and wires named by a part of one of `names` are kept, so a large netlist costs one scan and little memory.
    """

    def __init__(self, path: str | None, top: str | None, names: list[str]):
        self.top = top
        self._modules: dict[str, _Module] = {}
        wanted: set[str] = set()
        for name in names:
            for part in split_name(name):
                wanted.add(part)
                wanted.add(re.sub(r"\[\d+\]$", "", part))
        if path and top and os.path.isfile(path):
            self._read(path, wanted)

    def _read(self, path: str, wanted: set[str]) -> None:
        module: _Module | None = None
        src: str | None = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _ATTR_RE.match(line)
                if m:
                    found = _SRC_RE.search(m.group(1))
                    if found:
                        src = found.group(1)
                    continue
                m = _MODULE_RE.match(line)
                if m:
                    module = self._modules.setdefault(_unescape(m.group(1)), _Module())
                    src = None
                    continue
                if module is None:
                    continue
                m = _DECL_RE.match(line)
                if m:
                    name = _unescape(m.group(1))
                    if name in wanted:
                        module.wires.add(name)
                    src = None
                    continue
                m = _CELL_RE.match(line)
                if m and m.group(1) not in _KEYWORDS:
                    name = _unescape(m.group(2))
                    if name in wanted:
                        module.cells[name] = (_unescape(m.group(1)), src)
                src = None

    def _scope(self, parts: list[str]) -> tuple[_Module | None, list[str]]:
        """The module the instance path `parts` leads into, and the instance names walked; None when a part is not an instance there."""
        module = self._modules.get(self.top) if self.top else None
        for part in parts:
            if module is None or part not in module.cells:
                return None, []
            module = self._modules.get(module.cells[part][0])
        return module, parts

    def cell(self, parts: list[str]) -> tuple[str, str | None] | None:
        """The (cell type, src) of the instance at hierarchical path `parts`, or None when the netlist has no such instance."""
        scope, _ = self._scope(parts[:-1])
        if scope is None:
            return None
        return scope.cells.get(parts[-1])

    def has_net(self, parts: list[str]) -> bool:
        """Whether the netlist declares the net at hierarchical path `parts`, a bit of a bus included."""
        scope, _ = self._scope(parts[:-1])
        if scope is None:
            return False
        leaf = parts[-1]
        return leaf in scope.wires or re.sub(r"\[\d+\]$", "", leaf) in scope.wires


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


def resolve_pin(netlist: NetlistNames, pin: str, nets: list[str]) -> Resolved:
    """Resolve pin `pin`, whose instance drives `nets`, to an RTL name (see the module docstring)."""
    parts = split_name(pin)
    if len(parts) == 1:
        return Resolved(raw=pin, rtl=parts[0])
    inst = parts[:-1]
    cell = netlist.cell(inst)
    src = _src_location(cell[1]) if cell else None
    for net in nets:
        net_parts = split_name(net)
        if not _is_tool_name(net_parts[-1]) and netlist.has_net(net_parts):
            return Resolved(raw=pin, rtl=".".join(net_parts), src=src)
    if cell is None:
        return Resolved(raw=pin, rtl=None)
    if not _is_tool_name(inst[-1]):
        return Resolved(raw=pin, rtl=".".join(parts), src=src)
    if src:
        where = ", ".join(os.path.basename(s) for s in src.split(", "))
        scope = ".".join(inst[:-1])
        return Resolved(raw=pin, rtl=f"{scope} ({where})" if scope else where, src=src)
    return Resolved(raw=pin, rtl=None)


def resolve_driver(netlist: NetlistNames, net: FanoutNet) -> tuple[Resolved, int]:
    """Resolve the driver of `net`, through the buffers and inverters before it: the first driver that resolves, and how many buffers it is behind; the net's own driver, unresolved, when none does."""
    first = None
    for hop, (driven, pin) in enumerate(net.drivers):
        if pin is None:
            break
        resolved = resolve_pin(netlist, pin, [driven])
        if resolved.resolved:
            return resolved, hop
        first = first or resolved
    return first or Resolved(raw="-", rtl=None), 0


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
    names += [n for p in reports.paths for n in p.start_nets + p.end_nets]
    names += [x for net in reports.nets for hop in net.drivers for x in hop if x]
    netlist = NetlistNames(netlist_path, top, names)

    fields: dict = {}
    lines = ["Worst setup path of each path group" + _corner_note(reports), ""]
    worst = None
    for path in reports.paths:
        start = resolve_pin(netlist, path.startpoint, path.start_nets)
        end = resolve_pin(netlist, path.endpoint, path.end_nets)
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
            driver, behind = resolve_driver(netlist, net)
            via = f" (behind {behind} buffer(s) or inverter(s))" if behind else ""
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
