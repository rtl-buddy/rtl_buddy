import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field as dc_field, replace
from datetime import datetime
from decimal import Decimal
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.dispatch import JobResources
from ..config.openroad_threads import ThreadPlan, parse_reported_threads, plan_threads
from ..config.pdk import RotatedMacroFootprint
from ..config.pnr import (
    CHECKPOINT_STAGES,
    POST_ROUTE_REPAIR_STAGE,
    ROTATED_ORIENTATIONS,
    BlockageType,
    GdsMode,
    MacroAnchor,
    MacroPlacement,
    PnrBlockage,
    PnrConfig,
    PnrFloorplan,
    with_planner_files,
)
from ..config.pnr_repair import (
    GlobalRouteRepair,
    PnrMode,
    PostRouteRepair,
    PostRouteReroute,
    RepairDesign,
    Reroute,
)
from ..dispatch.base import PnrStageJobSpec
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..pnr.klayout.def2stream import REPORT_SCHEMA
from ..process_utils import run_managed_process
from ..runner.pnr_results import PnrFailResults, PnrPassResults, PnrResults
from . import openroad_corners
from .artifact_paths import (
    clear_managed_outputs,
    clear_stale_artefacts,
    project_relative,
    project_root_or_none,
)
from . import (
    block_params,
    pnr_abstract,
    pnr_checkpoints,
    pnr_dispatch,
    pnr_placement_check,
    pnr_stages,
)
from .synth_yosys import probe_block_parameters
from .liberty_units import (
    LibertyTimeUnitError,
    format_time_unit,
    liberty_time_unit_ps,
)


_TEMPLATE_PACKAGE = "rtl_buddy.pnr"
_TEMPLATE_FILE = "flow.tcl.template"
_MACRO_PACK_FILE = "macro_pack.tcl"
# Cuts the rows under hard placement blockages before tap insertion; see `_floorplan_directives`.
_CUT_ROWS_FILE = "cut_rows.tcl"
# Replaces the pdn-config's core grid with a run's `pdn:` block; see `_pdn_block`.
_PDN_GRID_FILE = "pdn_grid.tcl"
# `floorplan.macros` directives; see `_macro_directives_block`.
_MACROS_FILE = "macros.tcl"
# `floorplan.pins` checks and commands; see `_floorplan_pins_block`.
_PINS_FILE = "pins.tcl"
# The OpenROAD#11735 workaround for macros turned by 90 degrees; see `_global_placement_blocks`.
_ROTATED_MACROS_FILE = "rotated_macros.tcl"
# Ties `blocks:` supply pins and joins top-layer ones to the parent's straps; see `_block_power_tcl`.
_BLOCK_POWER_FILE = "block_power.tcl"
# Sources the run's `hooks:` files; see `_hook_procs_block`.
_HOOKS_FILE = "hooks.tcl"
# Wraps `rb::hook::run` in a session a later session follows, to record the commands a hook changes.
_HOOK_COMMANDS_FILE = "hook_commands.tcl"
# Runs an `eco` post-route repair round's repairs on Steiner trees; see `_post_route_repair_script`.
_ECO_REPAIR_FILE = "eco_repair.tcl"

# Non-log files the flow writes under `$OUT_DIR`; `test_pnr.py` checks this covers every one.
_FLOW_OUTPUT_NAMES = (
    "route.drc.rpt",
    "timing.rpt",
    "electrical.rpt",
    "{design}.def",
    "{design}.routed.v",
    "{design}.routed.sdc",
    "{design}.routed.odb",
    # Written only when the PDK declares `rcx-rules`, but cleared on every run so `rb power` never reads a stale SPEF.
    "{design}.routed.spef",
)

# Cleared with the OpenROAD outputs so a rerun that never reaches KLayout leaves no old layout.
_KLAYOUT_OUTPUT_NAMES = ("{design}.gds", "{design}.png")

# Cleared by suffix so no `{design}` is needed and no previous design's files survive. Logs, `.py` and `.json` files must stay unmatched.
_MANAGED_OUTPUT_SUFFIXES = (
    ".def",
    ".routed.v",
    ".routed.sdc",
    ".routed.odb",
    ".routed.spef",
    ".gds",
    ".png",
)

# Suffix of the OpenRCX SPEF written when the PDK declares `rcx-rules`; `rb power` resolves it by this.
ROUTED_SPEF_SUFFIX = ".routed.spef"

_FIXED_OUTPUT_NAMES = tuple(
    name for name in _FLOW_OUTPUT_NAMES if "{design}" not in name
)

# An input, not an output: cleared up front only (see `_clear_stale_outputs`).
_SCRIPT_NAME = "pnr.tcl"

# Written by global routing only when it overflows. It explains a failed route, so like the script it is cleared up front only.
_CONGESTION_REPORT_NAME = "congestion.rpt"

# Where `harden: true` elaborates the top to record its parameter values; see `_record_block_parameters`.
_PARAM_PROBE_DIR = "param_probe"
# The synth netlist with `blocks:` parameter overrides stripped, read in its place; an input like the script.
_BLOCK_NETLIST_NAME = "pnr_netlist.v"
# Public: `rb power` uses it to check the SPEF and ODB came from the same run.
PNR_SCRIPT_NAME = _SCRIPT_NAME

# Printed by `block_power.tcl` for each block supply pin it cannot tie or join, and for each stray pin shape off the parent's straps.
_BLOCK_POWER_ERROR_TAG = "RB-BLOCK-POWER-ERROR:"
_BLOCK_POWER_WARNING_TAG = "RB-BLOCK-POWER-WARNING:"

# Judged by presence, so it is cleared with the GDS and PNG.
_DEF2STREAM_REPORT_NAME = "def2stream.report.json"

# Only `rb pnr-export` writes it; a P&R run clears it because it replaces the DEF it describes.
_EXPORT_PROVENANCE_NAME = "export.provenance.json"

#: Bumped on an incompatible change to the export provenance document.
EXPORT_PROVENANCE_SCHEMA = 1

#: Default render size; `--png-width` and `--png-height` override it.
DEFAULT_PNG_WIDTH = 2048
DEFAULT_PNG_HEIGHT = 2048


_DONT_USE_VIOLATION_TAG = "RB-DONT-USE-VIOLATION:"

# Printed once by the final reports: the routed design's logical and physical-only instance counts.
_CELL_COUNT_TAG = "RB-CELL-COUNT:"
#: The total row of global routing's usage table (GRT-0096); the last number is the total overflow.
_GR_TOTAL_RE = re.compile(
    r"^Total\s+\d+\s+\d+\s+[\d.]+%\s+\d+\s*/\s*\d+\s*/\s*(\d+)\s*$", re.MULTILINE
)

_CELL_COUNT_RE = re.compile(
    rf"^{re.escape(_CELL_COUNT_TAG)} routed (\d+) physical (\d+)\s*$", re.MULTILINE
)

# Printed once per check by the final reports: the routed design's max-slew, max-capacitance and max-fanout violator counts.
_ELECTRICAL_TAG = "RB-ELECTRICAL:"
_ELECTRICAL_RE = re.compile(
    rf"^{re.escape(_ELECTRICAL_TAG)} max_(slew|capacitance|fanout) (\d+)\s*$",
    re.MULTILINE,
)
# Result field per check, and the word a description uses for it.
_ELECTRICAL_FIELDS = {
    "slew": ("max_slew_violation_count", "max-slew"),
    "capacitance": ("max_capacitance_violation_count", "max-capacitance"),
    "fanout": ("max_fanout_violation_count", "max-fanout"),
}

# OpenROAD's warnings that a pre-route step saw zero wire R/C: EST-0018 from `estimate_parasitics -placement`, CTS-0104 from clock-tree latency balancing.
_NO_WIRE_RC_CODES = ("EST-0018", "CTS-0104")

# Where the `pnr.no_wire_rc` warning sends the reader.
_NO_WIRE_RC_DOCS = "rb docs show concepts/pnr#set-wire-rc"

_STA_CELL_NOT_FOUND = re.compile(
    r"^\[WARNING STA-0122\] cell '(.+)' not found\.$", re.M
)
# Warning for a `lib/cell` pattern whose library does not exist; no STA-0122 follows.
_STA_LIBRARY_NOT_FOUND = re.compile(
    r"^\[WARNING STA-0121\] library '(.+)' not found\.$", re.M
)


def _dont_use_check_tcl(cells: list[str]) -> str:
    """Return Tcl that fails the run if a don't-use cell is in the placed design, naming each offender.

    `set_dont_use` only stops the resizer and CTS from choosing a cell, so cells already in the netlist need this check. It runs before fill insertion and before any output is written. The result is empty when no cell is excluded.
    """
    if not cells:
        return ""
    return (
        '\nputs ">>> Don\'t-use check"\n'
        "set rb_dont_use_masters [dict create]\n"
        f"foreach rb_pattern [list {' '.join(cells)}] {{\n"
        "  foreach rb_cell [get_lib_cells -quiet $rb_pattern] {\n"
        "    dict set rb_dont_use_masters [get_name $rb_cell] $rb_pattern\n"
        "  }\n"
        "}\n"
        "set rb_dont_use_hits 0\n"
        "foreach inst [[ord::get_db_block] getInsts] {\n"
        "  set master [[$inst getMaster] getName]\n"
        "  if {[dict exists $rb_dont_use_masters $master]} {\n"
        f'    puts "{_DONT_USE_VIOLATION_TAG} [$inst getName] $master '
        '[dict get $rb_dont_use_masters $master]"\n'
        "    incr rb_dont_use_hits\n"
        "  }\n"
        "}\n"
        "if {$rb_dont_use_hits > 0} {\n"
        '  error "$rb_dont_use_hits instance(s) of dont-use-cells in the '
        'routed design"\n'
        "}\n"
    )


def tcl_source(path: str) -> str:
    """Return a Tcl `source` of `path`, double-quoted with substitutions suppressed."""
    return f"source {pnr_checkpoints._tcl_quote(path)}"


def _hook_block(title: str, path: str) -> str:
    """Return a flow block sourcing a PDK Tcl hook, or `""` when the hook is unset.

    The block carries its own leading newline so an unset hook renders the template byte-identically.
    """
    if not path:
        return ""
    return f'\nputs ">>> {title}"\n{tcl_source(path)}\n'


def _run_hook_lines(point: str, paths, indent: str = "") -> str:
    """Return one announce-and-source pair per hook file of `point`, each line ending in a newline."""
    return "".join(
        f"{indent}puts {pnr_checkpoints._tcl_quote(f'>>> Hook {point}: {path}')}\n"
        f"{indent}rb::hook::run {point} {pnr_checkpoints._tcl_quote(path)}\n"
        for path in paths
    )


def _run_hook_block(point: str, hooks: dict) -> str:
    """Return the flow block sourcing a run's hook files at `point`, or `""` when it has none.

    The block carries its own leading newline so a run without hooks renders the template byte-identically.
    """
    paths = hooks.get(point)
    return f"\n{_run_hook_lines(point, paths)}" if paths else ""


def _macro_hook_block(fp: PnrFloorplan, paths) -> str:
    """Return the macro branch's `macro-placement` hook lines, which replace rb's placement.

    Without `floorplan.macros`, `$rb_movable_macros` is set to every macro so a hook can read it either way.
    """
    movable = "" if fp.macros else "  set rb_movable_macros $macros\n"
    return movable + _run_hook_lines("macro-placement", paths, "  ")


def _final_reports(log_text: str) -> str:
    """Return `pnr.log` from its last `>>> Final reports` line on, or all of it without one.

    A report a hook or an earlier step prints never reads as the result.
    """
    starts = [m.start() for m in re.finditer(r"^>>> Final reports$", log_text, re.M)]
    return log_text[starts[-1] :] if starts else log_text


def _hook_role(point: str) -> str:
    """Return the manifest input role for a run hook point, such as `hook_after_cts`."""
    return "hook_" + point.replace("-", "_")


def _hook_procs_block(hooks: dict) -> str:
    """Return the Tcl that defines `rb::hook::run`, or `""` when the run has no hooks."""
    if not hooks:
        return ""
    return f"\n{files(_TEMPLATE_PACKAGE).joinpath(_HOOKS_FILE).read_text()}"


def _with_hook_tracer(substitutions: dict) -> dict:
    """`substitutions` with the hook-command tracer after `rb::hook::run`, for a session a later session follows; unchanged for a run without hooks."""
    if not substitutions["hook_procs_block"]:
        return substitutions
    return {
        **substitutions,
        "hook_procs_block": substitutions["hook_procs_block"]
        + files(_TEMPLATE_PACKAGE).joinpath(_HOOK_COMMANDS_FILE).read_text(),
    }


def _buffer_ports_tcl(enabled: bool, buffer_cell: str | None) -> str:
    """Return the port-buffering step, or `""` when the run leaves ports unbuffered.

    As ORFS `global_place.tcl` does by default: `buffer_ports` after IO placement and before global placement, which places the new buffers, and `repair_design` then sizes them. A port's internal loads then hang off a buffer, so the port sees one buffer input and a hardened block's Liberty pin capacitance stops depending on its fanout. OpenROAD skips clock, constant and special nets. Without the platform's `port-buffer` it picks its own buffer, which on sky130hd is the delay cell `clkdlybuf4s50_1`.
    """
    if not enabled:
        return ""
    cell = f" -buffer_cell {buffer_cell}" if buffer_cell else ""
    return f'\nputs ">>> Port buffers"\nbuffer_ports -inputs -outputs{cell}\n'


#: The flow's global route; a `reroute: full` global-route repair runs it again.
_GLOBAL_ROUTE_CMD = (
    "global_route -congestion_iterations 20 "
    "-congestion_report_file $CONGESTION_RPT -verbose\n"
)
#: Closes an incremental global route around a repair; it logs the usage table again.
_END_INCREMENTAL_CMD = (
    "global_route -end_incremental -congestion_report_file $CONGESTION_RPT -verbose\n"
)


def _fmt_number(value: float) -> str:
    """A number as Tcl reads it, without float noise."""
    return f"{value:.10g}"


def _legalize_tcl(dpl_options: str) -> str:
    """Legalize the cells a repair added or moved inside an incremental global route, which reroutes their nets, as ORFS does after each repair."""
    return (
        "global_route -start_incremental\n"
        f"detailed_placement{dpl_options}\n"
        + _END_INCREMENTAL_CMD
        + "check_placement -verbose\n"
    )


def _repair_steps_tcl(
    title: str,
    design: RepairDesign | None,
    setup_ns: float | None,
    hold_ns: float | None,
    ps_per_unit: float | None,
    legalize: str | None,
) -> str:
    """Return `repair_design`, then setup and hold `repair_timing`, each after a log line naming it.

    Margins are given in ns and passed in the Liberty time unit (`ps_per_unit` picoseconds), which is what `repair_timing` reads; the log line gives both. A zero margin is the tool's default and passes no option. The hold repair also passes a set setup margin, as ORFS's `repair_timing_helper` does, so it keeps that much setup slack on the paths it delays.

    Between `repair_design` and `repair_timing` runs `legalize` (`_legalize_tcl`), as in ORFS: `repair_timing` on global-route parasitics builds buffer trees from the global routes, and the nets `repair_design` split have none until they are rerouted (RSZ-0074). `None` runs nothing there, for repairs that build them from Steiner trees. The caller legalizes after the timing repair.
    """
    lines = []
    if design is not None:
        command = f"repair_design{design.options()}"
        lines += [f'puts ">>> {title}: {command}"', command]
        if legalize is not None and (setup_ns is not None or hold_ns is not None):
            lines += [
                f'puts ">>> {title}: legalize after repair_design"',
                legalize.rstrip("\n"),
            ]

    def _option(kind: str, margin: float) -> tuple[str, str]:
        """`margin`'s option (empty for 0) and its log note."""
        if margin == 0:
            return "", f"{kind} margin 0 ns"
        if ps_per_unit is None:
            raise RuntimeError("the Liberty time unit is unknown; cannot set a margin")
        value = _fmt_number(margin * 1000.0 / ps_per_unit)
        return f" -{kind}_margin {value}", f"{kind} margin {_fmt_number(margin)} ns"

    for kind, margin in (("setup", setup_ns), ("hold", hold_ns)):
        if margin is None:
            continue
        options = [_option(kind, margin)]
        if kind == "hold" and setup_ns is not None:
            options.append(_option("setup", setup_ns))
        command = f"repair_timing -{kind}" + "".join(opt for opt, _ in options)
        note = ", ".join(n for _, n in options)
        if any(opt for opt, _ in options):
            note += f" in Liberty time unit {format_time_unit(ps_per_unit)}"
        lines += [f'puts ">>> {title}: {command} ({note})"', command]
    return "".join(f"{line}\n" for line in lines)


def _post_cts_repair_tcl(platform, mode: PnrMode) -> str:
    """Return the post-CTS timing repair on placement parasitics: setup when `post-cts-setup-repair` is set, then hold. A probe skips it.

    The block carries its own leading newline, as the template once held the text itself.
    """
    if mode is PnrMode.PROBE:
        return '\nputs ">>> Post-CTS timing repair: skipped (mode: probe)"\n'
    setup = "repair_timing -setup\n" if platform.get_post_cts_setup_repair() else ""
    return (
        '\nputs ">>> Post-CTS timing repair"\n'
        "estimate_parasitics -placement\n"
        f"{setup}repair_timing -hold\n"
    )


def _global_route_repair_tcl(
    repair: GlobalRouteRepair | None,
    mode: PnrMode,
    ps_per_unit: float | None,
    dpl_options: str = "",
) -> str:
    """Return the repair on global-route parasitics, as ORFS runs it, or `""` when the platform and run leave it off. A probe skips it.

    The global-route estimate includes the wires that post-CTS repair (on placement parasitics) could not see. The resizer reroutes the nets it changes; the cells it adds are legalized, then `reroute: incremental` reroutes their nets incrementally and `full` runs the flow's global route again. The closing route is `-verbose`, so the log repeats the congestion table after the repair. `dpl_options` follows its `detailed_placement`, as it does the flow's others.

    A hold-only repair with no margin is what `global-route-hold-repair: true` stands for, and renders as that key always has, so its scripts and checkpoints compare equal.
    """
    if repair is None:
        return ""
    if mode is PnrMode.PROBE:
        return 'puts ">>> Global-route repair: skipped (mode: probe)"\n'
    if repair == GlobalRouteRepair(hold_margin=0.0):
        return (
            'puts ">>> Post-global-route hold repair"\n'
            "estimate_parasitics -global_routing\n"
            "repair_timing -hold\n"
            "global_route -start_incremental\n"
            f"detailed_placement{dpl_options}\n"
            + _END_INCREMENTAL_CMD
            + "check_placement -verbose\n"
        )
    title = "Global-route repair"
    legalize = _legalize_tcl(dpl_options)
    steps = _repair_steps_tcl(
        title,
        repair.repair_design,
        repair.setup_margin,
        repair.hold_margin,
        ps_per_unit,
        legalize,
    )
    head = (
        f'puts ">>> {title} (reroute {repair.reroute.value})"\n'
        "estimate_parasitics -global_routing\n"
    )
    if repair.reroute is Reroute.FULL:
        return (
            head
            + steps
            + f"detailed_placement{dpl_options}\n"
            + "check_placement -verbose\n"
            + f'puts ">>> {title}: global route"\n'
            + _GLOBAL_ROUTE_CMD
        )
    return head + steps + legalize


def _layer_adjustment_tcl(platform) -> str:
    """Return the `set_global_routing_layer_adjustment` line over the signal layers, as ORFS does, or `""` when the platform sets no adjustment."""
    adjustment = platform.get_routing_layer_adjustment()
    if adjustment is None:
        return ""
    return f"set_global_routing_layer_adjustment $SIGNAL_LAYERS {adjustment:g}\n"


def _reference_hpwl_option(platform) -> str:
    """Return ` -reference_hpwl <n>` for global placement, or `""` for the placer's own reference."""
    hpwl = platform.get_placement_reference_hpwl()
    return f" -reference_hpwl {hpwl:.15g}" if hpwl is not None else ""


def _routability_options(platform) -> str:
    """Return ` -routability_driven` and ` -routability_use_grt` for global placement when set, else `""`."""
    options = ""
    if platform.get_placement_routability_driven():
        options += " -routability_driven"
    if platform.get_placement_routability_use_grt():
        options += " -routability_use_grt"
    return options


def _cts_apply_ndr_option(platform) -> str:
    """Return ` -apply_ndr <value>` for the CTS invocation, or `""` for the tool's default."""
    ndr = platform.get_cts_apply_ndr()
    return f" -apply_ndr {ndr}" if ndr is not None else ""


def max_fanout_cmd(platform) -> str:
    """Return the `set_max_fanout` command that follows each `read_sdc`, or `""` when the platform sets no max fanout."""
    fanout = platform.get_max_fanout()
    return f"set_max_fanout {fanout} [current_design]" if fanout is not None else ""


def run_output_paths(artefact_dir: str, design: str) -> list[str]:
    """Return the absolute paths of every non-log artefact one pnr run produces."""
    return [
        os.path.join(artefact_dir, name.format(design=design))
        for name in _FLOW_OUTPUT_NAMES + _KLAYOUT_OUTPUT_NAMES
    ] + [
        os.path.join(artefact_dir, _DEF2STREAM_REPORT_NAME),
        os.path.join(artefact_dir, _EXPORT_PROVENANCE_NAME),
        os.path.join(artefact_dir, _CONGESTION_REPORT_NAME),
    ]


_KLAYOUT_PACKAGE = "rtl_buddy.pnr.klayout"

_DEF2STREAM_INPUTS_NAME = "def2stream.inputs.json"


@dataclass(frozen=True)
class Def2StreamInputs:
    """Every file KLayout stream-out reads, resolved and in reader order.

    `missing` is the subset that is configured but not on disk.
    """

    tech: str
    gds: list[str] = dc_field(default_factory=list)
    lef: list[str] = dc_field(default_factory=list)
    missing: list[str] = dc_field(default_factory=list)


# `complete`: every cell has layout, allow-listed empties included. `incomplete`: a GDS with layout-less cells. `failed`: no usable layout.
GDS_COMPLETE = "complete"
GDS_INCOMPLETE = "incomplete"
GDS_FAILED = "failed"

_DESC_CELL_LIMIT = 3


@dataclass(frozen=True)
class GdsExport:
    """What a requested KLayout export delivered, returned by `OpenRoadPnr.export_layout`.

    `desc` is the one-line qualifier for summary rows; it is empty when the export delivered everything asked for.
    """

    mode: str
    status: str
    png_requested: bool = False
    gds_path: str | None = None
    png_path: str | None = None
    missing_cells: list[str] = dc_field(default_factory=list)
    allowed_empty_cells: list[str] = dc_field(default_factory=list)
    desc: str = ""

    @property
    def delivered(self) -> bool:
        """Return whether the export produced everything asked for, complete.

        A `strict` run that answers False publishes nothing and fails; a `preview` run keeps what it has.
        """
        return self.status == GDS_COMPLETE and (
            self.png_path is not None or not self.png_requested
        )

    def result_fields(self) -> dict:
        """Return the export as result-dict keys for the machine output."""
        return {
            "gds_path": self.gds_path,
            "png_path": self.png_path,
            "gds_mode": str(self.mode),
            "gds_status": self.status,
            "gds_missing_cells": list(self.missing_cells),
            "gds_missing_cell_count": len(self.missing_cells) or None,
            "gds_allowed_empty_cells": list(self.allowed_empty_cells),
        }


def describe_missing_cells(cells: list[str]) -> str:
    """Return the missing-cell count with names, such as `2 cells (a, b)`."""
    shown = ", ".join(cells[:_DESC_CELL_LIMIT])
    if len(cells) > _DESC_CELL_LIMIT:
        shown += f", +{len(cells) - _DESC_CELL_LIMIT} more"
    return f"{len(cells)} cell{'s' if len(cells) != 1 else ''} ({shown})"


_DEF_DESIGN_RE = re.compile(r"^\s*DESIGN\s+(\S+)\s*;", re.MULTILINE)

_DEF_HEADER_BYTES = 64 * 1024


def read_def_design_name(path: str) -> str | None:
    """Return the design named by the DEF's `DESIGN <name> ;` header, or None.

    This is the staleness check for an export over a saved result. It deliberately does not compare mtimes, which a checkout or copy rewrites.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_DEF_HEADER_BYTES)
    except OSError:
        return None
    match = _DEF_DESIGN_RE.search(head.decode("utf-8", "replace"))
    return match.group(1) if match else None


def count_drc_violations(path: str) -> int:
    """The violations a detailed-route DRC report lists, each a `violation type:` line and its detail lines; 0 without the report."""
    try:
        with open(path) as f:
            return sum(1 for line in f if line.startswith("violation type:"))
    except OSError:
        return 0


def _file_fingerprint(path: str | None) -> dict | None:
    """Return `{path, size, sha256}` for a file, or None without a path; an unreadable file gets a null size and digest."""
    if not path:
        return None
    digest = hashlib.sha256()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return {"path": path, "size": None, "sha256": None}
    return {"path": path, "size": size, "sha256": digest.hexdigest()}


def _dedup_paths(paths) -> list[str]:
    """Return the paths in order with empties dropped and duplicates removed by resolved path.

    KLayout re-registers every master in a LEF passed twice.
    """
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


MIN_OPENROAD_VERSION = "25Q1"


def _parse_version_token(version: str) -> tuple:
    """Return a comparable tuple from an OpenROAD version such as `26Q2-911-g...` or `v2.0`; unknown formats fall back to the raw string."""
    m = re.match(r"^v?(\d+)(?:[.Qq](\d+))?", version.strip())
    if not m:
        return (version,)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    return (major, minor)


def _resolve_klayout_exe() -> str | None:
    return shutil.which("klayout")


def _tcl_microns(value: float) -> str:
    """Format a micron coordinate as a Tcl number, to the nanometre."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _tcl_exact(value: float) -> str:
    """Format a micron coordinate as a Tcl number at full precision, without an exponent, so OpenROAD rounds it once, to the DBU."""
    text = f"{Decimal(repr(float(value))):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _blockage_command(blockage: PnrBlockage) -> str:
    """The `create_blockage` command for one `floorplan.blockages` entry."""
    region = "{" + " ".join(_tcl_microns(v) for v in blockage.rect) + "}"
    command = f"create_blockage -region {region}"
    if blockage.type is BlockageType.SOFT:
        return f"{command} -soft"
    if blockage.type is BlockageType.PARTIAL:
        return f"{command} -max_density {blockage.max_density * 100:.6g}"
    return command


def _floorplan_directives(fp: PnrFloorplan) -> tuple[str, str]:
    """Return `(blockages_block, macro_pack_directives)` for `floorplan.blockages` and `floorplan.macro-anchor`.

    Both are empty when the floorplan sets neither key. Blockages with `lifetime: global-placement` are left to `_global_placement_blocks`. The blockages block runs right after the floorplan. Hard blockages also become `MACRO_KEEPOUTS`, which the packer keeps macros out of, and the rows under them are cut, so the later tap insertion leaves them empty (rtl_buddy#773); soft and partial ones only thin standard cells. The directives are the anchor and keep-outs appended to the `rb::macro_pack::solve` call.
    """
    lines = []
    has_keepouts = False
    for index, cutout in enumerate(fp.core_cutouts):
        # A cut-out is a hard blockage that must overlap the core; OpenROAD's site
        # snapping may move the core edges a little inside the requested ones.
        has_keepouts = True
        region = "{" + " ".join(_tcl_microns(v) for v in cutout) + "}"
        lines.append(f"set blockage_box [[create_blockage -region {region}] getBBox]")
        lines.append(
            "set rb_core [[ord::get_db_block] getCoreArea]\n"
            "if {[$blockage_box xMin] >= [$rb_core xMax] || [$blockage_box yMin] >= [$rb_core yMax]"
            " || [$blockage_box xMax] <= [$rb_core xMin] || [$blockage_box yMax] <= [$rb_core yMin]} "
            f"{{ error {{floorplan.core-cutouts[{index}] {region} um does not overlap "
            "the core; a cut-out is carved out of the core} }"
        )
        lines.append(
            "lappend MACRO_KEEPOUTS "
            "[list [$blockage_box xMin] [$blockage_box yMin] [$blockage_box xMax] [$blockage_box yMax]]"
        )
    blockages = fp.flow_blockages()
    for blockage in blockages:
        command = _blockage_command(blockage)
        if blockage.type is BlockageType.HARD:
            has_keepouts = True
            lines.append(f"set blockage_box [[{command}] getBBox]")
            lines.append(
                "lappend MACRO_KEEPOUTS "
                "[list [$blockage_box xMin] [$blockage_box yMin] [$blockage_box xMax] [$blockage_box yMax]]"
            )
        else:
            lines.append(command)
    blockages_block = ""
    if lines:
        header = [
            'puts ">>> Placement blockages"'
            if blockages
            else 'puts ">>> Core cut-outs"'
        ]
        if has_keepouts:
            header.append("set MACRO_KEEPOUTS {}")
            procs = files(_TEMPLATE_PACKAGE).joinpath(_CUT_ROWS_FILE).read_text()
            lines += [
                procs.rstrip("\n"),
                'puts ">>>   cut [rb::rows::cut_under [ord::get_db_block] $MACRO_KEEPOUTS]'
                ' row(s) under hard placement blockages"',
            ]
        blockages_block = "\n" + "\n".join(header + lines) + "\n"

    directives = ""
    if has_keepouts:
        directives = f" \\\n      {fp.macro_anchor.value} $MACRO_KEEPOUTS"
    elif fp.macro_anchor is not MacroAnchor.LOWER_LEFT:
        directives = f" \\\n      {fp.macro_anchor.value}"
    return blockages_block, directives


def _initialize_floorplan_tcl(fp: PnrFloorplan) -> str:
    """Return the `initialize_floorplan` command: explicit die and core areas, or utilization sizing."""
    if fp.die_area is not None and fp.core_area is not None:
        die = " ".join(_tcl_microns(v) for v in fp.die_area)
        core = " ".join(_tcl_microns(v) for v in fp.core_area)
        return (
            "initialize_floorplan \\\n"
            "    -site      $SITE \\\n"
            f"    -die_area  {{{die}}} \\\n"
            f"    -core_area {{{core}}}"
        )
    return (
        "initialize_floorplan \\\n"
        "    -site         $SITE \\\n"
        "    -utilization  $CORE_UTIL_PCT \\\n"
        "    -aspect_ratio $CORE_ASPECT \\\n"
        "    -core_space   $CORE_MARGIN"
    )


def _pdn_grid_tcl(pdn) -> str:
    """Return the Tcl that defines a run's `pdn:` core grid, after its pdn-config is sourced without one."""
    ring = pdn.ring
    lines = [
        files(_TEMPLATE_PACKAGE).joinpath(_PDN_GRID_FILE).read_text().rstrip("\n"),
        "rb::pdn::source_base $PDN_CONFIG",
        "set rb_pdn_grid [rb::pdn::define_core_grid]",
    ]
    extend = " -extend_to_core_ring" if ring is not None else ""
    if ring is not None:
        lines.append(
            f"add_pdn_ring -grid $rb_pdn_grid -layers {_tcl_list(ring.layers)} "
            f"-widths {_tcl_microns(ring.width)} "
            f"-spacings {_tcl_microns(ring.spacing)} "
            f"-core_offsets {_tcl_microns(ring.offset)}"
        )
    for stripe in pdn.stripes:
        command = (
            f"add_pdn_stripe -grid $rb_pdn_grid -layer {{{stripe.layer}}} "
            f"-width {_tcl_microns(stripe.width)}"
        )
        if stripe.followpins:
            command += " -followpins"
        else:
            command += f" -pitch {_tcl_microns(stripe.pitch)}"
            if stripe.offset is not None:
                command += f" -offset {_tcl_microns(stripe.offset)}"
            if stripe.spacing is not None:
                command += f" -spacing {_tcl_microns(stripe.spacing)}"
        lines.append(command + extend)
    for lower, upper in pdn.connect:
        lines.append(f"add_pdn_connect -grid $rb_pdn_grid -layers {{{lower} {upper}}}")
    return "\n".join(lines) + "\n"


def _tcl_list(items) -> str:
    """A braced Tcl list of names `config.pnr` has already checked for Tcl-unsafe characters."""
    return "{" + " ".join(items) + "}"


def _floorplan_pins_block(fp: PnrFloorplan) -> str:
    """Return the `floorplan.pins` and pins-file Tcl run just before `place_pins`, or "" without entries or `pins-complete`.

    Exact pins go first, inline entries before the file's, so a side constraint that also matches one leaves it where it is: `place_pins` keeps a FIRM pin. Under `pins-complete` the block records which entries cover each port and ends with the coverage check.
    """
    if not fp.pins and not fp.pins_complete:
        return ""
    lines = [
        'puts ">>> Pin constraints (floorplan.pins)"',
        files(_TEMPLATE_PACKAGE).joinpath(_PINS_FILE).read_text().rstrip("\n"),
    ]
    if fp.pins_complete:
        lines.append("set rb::pins::track 1")
    if any(pin.location is not None for pin in fp.pins):
        lines.append(
            f'puts "{pnr_placement_check.DBU_TAG} '
            '[[ord::get_db_block] getDbUnitsPerMicron]"'
        )
    snap = "" if fp.pin_snap else " 0"
    ordered = sorted(enumerate(fp.pins), key=lambda item: item[1].location is None)
    for index, pin in ordered:
        where = f"{{{pin.label or f'floorplan.pins[{index}]'}}}"
        number = _tcl_exact if pin.exact else _tcl_microns
        if pin.location is not None:
            x, y = (number(v) for v in pin.location)
            size = (
                _tcl_list(number(v) for v in pin.size) if pin.size is not None else "{}"
            )
            lines.append(
                f"rb::pins::place {where} {{{pin.names[0]}}} {x} {y} "
                f"{{{pin.layer or ''}}} {size}{snap}"
            )
            continue
        region = ""
        if pin.side is not None:
            start = number(pin.start) if pin.start is not None else "*"
            end = number(pin.end) if pin.end is not None else "*"
            interval = "*" if start == end == "*" else f"{start}-{end}"
            region = f"{pin.side.value}:{interval}"
        lines.append(
            f"rb::pins::constrain {where} {_tcl_list(pin.names)} {{{region}}} "
            f"{int(pin.group)} {int(pin.order)}"
        )
    if fp.pins_complete:
        lines.append("rb::pins::check_complete")
    return "\n".join(lines) + "\n"


def _macro_directives_block(fp: PnrFloorplan) -> str:
    """Return the `floorplan.macros` and macros-file Tcl that opens the macro branch, or "" without directives.

    Inline entries come first. It leaves the macros still to place in `rb_movable_macros`; located ones are LOCKED and, for the packer, keep-outs.
    """
    if not fp.macros:
        return ""
    lines = [
        '  puts ">>> Macro directives (floorplan.macros)"',
        files(_TEMPLATE_PACKAGE).joinpath(_MACROS_FILE).read_text().rstrip("\n"),
        "  set MACRO_DIRECTIVES {}",
    ]
    for index, macro in enumerate(fp.macros):
        number = _tcl_exact if macro.exact else _tcl_microns
        fields = [
            f"{{{macro.label or f'floorplan.macros[{index}]'}}}",
            f"{{{macro.instance}}}",
            _tcl_list(number(v) for v in macro.location)
            if macro.location is not None
            else "{}",
            macro.orientation or "{}",
            _tcl_list(number(v) for v in macro.halo)
            if macro.halo is not None
            else "{}",
        ]
        lines.append(f"  lappend MACRO_DIRECTIVES [list {' '.join(fields)}]")
    snap = "" if fp.macro_snap else " 0"
    lines.append(
        "  set rb_movable_macros [rb::macros::apply $block $macros $MACRO_DIRECTIVES "
        "$site_grid_dbu [expr {int(round([ord::microns_to_dbu $MACRO_HALO]))}] "
        f"$dbu_per_micron{snap}]"
    )
    return "\n".join(lines) + "\n"


def _no_macros_block(fp: PnrFloorplan) -> str:
    """Return the `else` of the macro branch that fails the run when entries locate macros but the netlist has none, or "" when no entry has a location.

    It names up to three located entries.
    """
    located = [
        f"{macro.label or f'floorplan.macros[{index}]'} ({macro.instance})"
        for index, macro in enumerate(fp.macros)
        if macro.location is not None
    ]
    if not located:
        return ""
    more = f", +{len(located) - 3} more" if len(located) > 3 else ""
    entries = "1 entry gives" if len(located) == 1 else f"{len(located)} entries give"
    message = (
        f"floorplan.macros: the netlist has no hard macros, but {entries} a macro "
        f"location: {', '.join(located[:3])}{more}"
    )
    return f" else {{\n  error {{{message}}}\n}}"


_PACK_PLACEMENT = """\
  set core [$block getCoreArea]
  set halo_dbu [expr {{int(round([ord::microns_to_dbu $MACRO_HALO]))}}]
  set footprints {{}}
  foreach inst {packed} {{
{footprint}  }}
  set placement [rb::macro_pack::solve \\
      [list [$core xMin] [$core yMin] [$core xMax] [$core yMax]] \\
      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron{directives}]
  foreach inst {packed} {{
    lassign [dict get $placement [$inst getName]] x y
    $inst setLocation $x $y
    $inst setPlacementStatus FIRM
    puts ">>>   placed [$inst getName] at ($x, $y) DBU"
  }}
"""

# A macro the packer places at its master's size, as every macro is R0 before `floorplan.macros` orients one.
_MASTER_FOOTPRINT = """\
    set master [$inst getMaster]
    lappend footprints [list [$inst getName] [$master getWidth] [$master getHeight]]
"""
# A macro the packer places at its oriented size: an R90 macro is its master's height wide.
_ORIENTED_FOOTPRINT = """\
    set bbox [$inst getBBox]
    lappend footprints [list [$inst getName] [$bbox getDX] [$bbox getDY]]
"""

# OpenROAD RTL-MP. The halo option spelling depends on the OpenROAD version, so both are emitted.
_RTL_MP_PLACEMENT = """\
  file mkdir $OUT_DIR/rtlmp
  if {[llength [info commands set_macro_base_halo]]} {
    set_macro_base_halo $MACRO_HALO $MACRO_HALO
    rtl_macro_placer -report_directory $OUT_DIR/rtlmp
  } else {
    rtl_macro_placer -halo_width $MACRO_HALO -halo_height $MACRO_HALO \\
        -report_directory $OUT_DIR/rtlmp
  }
  foreach inst $macros {
    lassign [$inst getLocation] x y
    puts ">>>   placed [$inst getName] at ($x, $y) DBU [$inst getOrient] (rtl-mp)"
  }
"""


def _macro_place_block(fp: PnrFloorplan, directives: str) -> str:
    """Return the macro-placement Tcl for the flow's macro branch.

    With `floorplan.macros` the packer packs only `rb_movable_macros`, keeping out of the located ones (in `MACRO_KEEPOUTS`), and is skipped when every macro is located.
    """
    if fp.macro_placement is MacroPlacement.RTL_MP:
        return _RTL_MP_PLACEMENT
    if not fp.macros:
        return _PACK_PLACEMENT.format(
            directives=directives, packed="$macros", footprint=_MASTER_FOOTPRINT
        )
    if not directives.endswith("$MACRO_KEEPOUTS"):
        anchor = f" \\\n      {fp.macro_anchor.value}"
        directives = f"{anchor} $MACRO_KEEPOUTS"
    body = _PACK_PLACEMENT.format(
        directives=directives,
        packed="$rb_movable_macros",
        footprint=_ORIENTED_FOOTPRINT,
    )
    return f"  if {{[llength $rb_movable_macros] > 0}} {{\n{body}  }}\n"


@dataclass(frozen=True)
class GlobalPlacementBlocks:
    """The Tcl around `global_placement`: `create` before it, `destroy` right after it, and `dpl_options` after every `detailed_placement`.

    All are empty when the run has no global-placement-only blockage and no macro can end up rotated, so the template renders byte-identically. `session` sets `dpl_options`' variable again in a staged session after the place stage.
    """

    create: str = ""
    destroy: str = ""
    dpl_options: str = ""
    session: str = ""


#: Run hook points before global placement, where a hook may turn a macro.
_HOOKS_BEFORE_GLOBAL_PLACEMENT = (
    "session",
    "after-floorplan",
    "macro-placement",
    "after-pdn",
    "after-pins",
)


def _may_rotate(fp: PnrFloorplan, hooks: dict) -> bool:
    """Whether a macro may be turned by 90 degrees before global placement: by a `floorplan.macros` or macros-file entry, or by a run hook; RTL-MP only flips macros."""
    return any(macro.orientation in ROTATED_ORIENTATIONS for macro in fp.macros) or any(
        point in hooks for point in _HOOKS_BEFORE_GLOBAL_PLACEMENT
    )


def _global_placement_blocks(
    fp: PnrFloorplan, footprint: str, hooks: dict, *, checkpoints: bool
) -> GlobalPlacementBlocks:
    """Return the Tcl for `lifetime: global-placement` blockages and `placement.rotated-macro-footprint`.

    The blockages are created immediately before `global_placement` and destroyed immediately after by the handles the flow created, so no checkpoint, row cut, packer keep-out or RTL-MP run sees them. With checkpoints, the floorplan checkpoint is written before they exist. `footprint` acts only when a macro is rotated at run time: `block` adds a hard blockage over each rotated macro's unrotated footprint (see `rotated_macros.tcl`), `diamond-legalizer` passes `-use_diamond_legalizer` to every `detailed_placement`.
    """
    blockages = fp.global_placement_blockages()
    rotated = _may_rotate(fp, hooks) and footprint != RotatedMacroFootprint.NONE
    block_rotated = rotated and footprint == RotatedMacroFootprint.BLOCK
    diamond = rotated and footprint == RotatedMacroFootprint.DIAMOND_LEGALIZER
    if not blockages and not rotated:
        return GlobalPlacementBlocks()
    lines = []
    if checkpoints and (blockages or block_rotated):
        lines.append("rb::ckpt::checkpoint_for global_placement enter")
    rotated_lines = [
        files(_TEMPLATE_PACKAGE)
        .joinpath(_ROTATED_MACROS_FILE)
        .read_text()
        .rstrip("\n"),
        "set rb_rotated_macros [rb::rotated::macros $macros]",
    ]
    if rotated:
        lines += rotated_lines
    destroy = ""
    if blockages or block_rotated:
        lines.append("set RB_GP_BLOCKAGES {}")
        if blockages:
            lines.append('puts ">>> Global-placement-only blockages"')
        for blockage in blockages:
            lines.append(f"lappend RB_GP_BLOCKAGES [{_blockage_command(blockage)}]")
        if block_rotated:
            lines.append(
                "lappend RB_GP_BLOCKAGES "
                "{*}[rb::rotated::block_footprints $block $rb_rotated_macros $site_grid_dbu]"
            )
        destroy = (
            "foreach rb_blockage $RB_GP_BLOCKAGES { odb::dbBlockage_destroy $rb_blockage }\n"
            "if {[llength $RB_GP_BLOCKAGES] > 0} {\n"
            '  puts ">>>   removed [llength $RB_GP_BLOCKAGES] global-placement-only '
            'blockage(s)"\n'
            "}\n"
        )
    dpl_options = ""
    session = ""
    if diamond:
        options = [
            "set RB_DPL_OPTIONS {}",
            "if {[llength $rb_rotated_macros] > 0} {",
            "  set RB_DPL_OPTIONS -use_diamond_legalizer",
            '  puts ">>>   [llength $rb_rotated_macros] rotated macro(s): '
            'detailed_placement -use_diamond_legalizer"',
            "}",
        ]
        lines += options
        dpl_options = " {*}$RB_DPL_OPTIONS"
        session = "\n".join(rotated_lines + options) + "\n"
    return GlobalPlacementBlocks(
        create="\n".join(lines) + "\n",
        destroy=destroy,
        dpl_options=dpl_options,
        session=session,
    )


#: Printed by a run that `--stop-after` ended, after its stage report; with the requested stage it tells a stopped run from one that ended early by accident.
STOPPED_MARKER = ">>> STOPPED after "
_STAGE_DONE_MARKER = pnr_stages.STAGE_DONE_MARKER
#: `fail_stage` of a run whose OpenROAD session exited 0 before the end marker its flow prints.
INCOMPLETE_FAIL_STAGE = "incomplete"
#: `fail_stage` of a run that did not write a stage checkpoint it had to write.
CHECKPOINT_FAIL_STAGE = "checkpoint"
#: Heads a stopped run's stage report, which its result is parsed from.
_STAGE_REPORT_MARKER = ">>> Stage report "
#: Heads a post-route repair round's timing, which rb reads to decide whether the round repairs.
_ROUND_TIMING_MARKER = ">>> Post-route repair timing, round "
#: Ends each post-route repair session, followed by `<round> extraction` or `<round> repair`.
_ROUND_DONE_MARKER = ">>> End of post-route repair round "
#: The flow's detailed route, which each post-route repair round runs again.
_DETAILED_ROUTE_CMD = (
    "detailed_route \\\n"
    "    -output_drc  $OUT_DIR/route.drc.rpt \\\n"
    "    -output_maze $OUT_DIR/route.maze.log \\\n"
    "    -verbose {{ detailed_route_verbose }}\n"
)

#: The parasitics a stopped run's stage report times on: none before placement, the placement estimate until global routing, then the global-route estimate. After post-route repair the report session extracts them instead (see `_post_route_report_script`).
_STAGE_PARASITICS = {
    "floorplan": "",
    "place": "estimate_parasitics -placement\n",
    "cts": "estimate_parasitics -placement\n",
    "global_route": "estimate_parasitics -global_routing\n",
    "detailed_route": "estimate_parasitics -global_routing\n",
    "post_route_repair": "",
}


def _stage_report_tcl(stage: str, corner_reports: str) -> str:
    """Return the report a `--stop-after` run prints after its last checkpoint, ending the flow.

    Area, setup and hold worst slack and TNS (per corner on a multi-corner platform), on `stage`'s parasitics. The global-route overflow is in the router's own table above it.
    """
    return (
        f'puts "{_STAGE_REPORT_MARKER}{stage}"\n'
        f"{_STAGE_PARASITICS[stage]}"
        "report_design_area\n"
        "report_worst_slack -max\n"
        "report_worst_slack -min\n"
        "report_tns\n"
        f"report_tns -min{corner_reports}\n"
        f'puts "{STOPPED_MARKER}{stage}"\n'
        "exit 0\n"
    )


def _checkpoint_name(stage: str) -> str:
    return f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}"


def _read_checkpoint_tcl(stage: str, where: str = "RB_PRED_DIR") -> str:
    """Return the prelude lines that open `stage`'s checkpoint from the directory in `$<where>`: its database, its SDC and the flow globals saved with it."""
    name = _checkpoint_name(stage)
    return (
        f'puts ">>> Reading checkpoint {name}"\n'
        f"read_db [file join ${where} {name}.odb]\n"
        f"read_sdc [file join ${where} {name}.sdc]\n"
        f"source [file join ${where} {name}.state.tcl]\n"
    )


def _read_global_route_tcl(stage: str) -> str:
    """Return the prelude lines that restore `stage`'s global route for `estimate_parasitics -global_routing`: its segments.

    The detailed router reads the guides the database keeps; `read_guides` would rewrite them without their via layers, and the route would differ from a one-session run's.
    """
    name = _checkpoint_name(stage)
    return (
        f'puts ">>> Reading global route {name}"\n'
        f"read_global_route_segments [file join $RB_PRED_DIR {name}.segments]\n"
    )


#: OpenROAD 26Q4 crashes in `sta::max_fanout_violation_count`, which the final reports call, until a fanout check has run in the session; a one-session run's repairs have run one.
_FANOUT_CHECK_TCL = "catch {sta::max_fanout_min_slack}\n"


def _threads_block(plan: ThreadPlan) -> str:
    """The flow's thread-count block for `plan`, or "" when it sets none."""
    tcl = plan.tcl()
    return f'\nputs ">>> Threads"\n{tcl}\n' if tcl else ""


def _maxrss_kb(maxrss: int) -> int:
    """`ru_maxrss` in kB; macOS reports bytes."""
    return maxrss // 1024 if sys.platform == "darwin" else maxrss


def _run_session(cmd: list[str], *, stdout, env, cwd: str) -> tuple[int, str, dict]:
    """Run one OpenROAD session in `cwd` and return its exit code, stderr and the resources it used: wall time, CPU time (user + system) and peak RSS of the process.

    The session is a managed process, so terminating rb terminates it.
    """
    begin = time.monotonic()
    result = run_managed_process(
        cmd,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        rusage=True,
    )
    usage = result.rusage
    return (
        result.returncode,
        result.stderr or "",
        {
            "wall_s": round(time.monotonic() - begin, 3),
            "cpu_s": round(usage.ru_utime + usage.ru_stime, 2) if usage else None,
            "peak_rss_kb": _maxrss_kb(usage.ru_maxrss) if usage else None,
            "measured_by": "process",
        },
    )


def _worst_slack(log_text: str, kind: str) -> float | None:
    """The `report_worst_slack -max` (`kind="max"`) or `-min` value in `log_text`, in the Liberty time unit."""
    m = re.search(rf"^worst slack {kind}\s+([-\d.]+)", log_text, re.MULTILINE)
    return float(m.group(1)) if m else None


def _unconstrained(log_text: str, kind: str) -> bool:
    """Whether `report_worst_slack -max` (`kind="max"`) or `-min` in `log_text` is INF: no path of that kind is constrained."""
    return bool(re.search(rf"^worst slack {kind}\s+INF\s*$", log_text, re.MULTILINE))


def _total_negative_slack(log_text: str, kind: str = "max") -> float | None:
    """Parse `report_tns` (`kind="max"`, setup) or `report_tns -min` (`"min"`, hold).

    A setup line may omit `max`; a hold line must say `min`.
    """
    qualifier = r"(?:max\s+)?" if kind == "max" else r"min\s+"
    m = re.search(rf"^tns\s+{qualifier}([-\d.]+)", log_text, re.MULTILINE)
    return float(m.group(1)) if m else None


def _post_route_file(suffix: str) -> str:
    """A file of the post-route repair stage in the checkpoint run directory, `06_post_route_repair<suffix>`."""
    return f"{_checkpoint_name(POST_ROUTE_REPAIR_STAGE)}{suffix}"


#: What the post-route repair stage reads from the checkpoint run directory besides its scripts (see `run_post_route_repair`).
POST_ROUTE_PLAN = _post_route_file(".plan.json")


def stage_scripts(run_dir: str, stage: str) -> list[str]:
    """The script files of `stage` in the staged run in `run_dir`: `NN_<stage>.tcl` and, for post-route repair, which runs other sessions and keeps that file as their record, every session in its plan."""
    files = [f"{_checkpoint_name(stage)}.tcl"]
    if stage == POST_ROUTE_REPAIR_STAGE:
        plan = json.loads(Path(run_dir, POST_ROUTE_PLAN).read_text())
        for entry in plan["rounds"]:
            files += [entry["extract"], entry["repair"]]
        if plan.get("report"):
            files.append(plan["report"])
    return [os.path.join(run_dir, name) for name in files]


@dataclass
class PostRouteOutcome:
    """How the post-route repair stage's sessions ended: the last session's exit code and stderr, why the stage failed if a session exited 0 without ending its step, the resources of all its sessions, and its log."""

    returncode: int
    stderr: str
    problem: str | None
    resources: dict
    log: bytes


def _round_timing(text: str, round_: int, plan: dict) -> dict:
    """Round `round_`'s worst slack and TNS, setup and hold, in ps, with each corner's on a multi-corner platform; a value the log lacks is None. `unconstrained` names each side whose worst slack is INF."""
    text = text[text.rfind(f"{_ROUND_TIMING_MARKER}{round_}") :]
    ps = plan["ps_per_unit"]

    def _ps(value):
        return value * ps if value is not None else None

    timing = {
        "wns_setup_ps": _ps(_worst_slack(text, "max")),
        "wns_hold_ps": _ps(_worst_slack(text, "min")),
        "tns_ps": _ps(_total_negative_slack(text)),
        "tns_hold_ps": _ps(_total_negative_slack(text, "min")),
        "unconstrained": [
            side
            for side, kind in (("setup", "max"), ("hold", "min"))
            if _unconstrained(text, kind)
        ],
    }
    if plan["corners"]:
        timing["corners"] = openroad_corners.parse_corner_timing(
            text, plan["corners"], ps
        )
    return timing


def _timing_met(timing: dict, sides) -> bool | None:
    """Whether the worst slack of each of `sides` (`setup`, `hold`: the ones the rounds repair) is non-negative on every corner (the worst over corners is reported); None when one is missing.

    An unconstrained side is met. With no side (a repair-design-only block) timing never stops the round.
    """
    if not sides:
        return False
    slacks = [
        timing[f"wns_{side}_ps"]
        for side in sides
        if side not in timing["unconstrained"]
    ]
    if any(slack is None for slack in slacks):
        return None
    return all(slack >= 0 for slack in slacks)


def _report_lines(path: str) -> int:
    """The non-blank lines of a DRC report; 0 when it is missing."""
    try:
        with open(path) as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _carry_checkpoint(run_dir: str, plan: dict) -> None:
    """Make the detailed route's checkpoint, with the reports it keeps, the post-route repair stage's when no round repaired: hard links where the filesystem allows, else copies."""
    carry = plan["carry"]
    target = _checkpoint_name(POST_ROUTE_REPAIR_STAGE)
    written = pnr_checkpoints.written_checkpoint(carry["dir"], carry["stage"]) or {}
    files = {}
    for kind, name in written.items():
        src = os.path.join(carry["dir"], str(name))
        if not os.path.isfile(src):
            continue
        dst_name = target + str(name).removeprefix(carry["name"])
        dst = os.path.join(run_dir, dst_name)
        if os.path.lexists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        files[kind] = dst_name
    pnr_checkpoints.record_checkpoint(
        run_dir,
        POST_ROUTE_REPAIR_STAGE,
        files,
        design=carry["design"],
        source=carry["name"],
    )


def run_post_route_repair(run_dir: str, session, *, run_name: str) -> PostRouteOutcome:
    """Run the post-route repair stage's sessions from the scripts and plan in `run_dir`.

    `session(path, label)` runs OpenROAD on one script and returns its exit code, stderr, resources and log. Round k's first session extracts the latest routed database and times every corner. The rounds stop when the worst slack of each side they repair (setup with `setup-margin` set, hold with `hold-margin` set) is non-negative on every corner or unconstrained, when that timing cannot be read, or when a round's detailed route has DRC violations; otherwise round k's second session repairs on its SPEF, routes and writes the stage's checkpoint, which the next round starts from. When no round repaired, the stage's checkpoint is the detailed route's. A session that fails, logs an `[ERROR` line or exits without ending its step ends the stage there. A `--stop-after post_route_repair` run ends with a session that reports timing on the extracted checkpoint.
    """
    plan = json.loads(Path(run_dir, POST_ROUTE_PLAN).read_text())
    sections: list[bytes] = []
    used = {"wall_s": 0.0, "cpu_s": 0.0, "peak_rss_kb": 0, "threads": 1}

    def _run(file: str, label: str, done: str) -> tuple[int, str, str | None, bool]:
        returncode, stderr, resources, section = session(
            os.path.join(run_dir, file), label
        )
        sections.append(section)
        Path(run_dir, file.removesuffix(".tcl") + ".log").write_bytes(section)
        used["wall_s"] += resources.get("wall_s") or 0.0
        used["cpu_s"] += resources.get("cpu_s") or 0.0
        used["peak_rss_kb"] = max(
            used["peak_rss_kb"], resources.get("peak_rss_kb") or 0
        )
        used["threads"] = max(used["threads"], resources.get("threads") or 1)
        problem = None
        if returncode == 0 and f"{done}\n".encode() not in section:
            problem = (
                f"stage {_checkpoint_name(POST_ROUTE_REPAIR_STAGE)}: OpenROAD exited "
                f"before the end of {label}"
            )
        return returncode, stderr, problem, _has_errors(section)

    def _outcome(returncode: int, stderr: str, problem: str | None):
        return PostRouteOutcome(
            returncode,
            stderr,
            problem,
            {
                "wall_s": round(used["wall_s"], 3),
                "cpu_s": round(used["cpu_s"], 2),
                "peak_rss_kb": used["peak_rss_kb"],
                "measured_by": "process",
                "threads": used["threads"],
            },
            b"".join(sections),
        )

    rounds = plan["rounds"]
    repaired = 0
    reason = f"{len(rounds)} round(s) repaired"
    for entry in rounds:
        round_ = entry["round"]
        returncode, stderr, problem, errors = _run(
            entry["extract"],
            f"round {round_} extraction",
            f"{_ROUND_DONE_MARKER}{round_} extraction",
        )
        if returncode != 0 or problem is not None or errors:
            return _outcome(returncode, stderr, problem)
        timing = _round_timing(sections[-1].decode(errors="replace"), round_, plan)
        met = _timing_met(timing, plan["sides"])
        fields = {
            "round": round_,
            "rounds": len(rounds),
            **timing,
            "repair": met is False,
            "setup_margin_ns": entry["setup_margin_ns"] if met is False else None,
            "hold_margin_ns": entry["hold_margin_ns"] if met is False else None,
        }
        log_event(logger, logging.INFO, "pnr.post_route_round", pnr=run_name, **fields)
        pnr_checkpoints.record_event(run_dir, "post_route_round", **fields)
        if met is not False:
            reason = "timing met" if met else "timing was not reported"
            break
        returncode, stderr, problem, errors = _run(
            entry["repair"],
            f"round {round_} repair",
            f"{_ROUND_DONE_MARKER}{round_} repair",
        )
        if returncode != 0 or problem is not None or errors:
            return _outcome(returncode, stderr, problem)
        repaired = round_
        drcs = _report_lines(plan["drc_report"])
        if drcs:
            reason = f"round {round_}'s detailed route has {drcs} DRC report line(s)"
            break
    if repaired == 0:
        _carry_checkpoint(run_dir, plan)
    log_event(
        logger,
        logging.INFO,
        "pnr.post_route_repair_done",
        pnr=run_name,
        repaired_rounds=repaired,
        rounds=len(rounds),
        reason=reason,
    )
    pnr_checkpoints.record_event(
        run_dir,
        "post_route_repair_done",
        repaired_rounds=repaired,
        rounds=len(rounds),
        reason=reason,
    )
    if plan["report"] is not None:
        returncode, stderr, problem, _errors = _run(
            plan["report"],
            "stage report",
            f"{STOPPED_MARKER}{POST_ROUTE_REPAIR_STAGE}",
        )
        return _outcome(returncode, stderr, problem)
    return _outcome(0, "", None)


@dataclass
class StageOutcome:
    """How one stage of a staged run ended: its session's exit code and stderr, why the stage failed when the session exited 0, the `fail_stage` that failure carries, and whether its log has an `[ERROR` line."""

    returncode: int
    stderr: str
    problem: str | None = None
    fail_stage: str | None = None
    errors: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.problem is None and not self.errors


def stage_checkpoint(run_dir: str, stage: str) -> dict | None:
    """Fingerprints of the checkpoint files a staged session wrote for `stage`, or None if it wrote none."""
    names = pnr_checkpoints.written_checkpoint(run_dir, stage)
    if not names:
        return None
    return {
        kind: _file_fingerprint(os.path.join(run_dir, str(name)))
        for kind, name in names.items()
    }


def checkpoint_error(run_dir: str | None, stage: str) -> str | None:
    """The error of the failed attempt to write `stage`'s checkpoint in `run_dir`, or None."""
    if run_dir is None:
        return None
    for event in pnr_checkpoints.read_progress(run_dir):
        if (
            event.get("event") == "checkpoint"
            and event.get("stage") == stage
            and event.get("status") == "error"
        ):
            return str(event.get("error"))
    return None


_SET_THREAD_COUNT_RE = re.compile(r"^set_thread_count (\d+)$", re.MULTILINE)


def _session_threads(script: str, log_text: str) -> int:
    """The threads a stage session ran on: the count OpenROAD reported, else the one its script set, else OpenROAD's default of one."""
    match = _SET_THREAD_COUNT_RE.search(script)
    if match is None:
        return 1
    reported = parse_reported_threads(log_text)
    return reported if reported is not None else int(match.group(1))


def _has_errors(section: bytes) -> bool:
    """Whether a session's log has an `[ERROR` line."""
    return any(
        line.startswith("[ERROR ")
        for line in section.decode(errors="replace").splitlines()
    )


def _stage_session(
    script: str,
    label: str,
    *,
    openroad: str,
    log,
    log_path: str,
    cwd: str,
    env: dict,
    run_name: str,
) -> tuple[int, str, dict, bytes]:
    """Run OpenROAD on `script`, a script the run recorded (inside `pnr_dispatch.job_scripts`, the job's copy of it), in `cwd`, appending its output to `log` (the open `log_path`); return its exit code, stderr, resources (with the threads it ran on) and its part of the log."""
    script = pnr_dispatch.session_script(script)
    cmd = [openroad, "-no_init", "-exit", script]
    log_event(
        logger,
        logging.DEBUG,
        "pnr.run_cmd",
        pnr=run_name,
        cmd=" ".join(cmd),
        log=log_path,
    )
    log.flush()
    start = os.path.getsize(log_path)
    try:
        with task_status(f"pnr {run_name} [openroad {label}]"):
            returncode, stderr, resources = _run_session(
                cmd, stdout=log, env=env, cwd=cwd
            )
    except OSError as e:
        returncode, stderr = 127, f"cannot run {openroad}: {e}"
        resources = {
            "wall_s": 0.0,
            "cpu_s": 0.0,
            "peak_rss_kb": 0,
            "measured_by": "process",
        }
    stderr = (stderr or "").strip()
    if stderr:
        log.write((stderr + "\n").encode())
    log.flush()
    with open(log_path, "rb") as f:
        f.seek(start)
        section = f.read()
    try:
        text = Path(script).read_text()
    except OSError:
        text = ""
    resources["threads"] = _session_threads(text, section.decode(errors="replace"))
    return returncode, stderr, resources, section


def run_stage(
    run_dir: str,
    stage: str,
    *,
    openroad: str,
    log,
    log_path: str,
    cwd: str,
    env: dict,
    run_name: str,
    fields: dict | None = None,
) -> StageOutcome:
    """Run stage `stage` of the staged run in `run_dir` and return how it ended.

    A local staged run calls this for each stage in turn, and a dispatched run's job (`rb _pnr-stage-job`) calls it for its one stage, so a stage is run and checked the same way either way. Everything the stage needs is in `run_dir`, which the head wrote before the first stage ran. The session runs in `cwd`, the run's artefact directory. Its output is appended to `log` (the open `log_path`, the run's `pnr.log`) as it runs and copied to `NN_<stage>.log` when it exits. `post_route_repair` runs its rounds' sessions (see `run_post_route_repair`), measured and logged as one. `stage_begin` records where the stage's part of `pnr.log` starts; `stage_end` records the exit, the wall time, CPU time, peak RSS and threads measured here and, for a clean stage, the checkpoint's fingerprints. `fields` are added to both events.

    The stage fails when a session exits non-zero, exits 0 before its end-of-stage line (`fail_stage: incomplete`) or without its checkpoint (`fail_stage: checkpoint`), or logs an `[ERROR` line.
    """
    name = _checkpoint_name(stage)
    log.flush()
    pnr_checkpoints.stage_begin(
        run_dir, stage, log_offset=os.path.getsize(log_path), **(fields or {})
    )

    def _session(script: str, label: str):
        return _stage_session(
            script,
            label,
            openroad=openroad,
            log=log,
            log_path=log_path,
            cwd=cwd,
            env=env,
            run_name=run_name,
        )

    problem = None
    fail_stage = None
    # The rounds stop at an `[ERROR` line, before the stage's checkpoint.
    stopped_on_error = False
    if stage == POST_ROUTE_REPAIR_STAGE:
        post = run_post_route_repair(
            run_dir,
            lambda script, label: _session(script, f"{name} {label}"),
            run_name=run_name,
        )
        returncode, stderr, problem = post.returncode, post.stderr, post.problem
        resources, section = post.resources, post.log
        stopped_on_error = _has_errors(section)
    else:
        returncode, stderr, resources, section = _session(
            os.path.join(run_dir, f"{name}.tcl"), name
        )
        if (
            returncode == 0
            and stage != "finish"
            and f"{_STAGE_DONE_MARKER}{name}\n".encode() not in section
        ):
            problem = f"stage {name}: OpenROAD exited before the end of the stage"
    if problem is not None:
        fail_stage = INCOMPLETE_FAIL_STAGE
    Path(run_dir, f"{name}.log").write_bytes(section)
    checkpoint = None
    if returncode == 0 and stage != "finish":
        checkpoint = stage_checkpoint(run_dir, stage)
        if problem is None and checkpoint is None and not stopped_on_error:
            problem = f"stage {name}: its checkpoint was not written"
            fail_stage = CHECKPOINT_FAIL_STAGE
    elif returncode != 0 and checkpoint_error(run_dir, stage):
        fail_stage = CHECKPOINT_FAIL_STAGE
    # An `[ERROR` line fails the run, as in one session, once this stage ends.
    outcome = StageOutcome(
        returncode=returncode,
        stderr=stderr,
        problem=problem,
        fail_stage=fail_stage,
        errors=_has_errors(section),
    )
    pnr_checkpoints.stage_end(
        run_dir,
        stage,
        returncode=returncode,
        resources=resources,
        status="ok" if outcome.ok else "error",
        checkpoint=checkpoint if outcome.ok else None,
        **(fields or {}),
    )
    return outcome


class OpenRoadPnr:
    """OpenROAD P&R backend.

    Reads the upstream `rb synth` netlist, runs floorplan, place, CTS, route and fill through a templated Tcl flow, and reports area, WNS setup/hold, TNS and DRC count.
    """

    def __init__(
        self,
        name: str,
        pnr_cfg: PnrConfig,
        suite_dir: str,
        root_cfg,
        openroad_executable: str = "openroad",
        emit_gds: bool = False,
        emit_png: bool = False,
        klayout_executable: str = "klayout",
        png_width: int = DEFAULT_PNG_WIDTH,
        png_height: int = DEFAULT_PNG_HEIGHT,
        gds_mode: str | None = None,
        klayout_props: str | None = None,
        accept_stale: bool = False,
        stop_after: str | None = None,
        resume: str | None = None,
        resume_from: str | None = None,
        dispatch=None,
    ):
        self.name = name
        self.suite_dir = suite_dir
        self.accept_stale = accept_stale
        self._mode = pnr_cfg.get_mode()
        # `--stop-after` as given; a probe without one stops after global route.
        self._requested_stop_after = stop_after
        # The stage after whose checkpoint the run ends.
        self.stop_after = (
            "global_route"
            if stop_after is None and self._mode is PnrMode.PROBE
            else stop_after
        )
        # `--resume` and `--from`: the run to resume (a run id or directory) and the first stage to run.
        self.resume = resume
        self.resume_from = resume_from
        # `--dispatch`: the backend that runs each stage as a job, or None to run here.
        self._dispatch = dispatch
        # Why submitting or waiting on the stage jobs failed; the scheduler's account of a failed stage job.
        self._dispatch_failure: str | None = None
        # The run's FAIL once rb gave the stage jobs up (an interrupt, `max-wait`, a failed submission).
        self._dispatch_results: PnrFailResults | None = None
        self._dispatch_reason: str | None = None
        # The failed stage job's first stderr line, which the job keeps whole.
        self._dispatch_first_line: str | None = None
        # A dispatched stage's thread plan, resolved against its job's CPUs.
        self._stage_thread_plans: dict[str, ThreadPlan] = {}
        # A resumed or dispatched run is staged whatever its config says.
        self._staged = (
            pnr_cfg.get_stages() or resume is not None or dispatch is not None
        )
        self._resume_source: pnr_stages.ResumeSource | None = None
        # The staged session that failed, for the result description, and the `fail_stage` its failure carries.
        self._failed_stage: str | None = None
        self._chain_fail_stage: str | None = None
        # Set from the resolved platform by `_configure_stages`: the run's stages, its post-route repair, and whether a one-session run continues in sessions after detailed route.
        self._stages: tuple[str, ...] = pnr_stages.TEMPLATE_STAGES
        self._post_route: PostRouteRepair | None = None
        self._split = False
        self.pnr_cfg = pnr_cfg
        self._configured_cfg = pnr_cfg
        self.root_cfg = root_cfg
        self.openroad_executable = openroad_executable
        self.emit_gds = emit_gds or emit_png
        self.emit_png = emit_png
        self.klayout_executable = klayout_executable
        self.png_width = png_width
        self.png_height = png_height
        self.gds_mode = gds_mode or pnr_cfg.get_gds_mode()
        if pnr_cfg.get_harden():
            # A hardened block is streamed into a parent, so its layout must be complete.
            self.emit_gds = True
            self.gds_mode = GdsMode.STRICT
        self.klayout_props = klayout_props
        self._thread_plan: ThreadPlan | None = None
        self._blocks: list[pnr_abstract.ResolvedBlock] = []

        artefact_root = Path(suite_dir) / "artefacts" / pnr_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)
        self._ckpt_run_dir: str | None = None
        self._openroad_returncode: int | None = None
        # Picoseconds per Liberty `time_unit`; set by `run()` once the blocks' Liberty is known.
        self._ps_per_unit: float | None = None
        # The netlist the script reads, when not the synth netlist itself; set by `_prepare_netlist`.
        self._openroad_netlist: str | None = None
        # The fingerprint that netlist will have, while a resume is checked before writing it.
        self._planned_netlist: dict | None = None

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, _SCRIPT_NAME)

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "pnr.log")

    def _resolve_netlist_path(self) -> str:
        """Return the path of the upstream synth run's tech-mapped netlist."""
        synth_cfg = self.pnr_cfg.resolve_synth_cfg()
        suite_dir = os.path.dirname(self.pnr_cfg.get_synth_suite_path())
        return os.path.join(
            suite_dir, "artefacts", synth_cfg.get_name(), "synth_netlist.v"
        )

    def _threads(self) -> ThreadPlan:
        """Return this run's OpenROAD thread plan, resolved once against the current allocation.

        A dispatched run's is the plan of the last stage it runs, whose session's thread count `pnr.log` reports last.
        """
        if self._thread_plan is None:
            if self._dispatch is not None:
                self._thread_plan = self._stage_threads(self._run_stages()[-1])
            else:
                self._thread_plan = plan_threads(
                    self.pnr_cfg.get_threads(),
                    flow="pnr",
                    run=self.pnr_cfg.get_name(),
                )
        return self._thread_plan

    def _stage_job_resources(self, stage: str) -> JobResources:
        """The reservation of a dispatched stage's job."""
        return self.pnr_cfg.stage_resources(self.root_cfg.get_dispatch_cfg(), stage)

    def _stage_threads(self, stage: str) -> ThreadPlan:
        """A dispatched stage's thread plan: `threads:` resolved against its job's CPUs, so `auto` takes that count."""
        plan = self._stage_thread_plans.get(stage)
        if plan is None:
            plan = plan_threads(
                self.pnr_cfg.get_threads(),
                flow="pnr",
                run=f"{self.pnr_cfg.get_name()} {stage}",
                allocation=(
                    self._stage_job_resources(stage).cpus,
                    pnr_dispatch.JOB_CPUS_SOURCE,
                ),
            )
            self._stage_thread_plans[stage] = plan
        return plan

    def _threads_fields(self) -> dict:
        """Return the `openroad_threads` result field with OpenROAD's reported count.

        A dispatched run's is the plan its last stage job resolved. Empty if no plan was resolved, meaning OpenROAD never launched.
        """
        if self._thread_plan is None:
            return {}
        try:
            reported = parse_reported_threads(Path(self._log_path()).read_text())
        except OSError:
            reported = None
        fields = self._thread_plan.fields(reported)
        job = self._last_job_threads()
        if job is not None:
            fields = {
                **job,
                "effective": reported if reported is not None else job.get("effective"),
            }
        return {"openroad_threads": fields}

    def _last_job_threads(self) -> dict | None:
        """The thread plan the last stage job that ran resolved against its own CPUs, or None."""
        if self._dispatch is None or self._ckpt_run_dir is None:
            return None
        for stage in reversed(self._run_stages()):
            result = pnr_dispatch.read_result(self._ckpt_run_dir, stage) or {}
            if isinstance(result.get("thread_plan"), dict):
                return result["thread_plan"]
        return None

    def _load_template(self) -> str:
        return files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_FILE).read_text()

    def _load_macro_pack(self) -> str:
        return files(_TEMPLATE_PACKAGE).joinpath(_MACRO_PACK_FILE).read_text()

    def _pdn_config(self, pdk) -> str:
        """The PDN Tcl this run sources: its own `pdn-config`, else the PDK's, else ""."""
        return self.pnr_cfg.get_pdn_config() or pdk.get_pdn_config()

    def _block_power_tcl(self, step: str) -> str:
        """Return the Tcl that runs one `block_power.tcl` step on the `blocks:` instances, or "" without blocks.

        `tie_supplies` goes before `pdngen` and carries the procs; `join_straps` goes after it.
        """
        if not self._blocks:
            return ""
        masters = " ".join(sorted({b.ref.name for b in self._blocks}))
        procs = (
            files(_TEMPLATE_PACKAGE).joinpath(_BLOCK_POWER_FILE).read_text()
            if step == "tie_supplies"
            else ""
        )
        return f"{procs}rb::block_power::{step} {{{masters}}}\n"

    def _write_script(self, platform, fp) -> str:
        """Generate the flow and write the scripts OpenROAD runs; return the path of `pnr.tcl`.

        A one-session run's `pnr.tcl` is the script itself. A staged run writes one script per stage into its checkpoint directory, and `pnr.tcl` records them in order.
        """
        self._prepare_flow(platform, fp)
        return self._emit_scripts()

    def _configure_stages(self, platform) -> None:
        """Set the run's stages from its post-route repair, which a probe never reaches, and whether a one-session run continues in separate sessions after detailed route."""
        self._post_route = (
            platform.get_post_route_repair() if self._mode is PnrMode.CLOSURE else None
        )
        self._stages = pnr_stages.run_stages(self._post_route is not None)
        self._split = (
            self._post_route is not None
            and not self._staged
            and self.stop_after in (None, POST_ROUTE_REPAIR_STAGE)
        )

    def _prepare_flow(self, platform, fp) -> None:
        """Split the flow template and compute its substitutions."""
        self._configure_stages(platform)
        self._platform = platform
        pdk = platform.get_pdk()
        netlist = self._openroad_netlist or self._resolve_netlist_path()
        sdc = self.pnr_cfg.get_constraints()
        if not sdc:
            raise RuntimeError(
                f"pnr run '{self.pnr_cfg.get_name()}': "
                "constraints (SDC path) is required"
            )

        fill_cells = " ".join(pdk.get_fill_cells())

        multi_corner = platform.is_multi_corner()
        if multi_corner:
            corner_libs = platform.get_sta_corner_lib_paths()
            read_liberty = "\n".join(openroad_corners.liberty_tcl(corner_libs, []))
            corner_reports = openroad_corners.timing_report_tcl(list(corner_libs))
        else:
            corner_libs = {}
            sta_libs = platform.get_sta_lib_paths()
            read_liberty = "\n".join(
                ["read_liberty $LIBERTY"]
                + [f"read_liberty {lib}" for lib in sta_libs[1:]]
            )
            corner_reports = ""

        extra_lines = []
        for lib in self.pnr_cfg.get_lib_paths():
            if multi_corner:
                extra_lines.extend(
                    f"read_liberty -corner {c} {lib}" for c in corner_libs
                )
            else:
                extra_lines.append(f"read_liberty {lib}")
        for lef in self.pnr_cfg.get_lef_paths():
            extra_lines.append(f"read_lef     {lef}")
        extra_libs_lefs = "\n".join(extra_lines)

        cts_buffers = platform.get_cts_buffers()
        if len(cts_buffers) > 1:
            cts_buf = "{" + " ".join(cts_buffers) + "}"
            cts_root_buf = "[lindex $CTS_BUF 0]"
        else:
            cts_buf = cts_buffers[0] if cts_buffers else ""
            cts_root_buf = "$CTS_BUF"

        dont_use_cells = platform.get_dont_use_cells()
        dont_use_block = (
            '\nputs ">>> Don\'t-use cells"\n'
            f"set_dont_use [list {' '.join(dont_use_cells)}]\n"
            if dont_use_cells
            else ""
        )
        dont_use_check_block = _dont_use_check_tcl(dont_use_cells)

        pdn_config = self._pdn_config(pdk)
        pdn = self.pnr_cfg.get_pdn()
        if pdn is not None:
            grid = f"set PDN_CONFIG {{{pdn_config}}}\n" + _pdn_grid_tcl(pdn)
        else:
            grid = f"source {pdn_config}\n"
        pdn_block = (
            f'\nputs ">>> Power distribution network"\n{grid}'
            f"{self._block_power_tcl('tie_supplies')}pdngen\n"
            f"{self._block_power_tcl('join_straps')}"
            if pdn_config
            else ""
        )

        # PDK Tcl hooks, each at the step where ORFS sources its counterpart.
        tracks_tcl = pdk.get_tracks_tcl()
        make_tracks = (
            f'puts ">>> Routing tracks (tracks-tcl)"\n{tcl_source(tracks_tcl)}'
            if tracks_tcl
            else "make_tracks"
        )

        # Must precede the first `read_liberty`.
        threads_block = _threads_block(self._threads())

        blockages_block, macro_pack_directives = _floorplan_directives(fp)
        gp_blocks = _global_placement_blocks(
            fp,
            platform.get_placement_rotated_macro_footprint(),
            self.pnr_cfg.get_hooks(),
            checkpoints=self._ckpt_run_dir is not None and not self._staged,
        )
        # With `rcx-rules`, extract after fill and time the final reports on the SPEF; otherwise they use the global-route estimate.
        rcx_rules = pdk.get_rcx_rules()
        self._rcx_rules = rcx_rules
        self._corners = list(corner_libs)
        if rcx_rules:
            rcx_block = (
                '\nputs ">>> Parasitic extraction (OpenRCX)"\n'
                "define_process_corner -ext_model_index 0 X\n"
                f"extract_parasitics -ext_model_file {rcx_rules}\n"
                f"write_spef $OUT_DIR/${{DESIGN}}{ROUTED_SPEF_SUFFIX}\n"
            )
            final_parasitics = f"read_spef $OUT_DIR/${{DESIGN}}{ROUTED_SPEF_SUFFIX}"
        else:
            rcx_block = ""
            final_parasitics = "estimate_parasitics -global_routing"

        tie_ports = [
            ("TIEHI_CELL_PORT", pdk.get_tie_hi()),
            ("TIELO_CELL_PORT", pdk.get_tie_lo()),
        ]
        tie_vars = [var for var, port in tie_ports if port]
        tie_cells_block = "".join(f"insert_tiecells ${var}\n" for var in tie_vars)
        separation = f"{platform.get_placement_tie_separation():g}"
        tie_fanout_block = (
            '\nputs ">>> Repair tie fanout"\n'
            + "".join(
                f"repair_tie_fanout -separation {separation} ${var}\n"
                for var in tie_vars
            )
            if tie_vars
            else ""
        )

        pin_script = self.pnr_cfg.pin_constraints
        pin_constraints_tcl = ""
        if pin_script is not None:
            if not os.path.isfile(pin_script):
                raise RuntimeError(f"pin-constraints file does not exist: {pin_script}")
            escaped = pin_script.replace("\\", "\\\\")
            for char in ("$", "[", "]", '"'):
                escaped = escaped.replace(char, "\\" + char)
            pin_constraints_tcl = f'source "{escaped}"'
        max_fanout = max_fanout_cmd(platform)
        hooks = self.pnr_cfg.get_hooks()
        substitutions = {
            "pin_constraints_tcl": pin_constraints_tcl,
            "floorplan_pins_block": _floorplan_pins_block(fp),
            "design": self.pnr_cfg.resolve_synth_cfg().get_top(),
            "netlist": netlist,
            "sdc": sdc,
            "liberty": platform.get_sta_lib_paths()[0],
            "read_liberty": read_liberty,
            "corner_reports": corner_reports,
            "tech_lef": pdk.get_tech_lef(),
            "macro_lef": pdk.get_macro_lef(),
            "site": pdk.get_site(),
            "util_pct": f"{fp.utilization * 100:.2f}",
            "aspect": f"{fp.aspect:.2f}",
            "core_margin": f"{fp.core_margin:.2f}",
            "tie_hi": pdk.get_tie_hi() or "{}",
            "tie_lo": pdk.get_tie_lo() or "{}",
            "tie_cells_block": tie_cells_block,
            "tie_fanout_block": tie_fanout_block,
            "cts_buf": cts_buf,
            "cts_root_buf": cts_root_buf,
            "place_density": f"{platform.get_placement_density():g}",
            "place_padding": str(platform.get_placement_padding()),
            "reference_hpwl_option": _reference_hpwl_option(platform),
            "routability_options": _routability_options(platform),
            "macro_halo": f"{platform.get_placement_macro_halo():g}",
            "macro_cell_halo": f"{platform.get_placement_macro_cell_halo():g}",
            "macro_pack_procs": self._load_macro_pack(),
            "macro_directives_block": _macro_directives_block(fp),
            "initialize_floorplan": _initialize_floorplan_tcl(fp),
            # A `macro-placement` hook replaces the packer or RTL-MP inside the macro branch.
            "macro_place_block": (
                _macro_hook_block(fp, hooks["macro-placement"])
                if "macro-placement" in hooks
                else _macro_place_block(fp, macro_pack_directives)
            ),
            "no_macros_block": _no_macros_block(fp),
            "blockages_block": blockages_block,
            "dont_use_block": dont_use_block,
            "dont_use_check_block": dont_use_check_block,
            "pdn_block": pdn_block,
            # Before the first `read_liberty`, as ORFS `PLATFORM_TCL`, so its `suppress_message` covers the Liberty reads.
            "platform_tcl_block": _hook_block(
                "Platform Tcl (platform-tcl)", pdk.get_platform_tcl()
            ),
            # After `read_sdc`, as ORFS `SET_RC_TCL`; every later parasitics estimate and CTS use it.
            "layer_rc_block": _hook_block(
                "Layer RC (layer-rc-tcl)", pdk.get_layer_rc_tcl()
            ),
            "make_tracks": make_tracks,
            # After macro placement and before the power grid, as ORFS `TAPCELL_TCL`, so endcaps see the placed macros.
            "tapcell_block": _hook_block(
                "Tap and endcap cells (tapcell-tcl)", pdk.get_tapcell_tcl()
            ),
            "threads_block": threads_block,
            "hook_procs_block": _hook_procs_block(hooks),
            "hook_session_block": _run_hook_block("session", hooks),
            "hook_after_floorplan_block": _run_hook_block("after-floorplan", hooks),
            "hook_after_pdn_block": _run_hook_block("after-pdn", hooks),
            "hook_after_pins_block": _run_hook_block("after-pins", hooks),
            "hook_after_global_placement_block": _run_hook_block(
                "after-global-placement", hooks
            ),
            "hook_after_repair_design_block": _run_hook_block(
                "after-repair-design", hooks
            ),
            "hook_after_cts_block": _run_hook_block("after-cts", hooks),
            "hook_after_global_route_block": _run_hook_block(
                "after-global-route", hooks
            ),
            "hook_before_detailed_route_block": _run_hook_block(
                "before-detailed-route", hooks
            ),
            "hook_before_reports_block": _run_hook_block("before-reports", hooks),
            "rcx_block": rcx_block,
            "final_parasitics": final_parasitics,
            "harden_block": (
                pnr_abstract.harden_tcl(self.pnr_cfg.get_abstract())
                if self.pnr_cfg.get_harden()
                else ""
            ),
            "post_cts_repair": _post_cts_repair_tcl(platform, self._mode),
            "layer_adjustment": _layer_adjustment_tcl(platform),
            "global_route_repair": _global_route_repair_tcl(
                platform.get_global_route_repair(),
                self._mode,
                self._ps_per_unit,
                gp_blocks.dpl_options,
            ),
            "gp_blockages_create": gp_blocks.create,
            "gp_blockages_destroy": gp_blocks.destroy,
            "dpl_options": gp_blocks.dpl_options,
            "detailed_route_verbose": str(self.pnr_cfg.get_detailed_route_verbose()),
            "cell_count_tag": _CELL_COUNT_TAG,
            "electrical_tag": _ELECTRICAL_TAG,
            "buffer_ports_block": _buffer_ports_tcl(
                self.pnr_cfg.get_buffer_ports(), platform.get_port_buffer()
            ),
            "cts_clustering_option": (
                "-sink_clustering_enable" if platform.get_cts_sink_clustering() else ""
            ),
            "cts_ndr_option": _cts_apply_ndr_option(platform),
            "max_fanout_block": f"{max_fanout}\n" if max_fanout else "",
            "signal_layers": platform.get_signal_layers(),
            "clock_layers": platform.get_clock_layers(),
            "pin_layer_horizontal": pdk.get_pin_layer_horizontal(),
            "pin_layer_vertical": pdk.get_pin_layer_vertical(),
            "fill_cells": fill_cells,
            "out_dir": self.artefact_dir,
            "extra_libs_lefs": extra_libs_lefs,
            # A run that continues in sessions after detailed route stops there, with the state the next session reads.
            "checkpoint_block": (
                pnr_checkpoints.render_tcl_block(
                    self._ckpt_run_dir,
                    self.pnr_cfg.get_checkpoints() or (),
                    order=self._stages,
                    stop_after="detailed_route" if self._split else self.stop_after,
                    state=pnr_checkpoints.SESSION_STATE if self._split else (),
                )
                if self._ckpt_run_dir is not None and not self._staged
                else ""
            ),
        }
        self._flow = pnr_stages.FlowTemplate(self._load_template())
        self._substitutions = substitutions
        self._gp_blocks = gp_blocks

    def _render(self, text: str, substitutions: dict) -> str:
        for key, value in substitutions.items():
            text = text.replace("{{ " + key + " }}", str(value))
        leftover = re.findall(r"\{\{\s*@?/?[\w]+\s*\}\}", text)
        if leftover:
            raise RuntimeError(
                f"pnr flow template has unsubstituted placeholders: {leftover}"
            )
        return text

    def _unbroken_marker(self, marker: str) -> str:
        """A one-session run's text for a template marker: nothing, except that with checkpoints armed the global-route stage ends after any global-route repair, before the `before-detailed-route` hooks."""
        if marker == "detailed_route" and self._ckpt_run_dir is not None:
            return "rb::ckpt::stage_end global_route\n"
        return ""

    def _run_stages(self) -> tuple[str, ...]:
        """The stages this run executes: from `--from` (or the start) through `--stop-after` (or `finish`)."""
        stages = self._stages
        first = stages.index(self.resume_from) if self.resume_from else 0
        last = stages.index(self.stop_after) if self.stop_after else len(stages) - 1
        return stages[first : last + 1]

    def _session_stages(self) -> tuple[str, ...]:
        """The stages rb runs as sessions of their own: every stage of a staged run, and the stages after detailed route of a one-session run with post-route repair."""
        stages = self._run_stages()
        if self._staged:
            return stages
        if self._split:
            return stages[stages.index("detailed_route") + 1 :]
        return ()

    def _predecessor(self, stage: str) -> str | None:
        return pnr_stages.predecessor(stage, self._stages)

    def _pred_dir(self, stage: str, run_dir: str) -> str | None:
        """The directory a staged session reads its predecessor's checkpoint from."""
        if stage == "floorplan":
            return None
        source = self._resume_source
        if source is not None and stage == source.from_stage:
            return source.pred_dir
        return run_dir

    def _session_substitutions(
        self, stage: str, run_dir: str, session: str = "staged run"
    ) -> dict:
        """The template substitutions of a session of `stage` bound to `run_dir`: the flow's, with that session's checkpoint block, in a dispatched run its stage job's threads, and before `finish` the hook-command tracer."""
        substitutions = {
            **self._substitutions,
            "checkpoint_block": pnr_checkpoints.render_staged_block(
                stage, run_dir, self._pred_dir(stage, run_dir), session=session
            ),
        }
        if self._dispatch is not None:
            substitutions["threads_block"] = _threads_block(self._stage_threads(stage))
        return _with_hook_tracer(substitutions) if stage != "finish" else substitutions

    def _prelude(self, stage: str, read_tcl: str) -> list[str]:
        """The start of a session of `stage` after `floorplan`: the template head, `read_tcl` opening the database it starts from, and what a database does not keep.

        That is the header variables, threads, `platform-tcl`, Liberty and LEF, then the database, SDC and saved globals, max fanout, layer RC, don't-use cells and `session` hooks; the flow's design variables; the detailed-placement options; propagated clocks after CTS; the routing layers after global routing; for detailed routing, the global route's segments; and, for `finish`, a fanout check.
        """
        flow = self._flow
        parts = [
            flow.head(),
            read_tcl,
            flow.piece("session"),
            flow.piece("design"),
        ]
        if pnr_stages.after(stage, "place"):
            parts.append(self._gp_blocks.session)
        if pnr_stages.after(stage, "cts"):
            parts.append(flow.piece("clocks"))
        if pnr_stages.after(stage, "global_route"):
            parts.append(flow.piece("layers"))
        if stage == "detailed_route":
            parts.append(_read_global_route_tcl(self._predecessor(stage)))
        if stage == "finish":
            parts.append(_FANOUT_CHECK_TCL)
        return parts

    def _stage_script(self, stage: str, run_dir: str | None = None) -> str:
        """Return one staged session's script: rb's prelude (see `_prelude`), then the stage's part of the flow, then its checkpoint. Every session takes its guides from its database, and `finish` its global route too. `run_dir` is the run directory the script binds, this run's by default.

        For `post_route_repair` it is the record of the stage's sessions (see `_post_route_record`).
        """
        if run_dir is None:
            run_dir = self._ckpt_run_dir
        if stage == POST_ROUTE_REPAIR_STAGE:
            return self._post_route_record(run_dir)
        flow = self._flow
        substitutions = self._session_substitutions(stage, run_dir)
        if stage == "floorplan":
            parts = [flow.head(), flow.piece("load"), flow.piece("session")]
        else:
            parts = self._prelude(stage, _read_checkpoint_tcl(self._predecessor(stage)))
        parts.append(flow.body(stage))
        if stage != "finish":
            parts.append(
                f"\nrb::ckpt::stage_end {stage}\n"
                f'puts "{_STAGE_DONE_MARKER}{_checkpoint_name(stage)}"\n'
            )
            parts.append(
                _stage_report_tcl(stage, substitutions["corner_reports"])
                if stage == self.stop_after
                else "exit 0\n"
            )
        return self._render("".join(parts), substitutions)

    def _post_route_source(self, round_: int) -> str:
        """The prelude lines that open the database round `round_` starts from: the detailed route in the first round, the previous round's repair after it."""
        if round_ == 1:
            return _read_checkpoint_tcl(self._predecessor(POST_ROUTE_REPAIR_STAGE))
        return _read_checkpoint_tcl(POST_ROUTE_REPAIR_STAGE, "RB_RUN_DIR")

    def _extract_tcl(self, spef: str) -> str:
        """OpenRCX extraction into `spef` in the run directory, read back once per corner: in-session extraction does not annotate timing, and one SPEF read shared by several corners keeps the first corner's reduced parasitics for all."""
        path = f"[file join $RB_RUN_DIR {spef}]"
        return (
            "define_process_corner -ext_model_index 0 X\n"
            f"extract_parasitics -ext_model_file {self._rcx_rules}\n"
            f"write_spef {path}\n" + self._read_spef_tcl(path)
        )

    def _read_spef_tcl(self, path: str) -> str:
        if self._corners:
            return "".join(f"read_spef -corner {c} {path}\n" for c in self._corners)
        return f"read_spef {path}\n"

    def _post_route_extract_script(self, round_: int, run_dir: str) -> str:
        """Round `round_`'s extraction session, bound to `run_dir`: extract the latest routed database and time every corner on the result."""
        title = f"Post-route repair round {round_}"
        parts = self._prelude(POST_ROUTE_REPAIR_STAGE, self._post_route_source(round_))
        parts += [
            f'\nputs ">>> {title}: extraction"\n',
            self._extract_tcl(_post_route_file(f".r{round_}.spef")),
            f'puts "{_ROUND_TIMING_MARKER}{round_}"\n',
            "report_worst_slack -max\n",
            "report_worst_slack -min\n",
            "report_tns\n",
            f"report_tns -min{self._substitutions['corner_reports']}\n",
            f'puts "{_ROUND_DONE_MARKER}{round_} extraction"\n',
            "exit 0\n",
        ]
        return self._render(
            "".join(parts),
            self._session_substitutions(
                POST_ROUTE_REPAIR_STAGE, run_dir, f"round {round_}: extraction"
            ),
        )

    def _post_route_repair_script(self, round_: int, run_dir: str) -> str:
        """Round `round_`'s repair session, bound to `run_dir`: repair on the round's SPEF, legalize inside an incremental global route, run the `before-detailed-route` hooks, route in detail and write the stage's checkpoint.

        The rounds work on the unfilled database; `finish` fills after the last. A repair in the session that routed fails detailed routing (DRT-0218), so each round is a fresh session that reads the routed database.

        With `reroute: eco` the session keeps the routed database's wires. It estimates placement parasitics and reads the SPEF over them, opens an incremental global route and runs the repairs in `rb::eco::repair` (`eco_repair.tcl`), which makes `design_is_routed` answer 0 for them alone: `repair_timing` then keeps the placement source instead of switching to detailed-routing parasitics, times the nets it does not change on the SPEF and the nets it changes on Steiner trees, and opens no incremental route of its own, so the session's one sees each netlist change once. Legalizing closes that route, which reroutes only the changed nets, and the detailed route keeps every other net's wires. On detailed-routing parasitics the repairs would build buffer trees from the global routes a fresh session rebuilds from the guides, which can miss pins (RSZ-0074), and a second incremental route open around them delivers each change twice (GRT-0229 on a later rip-up).

        With `reroute: full` it removes the detailed route and routes globally first, then reads the SPEF over the global-route estimate; the repairs run before `global_route -start_incremental`, in ORFS order, since `repair_timing` on global-route parasitics opens its own incremental route. Its detailed route routes every net again.
        """
        repair = self._post_route
        setup, hold = repair.margins(round_)
        title = f"Post-route repair round {round_}"
        spef = self._read_spef_tcl(
            f"[file join $RB_RUN_DIR {_post_route_file(f'.r{round_}.spef')}]"
        )
        parts = self._prelude(POST_ROUTE_REPAIR_STAGE, self._post_route_source(round_))
        parts.append(
            f'\nputs ">>> {title} of {repair.rounds}: repair on extracted parasitics '
            f'(reroute {repair.reroute.value})"\n'
        )
        if repair.reroute is PostRouteReroute.FULL:
            legalize = _legalize_tcl("{{ dpl_options }}")
            parts += [
                "remove_fillers\n",
                "[ord::get_db_block] destroyNetWires\n",
                f'puts ">>> {title}: global route"\n',
                _GLOBAL_ROUTE_CMD,
                "estimate_parasitics -global_routing\n",
                spef,
                _repair_steps_tcl(
                    title,
                    repair.repair_design,
                    setup,
                    hold,
                    self._ps_per_unit,
                    legalize,
                ),
                legalize,
            ]
        else:
            steps = _repair_steps_tcl(
                title, repair.repair_design, setup, hold, self._ps_per_unit, None
            )
            parts += [
                files(_TEMPLATE_PACKAGE).joinpath(_ECO_REPAIR_FILE).read_text(),
                "estimate_parasitics -placement\n",
                spef,
                "remove_fillers\n",
                "global_route -start_incremental\n",
                f"rb::eco::repair {round_} {{\n{steps}}}\n",
                "detailed_placement{{ dpl_options }}\n",
                "check_placement -verbose\n",
                _END_INCREMENTAL_CMD,
            ]
        parts += [
            "{{ hook_before_detailed_route_block }}",
            f'puts ">>> {title}: detailed route"\n',
            # So the round's DRC count is its own route's.
            "file delete -force $OUT_DIR/route.drc.rpt\n",
            _DETAILED_ROUTE_CMD,
            f"\nrb::ckpt::stage_end {POST_ROUTE_REPAIR_STAGE}\n",
            f'puts "{_ROUND_DONE_MARKER}{round_} repair"\n',
            "exit 0\n",
        ]
        return self._render(
            "".join(parts),
            self._session_substitutions(
                POST_ROUTE_REPAIR_STAGE, run_dir, f"round {round_}: repair"
            ),
        )

    def _eco_hint(self, log_text: str) -> str:
        """What to change when an `eco` post-route repair round failed RSZ-0074, else "".

        An eco round's repairs build buffer trees from Steiner trees while `rb::eco::repair` can override `design_is_routed`; where it cannot (`pnr.post_route_eco_unguarded`), they build them from the global routes a fresh session rebuilds from the routed database's guides, which can miss pins.
        """
        repair = self._post_route
        if (
            repair is None
            or repair.reroute is not PostRouteReroute.ECO
            or self._failed_stage != _checkpoint_name(POST_ROUTE_REPAIR_STAGE)
            or "[ERROR RSZ-0074]" not in log_text
        ):
            return ""
        return (
            "; the eco round's repair could not use the routes rebuilt from the "
            "routed database: set post-route-repair.reroute: full"
        )

    def _post_route_report_script(self, run_dir: str) -> str:
        """The session a `--stop-after post_route_repair` run ends with: the stage report, timed on the extracted stage checkpoint."""
        parts = self._prelude(
            POST_ROUTE_REPAIR_STAGE,
            _read_checkpoint_tcl(POST_ROUTE_REPAIR_STAGE, "RB_RUN_DIR"),
        )
        parts += [
            "\n",
            self._extract_tcl(_post_route_file(".spef")),
            _stage_report_tcl(
                POST_ROUTE_REPAIR_STAGE, self._substitutions["corner_reports"]
            ),
        ]
        return self._render(
            "".join(parts),
            self._session_substitutions(
                POST_ROUTE_REPAIR_STAGE, run_dir, "stage report"
            ),
        )

    def _post_route_scripts(self, run_dir: str) -> list[tuple[str, str]]:
        """Every session the post-route repair may run, bound to `run_dir`, as (file name, script) in order: each round's extraction, then its repair."""
        scripts = []
        for round_ in range(1, self._post_route.rounds + 1):
            scripts += [
                (
                    _post_route_file(f".r{round_}.extract.tcl"),
                    self._post_route_extract_script(round_, run_dir),
                ),
                (
                    _post_route_file(f".r{round_}.repair.tcl"),
                    self._post_route_repair_script(round_, run_dir),
                ),
            ]
        return scripts

    def _post_route_record(self, run_dir: str) -> str:
        """The post-route repair stage's record, bound to `run_dir`: every session it may run, the sessions after the first without the header and threads they share with it. Its identity is the stage's script identity for `--resume`."""
        scripts = self._post_route_scripts(run_dir)
        return (
            "# rb pnr post-route repair: rb runs these sessions in order and stops "
            "once timing is met; each starts with the first one's header and threads.\n"
            + "".join(
                f"\n# ---- {name} ----\n"
                + (text if index == 0 else pnr_stages.session_body(text))
                for index, (name, text) in enumerate(scripts)
            )
        )

    def _write_stage_scripts(self, stage: str) -> str:
        """Write `stage`'s session scripts into the checkpoint run directory and return its text for the `pnr.tcl` record."""
        name = f"{_checkpoint_name(stage)}.tcl"
        text = self._stage_script(stage)
        Path(self._ckpt_run_dir, name).write_text(text)
        record = f"\n# ---- {name} ----\n{text}"
        if stage == POST_ROUTE_REPAIR_STAGE:
            for session, script in self._post_route_scripts(self._ckpt_run_dir):
                Path(self._ckpt_run_dir, session).write_text(script)
            report = None
            if self.stop_after == POST_ROUTE_REPAIR_STAGE:
                report = _post_route_file(".report.tcl")
                script = self._post_route_report_script(self._ckpt_run_dir)
                Path(self._ckpt_run_dir, report).write_text(script)
                record += f"\n# ---- {report} ----\n{script}"
            Path(self._ckpt_run_dir, POST_ROUTE_PLAN).write_text(
                json.dumps(self._post_route_plan(report), indent=2) + "\n"
            )
        return record

    def _post_route_plan(self, report: str | None) -> dict:
        """What `run_post_route_repair` reads besides the scripts: each round's scripts and margins, the report session, how to read timing, the DRC report, and the checkpoint the stage keeps when no round repairs."""
        repair = self._post_route
        rounds = []
        for round_ in range(1, repair.rounds + 1):
            setup, hold = repair.margins(round_)
            rounds.append(
                {
                    "round": round_,
                    "extract": _post_route_file(f".r{round_}.extract.tcl"),
                    "repair": _post_route_file(f".r{round_}.repair.tcl"),
                    "setup_margin_ns": setup,
                    "hold_margin_ns": hold,
                }
            )
        source = self._predecessor(POST_ROUTE_REPAIR_STAGE)
        return {
            "rounds": rounds,
            "sides": list(repair.sides()),
            "report": report,
            "ps_per_unit": self._ps_per_unit,
            "corners": list(self._corners),
            "drc_report": self._drc_report_path(),
            "carry": {
                "dir": self._pred_dir(POST_ROUTE_REPAIR_STAGE, self._ckpt_run_dir),
                "stage": source,
                "name": _checkpoint_name(source),
                "design": self.pnr_cfg.resolve_synth_cfg().get_top(),
            },
        }

    def _emit_scripts(self) -> str:
        """Write the scripts OpenROAD runs (see `_write_script`) and return the path of `pnr.tcl`.

        A one-session run with post-route repair runs `pnr.tcl` through detailed route, then the later stages' scripts from the checkpoint run directory. Its `pnr.tcl` records those after the `exit` that ends the first session, so it holds the whole flow, as a staged run's does (`rb power` reads it for the `write_spef` that vouches for the routed SPEF).
        """
        script_path = self._script_path()
        if not self._staged:
            stop = self.stop_after
            if self._split:
                text = (
                    self._flow.through("detailed_route", self._unbroken_marker)
                    + "\nrb::ckpt::stage_end detailed_route stop\n"
                    + f'puts "{_STAGE_DONE_MARKER}{_checkpoint_name("detailed_route")}"\n'
                    + "exit 0\n"
                )
            elif stop is None:
                text = self._flow.whole(self._unbroken_marker)
            else:
                text = (
                    self._flow.through(stop, self._unbroken_marker)
                    + f"\nrb::ckpt::stage_end {stop} stop\n"
                    + _stage_report_tcl(stop, self._substitutions["corner_reports"])
                )
            text = self._render(
                text,
                _with_hook_tracer(self._substitutions)
                if self._split
                else self._substitutions,
            )
            records = [self._write_stage_scripts(s) for s in self._session_stages()]
            if records:
                text += (
                    "\n# rb pnr: after the session above exits, rb runs each session "
                    "below as its own OpenROAD session.\n" + "".join(records)
                )
            Path(script_path).write_text(text)
            return script_path
        record = [
            "# rb pnr staged run: each stage below ran as its own OpenROAD session.\n"
        ]
        for entry in self._resume_source.chain if self._resume_source else []:
            path = (entry.get("script") or {}).get("path")
            try:
                text = Path(path).read_text()
            except (OSError, TypeError):
                text = f"# not readable: {path}\n"
            record.append(
                f"\n# ---- {os.path.basename(str(path))} (inherited) ----\n{text}"
            )
        for stage in self._run_stages():
            record.append(self._write_stage_scripts(stage))
        Path(script_path).write_text("".join(record))
        return script_path

    def _chain_records(self) -> list[dict]:
        """The sessions rb runs, in order, for the manifest: script (with its identity) and log of each, inherited ones first."""
        records = list(self._resume_source.chain) if self._resume_source else []
        run_id = os.path.basename(self._ckpt_run_dir)
        for stage in self._session_stages():
            name = _checkpoint_name(stage)
            script = os.path.join(self._ckpt_run_dir, f"{name}.tcl")
            fingerprint = _file_fingerprint(script) or {}
            fingerprint["identity"] = pnr_stages.script_identity(
                Path(script).read_text()
            )
            records.append(
                {
                    "stage": stage,
                    "index": pnr_checkpoints.STAGE_INDEX[stage],
                    "run_id": run_id,
                    "script": fingerprint,
                    "log": {"path": os.path.join(self._ckpt_run_dir, f"{name}.log")},
                }
            )
        return records

    def _parse_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"^Design area\s+([\d.]+)\s+um\^2", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _parse_cell_count(self, log_text: str) -> int | None:
        """The input netlist's instance count: the floorplan's first `Number of instances:` line, before any cell the flow adds."""
        m = re.search(r"Number of instances:\s+(\d+)", log_text)
        return int(m.group(1)) if m else None

    def _parse_routed_cell_counts(self, log_text: str) -> tuple[int | None, int | None]:
        """Return `(routed, physical)` from the final reports' tagged count, or `(None, None)` when absent."""
        m = _CELL_COUNT_RE.search(log_text)
        if not m:
            return None, None
        return int(m.group(1)), int(m.group(2))

    def _parse_electrical(self, log_text: str) -> dict:
        """Return the final reports' max-slew, max-capacitance and max-fanout violator counts as result fields.

        A check whose count was not printed, as on an OpenROAD without the `sta::` counter, is left out.
        """
        fields = {}
        for m in _ELECTRICAL_RE.finditer(log_text):
            fields[_ELECTRICAL_FIELDS[m.group(1)][0]] = int(m.group(2))
        return fields

    def _describe_electrical(self, fields: dict) -> str:
        """Return `"N max-slew, M max-capacitance violator(s)"` for the nonzero counts, or `""` when there are none."""
        parts = [
            f"{fields[key]} {word}"
            for key, word in _ELECTRICAL_FIELDS.values()
            if fields.get(key)
        ]
        return f"{', '.join(parts)} violator(s)" if parts else ""

    def _parse_wns(self, log_text: str, kind: str) -> float | None:
        return _worst_slack(log_text, kind)

    def _parse_tns(self, log_text: str, kind: str = "max") -> float | None:
        return _total_negative_slack(log_text, kind)

    def _corner_fields(self, platform, log_text: str) -> dict:
        """Return per-corner timing result fields for a multi-corner run.

        `corners` maps each corner, in config order, to its `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps`; `worst_setup_corner` and `worst_hold_corner` name the corner behind the scalar WNS fields. Empty for a single-corner platform.
        """
        if not platform.is_multi_corner():
            return {}
        per_corner = openroad_corners.parse_corner_timing(
            log_text, platform.get_sta_corners(), self._ps_per_unit
        )
        return {
            "corners": per_corner,
            "worst_setup_corner": openroad_corners.worst_corner(
                per_corner, "wns_setup_ps"
            ),
            "worst_hold_corner": openroad_corners.worst_corner(
                per_corner, "wns_hold_ps"
            ),
        }

    def _probe_openroad_version(self) -> str | None:
        try:
            r = subprocess.run(
                [self.openroad_executable, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = (r.stdout or r.stderr).strip()
        return out.splitlines()[0] if out else None

    def _version_below_min(self, version: str) -> bool:
        return _parse_version_token(version) < _parse_version_token(
            MIN_OPENROAD_VERSION
        )

    def _has_tcl_command(self, command: str) -> bool:
        """Return whether the OpenROAD build has a Tcl command, such as `write_gds`; False if it cannot be determined."""
        probe = (
            f'if {{[info commands {command}] eq ""}} '
            f'{{ puts "RB_HAS_CMD:{command}:no" }} '
            f'else {{ puts "RB_HAS_CMD:{command}:yes" }}\nexit\n'
        )
        try:
            r = subprocess.run(
                [self.openroad_executable, "-no_init", "-exit"],
                input=probe,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return f"RB_HAS_CMD:{command}:yes" in (r.stdout or "")

    def _drc_report_path(self) -> str:
        return os.path.join(self.artefact_dir, "route.drc.rpt")

    def _count_drcs(self) -> int:
        return count_drc_violations(self._drc_report_path())

    def _klayout_script_path(self, name: str) -> str:
        """Copy a bundled KLayout helper into the artefact directory and return its path.

        KLayout's `-r` needs a real file; the package may be loaded from a zip.
        """
        target = Path(self.artefact_dir) / name
        target.write_text(files(_KLAYOUT_PACKAGE).joinpath(name).read_text())
        return str(target)

    def gather_def2stream_inputs(self, platform) -> Def2StreamInputs:
        """Resolve every file KLayout stream-out reads and record which are missing on disk.

        GDS inputs are the PDK's `cell-gds` followed by the run's `gds-paths`. LEF inputs are the technology LEF, the PDK macro LEF, then the run's `lef-paths`, in the order OpenROAD read them. Both lists are de-duplicated.
        """
        pdk = platform.get_pdk()
        tech = pdk.get_klayout_tech()
        gds = _dedup_paths([*pdk.get_cell_gds_paths(), *self.pnr_cfg.get_gds_paths()])
        lef = _dedup_paths(
            [
                pdk.get_tech_lef(),
                pdk.get_macro_lef(),
                *self.pnr_cfg.get_lef_paths(),
            ]
        )
        missing = [
            path
            for path in ([tech] if tech else []) + gds + lef
            if not os.path.isfile(path)
        ]
        return Def2StreamInputs(tech=tech, gds=gds, lef=lef, missing=missing)

    def _write_def2stream_inputs(self, inputs: Def2StreamInputs) -> str:
        """Write the JSON manifest of GDS and LEF lists, allow-empty cells and report path that the helper reads.

        KLayout's `-rd` carries only scalars, so lists travel in a file.
        """
        path = os.path.join(self.artefact_dir, _DEF2STREAM_INPUTS_NAME)
        Path(path).write_text(
            json.dumps(
                {
                    "gds": inputs.gds,
                    "lef": inputs.lef,
                    "allow_empty": self.pnr_cfg.get_gds_allow_empty(),
                    "report": self._def2stream_report_path(),
                },
                indent=2,
            )
            + "\n"
        )
        return path

    def _def2stream_report_path(self) -> str:
        return os.path.join(self.artefact_dir, _DEF2STREAM_REPORT_NAME)

    def _strict(self) -> bool:
        return self.gds_mode == GdsMode.STRICT

    def _gds_log_level(self) -> int:
        """Return ERROR in `strict` mode, where a failed export fails the run, else WARNING."""
        return logging.ERROR if self._strict() else logging.WARNING

    def _export_failed(self, desc: str) -> GdsExport:
        return GdsExport(
            mode=self.gds_mode,
            status=GDS_FAILED,
            png_requested=self.emit_png,
            desc=f"GDS export failed: {desc}",
        )

    def _read_def2stream_report(self) -> dict | None:
        """Return the helper's stream-out report, or None if it is missing, truncated or of an unknown schema.

        None means nothing vouches for the layout, so callers treat it as a failed export.
        """
        try:
            data = json.loads(Path(self._def2stream_report_path()).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != REPORT_SCHEMA:
            return None
        return data

    def export_layout(
        self, platform, design: str, *, in_def: str | None = None
    ) -> GdsExport:
        """Stream the routed DEF out to GDS and, if requested, render a PNG.

        Covers gather, validate, stream out, read the report and render, and needs no OpenROAD. `in_def` streams another DEF than `<design>.def`; every other input still comes from the run's configuration.
        """
        return self._render_and_gate(
            platform, self._run_def2stream(platform, design, in_def=in_def), design
        )

    def rerender_layout(self, platform, design: str) -> GdsExport:
        """Render the PNG again from the GDS already in the artefact directory.

        The stream-out is skipped and the GDS is never cleared or rewritten. Missing cells are carried over from `def2stream.report.json`; a GDS with no readable report is reported incomplete, which `preview` renders and `strict` refuses.
        """
        gds_path = os.path.join(self.artefact_dir, f"{design}.gds")
        if not os.path.isfile(gds_path) or os.path.getsize(gds_path) == 0:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.no_gds",
                pnr=self.pnr_cfg.get_name(),
                path=gds_path,
            )
            return self._export_failed(f"no GDS to re-render at {gds_path}")
        report = self._read_def2stream_report()
        missing = [str(c) for c in (report or {}).get("missing_cells", [])]
        allowed_empty = [str(c) for c in (report or {}).get("allowed_empty_cells", [])]
        if report is None:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr_export.rerender_unverified",
                pnr=self.pnr_cfg.get_name(),
                gds=gds_path,
                report=self._def2stream_report_path(),
            )
            desc = "no stream-out report beside the GDS; completeness unknown"
        elif missing:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr_export.rerender_incomplete",
                pnr=self.pnr_cfg.get_name(),
                mode=str(self.gds_mode),
                count=len(missing),
                cells=missing,
            )
            desc = f"GDS incomplete: no layout for {describe_missing_cells(missing)}"
        else:
            desc = ""
        export = GdsExport(
            mode=self.gds_mode,
            status=GDS_COMPLETE
            if report is not None and not missing
            else GDS_INCOMPLETE,
            png_requested=True,
            gds_path=gds_path,
            missing_cells=missing,
            allowed_empty_cells=allowed_empty,
            desc=desc,
        )
        return self._render_and_gate(platform, export, design, own_gds=False)

    def _render_and_gate(
        self, platform, export: GdsExport, design: str, *, own_gds: bool = True
    ) -> GdsExport:
        """Render the requested PNG, then apply the GDS mode to the result.

        In `strict` mode an export that did not deliver everything is removed. `preview` keeps what it produced with a qualifier. `own_gds` is true when this invocation wrote the GDS: a rejected stream-out then removes the GDS and report, a rejected re-render only its own image.
        """
        if export.gds_path is not None and self.emit_png:
            png_path = self._run_gds2png(platform, export.gds_path, design)
            if png_path is None:
                note = "PNG render failed"
                export = replace(
                    export, desc=f"{export.desc}; {note}" if export.desc else note
                )
            else:
                export = replace(export, png_path=png_path)
        if self._strict() and not export.delivered:
            log_event(
                logger,
                logging.ERROR,
                "pnr.gds_export_rejected",
                pnr=self.pnr_cfg.get_name(),
                mode=str(self.gds_mode),
                status=export.status,
                missing=export.missing_cells,
                desc=export.desc,
            )
            clear_stale_artefacts(
                [
                    export.gds_path if own_gds else None,
                    export.png_path,
                    self._def2stream_report_path() if own_gds else None,
                ],
                owner=self.pnr_cfg.get_name(),
            )
            export = replace(
                export,
                gds_path=None if own_gds else export.gds_path,
                png_path=None,
            )
        return export

    def _run_def2stream(
        self, platform, design: str, *, in_def: str | None = None
    ) -> GdsExport:
        pdk = platform.get_pdk()
        inputs = self.gather_def2stream_inputs(platform)
        if not inputs.tech:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr.gds_no_klayout_tech",
                pnr=self.pnr_cfg.get_name(),
                pdk=pdk.get_name(),
            )
            return self._export_failed(
                f"PDK '{pdk.get_name()}' configures no klayout-tech"
            )
        klayout = _resolve_klayout_exe()
        if not klayout:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr.no_klayout",
                pnr=self.pnr_cfg.get_name(),
            )
            return self._export_failed("KLayout not found on PATH")
        if inputs.missing:
            # Stop before KLayout: with a missing input it still writes a GDS that looks complete.
            log_event(
                logger,
                logging.ERROR,
                "pnr.gds_missing_inputs",
                pnr=self.pnr_cfg.get_name(),
                pdk=pdk.get_name(),
                count=len(inputs.missing),
                missing=inputs.missing,
            )
            return self._export_failed(
                f"{len(inputs.missing)} configured input(s) not on disk"
            )
        in_def = in_def or os.path.join(self.artefact_dir, f"{design}.def")
        out_gds = os.path.join(self.artefact_dir, f"{design}.gds")
        report_path = self._def2stream_report_path()
        inputs_json = self._write_def2stream_inputs(inputs)
        script = self._klayout_script_path("def2stream.py")
        cmd = [
            klayout,
            "-zz",
            "-nc",
            "-rd",
            f"tech_file={inputs.tech}",
            "-rd",
            "layer_map=",
            "-rd",
            f"in_def={in_def}",
            "-rd",
            f"design_name={design}",
            "-rd",
            f"inputs_json={inputs_json}",
            "-rd",
            "seal_file=",
            "-rd",
            f"out_file={out_gds}",
            "-r",
            script,
        ]
        log_path = os.path.join(self.artefact_dir, "klayout.def2stream.log")
        # Both outputs are judged by presence, so old ones must not survive.
        clear_stale_artefacts([out_gds, report_path], owner=self.pnr_cfg.get_name())
        with task_status(f"pnr {self.pnr_cfg.get_name()} [klayout gds]"):
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        Path(log_path).write_text((r.stdout or "") + (r.stderr or ""))
        if not os.path.isfile(out_gds) or os.path.getsize(out_gds) == 0:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr.gds_failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                log=log_path,
            )
            # Also removes a zero-length GDS, which `isfile` would take as a layout.
            clear_stale_artefacts([out_gds, report_path], owner=self.pnr_cfg.get_name())
            return self._export_failed(f"KLayout wrote no GDS (exit {r.returncode})")

        report = self._read_def2stream_report()
        if report is None:
            log_event(
                logger,
                logging.ERROR,
                "pnr.gds_report_unreadable",
                pnr=self.pnr_cfg.get_name(),
                report=report_path,
                returncode=r.returncode,
                log=log_path,
            )
            clear_stale_artefacts([out_gds, report_path], owner=self.pnr_cfg.get_name())
            return self._export_failed("stream-out wrote no readable report")
        missing = [str(c) for c in report.get("missing_cells", [])]
        allowed_empty = [str(c) for c in report.get("allowed_empty_cells", [])]
        other_errors = int(report.get("other_errors") or 0)
        # Exit code is the helper's error count, mod 256. Missing-cell errors are what `preview` tolerates; any other error fails both modes.
        if other_errors or r.returncode % 256 != report.get("errors", 0) % 256:
            log_event(
                logger,
                logging.ERROR,
                "pnr.gds_failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                other_errors=other_errors,
                orphan_cells=[str(c) for c in report.get("orphan_cells", [])],
                log=log_path,
            )
            clear_stale_artefacts([out_gds, report_path], owner=self.pnr_cfg.get_name())
            return self._export_failed(
                f"stream-out reported {other_errors} error(s) beyond missing cells"
                if other_errors
                else f"KLayout exited {r.returncode} after streaming"
            )

        if allowed_empty:
            log_event(
                logger,
                logging.INFO,
                "pnr.gds_allowed_empty_cells",
                pnr=self.pnr_cfg.get_name(),
                count=len(allowed_empty),
                cells=allowed_empty,
            )
        if missing:
            log_event(
                logger,
                self._gds_log_level(),
                "pnr.gds_incomplete",
                pnr=self.pnr_cfg.get_name(),
                mode=str(self.gds_mode),
                count=len(missing),
                cells=missing,
                log=log_path,
            )
        return GdsExport(
            mode=self.gds_mode,
            status=GDS_INCOMPLETE if missing else GDS_COMPLETE,
            png_requested=self.emit_png,
            gds_path=out_gds,
            missing_cells=missing,
            allowed_empty_cells=allowed_empty,
            desc=(
                f"GDS incomplete: no layout for {describe_missing_cells(missing)}"
                if missing
                else ""
            ),
        )

    def _run_gds2png(self, platform, gds_path: str, design: str) -> str | None:
        klayout = _resolve_klayout_exe()
        if not klayout:
            return None
        lyp = self.klayout_props or platform.get_pdk().get_klayout_props()
        out_png = os.path.join(self.artefact_dir, f"{design}.png")
        script = self._klayout_script_path("gds2png.py")
        cmd = [
            klayout,
            "-zz",
            "-nc",
            "-rd",
            f"in_gds={gds_path}",
            "-rd",
            f"lyp_file={lyp}",
            "-rd",
            f"out_png={out_png}",
            "-rd",
            f"width={self.png_width}",
            "-rd",
            f"height={self.png_height}",
            "-r",
            script,
        ]
        log_path = os.path.join(self.artefact_dir, "klayout.gds2png.log")
        clear_stale_artefacts([out_png], owner=self.pnr_cfg.get_name())
        with task_status(f"pnr {self.pnr_cfg.get_name()} [klayout png]"):
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        Path(log_path).write_text((r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0 or not os.path.isfile(out_png):
            log_event(
                logger,
                self._gds_log_level(),
                "pnr.png_failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                log=log_path,
            )
            clear_stale_artefacts([out_png], owner=self.pnr_cfg.get_name())
            return None
        return out_png

    def _export_provenance_path(self) -> str:
        return os.path.join(self.artefact_dir, _EXPORT_PROVENANCE_NAME)

    def _clear_export_outputs(self, design: str, *, keep_gds: bool = False) -> None:
        """Clear the export's own outputs: GDS, PNG, stream-out report, input manifest and provenance.

        It must not touch anything OpenROAD wrote, since the export reads the routed DEF and `rb power` reads the ODB. It runs before validation so an export that fails early leaves no previous layout behind. `keep_gds` keeps the GDS and its report, which a PNG-only re-render takes as inputs.
        """
        cleared = clear_stale_artefacts(
            [
                None if keep_gds else os.path.join(self.artefact_dir, f"{design}.gds"),
                os.path.join(self.artefact_dir, f"{design}.png"),
                None if keep_gds else self._def2stream_report_path(),
                None
                if keep_gds
                else os.path.join(self.artefact_dir, _DEF2STREAM_INPUTS_NAME),
                self._export_provenance_path(),
            ],
            owner=self.pnr_cfg.get_name(),
        )
        if cleared:
            log_event(
                logger,
                logging.DEBUG,
                "pnr_export.stale_artefacts_removed",
                pnr=self.pnr_cfg.get_name(),
                paths=cleared,
            )

    def _check_routed_def(self, in_def: str, design: str) -> str | None:
        """Return why this DEF cannot be exported (absent, empty, not a DEF, or another design's), or None.

        It runs before KLayout, which would otherwise write a layout that looks produced.
        """
        if not os.path.isfile(in_def):
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.no_def",
                pnr=self.pnr_cfg.get_name(),
                design=design,
                path=in_def,
            )
            return f"no routed DEF at {in_def} — run rb pnr first"
        if os.path.getsize(in_def) == 0:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.empty_def",
                pnr=self.pnr_cfg.get_name(),
                design=design,
                path=in_def,
            )
            return f"routed DEF is empty: {in_def}"
        found = read_def_design_name(in_def)
        if found is None:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.def_unreadable",
                pnr=self.pnr_cfg.get_name(),
                design=design,
                path=in_def,
            )
            return f"{in_def} has no DESIGN statement — not a DEF"
        if found != design:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.def_stale",
                pnr=self.pnr_cfg.get_name(),
                path=in_def,
                expected=design,
                found=found,
            )
            return (
                f"{in_def} holds design '{found}', not '{design}' — "
                "the saved result does not belong to this run"
            )
        return None

    def _probe_klayout_version(self, klayout: str) -> str | None:
        """Return KLayout's version banner, or None if the probe fails.

        A failed probe is recorded as an unknown version, not raised; `_run_def2stream` rejects a missing KLayout.
        """
        try:
            r = subprocess.run(
                [klayout, "-v"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = (r.stdout or r.stderr or "").strip()
        return out.splitlines()[0] if out else None

    def _write_export_provenance(
        self,
        platform,
        design: str,
        *,
        export: GdsExport,
        in_def: str | None,
        png_only: bool,
        klayout: str | None,
        klayout_version: str | None,
        checkpoint: "pnr_checkpoints.CheckpointRef | None" = None,
    ) -> str:
        """Write `export.provenance.json` recording what this export read and produced, and return its path.

        Only `rb pnr-export` writes it, whatever the outcome. It holds a schema version, generator, timestamp and project-relative POSIX paths. The source DEF (or, for a re-render, GDS) also carries its size and SHA-256.
        """
        root = project_root_or_none(self.artefact_dir)

        def _rel(path):
            return project_relative(path, root) if root and path else path

        def _fingerprint(path):
            record = _file_fingerprint(path)
            if record is not None:
                record["path"] = _rel(record["path"])
            return record

        inputs = self.gather_def2stream_inputs(platform)
        source_gds = export.gds_path if png_only else None
        document = {
            "schema_version": EXPORT_PROVENANCE_SCHEMA,
            "generator": f"rtl-buddy {version('rtl-buddy')}",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "command": "pnr-export",
            "run": self.pnr_cfg.get_name(),
            "top": design,
            "gds_mode": str(self.gds_mode),
            "png_only": png_only,
            "checkpoint": checkpoint.provenance(root) if checkpoint else None,
            "tool": {"name": "klayout", "path": klayout, "version": klayout_version},
            "inputs": {
                "def": _fingerprint(in_def),
                "gds": _fingerprint(source_gds),
                "tech": _rel(inputs.tech) or None,
                "cell_gds": [_rel(p) for p in inputs.gds],
                "lef": [_rel(p) for p in inputs.lef],
                "missing": [_rel(p) for p in inputs.missing],
                "allow_empty": self.pnr_cfg.get_gds_allow_empty(),
            },
            "render": {
                "requested": self.emit_png,
                "lyp": _rel(
                    self.klayout_props or platform.get_pdk().get_klayout_props()
                )
                or None,
                "width": self.png_width,
                "height": self.png_height,
            },
            "outputs": {
                "gds": _rel(export.gds_path),
                "png": _rel(export.png_path),
            },
            "outcome": {
                "status": export.status,
                "delivered": export.delivered,
                "missing_cells": list(export.missing_cells),
                "allowed_empty_cells": list(export.allowed_empty_cells),
                "desc": export.desc,
            },
        }
        path = self._export_provenance_path()
        Path(path).write_text(json.dumps(document, indent=2) + "\n")
        return path

    def export_only(
        self,
        *,
        def_path: str | None = None,
        png_only: bool = False,
        checkpoint: str | None = None,
    ) -> PnrResults:
        """Export a saved P&R result's layout (DEF to GDS to PNG) without running OpenROAD or synthesis.

        The design name comes from the synth entry in `synth.yaml`, so no synthesis artefacts are needed. Unlike `rb pnr`, the export is the job: anything short of the requested artefacts is a FAIL in both modes, except that `preview` passes a layout with cells that have no GDS, qualified.

        `checkpoint` exports a stage checkpoint instead. Its DEF is the input and all outputs go to the checkpoint's `export/<stage>` directory; the provenance and result name the stage and mark it not final.
        """
        if png_only:
            self.emit_png = True
        log_event(
            logger,
            logging.INFO,
            "pnr_export.start",
            pnr=self.pnr_cfg.get_name(),
            mode=str(self.gds_mode),
            png_only=png_only,
            png=self.emit_png,
        )
        try:
            platform = self.root_cfg.get_pnr_platform_cfg(self.pnr_cfg.get_platform())
        except Exception as e:
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"platform lookup failed: {e}",
                fail_stage="setup",
            )
        try:
            design = self.pnr_cfg.resolve_synth_cfg().get_top()
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.no_design",
                pnr=self.pnr_cfg.get_name(),
                synth=self.pnr_cfg.get_synth_name(),
                error=str(e),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"cannot resolve the design name from the synth entry: {e}",
                fail_stage="setup",
            )
        blocks_failure = self._resolve_blocks(platform)
        if blocks_failure is not None:
            return blocks_failure
        ckpt = None
        if checkpoint is not None:
            ckpt = pnr_checkpoints.resolve_checkpoint(self.artefact_dir, checkpoint)
            if isinstance(ckpt, str):
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr_export.no_checkpoint",
                    pnr=self.pnr_cfg.get_name(),
                    checkpoint=checkpoint,
                    reason=ckpt,
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"checkpoint unusable: {ckpt}",
                    fail_stage="setup",
                )
            self.artefact_dir = ckpt.export_dir()
            os.makedirs(self.artefact_dir, exist_ok=True)
            def_path = ckpt.def_path
        # Before validation, so a failing export leaves no previous layout behind.
        self._clear_export_outputs(design, keep_gds=png_only)
        if self.klayout_props and not os.path.isfile(self.klayout_props):
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.no_lyp",
                pnr=self.pnr_cfg.get_name(),
                path=self.klayout_props,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"layer properties file not found: {self.klayout_props}",
                fail_stage="setup",
            )
        # Probed up front so the version is recorded even when the export fails.
        klayout = _resolve_klayout_exe()
        klayout_version = self._probe_klayout_version(klayout) if klayout else None

        in_def = None
        if png_only:
            export = self.rerender_layout(platform, design)
        else:
            in_def = def_path or os.path.join(self.artefact_dir, f"{design}.def")
            problem = self._check_routed_def(in_def, design)
            export = (
                self._export_failed(problem)
                if problem is not None
                else self.export_layout(platform, design, in_def=in_def)
            )

        provenance = self._write_export_provenance(
            platform,
            design,
            export=export,
            in_def=in_def,
            png_only=png_only,
            klayout=klayout,
            klayout_version=klayout_version,
            checkpoint=ckpt,
        )
        fields = {**export.result_fields(), "export_provenance": provenance}
        qualifier = ""
        if ckpt is not None:
            fields.update(
                checkpoint_stage=ckpt.name,
                checkpoint_run_id=ckpt.run_id,
                checkpoint_final=False,
            )
            qualifier = f"checkpoint {ckpt.name} of run {ckpt.run_id} (not final)"
        produced = export.gds_path is not None and (
            export.png_path is not None or not export.png_requested
        )
        if not produced:
            log_event(
                logger,
                logging.ERROR,
                "pnr_export.failed",
                pnr=self.pnr_cfg.get_name(),
                mode=str(self.gds_mode),
                status=export.status,
                desc=export.desc,
            )
            desc = export.desc or "export not delivered"
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"{qualifier}: {desc}" if qualifier else desc,
                fail_stage="export",
                fields=fields,
            )
        log_event(
            logger,
            logging.INFO,
            "pnr_export.done",
            pnr=self.pnr_cfg.get_name(),
            status=export.status,
            gds=export.gds_path,
            png=export.png_path,
            provenance=provenance,
        )
        return PnrPassResults(
            name=self.name + "/results",
            desc="; ".join(
                part
                for part in (
                    qualifier,
                    export.desc or ("PNG re-rendered" if png_only else "GDS exported"),
                )
                if part
            ),
            fields=fields,
        )

    def _clear_stale_outputs(self, *, include_script: bool = False) -> None:
        """Remove the previous run's outputs; the first thing `run` does.

        Every exit from `run` must leave them absent: the DRC count, `rb power` and the GDS/PNG checks all trust presence. The one exception is a `--resume` run that ends before it is accepted, which leaves the previous run's outputs and `latest` as they were. Design-named outputs are matched by suffix so this needs no synth back-reference and strands nothing from a previous design. The stream-out report and any `rb pnr-export` record go too. `pnr.log` is truncated when OpenROAD launches.

        `include_script` also clears `pnr.tcl`, the stream-out input manifest and `congestion.rpt` so a rerun that dies before `_write_script` leaves no stale script. Only `run` sets it; `_fail_after_openroad` keeps them because they are what the tools read and why a route failed.
        """
        stale = clear_stale_artefacts(
            [
                os.path.join(self.artefact_dir, name)
                for name in (
                    *_FIXED_OUTPUT_NAMES,
                    _DEF2STREAM_REPORT_NAME,
                    _EXPORT_PROVENANCE_NAME,
                    *(
                        (
                            _SCRIPT_NAME,
                            _DEF2STREAM_INPUTS_NAME,
                            _BLOCK_NETLIST_NAME,
                            _CONGESTION_REPORT_NAME,
                        )
                        if include_script
                        else ()
                    ),
                )
            ]
            + (
                [pnr_checkpoints.latest_pointer(self.artefact_dir)]
                if include_script
                else []
            ),
            owner=self.pnr_cfg.get_name(),
        )
        # `own` lets a design colliding with a sibling's protected name still clear its own files.
        own: list[str] = []
        try:
            design = self.pnr_cfg.resolve_synth_cfg().get_top()
        except Exception:
            pass
        else:
            own = [f"{design}{suffix}" for suffix in _MANAGED_OUTPUT_SUFFIXES]
        stale += clear_managed_outputs(
            self.artefact_dir,
            _MANAGED_OUTPUT_SUFFIXES,
            owner=self.pnr_cfg.get_name(),
            own=own,
            own_flow="pnr-openroad",
        )
        # The abstract is cut from the result being replaced, whether or not this run hardens.
        stale += pnr_abstract.clear_abstract(self.artefact_dir)
        # The parameter probe's scratch files belong to the abstract.
        probe_dir = os.path.join(self.artefact_dir, _PARAM_PROBE_DIR)
        if include_script and os.path.isdir(probe_dir):
            shutil.rmtree(probe_dir, ignore_errors=True)
            stale.append(probe_dir)
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "pnr.stale_artefacts_removed",
                pnr=self.pnr_cfg.get_name(),
                paths=stale,
            )

    def _dont_use_violations(self, log_text: str) -> list[tuple[str, str, str]]:
        """Return `(instance, master, pattern)` for each placed don't-use cell, in log order."""
        hits = []
        for line in log_text.splitlines():
            if line.startswith(_DONT_USE_VIOLATION_TAG):
                # rsplit: only the instance name can contain whitespace.
                fields = line[len(_DONT_USE_VIOLATION_TAG) :].strip().rsplit(None, 2)
                if len(fields) == 3:
                    hits.append(tuple(fields))
        return hits

    def _warn_unmatched_dont_use(self, log_text: str, cells: list[str]) -> None:
        """Log a warning naming every `dont-use-cells` pattern that matched no Liberty cell.

        OpenROAD only warns (`STA-0122`, or `STA-0121` for a missing library), so a misspelt pattern would otherwise exclude nothing silently. STA prints only the cell part of a `lib/cell` pattern, so both spellings are compared.
        """
        if not cells:
            return
        missed = set(_STA_CELL_NOT_FOUND.findall(log_text))
        missed_libs = set(_STA_LIBRARY_NOT_FOUND.findall(log_text))
        unmatched = [
            c
            for c in cells
            if c in missed
            or c.rsplit("/", 1)[-1] in missed
            or ("/" in c and c.rsplit("/", 1)[0] in missed_libs)
        ]
        if unmatched:
            log_event(
                logger,
                logging.WARNING,
                "pnr.dont_use_unmatched",
                pnr=self.pnr_cfg.get_name(),
                patterns=unmatched,
                log=self._log_path(),
            )

    def _no_wire_rc_qualifier(self, platform, log_text: str) -> str:
        """Warn when CTS, placement parasitics and hold repair ran with zero wire R/C, and return the result qualifier, or `""`.

        Without a PDK `layer-rc-tcl` the technology LEF's layer RC is all the pre-route steps see, which is zero on most open PDKs. The run still passes, but CTS balances and hold repair fixes paths whose wires are not there yet, so the final extracted timing can fail hold that repair never saw. Either cause is enough: no `layer-rc-tcl`, or OpenROAD's own warning that the RC it has is zero.
        """
        no_layer_rc = not platform.get_pdk().get_layer_rc_tcl()
        codes = [code for code in _NO_WIRE_RC_CODES if f"[WARNING {code}]" in log_text]
        if not no_layer_rc and not codes:
            return ""
        causes = (["no layer-rc-tcl"] if no_layer_rc else []) + codes
        log_event(
            logger,
            logging.WARNING,
            "pnr.no_wire_rc",
            pnr=self.pnr_cfg.get_name(),
            layer_rc_tcl=not no_layer_rc,
            codes=codes,
            docs=_NO_WIRE_RC_DOCS,
            log=self._log_path(),
        )
        return (
            f"pnr.no_wire_rc: pre-route steps saw no wire RC "
            f"({', '.join(causes)}); see {_NO_WIRE_RC_DOCS}"
        )

    def _fail_after_openroad(
        self, desc: str, fail_stage: str | None = None
    ) -> PnrFailResults:
        """Return a FAIL for a run that already invoked OpenROAD, after removing its outputs.

        OpenROAD may leave a partial `<top>.routed.odb`, which `rb power` would accept by existence. Every post-OpenROAD failure return must go through here. `fail_stage` marks a failure that is not a verdict on the design, so `xfail:` does not excuse it.
        """
        self._clear_stale_outputs()
        return PnrFailResults(
            name=self.name + "/results",
            desc=desc,
            fail_stage=fail_stage,
            fields={
                **self._blocks_fields(),
                **self._threads_fields(),
                **(self._close_checkpoints("FAIL", desc, announce=True) or {}),
            },
        )

    def _check_routed_placement(self, log_text: str) -> str | None:
        """Compare the routed DEF with the macros and pins the floorplan fixed; return the failure description, or None when they agree.

        The flow tags each located macro and exact pin as placed (see `pnr_placement_check`); a run without either has nothing to compare.
        """
        fp = self.pnr_cfg.get_floorplan()
        located = sum(1 for m in fp.macros if m.location is not None)
        exact = any(p.location is not None for p in fp.pins)
        if not located and not exact:
            return None
        tags = pnr_placement_check.parse_tags(log_text)
        design = self.pnr_cfg.resolve_synth_cfg().get_top()
        def_path = os.path.join(self.artefact_dir, f"{design}.def")
        if len(tags.macros) != located or (exact and not tags.pins):
            problems = [
                f"pnr.log reports {len(tags.macros)} of {located} located macro(s) and "
                f"{len(tags.pins)} exact pin(s); expected every one"
            ]
        else:
            try:
                problems = pnr_placement_check.check_routed_def(tags, def_path)
            except (OSError, ValueError, IndexError) as e:
                problems = [f"cannot read {def_path}: {e}"]
        if not problems:
            log_event(
                logger,
                logging.INFO,
                "pnr.routed_check_passed",
                pnr=self.pnr_cfg.get_name(),
                macros=len(tags.macros),
                pins=len(tags.pins),
                def_path=def_path,
            )
            return None
        desc = pnr_placement_check.describe(problems)
        log_event(
            logger,
            logging.ERROR,
            "pnr.routed_check_failed",
            pnr=self.pnr_cfg.get_name(),
            count=len(problems),
            problems=problems,
            desc=desc,
            def_path=def_path,
        )
        return desc

    def _checkpoint_inputs(self, platform, script_path: str | None) -> dict:
        """Return fingerprints of the files the generated script reads, for the checkpoint manifest.

        Files those files source themselves are not followed. `netlist` is the file OpenROAD reads; when that is the `blocks:`-stripped copy, `synth_netlist` records the synth netlist it was made from. `script` is left out without `script_path`.
        """
        pdk = platform.get_pdk()
        stripped = (
            {"synth_netlist": _file_fingerprint(self._resolve_netlist_path())}
            if self._openroad_netlist
            else {}
        )
        return {
            "netlist": _file_fingerprint(
                self._openroad_netlist or self._resolve_netlist_path()
            ),
            **stripped,
            "sdc": _file_fingerprint(self.pnr_cfg.get_constraints()),
            # Every corner's, each corner in `corners:` order.
            "liberty": [
                _file_fingerprint(p)
                for p in [
                    *(
                        lib
                        for libs in platform.get_sta_corner_lib_paths().values()
                        for lib in libs
                    ),
                    *self.pnr_cfg.get_lib_paths(),
                ]
            ],
            "lef": [
                _file_fingerprint(p)
                for p in _dedup_paths(
                    [
                        pdk.get_tech_lef(),
                        pdk.get_macro_lef(),
                        *self.pnr_cfg.get_lef_paths(),
                    ]
                )
            ],
            "pin_constraints": _file_fingerprint(self.pnr_cfg.pin_constraints),
            **self._planner_files(_file_fingerprint),
            "pdn_config": _file_fingerprint(self._pdn_config(pdk)),
            "rcx_rules": _file_fingerprint(pdk.get_rcx_rules()),
            **{
                key.replace("-", "_"): _file_fingerprint(path)
                for key, path in pdk.get_tcl_hooks().items()
            },
            **{
                _hook_role(point): [_file_fingerprint(p) for p in paths]
                for point, paths in self.pnr_cfg.get_hooks().items()
            },
            **({"script": _file_fingerprint(script_path)} if script_path else {}),
        }

    def _close_checkpoints(
        self, result: str, desc: str | None, *, announce: bool = False
    ) -> dict | None:
        """Complete this run's checkpoint manifest and return its result fields, or None without checkpoints.

        `announce` logs the step the run stopped in and the last stage saved; it is set for failures inside OpenROAD.
        """
        if self._ckpt_run_dir is None:
            return None
        try:
            summary = pnr_checkpoints.finish_run(
                self._ckpt_run_dir,
                returncode=self._openroad_returncode,
                result=result,
                desc=desc or "",
                fingerprint=_file_fingerprint,
            )
        except OSError as e:
            log_event(
                logger,
                logging.WARNING,
                "pnr.checkpoint_manifest_failed",
                pnr=self.pnr_cfg.get_name(),
                dir=self._ckpt_run_dir,
                error=str(e),
            )
            return {"checkpoint_dir": self._ckpt_run_dir}
        if announce:
            log_event(
                logger,
                logging.WARNING,
                "pnr.checkpoints_retained",
                pnr=self.pnr_cfg.get_name(),
                dir=summary["checkpoint_dir"],
                stages=summary["checkpoint_stages"],
                step=(summary["last_step"] or {}).get("step"),
                step_status=(summary["last_step"] or {}).get("status"),
            )
        resumed_from = self._resumed_from()
        return {**summary, "resumed_from": resumed_from} if resumed_from else summary

    def _resolve_blocks(self, platform) -> PnrFailResults | None:
        """Resolve `blocks:` to published abstracts and append their views to the run's path lists.

        Each block's LEF, Liberty and GDS are appended to `lef-paths`, `lib-paths` and `gds-paths`, so blocks are treated like hand-wired macros. Returns a FAIL before OpenROAD when an abstract is missing, stale or built for another technology or corner; blocks are never re-run.
        """
        refs = self.pnr_cfg.get_blocks()
        if not refs:
            return None
        try:
            if platform.is_multi_corner():
                raise pnr_abstract.BlockResolutionError(
                    f"blocks: needs a single-corner platform; "
                    f"'{self.pnr_cfg.get_platform()}' declares corners, and an "
                    "abstract carries one corner's timing model"
                )
            resolved = pnr_abstract.resolve_blocks(refs)
            for block in resolved:
                pnr_abstract.check_technology(
                    block,
                    liberty=platform.get_sta_lib_paths(),
                    tech_lef=platform.get_pdk().get_tech_lef(),
                )
            resolved = pnr_abstract.assess_blocks(
                resolved, self.root_cfg, accept_stale=self.accept_stale
            )
        except pnr_abstract.BlockResolutionError as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr.block_unresolved",
                pnr=self.pnr_cfg.get_name(),
                reason=str(e),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=str(e),
                fail_stage="setup",
            )
        self._blocks = resolved
        for block in resolved:
            if block.stale:
                log_event(
                    logger,
                    logging.WARNING,
                    "pnr.block_stale_accepted",
                    pnr=self.pnr_cfg.get_name(),
                    block=block.ref.name,
                    changes=list(block.changes),
                )
        self.pnr_cfg = replace(
            self.pnr_cfg,
            lef_paths=[*self.pnr_cfg.get_lef_paths(), *(b.lef for b in resolved)],
            lib_paths=[*self.pnr_cfg.get_lib_paths(), *(b.lib for b in resolved)],
            gds_paths=[*self.pnr_cfg.get_gds_paths(), *(b.gds for b in resolved)],
        )
        log_event(
            logger,
            logging.INFO,
            "pnr.blocks_resolved",
            pnr=self.pnr_cfg.get_name(),
            blocks=[b.ref.name for b in resolved],
        )
        return None

    def _prepare_netlist(self, *, write: bool = True) -> PnrFailResults | None:
        """Point the script at a copy of the synth netlist with the `blocks:` instances' parameter overrides stripped.

        OpenROAD's reader rejects the `#(...)` Yosys leaves on a parameterised block's instances. Since `rb synth` with `blocks:` already strips them, this matters for a netlist from a synthesis without `blocks:`. Returns a FAIL when an override differs from the block's synthesis `params:` or between instances; see :mod:`.block_params`.

        Without `write` the copy is only planned: nothing is written or removed, and `_planned_netlist` holds the fingerprint the copy will have.
        """
        self._openroad_netlist = None
        self._planned_netlist = None
        if not self._blocks:
            return None
        source = self._resolve_netlist_path()
        if not os.path.isfile(source):
            return None
        destination = os.path.join(self.artefact_dir, _BLOCK_NETLIST_NAME)
        try:
            if write:
                path, instances = block_params.clean_netlist(
                    source, destination, self._blocks
                )
            else:
                path, instances = self._plan_netlist(source, destination)
        except (block_params.BlockParamError, OSError) as e:
            mismatch = isinstance(e, block_params.BlockParamError)
            desc = (
                str(e)
                if mismatch
                else f"could not strip block parameter overrides from {source}: {e}"
            )
            log_event(
                logger,
                logging.ERROR,
                "pnr.block_params_mismatch" if mismatch else "pnr.block_netlist_failed",
                pnr=self.pnr_cfg.get_name(),
                error=desc,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=desc,
                fail_stage="setup",
                fields=self._blocks_fields(),
            )
        if instances:
            self._openroad_netlist = path
        if instances and write:
            log_event(
                logger,
                logging.INFO,
                "pnr.block_params_stripped",
                pnr=self.pnr_cfg.get_name(),
                instances=[f"{i.module}:{i.instance}" for i in instances],
                netlist=path,
            )
        return None

    def _plan_netlist(self, source: str, destination: str):
        """`block_params.clean_netlist` without writing: the path OpenROAD will read and the rewritten instances; sets `_planned_netlist` when a copy is needed."""
        with open(source) as f:
            text = f.read()
        cleaned, instances = block_params.clean_text(text, self._blocks, source)
        if cleaned == text:
            return source, []
        data = cleaned.encode()
        self._planned_netlist = {
            "path": destination,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        return destination, instances

    def _blocks_fields(self) -> dict:
        """Return the `blocks` result field listing the abstracts this run consumed."""
        if not self._blocks:
            return {}
        return {"blocks": [b.result_row() for b in self._blocks]}

    def _planner_files(self, record) -> dict:
        """`macros_file` and `pins_file`, each through `record`, only when set, so other runs' records keep their roles."""
        fp = self.pnr_cfg.get_floorplan()
        return {
            role: record(path)
            for role, path in (
                ("macros_file", fp.macros_file),
                ("pins_file", fp.pins_file),
            )
            if path is not None
        }

    def abstract_inputs(self, platform) -> dict:
        """Return every input file a hardened result was made from, by role.

        `rtl` lists the synthesis filelist's sources so an RTL edit is detected before re-synthesis.
        """
        pdk = platform.get_pdk()
        synth_cfg = self.pnr_cfg.resolve_synth_cfg()
        synth_dir = os.path.join(
            os.path.dirname(self.pnr_cfg.get_synth_suite_path()),
            "artefacts",
            synth_cfg.get_name(),
        )
        return {
            "rtl": pnr_abstract.filelist_sources(os.path.join(synth_dir, "synth.f")),
            "netlist": self._resolve_netlist_path(),
            "sdc": self.pnr_cfg.get_constraints(),
            "liberty": [*platform.get_sta_lib_paths(), *self.pnr_cfg.get_lib_paths()],
            "lef": _dedup_paths(
                [pdk.get_tech_lef(), pdk.get_macro_lef(), *self.pnr_cfg.get_lef_paths()]
            ),
            "pin_constraints": self.pnr_cfg.pin_constraints,
            **self._planner_files(lambda path: path),
            "pdn_config": self._pdn_config(pdk) or None,
            # Only the configured hooks, so an abstract hardened without any records no extra roles.
            **{
                key.replace("-", "_"): path for key, path in pdk.get_tcl_hooks().items()
            },
            **{
                _hook_role(point): list(paths)
                for point, paths in self.pnr_cfg.get_hooks().items()
            },
        }

    def _record_block_parameters(self, staging: str, design: str) -> None:
        """Write `<top>.params.json`, the parameter values the hardened top was elaborated with, into the staging abstract.

        A parent's instance overrides are checked against it (see `block_params`). A probe that cannot run leaves no record and a warning: the abstract is still usable, and its consumers fall back to the synthesis's `params:`.
        """
        synth_cfg = self.pnr_cfg.resolve_synth_cfg()
        synth_dir = os.path.join(
            os.path.dirname(self.pnr_cfg.get_synth_suite_path()),
            "artefacts",
            synth_cfg.get_name(),
        )
        try:
            record = probe_block_parameters(
                synth_cfg,
                self.root_cfg,
                synth_dir,
                os.path.join(self.artefact_dir, _PARAM_PROBE_DIR),
            )
            Path(pnr_abstract.view_path(staging, design, "params.json")).write_text(
                record.to_json()
            )
        except Exception as e:  # never fails the harden; consumers fall back
            log_event(
                logger,
                logging.WARNING,
                "pnr.block_params_unrecorded",
                pnr=self.pnr_cfg.get_name(),
                error=str(e),
            )
            return
        log_event(
            logger,
            logging.INFO,
            "pnr.block_params_recorded",
            pnr=self.pnr_cfg.get_name(),
            frontend=record.frontend,
            complete=record.complete,
            parameters=record.parameters,
        )

    def _publish_abstract(
        self, platform, openroad_version: str | None, export: GdsExport | None
    ) -> dict | str:
        """Copy the GDS into staging, write the manifest and publish the abstract.

        Returns the result fields, or the reason the abstract could not be produced after removing every trace of the attempt.
        """
        design = self.pnr_cfg.resolve_synth_cfg().get_top()
        staging = pnr_abstract.staging_dir(self.artefact_dir)
        missing = [
            os.path.basename(pnr_abstract.view_path(staging, design, view))
            for view in ("lef", "lib")
            if not os.path.isfile(pnr_abstract.view_path(staging, design, view))
            or os.path.getsize(pnr_abstract.view_path(staging, design, view)) == 0
        ]
        gds = export.gds_path if export is not None else None
        if not gds or not os.path.isfile(gds):
            missing.append(f"{design}.gds")
        problem = (
            f"abstract view(s) not produced: {', '.join(missing)}" if missing else None
        )
        feedthrough = self.pnr_cfg.get_abstract().feedthrough_layers
        feedthrough_lef = pnr_abstract.view_path(
            staging, design, pnr_abstract.FEEDTHROUGH_VIEW
        )
        if problem is None and feedthrough and not os.path.isfile(feedthrough_lef):
            problem = (
                f"abstract view(s) not produced: {os.path.basename(feedthrough_lef)}"
            )
        if problem is None:
            self._record_block_parameters(staging, design)
            try:
                if feedthrough:
                    unused = pnr_abstract.splice_feedthrough_obs(
                        pnr_abstract.view_path(staging, design, "lef"),
                        feedthrough_lef,
                        feedthrough,
                    )
                    if unused:
                        log_event(
                            logger,
                            logging.WARNING,
                            "pnr.abstract_feedthrough_layers_unused",
                            pnr=self.pnr_cfg.get_name(),
                            layers=unused,
                        )
                shutil.copyfile(gds, pnr_abstract.view_path(staging, design, "gds"))
                pnr_abstract.write_manifest(
                    staging,
                    artefact_dir=self.artefact_dir,
                    design=design,
                    run=self.pnr_cfg.get_name(),
                    platform=self.pnr_cfg.get_platform(),
                    pdk=platform.get_pdk().get_name(),
                    openroad={
                        "path": shutil.which(self.openroad_executable),
                        "version": openroad_version,
                    },
                    technology={
                        "tech_lef": platform.get_pdk().get_tech_lef(),
                        "liberty": pnr_abstract.one_or_many(
                            platform.get_sta_lib_paths()
                        ),
                    },
                    inputs=self.abstract_inputs(platform),
                    config=pnr_abstract.abstract_config(self._configured_cfg, platform),
                )
                published = pnr_abstract.publish(self.artefact_dir)
            except OSError as e:
                problem = f"abstract could not be written: {e}"
        if problem is not None:
            pnr_abstract.clear_abstract(self.artefact_dir)
            log_event(
                logger,
                logging.ERROR,
                "pnr.abstract_failed",
                pnr=self.pnr_cfg.get_name(),
                reason=problem,
                log=self._log_path(),
            )
            return problem
        manifest = os.path.join(published, pnr_abstract.ABSTRACT_MANIFEST_NAME)
        log_event(
            logger,
            logging.INFO,
            "pnr.abstract_published",
            pnr=self.pnr_cfg.get_name(),
            dir=published,
            manifest=manifest,
        )
        return {"abstract_dir": published, "abstract_manifest": manifest}

    def _stage_option_error(self) -> str | None:
        return pnr_stages.option_error(
            self._requested_stop_after, self.resume, self.resume_from
        )

    def _ckpt_stages(self) -> tuple[str, ...]:
        """The stages whose checkpoint this run writes."""
        stages = self._run_stages()
        if self._staged:
            return tuple(s for s in stages if s in CHECKPOINT_STAGES)
        wanted = set(self.pnr_cfg.get_checkpoints() or ())
        if self.stop_after is not None:
            wanted.add(self.stop_after)
        if self._split:
            wanted.update(self._session_stages())
            wanted.add("detailed_route")
        return tuple(s for s in CHECKPOINT_STAGES if s in wanted and s in stages)

    def _check_resume(
        self, platform, version: str | None, run_dir: str | None
    ) -> str | None:
        """Check the run `--resume` names and keep what this run inherits from it; return why it is refused, or None.

        Runs before this run clears or writes anything: the stage scripts are rendered for a placeholder run directory, which script identity ignores, and a `blocks:` netlist is compared as `_prepare_netlist` would write it.
        """
        if run_dir is None:
            return (
                f"no checkpoint run {self.resume!r}: name a run directory, a run id "
                f"under {pnr_checkpoints.checkpoints_root(self.artefact_dir)}, or latest"
            )
        inherited = self._stages[: self._stages.index(self.resume_from)]
        inputs = self._checkpoint_inputs(platform, None)
        if self._planned_netlist is not None:
            inputs["netlist"] = self._planned_netlist
        checked = pnr_stages.check_resume(
            run_dir,
            self.resume_from,
            stages=self._stages,
            mode=self._mode,
            openroad_version=version,
            scripts={
                stage: self._stage_script(stage, run_dir="<run>") for stage in inherited
            },
            inputs=inputs,
            fingerprint=_file_fingerprint,
        )
        if isinstance(checked, str):
            return checked
        self._resume_source = checked
        return None

    def _restore_inherited_reports(self) -> None:
        """Put back the reports an inherited stage's checkpoint keeps (`pnr_checkpoints.STAGE_REPORTS`), so a run resumed after detailed routing reports that route's DRC count."""
        for stage, kinds in pnr_checkpoints.STAGE_REPORTS.items():
            entry = self._resume_source.checkpoints.get(stage)
            if entry is None:
                continue
            files = entry.get("files") or {}
            for kind, name in kinds.items():
                kept = (files.get(kind) or {}).get("path")
                target = os.path.join(self.artefact_dir, name)
                if kept and os.path.isfile(kept):
                    shutil.copyfile(kept, target)
                elif os.path.lexists(target):
                    os.unlink(target)

    def _resumed_from(self) -> dict | None:
        source = self._resume_source
        if source is None:
            return None
        return {
            "run_id": source.run_id,
            "path": source.run_dir,
            "from": source.from_stage,
            "checkpoint": _checkpoint_name(self._predecessor(source.from_stage)),
        }

    def _manifest_staging(self) -> dict:
        """The manifest's staged-run entries (see `pnr_checkpoints.begin_run`)."""
        if not self._staged:
            return {
                "staged": False,
                "stop_after": self.stop_after,
                **({"chain": self._chain_records()} if self._split else {}),
            }
        source = self._resume_source
        return {
            "staged": True,
            "stop_after": self.stop_after,
            "resumed_from": self._resumed_from(),
            "chain": self._chain_records(),
            "inherited_checkpoints": source.checkpoints if source else None,
            "dispatch": self._dispatch_record(),
        }

    def _dispatch_record(self) -> dict | None:
        """The manifest's `dispatch` entry: the backend, and each stage job's reservation, the run's `threads:` and the OpenROAD threads that reservation gives (the job sets them again from the CPUs it receives)."""
        if self._dispatch is None:
            return None
        stages = {}
        for stage in self._run_stages():
            resources = self._stage_job_resources(stage)
            plan = self._stage_threads(stage)
            stages[stage] = {
                "cpus": resources.cpus,
                "mem": resources.mem,
                "time": resources.time,
                "threads_requested": plan.requested,
                "threads": plan.count if plan.emit else None,
            }
        return {"backend": self._dispatch.name, "stages": stages}

    def _start_chain_log(self, log_path: str) -> None:
        """Start a staged run's `pnr.log`: empty, or for a resumed run the logs of the stages it inherits."""
        Path(log_path).write_bytes(b"")
        with open(log_path, "ab") as log:
            for entry in self._resume_source.chain if self._resume_source else []:
                path = (entry.get("log") or {}).get("path")
                try:
                    log.write(Path(path).read_bytes())
                except (OSError, TypeError):
                    log.write(
                        f">>> rb: log of inherited stage {entry.get('stage')} "
                        f"not readable: {path}\n".encode()
                    )

    def _run_chain(self, env) -> tuple[int, str, str | None]:
        """Run the stages of `_session_stages` in order (`run_stage`) and return the last session's exit code and stderr, and why the chain stopped if a session exited 0 without ending its stage.

        A staged run's `pnr.log` starts afresh (see `_start_chain_log`); a one-session run's continues after its first session. The chain stops at the first stage that fails, including one whose log has an `[ERROR` line; `_failed_stage` and `_chain_fail_stage` then name it and its `fail_stage`.
        """
        log_path = self._log_path()
        returncode, stderr = 0, ""
        if self._staged:
            self._start_chain_log(log_path)
        with open(log_path, "ab") as log:
            for stage in self._session_stages():
                outcome = run_stage(
                    self._ckpt_run_dir,
                    stage,
                    openroad=self.openroad_executable,
                    log=log,
                    log_path=log_path,
                    cwd=self.artefact_dir,
                    env=env,
                    run_name=self.pnr_cfg.get_name(),
                )
                returncode, stderr = outcome.returncode, outcome.stderr
                if not outcome.ok:
                    self._failed_stage = _checkpoint_name(stage)
                    self._chain_fail_stage = outcome.fail_stage
                    return returncode, stderr, outcome.problem
        return returncode, stderr, None

    def _dispatch_chain(self) -> tuple[int | None, str, str | None]:
        """Run a staged run's stages as one chained job each and return what `_run_chain` returns.

        Every stage script is already written, so the jobs run the configuration as it was at submission. The head blocks until the chain has left the queue, then reads how it ended with signals deferred. When rb gives the jobs up (an interrupt, `max-wait`, a failed submission or wait, or any other exception before the chain's end is read), they are cancelled and the run is finished here (`_dispatch_results`); an exception other than a dispatch failure is then raised again.
        """
        run_dir = self._ckpt_run_dir
        log_path = self._log_path()
        self._start_chain_log(log_path)
        os.makedirs(pnr_dispatch.jobs_dir(run_dir), exist_ok=True)
        self._warn_default_resources()
        openroad = shutil.which(self.openroad_executable) or self.openroad_executable
        chain = pnr_dispatch.StageChain(self._dispatch, run=self.pnr_cfg.get_name())
        try:
            with chain:
                for stage in self._run_stages():
                    chain.submit(
                        PnrStageJobSpec(
                            suite_dir=self.suite_dir,
                            run_name=self.pnr_cfg.get_name(),
                            run_dir=run_dir,
                            stage=stage,
                            openroad=openroad,
                            pnr_log=log_path,
                            resources=self._stage_job_resources(stage),
                            log_path=Path(
                                pnr_dispatch.job_file(run_dir, stage, "slurm.log")
                            ),
                            submission_path=Path(
                                pnr_dispatch.job_file(run_dir, stage, "sbatch")
                            ),
                        )
                    )
                    pnr_dispatch.record_jobs(run_dir, chain)
                chain.wait()
            # A signal here is raised once the chain's end is read, and then finishes the run below.
            with pnr_dispatch.signals_deferred(block=False):
                pnr_dispatch.record_jobs(run_dir, chain, drained=True)
                outcome = pnr_dispatch.chain_outcome(run_dir, chain, log_path=log_path)
        except BaseException as e:
            # A repeated signal ends the wait for the cancelled jobs early, and is raised once the run is finished.
            with pnr_dispatch.signals_deferred(block=False):
                self._dispatch_failure = pnr_dispatch.settle_cancelled_chain(
                    run_dir, chain, log_path=log_path, error=e
                )
                self._openroad_returncode = None
                self._dispatch_results = self._fail_after_openroad(
                    self._dispatch_failure,
                    fail_stage=pnr_dispatch.DISPATCH_FAIL_STAGE,
                )
            if not isinstance(e, FatalRtlBuddyError):
                raise
            return None, "", None
        if outcome.failed_stage is not None:
            self._failed_stage = _checkpoint_name(outcome.failed_stage)
            self._chain_fail_stage = outcome.fail_stage
            self._dispatch_reason = outcome.reason
            self._dispatch_first_line = outcome.stderr_first_line
        return outcome.returncode, outcome.stderr, outcome.problem

    def _continue_in_sessions(self, env) -> tuple[int, str, str | None]:
        """Run the stages after detailed route of a one-session run with post-route repair, once its first session ended with the detailed route's checkpoint and without an `[ERROR` line."""
        name = _checkpoint_name("detailed_route")
        try:
            log_text = Path(self._log_path()).read_text()
        except OSError:
            log_text = ""
        lines = log_text.splitlines()
        if f"{_STAGE_DONE_MARKER}{name}" not in lines:
            self._failed_stage = name
            self._chain_fail_stage = INCOMPLETE_FAIL_STAGE
            return 0, "", f"stage {name}: OpenROAD exited before the end of the stage"
        if any(line.startswith("[ERROR ") for line in lines):
            self._failed_stage = name
            return 0, "", None
        return self._run_chain(env)

    def _log_dispatched_rounds(self) -> None:
        """Log the post-route repair rounds of a dispatched run from its progress events: the stage job logged them only to its own log."""
        codes = {
            "post_route_round": "pnr.post_route_round",
            "post_route_repair_done": "pnr.post_route_repair_done",
        }
        for event in pnr_checkpoints.read_progress(self._ckpt_run_dir):
            code = codes.get(event.get("event"))
            if code is not None:
                fields = {k: v for k, v in event.items() if k not in ("event", "t")}
                log_event(
                    logger, logging.INFO, code, pnr=self.pnr_cfg.get_name(), **fields
                )

    def _warn_default_resources(self) -> None:
        """Warn when a dispatched run has no `resources:` block, so every stage job gets `cfg-dispatch.resources` or the built-in defaults."""
        if self.pnr_cfg.get_resources() is not None:
            return
        resources = self._stage_job_resources(self._run_stages()[0])
        log_event(
            logger,
            logging.WARNING,
            "pnr.dispatch_resources_default",
            pnr=self.pnr_cfg.get_name(),
            cpus=resources.cpus,
            mem=resources.mem,
            time=resources.time,
        )

    def _queue_backend(self):
        """The backend that looks up an earlier dispatched run's jobs: this run's, or Slurm's; `NoSchedulerClient` on a host without `squeue`."""
        if self._dispatch is not None:
            return self._dispatch
        if shutil.which("squeue") is None:
            raise pnr_dispatch.NoSchedulerClient(
                "this host has no Slurm client to look them up (squeue not found)"
            )
        from ..dispatch.slurm import SlurmDispatchBackend

        return SlurmDispatchBackend(self.root_cfg.get_dispatch_cfg())

    def _warn_hook_commands(self) -> None:
        """Log each hook of this run's sessions that changed global Tcl commands, once per hook and change: a warning for commands that existed before it, which later sessions have in their original form, and info for new ones, whose later use fails as an unknown command."""
        seen = set()
        for event in pnr_checkpoints.read_progress(self._ckpt_run_dir):
            if event.get("event") != "hook_commands":
                continue
            for key, level, code in (
                ("changed", logging.WARNING, "pnr.hook_commands_not_kept"),
                ("added", logging.INFO, "pnr.hook_commands_added"),
            ):
                once = (key, event.get("point"), event.get("path"), str(event.get(key)))
                if event.get(key) and once not in seen:
                    seen.add(once)
                    log_event(
                        logger,
                        level,
                        code,
                        pnr=self.pnr_cfg.get_name(),
                        point=event.get("point"),
                        path=event.get("path"),
                        commands=event.get(key),
                    )

    def _warn_eco_unguarded(self) -> None:
        """Warn once when `eco` post-route repair rounds ran their repairs without the `design_is_routed` override (`eco_repair.tcl`), because this OpenROAD's `repair_timing` does not call it."""
        rounds = [
            event.get("round")
            for event in pnr_checkpoints.read_progress(self._ckpt_run_dir)
            if event.get("event") == "post_route_eco_unguarded"
        ]
        if rounds:
            log_event(
                logger,
                logging.WARNING,
                "pnr.post_route_eco_unguarded",
                pnr=self.pnr_cfg.get_name(),
                rounds=rounds,
            )

    def _unwritten_checkpoint(self, log_text: str) -> str | None:
        """Why a one-session run that reached its end left a requested checkpoint unwritten, or None.

        Such a stage ends where its anchor command runs (`STAGE_ANCHORS`); a hook that wraps that command with `rename` hides it from the tracer.
        """
        if self._staged or self._ckpt_run_dir is None:
            return None
        end = (
            ">>> DONE"
            if self.stop_after is None
            else f"{STOPPED_MARKER}{self.stop_after}"
        )
        if end not in log_text.splitlines():
            return None
        written = {
            e.get("stage")
            for e in pnr_checkpoints.read_progress(self._ckpt_run_dir)
            if e.get("event") == "checkpoint" and e.get("status") == "ok"
        }
        for stage in self._ckpt_stages():
            if stage in written or stage == self.stop_after:
                continue
            error = checkpoint_error(self._ckpt_run_dir, stage)
            if error is not None:
                return f"checkpoint {_checkpoint_name(stage)} was not written: {error}"
            anchor = pnr_checkpoints.STAGE_ANCHORS.get(stage)
            if anchor is None:
                continue
            command, edge = anchor
            return (
                f"checkpoint {_checkpoint_name(stage)} was not written: the {stage} "
                f"stage ends on {'entering' if edge == 'enter' else 'leaving'} "
                f"{command}, which the run never did under that name; wrapping "
                f"{command} with rename is not supported in a one-session run"
            )
        return None

    def _parse_gr_overflow(self, log_text: str) -> int | None:
        """The total overflow in global routing's last usage table, or None without one."""
        totals = _GR_TOTAL_RE.findall(log_text)
        return int(totals[-1]) if totals else None

    def _stopped_result(self, platform, log_text: str) -> PnrResults:
        """Return the result of a run `--stop-after` ended: PASS with the stage report's numbers, or FAIL when the run ended without its stop marker."""
        stage = self.stop_after
        marker = f"{STOPPED_MARKER}{stage}"
        if marker not in log_text.splitlines():
            return self._fail_after_openroad(
                f"OpenROAD exited before --stop-after {stage} finished "
                f"(no '{marker}' line in pnr.log)",
                fail_stage=INCOMPLETE_FAIL_STAGE,
            )
        report = log_text[log_text.rfind(f"{_STAGE_REPORT_MARKER}{stage}") :]
        ps_per_unit = self._ps_per_unit

        def _ps(value):
            return value * ps_per_unit if value is not None else None

        metrics = {
            "routed": False,
            "mode": self._mode.value,
            "stopped_after": stage,
            "area_um2": self._parse_area_um2(report),
            "cell_count": self._parse_cell_count(log_text),
            "wns_setup_ps": _ps(self._parse_wns(report, "max")),
            "wns_hold_ps": _ps(self._parse_wns(report, "min")),
            "tns_ps": _ps(self._parse_tns(report)),
            "tns_hold_ps": _ps(self._parse_tns(report, "min")),
        }
        if stage in pnr_checkpoints.GLOBAL_ROUTED:
            metrics["global_route_overflow"] = self._parse_gr_overflow(
                log_text[: log_text.rfind(f"{_STAGE_REPORT_MARKER}{stage}")]
            )
        if stage in pnr_checkpoints.DETAIL_ROUTED:
            metrics["drc_count"] = self._count_drcs()
        corner_fields = self._corner_fields(platform, report)
        checkpoint = os.path.join(self._ckpt_run_dir, _checkpoint_name(stage))
        log_event(
            logger,
            logging.INFO,
            "pnr.stopped",
            pnr=self.pnr_cfg.get_name(),
            stage=stage,
            checkpoint=checkpoint,
            **{
                k: v for k, v in metrics.items() if k not in ("routed", "stopped_after")
            },
        )
        probe = "probe " if self._mode is PnrMode.PROBE else ""
        return PnrPassResults(
            name=self.name + "/results",
            desc=f"P&R {probe}stopped after {stage} (not routed)",
            fields={
                **metrics,
                **corner_fields,
                **self._blocks_fields(),
                **self._threads_fields(),
                **(self._close_checkpoints("PASS", None) or {}),
            },
        )

    def run(self) -> PnrResults:
        log_event(
            logger,
            logging.INFO,
            "pnr.start",
            pnr=self.pnr_cfg.get_name(),
            tool=self.openroad_executable,
        )

        option_error = self._stage_option_error()
        # Before the clear removes `latest`.
        resume_dir = (
            pnr_stages.locate_run(self.artefact_dir, self.resume)
            if self.resume is not None and option_error is None
            else None
        )

        if option_error is None:
            # Before anything is cleared: a job of an earlier run would write into this one.
            orphans = pnr_dispatch.live_orphans(self.artefact_dir, self._queue_backend)
            if orphans is not None:
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=orphans.desc(),
                    fail_stage=pnr_dispatch.DISPATCH_FAIL_STAGE,
                )

        # A resume clears only once it is accepted, so a refused one leaves the previous run as it was.
        if self.resume is None:
            self._clear_stale_outputs(include_script=True)

        if option_error is not None:
            return PnrFailResults(
                name=self.name + "/results", desc=option_error, fail_stage="setup"
            )

        if not shutil.which(self.openroad_executable):
            log_event(
                logger,
                logging.WARNING,
                "pnr.no_openroad",
                pnr=self.pnr_cfg.get_name(),
                exe=self.openroad_executable,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"{self.openroad_executable!r} not found",
                fail_stage="setup",
            )

        version = self._probe_openroad_version()
        if version:
            log_event(
                logger,
                logging.INFO,
                "pnr.openroad_version",
                pnr=self.pnr_cfg.get_name(),
                version=version,
                min_version=MIN_OPENROAD_VERSION,
            )
            if self._version_below_min(version):
                log_event(
                    logger,
                    logging.WARNING,
                    "pnr.openroad_version_below_min",
                    pnr=self.pnr_cfg.get_name(),
                    version=version,
                    min_version=MIN_OPENROAD_VERSION,
                )

        try:
            platform = self.root_cfg.get_pnr_platform_cfg(self.pnr_cfg.get_platform())
        except Exception as e:
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"platform lookup failed: {e}",
                fail_stage="setup",
            )

        # Check before OpenROAD starts; a missing snippet would otherwise fail minutes in.
        pdn_config = self._pdn_config(platform.get_pdk())
        if self.pnr_cfg.get_pdn() is not None and not pdn_config:
            log_event(
                logger,
                logging.ERROR,
                "pnr.pdn_without_config",
                pnr=self.pnr_cfg.get_name(),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=(
                    "pdn: needs a pdn-config (the PDK's or the run's) for the "
                    "global connections and voltage domain"
                ),
                fail_stage="setup",
            )
        if pdn_config and not os.path.isfile(pdn_config):
            log_event(
                logger,
                logging.ERROR,
                "pnr.pdn_config_missing",
                pnr=self.pnr_cfg.get_name(),
                path=pdn_config,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"pdn-config not found: {pdn_config}",
                fail_stage="setup",
            )

        for key, path in platform.get_pdk().get_tcl_hooks().items():
            if not os.path.isfile(path):
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr.tcl_hook_missing",
                    pnr=self.pnr_cfg.get_name(),
                    key=key,
                    path=path,
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"{key} not found: {path}",
                    fail_stage="setup",
                )

        for point, paths in self.pnr_cfg.get_hooks().items():
            for index, path in enumerate(paths):
                if not os.path.isfile(path):
                    log_event(
                        logger,
                        logging.ERROR,
                        "pnr.run_hook_missing",
                        pnr=self.pnr_cfg.get_name(),
                        point=point,
                        index=index,
                        path=path,
                    )
                    return PnrFailResults(
                        name=self.name + "/results",
                        desc=f"hooks.{point}[{index}] not found: {path}",
                        fail_stage="setup",
                    )

        # Read only after detailed route, so check now, before the checkpoint directory is allocated.
        rcx_rules = platform.get_pdk().get_rcx_rules()
        if rcx_rules and not os.path.isfile(rcx_rules):
            log_event(
                logger,
                logging.ERROR,
                "pnr.rcx_rules_missing",
                pnr=self.pnr_cfg.get_name(),
                path=rcx_rules,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"rcx-rules not found: {rcx_rules}",
                fail_stage="setup",
            )

        # `create_blockage` needs OpenROAD 26Q1, above the general minimum.
        if self.pnr_cfg.get_floorplan().blockages and not self._has_tcl_command(
            "create_blockage"
        ):
            log_event(
                logger,
                logging.ERROR,
                "pnr.blockages_unsupported",
                pnr=self.pnr_cfg.get_name(),
                exe=self.openroad_executable,
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=(
                    "floorplan.blockages needs OpenROAD's create_blockage "
                    "(26Q1 or newer); this OpenROAD has none"
                ),
                fail_stage="setup",
            )

        # An abstract carries one corner's Liberty model, which a parent would read as the whole block.
        if self.pnr_cfg.get_harden() and platform.is_multi_corner():
            log_event(
                logger,
                logging.ERROR,
                "pnr.harden_multi_corner",
                pnr=self.pnr_cfg.get_name(),
                platform=self.pnr_cfg.get_platform(),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=(
                    f"harden: needs a single-corner platform; "
                    f"'{self.pnr_cfg.get_platform()}' declares corners"
                ),
                fail_stage="setup",
            )

        # Before OpenROAD, so a bad planner file or placement override fails in seconds.
        try:
            if self.pnr_cfg.get_placement() is not None:
                platform = platform.with_run_placement(
                    self.pnr_cfg.get_placement(), self.pnr_cfg.get_name()
                )
            # Resolved here, so an incomplete repair block fails as a setup error that names the run.
            platform = platform.with_run_repairs(
                self.pnr_cfg.global_route_repair,
                self.pnr_cfg.post_route_repair,
                self.pnr_cfg.get_name(),
            )
            platform.get_global_route_repair()
            platform.get_post_route_repair()
            fp = self.pnr_cfg.get_floorplan()
            if fp.macros_file is not None or fp.pins_file is not None:
                self.pnr_cfg = replace(
                    self.pnr_cfg,
                    floorplan=with_planner_files(self.pnr_cfg.get_name(), fp),
                )
        except FatalRtlBuddyError as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr.setup_invalid",
                pnr=self.pnr_cfg.get_name(),
                error=str(e),
            )
            return PnrFailResults(
                name=self.name + "/results", desc=str(e), fail_stage="setup"
            )

        self._configure_stages(platform)
        stage_problem = pnr_stages.run_option_error(
            self._stages, self._mode, self._requested_stop_after, self.resume_from
        )
        if stage_problem is not None:
            return PnrFailResults(
                name=self.name + "/results", desc=stage_problem, fail_stage="setup"
            )
        # Each round times extracted parasitics.
        if POST_ROUTE_REPAIR_STAGE in self._run_stages() and not rcx_rules:
            log_event(
                logger,
                logging.ERROR,
                "pnr.post_route_repair_without_rcx",
                pnr=self.pnr_cfg.get_name(),
                pdk=platform.get_pdk_name(),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=(
                    f"post-route-repair needs rcx-rules on PDK "
                    f"'{platform.get_pdk_name()}': each round times the route on "
                    "extracted parasitics, and the PDK sets no rcx-rules"
                ),
                fail_stage="setup",
            )

        blocks_failure = self._resolve_blocks(platform)
        if blocks_failure is not None:
            return blocks_failure
        netlist_failure = self._prepare_netlist(write=self.resume is None)
        if netlist_failure is not None:
            return netlist_failure

        try:
            self._ps_per_unit = liberty_time_unit_ps(
                [
                    *(
                        lib
                        for libs in platform.get_sta_corner_lib_paths().values()
                        for lib in libs
                    ),
                    *self.pnr_cfg.get_lib_paths(),
                ]
            )
        except LibertyTimeUnitError as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr.liberty_time_unit_error",
                pnr=self.pnr_cfg.get_name(),
                error=str(e),
                units=e.units,
            )
            return PnrFailResults(
                name=self.name + "/results", desc=str(e), fail_stage="setup"
            )

        # Before the script, so a clamped thread count is reported before OpenROAD starts.
        self._threads()
        if self.resume is not None:
            refused = None
            try:
                # The stages before `--from` are regenerated and checked before anything is cleared or written.
                self._prepare_flow(platform, self.pnr_cfg.get_floorplan())
                refused = self._check_resume(platform, version, resume_dir)
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr.template_failed",
                    pnr=self.pnr_cfg.get_name(),
                    error=str(e),
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"template error: {e}",
                    fail_stage="setup",
                )
            if refused:
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr.resume_refused",
                    pnr=self.pnr_cfg.get_name(),
                    resume=self.resume,
                    from_stage=self.resume_from,
                    reason=refused,
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"resume refused: {refused}",
                    fail_stage="resume",
                )
            self._clear_stale_outputs(include_script=True)
            self._restore_inherited_reports()
            netlist_failure = self._prepare_netlist()
            if netlist_failure is not None:
                return netlist_failure
        if (
            self.pnr_cfg.get_checkpoints() is not None
            or self._staged
            or self.stop_after is not None
            or self._split
        ):
            try:
                self._ckpt_run_dir = pnr_checkpoints.allocate_run_dir(self.artefact_dir)
            except (OSError, RuntimeError) as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr.checkpoint_setup_failed",
                    pnr=self.pnr_cfg.get_name(),
                    error=str(e),
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"checkpoint setup failed: {e}",
                    fail_stage="setup",
                )
        try:
            if self.resume is None:
                script_path = self._write_script(platform, self.pnr_cfg.get_floorplan())
            else:
                script_path = self._emit_scripts()
        except Exception as e:
            if self._ckpt_run_dir is not None:
                shutil.rmtree(self._ckpt_run_dir, ignore_errors=True)
                self._ckpt_run_dir = None
            log_event(
                logger,
                logging.ERROR,
                "pnr.template_failed",
                pnr=self.pnr_cfg.get_name(),
                error=str(e),
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"template error: {e}",
                fail_stage="setup",
            )

        if self._ckpt_run_dir is not None:
            try:
                manifest = pnr_checkpoints.begin_run(
                    self._ckpt_run_dir,
                    artefact_dir=self.artefact_dir,
                    run=self.pnr_cfg.get_name(),
                    design=self.pnr_cfg.resolve_synth_cfg().get_top(),
                    stages=self._ckpt_stages(),
                    openroad={
                        "path": shutil.which(self.openroad_executable),
                        "version": version,
                    },
                    inputs=self._checkpoint_inputs(platform, script_path),
                    staged=self._manifest_staging(),
                    mode=self._mode.value,
                )
            except Exception as e:
                # Stop before OpenROAD: unrecorded checkpoints would be unidentifiable.
                shutil.rmtree(self._ckpt_run_dir, ignore_errors=True)
                self._ckpt_run_dir = None
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr.checkpoint_setup_failed",
                    pnr=self.pnr_cfg.get_name(),
                    error=str(e),
                )
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=f"checkpoint setup failed: {e}",
                    fail_stage="setup",
                )
            log_event(
                logger,
                logging.INFO,
                "pnr.checkpoints_armed",
                pnr=self.pnr_cfg.get_name(),
                dir=self._ckpt_run_dir,
                stages=list(self._ckpt_stages()),
                staged=self._staged,
                stop_after=self.stop_after,
                resumed_from=(self._resumed_from() or {}).get("run_id"),
                manifest=manifest,
            )

        log_path = self._log_path()
        env = os.environ.copy()
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

        chain_problem = None
        if self._dispatch is not None:
            returncode, stderr_text, chain_problem = self._dispatch_chain()
            self._log_dispatched_rounds()
            self._warn_hook_commands()
            self._warn_eco_unguarded()
        elif self._staged:
            returncode, stderr_text, chain_problem = self._run_chain(env)
            self._warn_hook_commands()
            self._warn_eco_unguarded()
        else:
            # No `-log`: OpenROAD block-buffers that file, so a long route showed nothing for minutes.
            # Its stdout is flushed per message, so it goes to the log directly instead.
            cmd = [
                self.openroad_executable,
                "-no_init",
                "-exit",
                script_path,
            ]
            log_event(
                logger,
                logging.DEBUG,
                "pnr.run_cmd",
                pnr=self.pnr_cfg.get_name(),
                cmd=" ".join(cmd),
                log=log_path,
            )
            # Opened before launch, so a failed launch leaves an empty log, not an old run's violation lines.
            with (
                task_status(f"pnr {self.pnr_cfg.get_name()} [openroad]"),
                open(log_path, "w") as log_file,
            ):
                returncode, stderr_text, _ = _run_session(
                    cmd, stdout=log_file, env=env, cwd=self.artefact_dir
                )

            # Tcl errors go to stderr; append them so the diagnostic survives.
            stderr_text = stderr_text.strip()
            if stderr_text:
                try:
                    with open(log_path, "a") as log_file:
                        log_file.write(stderr_text + "\n")
                except OSError:
                    pass
            if self._split:
                if returncode == 0:
                    returncode, stderr_text, chain_problem = self._continue_in_sessions(
                        env
                    )
                self._warn_hook_commands()
                self._warn_eco_unguarded()
        if self._dispatch_results is not None:
            return self._dispatch_results
        self._openroad_returncode = returncode

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        # Before the exit-code check: the violating instances are the diagnostic.
        self._warn_unmatched_dont_use(log_text, platform.get_dont_use_cells())
        dont_use_hits = self._dont_use_violations(log_text)
        if dont_use_hits:
            log_event(
                logger,
                logging.ERROR,
                "pnr.dont_use_instantiated",
                pnr=self.pnr_cfg.get_name(),
                count=len(dont_use_hits),
                instances=dont_use_hits,
                log=log_path,
            )
            inst, master, pattern = dont_use_hits[0]
            more = (
                f" (+{len(dont_use_hits) - 1} more)" if len(dont_use_hits) > 1 else ""
            )
            return self._fail_after_openroad(
                f"{len(dont_use_hits)} instance(s) of dont-use-cells in the "
                f"routed design: {inst} is {master} (pattern {pattern!r}){more}"
            )

        block_power_errors = [
            line[len(_BLOCK_POWER_ERROR_TAG) :].strip()
            for line in log_text.splitlines()
            if line.startswith(_BLOCK_POWER_ERROR_TAG)
        ]
        for line in log_text.splitlines():
            if line.startswith(_BLOCK_POWER_WARNING_TAG):
                log_event(
                    logger,
                    logging.WARNING,
                    "pnr.block_power_warning",
                    pnr=self.pnr_cfg.get_name(),
                    detail=line[len(_BLOCK_POWER_WARNING_TAG) :].strip(),
                )
        if block_power_errors:
            log_event(
                logger,
                logging.ERROR,
                "pnr.block_power_failed",
                pnr=self.pnr_cfg.get_name(),
                count=len(block_power_errors),
                errors=block_power_errors,
                log=log_path,
            )
            more = (
                f" (+{len(block_power_errors) - 1} more)"
                if len(block_power_errors) > 1
                else ""
            )
            return self._fail_after_openroad(
                f"block power: {block_power_errors[0]}{more}"
            )

        incomplete = pnr_placement_check.parse_pins_complete(log_text)
        if incomplete:
            desc = pnr_placement_check.describe_pins_complete(incomplete)
            log_event(
                logger,
                logging.ERROR,
                "pnr.pins_incomplete",
                pnr=self.pnr_cfg.get_name(),
                classes={cls: count for cls, count, _ in incomplete},
                desc=desc,
                log=log_path,
            )
            # `fail_stage` keeps an `xfail:` from excusing pin entries that do not cover the ports.
            return self._fail_after_openroad(desc, fail_stage="pins_complete")

        stage_prefix = f"stage {self._failed_stage}: " if self._failed_stage else ""
        # A local session reports a problem only when it exited 0; a dispatched stage job killed by the scheduler has a problem and no exit code.
        if chain_problem is not None:
            log_event(
                logger,
                logging.WARNING,
                "pnr.failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=returncode,
                stage=self._failed_stage,
                log=log_path,
            )
            return self._fail_after_openroad(
                chain_problem + self._eco_hint(log_text),
                fail_stage=self._chain_fail_stage,
            )
        if returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "pnr.failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=returncode,
                stage=self._failed_stage,
                log=log_path,
            )
            first_line = next((ln for ln in stderr_text.splitlines() if ln.strip()), "")
            if self._dispatch_first_line is not None:
                first_line = self._dispatch_first_line
            desc = f"{stage_prefix}OpenROAD exited with code {returncode}"
            if first_line:
                desc = f"{desc}: {first_line.strip()}"
            if self._dispatch_reason:
                desc = f"{desc} ({self._dispatch_reason})"
            desc += self._eco_hint(log_text)
            if not self._staged and self.stop_after is not None:
                stop_unwritten = checkpoint_error(self._ckpt_run_dir, self.stop_after)
            else:
                stop_unwritten = None
            return self._fail_after_openroad(
                desc,
                fail_stage=CHECKPOINT_FAIL_STAGE
                if stop_unwritten
                else self._chain_fail_stage,
            )

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            desc = f"{stage_prefix}{len(error_lines)} ERROR(s) in OpenROAD log"
            if self._dispatch_reason:
                desc = f"{desc} ({self._dispatch_reason})"
            desc += self._eco_hint(log_text)
            return self._fail_after_openroad(desc, fail_stage=self._chain_fail_stage)

        unwritten = self._unwritten_checkpoint(log_text)
        if unwritten is not None:
            return self._fail_after_openroad(
                unwritten, fail_stage=CHECKPOINT_FAIL_STAGE
            )

        if self.stop_after is not None:
            return self._stopped_result(platform, log_text)

        # Logged for every run that got this far; only a pass carries it in the result description.
        no_wire_rc = self._no_wire_rc_qualifier(platform, log_text)

        final = _final_reports(log_text)
        area = self._parse_area_um2(final)
        cells = self._parse_cell_count(log_text)
        routed_cells, physical_cells = self._parse_routed_cell_counts(log_text)
        wns_setup = self._parse_wns(final, "max")
        wns_hold = self._parse_wns(final, "min")
        tns = self._parse_tns(final)
        tns_hold = self._parse_tns(final, "min")
        drcs = self._count_drcs()

        ps_per_unit = self._ps_per_unit
        metrics = {
            "routed": True,
            "mode": self._mode.value,
            "area_um2": area,
            "cell_count": cells,
            "routed_cell_count": routed_cells,
            "physical_cell_count": physical_cells,
            "wns_setup_ps": wns_setup * ps_per_unit if wns_setup is not None else None,
            "wns_hold_ps": wns_hold * ps_per_unit if wns_hold is not None else None,
            "tns_ps": tns * ps_per_unit if tns is not None else None,
            "tns_hold_ps": tns_hold * ps_per_unit if tns_hold is not None else None,
            "drc_count": drcs,
            **self._parse_electrical(log_text),
        }
        corner_fields = self._corner_fields(platform, final)

        mismatch = self._check_routed_placement(log_text)
        if mismatch is not None:
            # `fail_stage` keeps an `xfail:` from excusing a floorplan the flow did not keep. The routed outputs stay for inspection.
            pnr_abstract.clear_abstract(self.artefact_dir)
            return PnrFailResults(
                name=self.name + "/results",
                desc=mismatch,
                fail_stage="routed_check",
                fields={
                    **metrics,
                    **corner_fields,
                    **self._blocks_fields(),
                    **self._threads_fields(),
                    **(self._close_checkpoints("FAIL", mismatch) or {}),
                },
            )
        # Logged whether or not it fails the run, so a pass with violators never reads as clean.
        electrical = self._describe_electrical(metrics)
        if electrical:
            log_event(
                logger,
                logging.WARNING,
                "pnr.electrical_violators",
                pnr=self.pnr_cfg.get_name(),
                **{
                    key: metrics.get(key, 0)
                    for key, _word in _ELECTRICAL_FIELDS.values()
                },
                fail_on_electrical=self.pnr_cfg.get_fail_on_electrical(),
                report=os.path.join(self.artefact_dir, "electrical.rpt"),
            )

        export: GdsExport | None = None
        if self.emit_gds:
            design = self.pnr_cfg.resolve_synth_cfg().get_top()
            export = self.export_layout(platform, design)

        if export is not None and self._strict() and not export.delivered:
            # `fail_stage="export"` keeps a timing `xfail:` from excusing this. The routed DEF and ODB stay; staged abstract views go.
            pnr_abstract.clear_abstract(self.artefact_dir)
            return PnrFailResults(
                name=self.name + "/results",
                desc=export.desc,
                fail_stage="export",
                fields={
                    **metrics,
                    **corner_fields,
                    **export.result_fields(),
                    **self._blocks_fields(),
                    **self._threads_fields(),
                    **(self._close_checkpoints("FAIL", export.desc) or {}),
                },
            )

        if electrical and self.pnr_cfg.get_fail_on_electrical():
            # A verdict on the design, so no `fail_stage`: an `xfail:` may excuse it. The routed outputs stay for inspection; no abstract is published.
            pnr_abstract.clear_abstract(self.artefact_dir)
            desc = (
                f"electrical violators in the routed design: {electrical} "
                "(fail-on-electrical; see electrical.rpt)"
            )
            return PnrFailResults(
                name=self.name + "/results",
                desc=desc,
                fields={
                    **metrics,
                    **corner_fields,
                    **(export.result_fields() if export else {}),
                    **self._blocks_fields(),
                    **self._threads_fields(),
                    **(self._close_checkpoints("FAIL", desc) or {}),
                },
            )

        abstract_fields: dict = {}
        if self.pnr_cfg.get_harden():
            published = self._publish_abstract(platform, version, export)
            if isinstance(published, str):
                return PnrFailResults(
                    name=self.name + "/results",
                    desc=published,
                    fail_stage="abstract",
                    fields={
                        **metrics,
                        **corner_fields,
                        **(export.result_fields() if export else {}),
                        **self._blocks_fields(),
                        **self._threads_fields(),
                        **(self._close_checkpoints("FAIL", published) or {}),
                    },
                )
            abstract_fields = published

        log_event(
            logger,
            logging.INFO,
            "pnr.passed",
            pnr=self.pnr_cfg.get_name(),
            **metrics,
            worst_setup_corner=corner_fields.get("worst_setup_corner"),
            worst_hold_corner=corner_fields.get("worst_hold_corner"),
            gds_status=export.status if export is not None else None,
            log=log_path,
        )
        qualifiers = [
            q
            for q in (
                export.desc if export else "",
                pnr_abstract.stale_qualifier(self._blocks),
                no_wire_rc,
                f"electrical {electrical}" if electrical else "",
            )
            if q
        ]
        return PnrPassResults(
            name=self.name + "/results",
            desc=f"P&R passed; {'; '.join(qualifiers)}" if qualifiers else None,
            fields={
                **metrics,
                **corner_fields,
                **(export.result_fields() if export else {}),
                **abstract_fields,
                **self._blocks_fields(),
                **self._threads_fields(),
                **(self._close_checkpoints("PASS", None) or {}),
            },
        )
