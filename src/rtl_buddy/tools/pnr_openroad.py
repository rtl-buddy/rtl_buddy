import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field as dc_field, replace
from datetime import datetime
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.openroad_threads import ThreadPlan, parse_reported_threads, plan_threads
from ..config.pnr import (
    BlockageType,
    GdsMode,
    MacroAnchor,
    MacroPlacement,
    PnrConfig,
    PnrFloorplan,
)
from ..logging_utils import log_event, task_status
from ..pnr.klayout.def2stream import REPORT_SCHEMA
from ..runner.pnr_results import PnrFailResults, PnrPassResults, PnrResults
from . import openroad_corners
from .artifact_paths import (
    clear_managed_outputs,
    clear_stale_artefacts,
    project_relative,
    project_root_or_none,
)
from . import pnr_abstract, pnr_checkpoints
from .liberty_units import LibertyTimeUnitError, liberty_time_unit_ps


_TEMPLATE_PACKAGE = "rtl_buddy.pnr"
_TEMPLATE_FILE = "flow.tcl.template"
_MACRO_PACK_FILE = "macro_pack.tcl"

# Non-log files the flow writes under `$OUT_DIR`; `test_pnr.py` checks this covers every one.
_FLOW_OUTPUT_NAMES = (
    "route.drc.rpt",
    "timing.rpt",
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
# Public: `rb power` uses it to check the SPEF and ODB came from the same run.
PNR_SCRIPT_NAME = _SCRIPT_NAME

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


def run_output_paths(artefact_dir: str, design: str) -> list[str]:
    """Return the absolute paths of every non-log artefact one pnr run produces."""
    return [
        os.path.join(artefact_dir, name.format(design=design))
        for name in _FLOW_OUTPUT_NAMES + _KLAYOUT_OUTPUT_NAMES
    ] + [
        os.path.join(artefact_dir, _DEF2STREAM_REPORT_NAME),
        os.path.join(artefact_dir, _EXPORT_PROVENANCE_NAME),
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


def _floorplan_directives(fp: PnrFloorplan) -> tuple[str, str]:
    """Return `(blockages_block, macro_pack_directives)` for `floorplan.blockages` and `floorplan.macro-anchor`.

    Both are empty when the floorplan sets neither key. The blockages block runs right after the floorplan. Hard blockages also become `MACRO_KEEPOUTS`, which the packer keeps macros out of; soft and partial ones only thin standard cells. The directives are the anchor and keep-outs appended to the `rb::macro_pack::solve` call.
    """
    lines = []
    has_keepouts = False
    for blockage in fp.blockages:
        region = "{" + " ".join(_tcl_microns(v) for v in blockage.rect) + "}"
        command = f"create_blockage -region {region}"
        if blockage.type is BlockageType.HARD:
            has_keepouts = True
            lines.append(f"set blockage_box [[{command}] getBBox]")
            lines.append(
                "lappend MACRO_KEEPOUTS "
                "[list [$blockage_box xMin] [$blockage_box yMin] [$blockage_box xMax] [$blockage_box yMax]]"
            )
        elif blockage.type is BlockageType.SOFT:
            lines.append(f"{command} -soft")
        else:
            density = f"{blockage.max_density * 100:.6g}"
            lines.append(f"{command} -max_density {density}")
    blockages_block = ""
    if lines:
        header = ['puts ">>> Placement blockages"']
        if has_keepouts:
            header.append("set MACRO_KEEPOUTS {}")
        blockages_block = "\n" + "\n".join(header + lines) + "\n"

    directives = ""
    if has_keepouts:
        directives = f" \\\n      {fp.macro_anchor.value} $MACRO_KEEPOUTS"
    elif fp.macro_anchor is not MacroAnchor.LOWER_LEFT:
        directives = f" \\\n      {fp.macro_anchor.value}"
    return blockages_block, directives


_PACK_PLACEMENT = """\
  set core [$block getCoreArea]
  set halo_dbu [expr {{int(round([ord::microns_to_dbu $MACRO_HALO]))}}]
  set footprints {{}}
  foreach inst $macros {{
    set master [$inst getMaster]
    lappend footprints [list [$inst getName] [$master getWidth] [$master getHeight]]
  }}
  set placement [rb::macro_pack::solve \\
      [list [$core xMin] [$core yMin] [$core xMax] [$core yMax]] \\
      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron{directives}]
  foreach inst $macros {{
    lassign [dict get $placement [$inst getName]] x y
    $inst setLocation $x $y
    $inst setPlacementStatus FIRM
    puts ">>>   placed [$inst getName] at ($x, $y) DBU"
  }}
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
    """Return the macro-placement Tcl for the flow's macro branch."""
    if fp.macro_placement is MacroPlacement.RTL_MP:
        return _RTL_MP_PLACEMENT
    return _PACK_PLACEMENT.format(directives=directives)


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
    ):
        self.name = name
        self.accept_stale = accept_stale
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
        """Return this run's OpenROAD thread plan, resolved once against the current allocation."""
        if self._thread_plan is None:
            self._thread_plan = plan_threads(
                self.pnr_cfg.get_threads(), flow="pnr", run=self.pnr_cfg.get_name()
            )
        return self._thread_plan

    def _threads_fields(self) -> dict:
        """Return the `openroad_threads` result field with OpenROAD's reported count.

        Empty if no plan was resolved, meaning OpenROAD never launched.
        """
        if self._thread_plan is None:
            return {}
        try:
            reported = parse_reported_threads(Path(self._log_path()).read_text())
        except OSError:
            reported = None
        return {"openroad_threads": self._thread_plan.fields(reported)}

    def _load_template(self) -> str:
        return files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_FILE).read_text()

    def _load_macro_pack(self) -> str:
        return files(_TEMPLATE_PACKAGE).joinpath(_MACRO_PACK_FILE).read_text()

    def _write_script(self, platform, fp) -> str:
        pdk = platform.get_pdk()
        netlist = self._resolve_netlist_path()
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

        pdn_config = pdk.get_pdn_config()
        pdn_block = (
            f'\nputs ">>> Power distribution network"\nsource {pdn_config}\npdngen\n'
            if pdn_config
            else ""
        )

        # Must precede the first `read_liberty`.
        threads_tcl = self._threads().tcl()
        threads_block = f'\nputs ">>> Threads"\n{threads_tcl}\n' if threads_tcl else ""

        blockages_block, macro_pack_directives = _floorplan_directives(fp)
        # With `rcx-rules`, extract after fill and time the final reports on the SPEF; otherwise they use the global-route estimate.
        rcx_rules = pdk.get_rcx_rules()
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
        substitutions = {
            "pin_constraints_tcl": pin_constraints_tcl,
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
            "macro_halo": f"{platform.get_placement_macro_halo():g}",
            "macro_cell_halo": f"{platform.get_placement_macro_cell_halo():g}",
            "macro_pack_procs": self._load_macro_pack(),
            "macro_place_block": _macro_place_block(fp, macro_pack_directives),
            "blockages_block": blockages_block,
            "dont_use_block": dont_use_block,
            "dont_use_check_block": dont_use_check_block,
            "pdn_block": pdn_block,
            "threads_block": threads_block,
            "rcx_block": rcx_block,
            "final_parasitics": final_parasitics,
            "harden_block": (
                pnr_abstract.harden_tcl() if self.pnr_cfg.get_harden() else ""
            ),
            "cts_clustering_option": (
                "-sink_clustering_enable" if platform.get_cts_sink_clustering() else ""
            ),
            "signal_layers": platform.get_signal_layers(),
            "clock_layers": platform.get_clock_layers(),
            "pin_layer_horizontal": pdk.get_pin_layer_horizontal(),
            "pin_layer_vertical": pdk.get_pin_layer_vertical(),
            "fill_cells": fill_cells,
            "out_dir": self.artefact_dir,
            "extra_libs_lefs": extra_libs_lefs,
            "checkpoint_block": (
                pnr_checkpoints.render_tcl_block(
                    self._ckpt_run_dir, self.pnr_cfg.get_checkpoints() or ()
                )
                if self._ckpt_run_dir is not None
                else ""
            ),
        }

        template = self._load_template()
        script = template
        for key, value in substitutions.items():
            script = script.replace("{{ " + key + " }}", str(value))

        leftover = re.findall(r"\{\{\s*[\w]+\s*\}\}", script)
        if leftover:
            raise RuntimeError(
                f"pnr flow template has unsubstituted placeholders: {leftover}"
            )

        script_path = self._script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _parse_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"^Design area\s+([\d.]+)\s+um\^2", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _parse_cell_count(self, log_text: str) -> int | None:
        m = re.search(r"Number of instances:\s+(\d+)", log_text)
        return int(m.group(1)) if m else None

    def _parse_wns(self, log_text: str, kind: str) -> float | None:
        m = re.search(rf"^worst slack {kind}\s+([-\d.]+)", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _parse_tns(self, log_text: str) -> float | None:
        m = re.search(r"^tns\s+(?:max|min)?\s*([-\d.]+)", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _corner_fields(self, platform, log_text: str) -> dict:
        """Return per-corner timing result fields for a multi-corner run.

        `corners` maps each corner, in config order, to its `wns_setup_ps`, `wns_hold_ps` and `tns_ps`; `worst_setup_corner` and `worst_hold_corner` name the corner behind the scalar WNS fields. Empty for a single-corner platform.
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
        drc_path = self._drc_report_path()
        if not os.path.isfile(drc_path):
            return 0
        try:
            with open(drc_path) as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

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

        Every exit from `run` must leave them absent: the DRC count, `rb power` and the GDS/PNG checks all trust presence. Design-named outputs are matched by suffix so this needs no synth back-reference and strands nothing from a previous design. The stream-out report and any `rb pnr-export` record go too. Logs are left to OpenROAD's `-log`, which truncates them.

        `include_script` also clears `pnr.tcl` and the stream-out input manifest so a rerun that dies before `_write_script` leaves no stale script. Only `run` sets it; `_fail_after_openroad` keeps them because they are what the tools read.
        """
        stale = clear_stale_artefacts(
            [
                os.path.join(self.artefact_dir, name)
                for name in (
                    *_FIXED_OUTPUT_NAMES,
                    _DEF2STREAM_REPORT_NAME,
                    _EXPORT_PROVENANCE_NAME,
                    *(
                        (_SCRIPT_NAME, _DEF2STREAM_INPUTS_NAME)
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

    def _fail_after_openroad(self, desc: str) -> PnrFailResults:
        """Return a FAIL for a run that already invoked OpenROAD, after removing its outputs.

        OpenROAD may leave a partial `<top>.routed.odb`, which `rb power` would accept by existence. Every post-OpenROAD failure return must go through here.
        """
        self._clear_stale_outputs()
        return PnrFailResults(
            name=self.name + "/results",
            desc=desc,
            fields={
                **self._blocks_fields(),
                **self._threads_fields(),
                **(self._close_checkpoints("FAIL", desc, announce=True) or {}),
            },
        )

    def _checkpoint_inputs(self, platform, script_path: str) -> dict:
        """Return fingerprints of the files the generated script reads, for the checkpoint manifest."""
        pdk = platform.get_pdk()
        return {
            "netlist": _file_fingerprint(self._resolve_netlist_path()),
            "sdc": _file_fingerprint(self.pnr_cfg.get_constraints()),
            "liberty": [
                _file_fingerprint(p)
                for p in [
                    *platform.get_sta_lib_paths(),
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
            "pdn_config": _file_fingerprint(pdk.get_pdn_config()),
            "script": _file_fingerprint(script_path),
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
        return summary

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

    def _blocks_fields(self) -> dict:
        """Return the `blocks` result field listing the abstracts this run consumed."""
        if not self._blocks:
            return {}
        return {"blocks": [b.result_row() for b in self._blocks]}

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
            "pdn_config": pdk.get_pdn_config() or None,
        }

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
        if problem is None:
            try:
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

    def run(self) -> PnrResults:
        log_event(
            logger,
            logging.INFO,
            "pnr.start",
            pnr=self.pnr_cfg.get_name(),
            tool=self.openroad_executable,
        )

        self._clear_stale_outputs(include_script=True)

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
        pdn_config = platform.get_pdk().get_pdn_config()
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

        blocks_failure = self._resolve_blocks(platform)
        if blocks_failure is not None:
            return blocks_failure

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
        if self.pnr_cfg.get_checkpoints() is not None:
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
            script_path = self._write_script(platform, self.pnr_cfg.get_floorplan())
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
                    stages=self.pnr_cfg.get_checkpoints() or (),
                    openroad={
                        "path": shutil.which(self.openroad_executable),
                        "version": version,
                    },
                    inputs=self._checkpoint_inputs(platform, script_path),
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
                stages=list(self.pnr_cfg.get_checkpoints() or ()),
                manifest=manifest,
            )

        log_path = self._log_path()
        # OpenROAD truncates the log only once running; remove it so a failed launch leaves no old violation lines.
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass
        env = os.environ.copy()
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

        cmd = [
            self.openroad_executable,
            "-no_init",
            "-exit",
            "-log",
            log_path,
            script_path,
        ]
        log_event(
            logger,
            logging.DEBUG,
            "pnr.run_cmd",
            pnr=self.pnr_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"pnr {self.pnr_cfg.get_name()} [openroad]"):
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
            )
        self._openroad_returncode = result.returncode

        # Tcl errors go to stderr, not `-log`; append them so the diagnostic survives.
        stderr_text = (result.stderr or "").strip()
        if stderr_text:
            try:
                with open(log_path, "a") as log_file:
                    log_file.write(stderr_text + "\n")
            except OSError:
                pass

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

        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "pnr.failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            first_line = next((ln for ln in stderr_text.splitlines() if ln.strip()), "")
            desc = f"OpenROAD exited with code {result.returncode}"
            if first_line:
                desc = f"{desc}: {first_line.strip()}"
            return self._fail_after_openroad(desc)

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            return self._fail_after_openroad(
                f"{len(error_lines)} ERROR(s) in OpenROAD log"
            )

        area = self._parse_area_um2(log_text)
        cells = self._parse_cell_count(log_text)
        wns_setup = self._parse_wns(log_text, "max")
        wns_hold = self._parse_wns(log_text, "min")
        tns = self._parse_tns(log_text)
        drcs = self._count_drcs()

        ps_per_unit = self._ps_per_unit
        metrics = {
            "area_um2": area,
            "cell_count": cells,
            "wns_setup_ps": wns_setup * ps_per_unit if wns_setup is not None else None,
            "wns_hold_ps": wns_hold * ps_per_unit if wns_hold is not None else None,
            "tns_ps": tns * ps_per_unit if tns is not None else None,
            "drc_count": drcs,
        }
        corner_fields = self._corner_fields(platform, log_text)

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
