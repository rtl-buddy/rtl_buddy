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
from ..config.pnr import GdsMode, PnrConfig
from ..logging_utils import log_event, task_status
from ..pnr.klayout.def2stream import REPORT_SCHEMA
from ..runner.pnr_results import PnrFailResults, PnrPassResults, PnrResults
from .artifact_paths import (
    clear_managed_outputs,
    clear_stale_artefacts,
    project_relative,
    project_root_or_none,
)


_TEMPLATE_PACKAGE = "rtl_buddy.pnr"
_TEMPLATE_FILE = "flow.tcl.template"
# Pure-Tcl macro packer, substituted into the flow ahead of the macro
# placement stage. It lives in its own file so it can be unit tested under a
# bare Tcl interpreter without an OpenROAD database (#626).
_MACRO_PACK_FILE = "macro_pack.tcl"

# Every non-log file `flow.tcl.template` writes under `$OUT_DIR`, as
# `{design}`-templated basenames. Kept here rather than spelled out at the
# call site so the clear list cannot drift away from the template — the
# `.log` targets are deliberately absent (a log is the one artefact worth
# keeping when the tool dies early), and `test_pnr.py` asserts this tuple
# still covers every non-log write target the template contains.
_FLOW_OUTPUT_NAMES = (
    "route.drc.rpt",
    "timing.rpt",
    "{design}.def",
    "{design}.routed.v",
    "{design}.routed.sdc",
    "{design}.routed.odb",
)

# KLayout's streamout / render outputs. Written after the OpenROAD run, but
# cleared with it: a rerun that dies inside OpenROAD — or one on a host with
# no KLayout — never reaches the helpers below, so an old layout would
# otherwise survive a run that produced no layout at all.
_KLAYOUT_OUTPUT_NAMES = ("{design}.gds", "{design}.png")

# The same outputs as a suffix set. The design-named ones are cleared by
# suffix rather than by name so the clear does not depend on resolving the
# synth back-reference that supplies `{design}` — and so that editing a run's
# design leaves nothing of the previous one behind. Safe because an artefact
# directory belongs to exactly one pnr run; the KLayout helper scripts it also
# holds are `.py` and their input manifest `.json`, and the logs are
# deliberately absent from this set.
_MANAGED_OUTPUT_SUFFIXES = (
    ".def",
    ".routed.v",
    ".routed.sdc",
    ".routed.odb",
    ".gds",
    ".png",
)

# Outputs whose names carry no design, cleared by exact name.
_FIXED_OUTPUT_NAMES = tuple(
    name for name in _FLOW_OUTPUT_NAMES if "{design}" not in name
)

# The generated OpenROAD flow script. Not an output — it is the *input*
# `_write_script` builds — but it is written into the artefact dir and is
# read by a user debugging a run, so a rerun that fails before
# `_write_script` must not leave the previous run's script describing it
# (#527). Cleared up front only; see `_clear_stale_outputs`.
_SCRIPT_NAME = "pnr.tcl"

# The stream-out result the bundled KLayout helper writes and
# `_run_def2stream` reads back: which cells came out empty, and whether
# anything else went wrong. An *output*, and one judged by presence, so it
# is cleared with the GDS and the PNG — a previous run's report read as
# this run's is exactly the stale-artefact failure of #469, and would
# report a complete export for a stream-out that never ran.
_DEF2STREAM_REPORT_NAME = "def2stream.report.json"

# What `rb pnr-export` records about an export it performed over a saved
# result: the tool, the inputs and the outcome (#618). Written by the
# export-only path alone — a P&R run writes no such record — but cleared
# by both, because a fresh run replaces the very DEF the record describes.
_EXPORT_PROVENANCE_NAME = "export.provenance.json"

#: Bumped when :func:`OpenRoadPnr._write_export_provenance`'s document
#: changes shape incompatibly.
EXPORT_PROVENANCE_SCHEMA = 1

#: Default render size, shared by the backend and the `--png-width` /
#: `--png-height` options that override it for one invocation (#618).
DEFAULT_PNG_WIDTH = 2048
DEFAULT_PNG_HEIGHT = 2048


# Marker the post-route don't-use check prints for each offending instance,
# and the one `run` looks for in the log to name them (#656).
_DONT_USE_VIOLATION_TAG = "RB-DONT-USE-VIOLATION:"

# `get_lib_cells` / `set_dont_use` warning for a pattern that matched no
# Liberty cell: `[WARNING STA-0122] cell '<pattern>' not found.`
_STA_CELL_NOT_FOUND = re.compile(
    r"^\[WARNING STA-0122\] cell '(.+)' not found\.$", re.M
)


def _dont_use_check_tcl(cells: list[str]) -> str:
    """The post-route check that no don't-use cell made it into the design.

    `set_dont_use` only stops the resizer and CTS from *choosing* a cell;
    it does nothing about one already in the synthesis netlist, and a
    pattern that matches nothing is only an STA warning. A probe cell in a
    routed SKY130 block fails the power grid much later (#656), so the flow
    checks the placed instances itself, with the same `get_lib_cells`
    matching `set_dont_use` used, and fails the run naming each offender.
    It runs before fill insertion — fill cells are named explicitly by the
    PDK and are not a repair pass's choice — and before any output is
    written. Empty, like the `set_dont_use` block, when no cell is excluded.
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
    """Absolute paths of every non-log artefact one pnr run produces."""
    return [
        os.path.join(artefact_dir, name.format(design=design))
        for name in _FLOW_OUTPUT_NAMES + _KLAYOUT_OUTPUT_NAMES
    ] + [
        os.path.join(artefact_dir, _DEF2STREAM_REPORT_NAME),
        # Not written by a run, but cleared by one: a rerun replaces the
        # DEF a previous `rb pnr-export` record describes, so leaving the
        # record would have it vouch for bytes that are gone (#618).
        os.path.join(artefact_dir, _EXPORT_PROVENANCE_NAME),
    ]


_KLAYOUT_PACKAGE = "rtl_buddy.pnr.klayout"

# The stream-out input manifest `_run_def2stream` hands the bundled KLayout
# helper. An input, not an output — it is written beside the generated
# `pnr.tcl` for the same reason, so a run's layout inputs can be read back
# off disk — and so it is absent from the managed-output suffixes.
_DEF2STREAM_INPUTS_NAME = "def2stream.inputs.json"


@dataclass(frozen=True)
class Def2StreamInputs:
    """Every file KLayout stream-out reads, resolved and in reader order.

    Gathered apart from the run so an export-only command (#618) and the
    completeness gate (#619) can ask for the same set without launching
    anything. `missing` is the subset that is configured but not on disk.
    """

    tech: str
    gds: list[str] = dc_field(default_factory=list)
    lef: list[str] = dc_field(default_factory=list)
    missing: list[str] = dc_field(default_factory=list)


# What a requested export came to. `complete` is a stream-out whose every
# cell has layout (allow-listed empties included — those are layout the
# design says it does not have); `incomplete` streamed a GDS with cells
# that have none; `failed` produced no usable layout at all.
GDS_COMPLETE = "complete"
GDS_INCOMPLETE = "incomplete"
GDS_FAILED = "failed"

# How many missing cell names a one-line description spells out before it
# starts counting. The full list is always in the machine output and in the
# structured log event; a summary table row is not the place for 200 names.
_DESC_CELL_LIMIT = 3


@dataclass(frozen=True)
class GdsExport:
    """What a requested KLayout export delivered, and how complete it is.

    Produced by :meth:`OpenRoadPnr.export_layout`, which is the whole of
    gather → validate → stream out → read the report → render, so the
    export-only command (#618) can hand back the same record without
    running OpenROAD at all.

    `desc` is the one-line qualifier a summary row and a results `desc`
    carry; it is empty exactly when the export delivered everything that
    was asked for, complete.
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
        """Whether the export produced everything asked for, complete.

        A `strict` run that answers False publishes nothing and fails; a
        `preview` one keeps what it has and says what is wrong with it.
        """
        return self.status == GDS_COMPLETE and (
            self.png_path is not None or not self.png_requested
        )

    def result_fields(self) -> dict:
        """The export as result-dict keys, for the machine output.

        Empty lists and `None`s are dropped by the results classes, so a
        run whose export was complete carries only its paths, its mode and
        its status.
        """
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
    """`'2 cells (a, b)'` — the missing-cell count with names attached."""
    shown = ", ".join(cells[:_DESC_CELL_LIMIT])
    if len(cells) > _DESC_CELL_LIMIT:
        shown += f", +{len(cells) - _DESC_CELL_LIMIT} more"
    return f"{len(cells)} cell{'s' if len(cells) != 1 else ''} ({shown})"


#: A DEF's own statement of which design it holds, as its header spells it.
_DEF_DESIGN_RE = re.compile(r"^\s*DESIGN\s+(\S+)\s*;", re.MULTILINE)

#: How much of a DEF is read looking for that statement. The header is the
#: first handful of lines and the body is megabytes of components, so the
#: read is bounded rather than streaming the whole file.
_DEF_HEADER_BYTES = 64 * 1024


def read_def_design_name(path: str) -> str | None:
    """The design a DEF declares, or ``None`` if its header does not say.

    The one staleness check an export over a saved result can make cheaply
    and without guessing (#618). `DESIGN <name> ;` is the cell KLayout is
    told to stream out, so a DEF belonging to some other design — a run
    whose `synth:` back-reference has since been re-pointed, or a `--def`
    from another tree — produces a GDS named after a design it does not
    contain, and the caller is none the wiser. Deliberately *not* an mtime
    comparison against the netlist or the ODB: a checkout, a copy or an
    archive restore rewrites those timestamps in any order, so a
    freshness verdict drawn from them is wrong as often as it is right.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_DEF_HEADER_BYTES)
    except OSError:
        return None
    match = _DEF_DESIGN_RE.search(head.decode("utf-8", "replace"))
    return match.group(1) if match else None


def _file_fingerprint(path: str | None) -> dict | None:
    """``{path, size, sha256}`` for an input whose exact bytes matter.

    What lets a reader of an export record decide, later, whether the
    layout on disk still belongs to the DEF beside it — the question a
    size or an mtime can only approximate.
    """
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
    """The paths in order, one entry per file, empties dropped.

    De-duplication is on the resolved path: a PDK macro LEF a run repeats in
    its own `lef-paths` is one LEF to the reader, and handing it twice makes
    KLayout re-register every master in it.
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


# Minimum OpenROAD release we test against. Older builds may still work for
# the basic flow but are not validated — we warn rather than refuse.
MIN_OPENROAD_VERSION = "25Q1"


def _parse_version_token(version: str) -> tuple:
    """Extract a comparable tuple from an OpenROAD version string.

    Handles `26Q2-911-g...`, `v2.0-1234-g...`, plain `v2.0`. Falls back to
    the raw string so unknown formats just sort consistently.
    """
    m = re.match(r"^v?(\d+)(?:[.Qq](\d+))?", version.strip())
    if not m:
        return (version,)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    return (major, minor)


def _resolve_klayout_exe() -> str | None:
    return shutil.which("klayout")


class OpenRoadPnr:
    """OpenROAD-driven P&R backend.

    Reads the upstream `rb synth` artefact (tech-mapped netlist), runs a
    floorplan → place → CTS → route → fill pipeline against a Nangate45-
    style PDK via a templated Tcl flow, and reports area, WNS
    setup/hold, TNS, and DRC count.
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
    ):
        self.name = name
        self.pnr_cfg = pnr_cfg
        self.root_cfg = root_cfg
        self.openroad_executable = openroad_executable
        self.emit_gds = emit_gds or emit_png
        self.emit_png = emit_png
        self.klayout_executable = klayout_executable
        self.png_width = png_width
        self.png_height = png_height
        # `--gds-mode` overrides the run's own `gds-mode` for this
        # invocation; `None` means the run's, which defaults to preview.
        self.gds_mode = gds_mode or pnr_cfg.get_gds_mode()
        # `--lyp` overrides the PDK's `klayout-props` for this render, so a
        # saved layout can be re-rendered with another palette without
        # editing the PDK every project shares (#618). `None` is the PDK's.
        self.klayout_props = klayout_props
        # Resolved once per run by `_threads()` (#654).
        self._thread_plan: ThreadPlan | None = None

        artefact_root = Path(suite_dir) / "artefacts" / pnr_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, _SCRIPT_NAME)

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "pnr.log")

    # ------------------------------------------------------------------
    # Inputs resolution
    # ------------------------------------------------------------------

    def _resolve_netlist_path(self) -> str:
        """Locate the upstream synth run's tech-mapped netlist."""
        synth_cfg = self.pnr_cfg.resolve_synth_cfg()
        suite_dir = os.path.dirname(self.pnr_cfg.get_synth_suite_path())
        return os.path.join(
            suite_dir, "artefacts", synth_cfg.get_name(), "synth_netlist.v"
        )

    # ------------------------------------------------------------------
    # Tcl templating
    # ------------------------------------------------------------------

    def _threads(self) -> ThreadPlan:
        """This run's OpenROAD thread plan, resolved once (#654).

        Resolved against the allocation the process is in *now*, so a
        clamp is reported before OpenROAD starts, and the script and the
        recorded provenance cannot disagree.
        """
        if self._thread_plan is None:
            self._thread_plan = plan_threads(
                self.pnr_cfg.get_threads(), flow="pnr", run=self.pnr_cfg.get_name()
            )
        return self._thread_plan

    def _threads_fields(self) -> dict:
        """The `openroad_threads` result field, with OpenROAD's own count.

        Empty before a plan exists — a run that failed before it resolved
        one never launched OpenROAD, so it has no thread count to report.
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

        # Design-specific macro libraries and LEFs (e.g. SRAM macros).
        extra_lines = []
        for lib in self.pnr_cfg.get_lib_paths():
            extra_lines.append(f"read_liberty {lib}")
        for lef in self.pnr_cfg.get_lef_paths():
            extra_lines.append(f"read_lef     {lef}")
        extra_libs_lefs = "\n".join(extra_lines)

        # `cts-buffer` is one name or a list. A single name keeps the Tcl
        # the flow has always emitted — `$CTS_BUF` for both flags — so a
        # config that never touched the key renders byte-identically. A
        # list becomes a Tcl list, with its first entry as the root buffer.
        cts_buffers = platform.get_cts_buffers()
        if len(cts_buffers) > 1:
            cts_buf = "{" + " ".join(cts_buffers) + "}"
            cts_root_buf = "[lindex $CTS_BUF 0]"
        else:
            cts_buf = cts_buffers[0] if cts_buffers else ""
            cts_root_buf = "$CTS_BUF"

        # Both blocks carry their own leading newline and are empty when
        # unconfigured, so the surrounding blank lines stay as they are.
        # The platform's list is the PDK's plus its own (#656).
        dont_use_cells = platform.get_dont_use_cells()
        dont_use_block = (
            '\nputs ">>> Don\'t-use cells"\n'
            f"set_dont_use [list {' '.join(dont_use_cells)}]\n"
            if dont_use_cells
            else ""
        )
        dont_use_check_block = _dont_use_check_tcl(dont_use_cells)

        # ORFS convention: the snippet declares the grid, the flow runs
        # `pdngen` after sourcing it.
        pdn_config = pdk.get_pdn_config()
        pdn_block = (
            f'\nputs ">>> Power distribution network"\nsource {pdn_config}\npdngen\n'
            if pdn_config
            else ""
        )

        # Ahead of the first `read_liberty`, and empty when `threads:` is
        # unset so the script is the one this flow has always emitted —
        # OpenROAD's own default is one thread (#654). The `puts` gives
        # the log a stage marker; OpenROAD itself then logs the count it
        # actually took (ORD-0030), which can be lower than asked on a host
        # with fewer cores.
        threads_tcl = self._threads().tcl()
        threads_block = f'\nputs ">>> Threads"\n{threads_tcl}\n' if threads_tcl else ""

        pin_script = self.pnr_cfg.pin_constraints
        pin_constraints_tcl = ""
        if pin_script is not None:
            if not os.path.isfile(pin_script):
                raise RuntimeError(f"pin-constraints file does not exist: {pin_script}")
            # Tcl double-quoted word: suppress substitutions in config paths.
            escaped = pin_script.replace("\\", "\\\\")
            for char in ("$", "[", "]", '"'):
                escaped = escaped.replace(char, "\\" + char)
            pin_constraints_tcl = f'source "{escaped}"'
        substitutions = {
            "pin_constraints_tcl": pin_constraints_tcl,
            "design": self.pnr_cfg.resolve_synth_cfg().get_top(),
            "netlist": netlist,
            "sdc": sdc,
            "liberty": platform.get_sta_lib_path(),
            "tech_lef": pdk.get_tech_lef(),
            "macro_lef": pdk.get_macro_lef(),
            "site": pdk.get_site(),
            "util_pct": f"{fp.utilization * 100:.2f}",
            "aspect": f"{fp.aspect:.2f}",
            "core_margin": f"{fp.core_margin:.2f}",
            "tie_hi": pdk.get_tie_hi(),
            "tie_lo": pdk.get_tie_lo(),
            "cts_buf": cts_buf,
            "cts_root_buf": cts_root_buf,
            "place_density": f"{platform.get_placement_density():g}",
            "place_padding": str(platform.get_placement_padding()),
            "macro_halo": f"{platform.get_placement_macro_halo():g}",
            "macro_pack_procs": self._load_macro_pack(),
            "dont_use_block": dont_use_block,
            "dont_use_check_block": dont_use_check_block,
            "pdn_block": pdn_block,
            "threads_block": threads_block,
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
        }

        template = self._load_template()
        script = template
        for key, value in substitutions.items():
            script = script.replace("{{ " + key + " }}", str(value))

        # Surface any unsubstituted placeholders early.
        leftover = re.findall(r"\{\{\s*[\w]+\s*\}\}", script)
        if leftover:
            raise RuntimeError(
                f"pnr flow template has unsubstituted placeholders: {leftover}"
            )

        script_path = self._script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    # ------------------------------------------------------------------
    # Log parsing
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Version + feature probes
    # ------------------------------------------------------------------

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
        """Probe whether the OpenROAD build exposes a Tcl command.

        Used as a feature-detect for things like `write_gds`. Returns False
        if we cannot determine availability (treated as missing).
        """
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

    # ------------------------------------------------------------------
    # KLayout streamout / render
    # ------------------------------------------------------------------

    def _klayout_script_path(self, name: str) -> str:
        """Materialize a bundled KLayout helper to the artefact dir.

        KLayout's `-r` flag wants a real path on disk; reading from
        importlib.resources isn't enough since some packagers expose the
        module via a zipfile loader. Always copy to the artefact dir.
        """
        target = Path(self.artefact_dir) / name
        target.write_text(files(_KLAYOUT_PACKAGE).joinpath(name).read_text())
        return str(target)

    def gather_def2stream_inputs(self, platform) -> Def2StreamInputs:
        """Resolve every file KLayout stream-out reads, and check it exists.

        The GDS side is the PDK's `cell-gds` — one path or a list of them —
        followed by the run's own `gds-paths`, which is where the layout of
        a hard macro lives (an OpenRAM SRAM, say). The LEF side is what the
        DEF reader needs to resolve the masters the DEF instantiates, in the
        order OpenROAD itself read them: technology LEF, the PDK's macro
        LEF, then the run's `lef-paths`. Both lists are de-duplicated; a
        macro named in both the PDK and the run is one input.

        Public, and separate from the run, so the export-only command (#618)
        and the completeness gate (#619) can gather and validate the same
        set without launching KLayout.
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
        # Only what the config named: the DEF and the helper script are this
        # run's own outputs and are judged where they are produced.
        missing = [
            path
            for path in ([tech] if tech else []) + gds + lef
            if not os.path.isfile(path)
        ]
        return Def2StreamInputs(tech=tech, gds=gds, lef=lef, missing=missing)

    def _write_def2stream_inputs(self, inputs: Def2StreamInputs) -> str:
        """Write the manifest the bundled helper reads.

        A file rather than `-rd` strings: KLayout's `-rd` carries one scalar
        per flag with no list contract, so a multi-path value could only
        travel joined on some separator and would break on the first path
        containing it (#617). The allow-empty list and the report path ride
        along for the same reason — and because the run's contract for
        which cells may be empty belongs in `pnr.yaml`, not in an
        environment variable the helper reads behind the caller's back
        (#619). The manifest is also what a user debugging a stream-out
        wants to see, beside the `pnr.tcl` of the same run.
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
        """ERROR when the export was required, WARNING when it was a bonus.

        `strict` was asked for by someone who needs the layout, and its
        failure fails the run; `preview` keeps going, so its own report of
        the same condition is a warning.
        """
        return logging.ERROR if self._strict() else logging.WARNING

    def _export_failed(self, desc: str) -> GdsExport:
        return GdsExport(
            mode=self.gds_mode,
            status=GDS_FAILED,
            png_requested=self.emit_png,
            desc=f"GDS export failed: {desc}",
        )

    def _read_def2stream_report(self) -> dict | None:
        """The helper's own account of the stream-out, or `None`.

        `None` covers every way the report can fail to say anything: the
        helper died before writing it, the file is truncated, or it carries
        a schema this rtl_buddy does not know. All of them mean the same
        thing to the caller — nobody vouched for this layout — and all of
        them are a failed export rather than a complete one (#619).
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
        """Stream the routed DEF out to GDS and, when asked, render it.

        The whole export in one place — gather, validate, stream out, read
        the helper's report, render — so the export-only command (#618) can
        run it over a saved result without OpenROAD, and so the two agree
        on what counts as a complete export.

        ``in_def`` streams a DEF other than the run's own
        ``<design>.def``, which is what `rb pnr-export --def` hands in; the
        design name, and every other input, still come from the run's
        configuration. ``None`` is the run's own routed DEF.
        """
        return self._render_and_gate(
            platform, self._run_def2stream(platform, design, in_def=in_def), design
        )

    def rerender_layout(self, platform, design: str) -> GdsExport:
        """Render the PNG again from the GDS already in the artefact dir.

        The re-render half of `rb pnr-export` (#618): new layer properties
        or a new resolution over a layout that is already correct is a
        KLayout `save_image`, not another DEF read, so the stream-out is
        skipped entirely and the GDS is an *input* here — never cleared,
        never rewritten.

        The qualifier travels with it. A layout streamed with cells that
        had no GDS is still incomplete however it is rendered, so the
        `def2stream.report.json` beside it is read back and its missing
        cells are carried onto this result. A GDS with no readable report
        is not thereby complete — nothing vouched for it (#619) — so it is
        reported as incomplete-without-a-list, which `preview` renders and
        `strict` refuses.
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
        """Render the requested PNG, then apply the mode to the result.

        The tail both exports share. In `strict` mode an export that did
        not deliver everything asked for publishes nothing: the layout is
        removed rather than left for the next reader to take as this run's
        (#469). `preview` keeps what it produced and carries the qualifier
        that says what is wrong with it.

        ``own_gds`` is whether this invocation is the one that published
        the layout. A stream-out withdraws the GDS and the report it just
        wrote; a re-render was handed a layout someone else published and
        withdraws only the image it made itself (#618).
        """
        if export.gds_path is not None and self.emit_png:
            png_path = self._run_gds2png(platform, export.gds_path, design)
            if png_path is None:
                # The GDS may well be complete — the render is a separate
                # step over a finished layout — but the export as a whole
                # did not produce what `--png` asked for, so it is still a
                # qualified result and, under `strict`, a failed one.
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
            # Every one of them, at ERROR, before KLayout is launched. A
            # stream-out missing one of its inputs does not fail: it writes
            # a GDS with whatever the DEF reader could not resolve left
            # empty, which is a layout that looks produced. An input the
            # config names and the disk does not have is a configuration
            # error, so the export stops here rather than publishing that.
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
        # Both outputs are judged by presence, so a previous run's would
        # mask a failure here and then be rendered and reported as this
        # run's layout — the report the more quietly of the two, since it
        # is what says the layout is complete (#469).
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
            # A zero-length GDS is what the size check above rejects, and it
            # is still a file: leaving it means the next run's `isfile` sees
            # a layout where none was produced (#469).
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
        # A non-zero exit is the helper's error count, and the errors it
        # accounts for are the cells with no layout — that is the case a
        # preview export exists for. Anything else behind that exit code
        # (an orphan cell, a KLayout that died after writing) is a failed
        # export in *either* mode: it is not the condition preview covers.
        # `% 256` because an exit code is a byte.
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
            # Named, not counted: which SRAM has no layout is the whole
            # content of this report, and a log line is where a user who
            # did not ask for machine output meets it (#619).
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
        # `--lyp` for this invocation, the PDK's `klayout-props` otherwise.
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
            # KLayout can render part of the image and then fail; a partial
            # PNG left here is reported as this run's layout by the next one.
            clear_stale_artefacts([out_png], owner=self.pnr_cfg.get_name())
            return None
        return out_png

    # ------------------------------------------------------------------
    # Export-only entry point (#618)
    # ------------------------------------------------------------------

    def _export_provenance_path(self) -> str:
        return os.path.join(self.artefact_dir, _EXPORT_PROVENANCE_NAME)

    def _clear_export_outputs(self, design: str, *, keep_gds: bool = False) -> None:
        """Clear what an export publishes — and nothing else (#618).

        `run` begins by clearing the *P&R* outputs as well, which is right
        for a run about to rewrite them and ruinous for an export over a
        saved result: the routed DEF this reads, and the ODB, netlist and
        SDC a later `rb power` reads, are exactly the files that clear
        removes. So the list here is the export's own — the GDS, the PNG,
        the stream-out report, the input manifest and this record — and
        nothing OpenROAD wrote is named in it.

        Up front, and not merely at each step, for the reason #469 gives:
        an export that fails *before* KLayout is launched (no DEF, no
        technology, an input off disk, no KLayout at all) would otherwise
        leave the previous export's layout sitting at the very path the
        result reports.

        ``keep_gds`` is the PNG-only re-render, where the GDS and its
        report are inputs rather than outputs.
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
        """Why this DEF cannot be exported, or ``None`` when it can.

        Up front and before KLayout, for the reason #617 gives about a
        missing input: a stream-out handed a DEF that is absent, empty or
        another design's does not fail loudly — it writes a layout that
        looks produced. "Fail clearly rather than silently rerouting"
        means saying which of the three it is (#618).
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
        """KLayout's own version banner, or ``None``.

        The readiness check an export makes up front, and the one thing in
        the export record that cannot be read off the configuration. A
        probe that fails says nothing about the export — `_run_def2stream`
        is what refuses a KLayout that is not there — so it is recorded as
        an unknown version rather than raised.
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
    ) -> str:
        """Record what this export read and what it produced (#618).

        Its own document, written by `rb pnr-export` alone: a P&R run's
        record is its `pnr.log`, its results and its `def2stream.inputs
        .json`, and an export performed days later must not be able to
        edit any of them. Written whatever the outcome, because the
        questions it answers — which KLayout, which technology, which GDS,
        which DEF bytes — are asked most often about an export that came
        out wrong.

        The shape follows `phys-manifest.json` (#558): a schema version,
        the generator and the timestamp, then project-relative POSIX
        paths, so the document still reads after the tree has been moved,
        archived or attached to a CI job. The DEF (or, for a re-render,
        the GDS) additionally carries its size and a SHA-256 of its bytes
        — the thing a later reader compares to decide whether the layout
        still belongs to the result beside it.
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
            "tool": {"name": "klayout", "path": klayout, "version": klayout_version},
            "inputs": {
                # Exactly one of these two is the layout's source: the DEF
                # a stream-out read, or the GDS a re-render rendered.
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
        self, *, def_path: str | None = None, png_only: bool = False
    ) -> PnrResults:
        """Export a saved P&R result's layout, running no P&R at all.

        DEF → GDS → PNG over what is already in the artefact directory
        (#618). Structurally incapable of launching OpenROAD or synthesis:
        it never calls :meth:`run`, :meth:`_write_script` or
        :meth:`_resolve_netlist_path`, and it never resolves the OpenROAD
        executable. The design's name comes from the upstream synth
        *entry* — the same `synth:` back-reference `rb pnr` substitutes
        into its flow script, read out of `synth.yaml` — so a box that has
        the configuration but none of the synthesis artefacts can still
        export.

        The verdict is not `rb pnr`'s. There the export is a bonus over a
        P&R verdict, so a `preview` export that fails leaves the run
        passing; here the export *is* the job, so anything short of the
        artefacts that were asked for is a FAIL in both modes. What
        `preview` still forgives is the case it exists for: a layout that
        was published with cells that have no GDS is a qualified pass, not
        a failure.
        """
        if png_only:
            # A re-render is a PNG whether or not `--png` was also typed;
            # there is nothing else for it to produce.
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
        # Before the validation, not after it: an export that fails on its
        # inputs must not leave the *previous* export's layout at the paths
        # a reader takes for this one's (#469). Everything above this line
        # is a failure that could not name those paths anyway.
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
        # KLayout readiness, up front and once: the version is the one
        # thing the record cannot read off the configuration, and it is
        # wanted most when the export goes on to fail. Whether a missing
        # KLayout fails the export is `_run_def2stream`'s call, not this.
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
        )
        fields = {**export.result_fields(), "export_provenance": provenance}
        # Everything that was asked for, on disk. `strict` has already
        # withdrawn what it would not publish, so a rejected export
        # arrives here with nothing to report either way.
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
            return PnrFailResults(
                name=self.name + "/results",
                desc=export.desc or "export not delivered",
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
            # An export that delivered but is qualified says so in the one
            # field every summary row shows first.
            desc=export.desc or ("PNG re-rendered" if png_only else "GDS exported"),
            fields=fields,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def _clear_stale_outputs(self, *, include_script: bool = False) -> None:
        """Drop the previous run's outputs. The FIRST thing `run` does.

        `_count_drcs` reads the routing DRC report off a fixed path and scores
        a *missing* file as zero violations, the DEF and ODB are handed on to
        KLayout streamout and to `rb power`, and the GDS / PNG are judged
        purely by "did a file appear". Every one of those is consumed after
        the fact, several of them by a *later command*, so every exit from
        `run` has to leave them absent — including the "openroad not found"
        and platform/template failures, which return before the tool is
        reached (#469). The logs are left to OpenROAD's own `-log`, which
        truncates them.

        The design-named outputs go by *suffix*. Naming them needs the synth
        back-reference, and resolving that here either preempts the error
        messages the run would otherwise give (when it is broken) or leaves
        `<top>.routed.odb` behind for `rb power` to accept by existence.
        Matching on the suffix also means editing a run's design does not
        strand the previous design's ODB in the same directory.

        The stream-out report goes with them: it is what says a layout is
        complete, so a previous run's would answer for a stream-out this
        run never performed (#619). So does any `rb pnr-export` record,
        which describes a DEF this run is about to overwrite (#618) — a
        run replaces an export, it never edits one.

        `include_script` additionally clears the generated `pnr.tcl` and the
        stream-out input manifest, and is set only by `run`. A rerun that
        dies before `_write_script` — no OpenROAD on the box, an
        unresolvable platform — would otherwise leave the *previous* run's
        flow script sitting beside this run's absent outputs, where it reads
        as the script this run used (#527); the manifest says which GDS and
        LEF were streamed and reads the same way. Neither is cleared by
        `_fail_after_openroad`: past that point what is on disk is what the
        tools really read, which is exactly what someone reading `pnr.log`
        or `klayout.def2stream.log` needs.
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
            ],
            owner=self.pnr_cfg.get_name(),
        )
        # This run's own design-named outputs, cleared whatever they are
        # called — a design that collides with a sibling's protected name
        # must not be able to stop the flow clearing its own files (#469).
        # The design may be unresolvable here; `own` is simply empty then,
        # and the suffix match still covers the ordinary case.
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
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "pnr.stale_artefacts_removed",
                pnr=self.pnr_cfg.get_name(),
                paths=stale,
            )

    def _dont_use_violations(self, log_text: str) -> list[tuple[str, str, str]]:
        """`(instance, master, pattern)` for each don't-use cell the flow's
        post-route check found placed, in log order (#656).

        Split from the right: the master is a Liberty cell name and the
        pattern was rejected at config load if it had whitespace, so only
        the instance name could ever carry any.
        """
        hits = []
        for line in log_text.splitlines():
            if line.startswith(_DONT_USE_VIOLATION_TAG):
                fields = line[len(_DONT_USE_VIOLATION_TAG) :].strip().rsplit(None, 2)
                if len(fields) == 3:
                    hits.append(tuple(fields))
        return hits

    def _warn_unmatched_dont_use(self, log_text: str, cells: list[str]) -> None:
        """Name every `dont-use-cells` pattern that matched no Liberty cell.

        OpenROAD reports one as `[WARNING STA-0122]` and carries on, so a
        misspelt pattern silently excludes nothing — and the post-route
        check, which matches the same way, cannot catch what it misses
        either (#656). STA prints the cell part of a `lib/cell` pattern,
        so both spellings are compared.
        """
        if not cells:
            return
        missed = set(_STA_CELL_NOT_FOUND.findall(log_text))
        unmatched = [c for c in cells if c in missed or c.rsplit("/", 1)[-1] in missed]
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
        """Fail a run that has already invoked OpenROAD, publishing nothing.

        The flow's `write_def` / `write_verilog` / `write_sdc` / `write_db`
        all run before the script ends, so OpenROAD can be killed — or exit
        non-zero, or log an `[ERROR ...]` line — with a complete or partial
        `<top>.routed.odb` on disk. `rb power` resolves that ODB by path and
        accepts it by existence, so returning a FAIL and leaving it there
        hands the next command a database this run never stood behind
        (#469). Every post-OpenROAD failure return goes through here, so a
        new failure gate added to `run` inherits the cleanup by using it.
        """
        self._clear_stale_outputs()
        return PnrFailResults(
            name=self.name + "/results", desc=desc, fields=self._threads_fields()
        )

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

        # A PDN snippet the config names and the disk does not have would
        # otherwise surface as a Tcl `source` error minutes into the run,
        # with a floorplan already written. Same reasoning as the
        # stream-out input check (#617): name it before the tool starts.
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

        # Before the script: a thread count above the allocation is
        # reported (and clamped) ahead of the tool, not after it (#654).
        self._threads()

        try:
            script_path = self._write_script(platform, self.pnr_cfg.get_floorplan())
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

        log_path = self._log_path()
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

        # OpenROAD's `-log` records what it writes to stdout. A Tcl error
        # — the macro packer refusing a floorplan, say — goes to stderr
        # instead, and used to be discarded with stdout, leaving a failed
        # run whose only explanation was an exit code. Append it to the
        # log so the diagnostic survives the run (#626).
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

        # Checked ahead of the exit code: the flow's own don't-use check
        # fails the script with a Tcl `error`, and the instances it names
        # are the diagnostic, not the exit code (#656).
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
            # The first stderr line is the one that names what went wrong;
            # the rest of a multi-line diagnostic stays in the log.
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

        metrics = {
            "area_um2": area,
            "cell_count": cells,
            "wns_setup_ps": wns_setup * 1000.0 if wns_setup is not None else None,
            "wns_hold_ps": wns_hold * 1000.0 if wns_hold is not None else None,
            "tns_ps": tns * 1000.0 if tns is not None else None,
            "drc_count": drcs,
        }

        export: GdsExport | None = None
        if self.emit_gds:
            design = self.pnr_cfg.resolve_synth_cfg().get_top()
            export = self.export_layout(platform, design)

        if export is not None and self._strict() and not export.delivered:
            # P&R itself is done and its verdict is a pass, but the export
            # the user explicitly asked for could not be delivered complete
            # — reporting that as an unqualified exit-0 PASS is the defect
            # #619 is about, so the run fails and says which cells. The
            # stage is named so an `xfail:` marker aimed at the design's
            # timing cannot excuse a collateral problem (#553, #594), and
            # the routed DEF / ODB stay: OpenROAD finished cleanly, and
            # `rb power` has every right to the database it wrote.
            # `export_layout` has already reported the export at ERROR,
            # naming the cells; this is the verdict, not a second report.
            return PnrFailResults(
                name=self.name + "/results",
                desc=export.desc,
                fail_stage="export",
                fields={
                    **metrics,
                    **export.result_fields(),
                    **self._threads_fields(),
                },
            )

        log_event(
            logger,
            logging.INFO,
            "pnr.passed",
            pnr=self.pnr_cfg.get_name(),
            **metrics,
            gds_status=export.status if export is not None else None,
            log=log_path,
        )
        return PnrPassResults(
            name=self.name + "/results",
            # An export that did not deliver qualifies the pass in the one
            # field every summary row shows.
            desc=f"P&R passed; {export.desc}" if export and export.desc else None,
            fields={
                **metrics,
                **(export.result_fields() if export else {}),
                **self._threads_fields(),
            },
        )
