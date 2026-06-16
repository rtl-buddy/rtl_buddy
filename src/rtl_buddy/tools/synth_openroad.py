import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from .synth_yosys import emit_frontend_read_cmds, slang_handles_params
from ..config.synth import (
    SynthConfig,
    SynthToolConfig,
    SynthEffortConfig,
    default_effort_config,
)
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..runner.synth_results import SynthFailResults, SynthPassResults, SynthResults

# ABC script used by the Yosys stage — area-focused, no timing window
# OpenROAD handles timing analysis with native multi-clock SDC support
_ABC_SCRIPT_AREA = (
    "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put"
)


class OpenRoadSynth:
    """Two-stage synthesis backend: Yosys (RTL→netlist) + OpenROAD (timing analysis).

    Stage 1 — Yosys maps RTL to a technology-specific gate-level netlist.
    Stage 2 — OpenROAD reads the netlist, applies the SDC with native
    multi-clock support, and reports area, WNS, and TNS.
    """

    def __init__(
        self,
        name: str,
        synth_cfg: SynthConfig,
        tool_cfg: SynthToolConfig,
        suite_dir: str,
        root_cfg=None,
        yosys_executable: str = "yosys",
        effort_cfg: SynthEffortConfig | None = None,
    ):
        self.name = name
        self.synth_cfg = synth_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg
        self.yosys_executable = yosys_executable
        self.effort_cfg = effort_cfg or default_effort_config()

        artefact_root = Path(suite_dir) / "artefacts" / synth_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.f")

    def _yosys_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.ys")

    def _yosys_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth_yosys.log")

    def _yosys_netlist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth_netlist.v")

    def _or_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.tcl")

    def _or_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.log")

    # ------------------------------------------------------------------
    # Helpers (shared with Yosys flow)
    # ------------------------------------------------------------------

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        _SKIP = ("+incdir+", "+libext+", "-y ", "-F ", "-f ")
        _SOURCE_PREFIX = "-v "
        paths = []
        with open(fl_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                if any(line.startswith(opt) for opt in _SKIP):
                    continue
                if line.startswith(_SOURCE_PREFIX):
                    line = line[len(_SOURCE_PREFIX) :]
                paths.append(os.path.normpath(os.path.join(fl_dir, line)))
        return paths

    def _resolve_lib_paths(self) -> list[str]:
        extras = list(self.synth_cfg.get_lib_paths())
        platform = self.synth_cfg.get_platform()
        if not platform or self.root_cfg is None:
            return extras
        return [self.root_cfg.get_synth_platform_cfg(platform).get_path()] + extras

    def _resolve_lef_paths(self) -> list[str]:
        platform = self.synth_cfg.get_platform()
        extras = list(self.synth_cfg.get_lef_paths())
        if not platform or self.root_cfg is None:
            return extras
        platform_lefs = self.root_cfg.get_synth_platform_cfg(platform).get_lef_paths()
        return list(platform_lefs) + extras

    # ------------------------------------------------------------------
    # Stage 1: Yosys — RTL to technology-mapped gate-level netlist
    # ------------------------------------------------------------------

    def _write_yosys_script(self, fl_path: str) -> str:
        top = self.synth_cfg.get_top()
        lib_paths = self._resolve_lib_paths()
        params = self.synth_cfg.get_params()
        defines = self.synth_cfg.get_defines()
        # The elaboration stage uses Yosys regardless of `tool:`, so its opts
        # (frontend, plugin_path, etc.) come from the yosys tool config plus
        # any `tool_overrides.yosys` block — not from this backend's openroad
        # tool config. Fall back to the openroad opts only when no yosys tool
        # config exists.
        opts = self.tool_cfg.get_opts(
            self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )
        if self.root_cfg is not None:
            try:
                yosys_tool_cfg = self.root_cfg.get_synth_tool_cfg("yosys")
                opts = yosys_tool_cfg.get_opts(
                    self.synth_cfg.get_tool_overrides_for("yosys")
                )
            except FatalRtlBuddyError:
                # No `yosys` entry under cfg-synth-tools — fall back to
                # the active tool_cfg's opts. Any other config error
                # (typo'd opts dict, malformed cfg-synth-tools entry,
                # etc.) is surfaced rather than silently degrading the
                # frontend selection to "verilog".
                pass

        lines = []
        for lib in lib_paths:
            lines.append(f"read_liberty -lib {lib}")

        source_files = self._source_files_from_filelist(fl_path)
        lines.extend(
            emit_frontend_read_cmds(
                opts=opts,
                source_files=source_files,
                top=top,
                defines=defines,
                params=params,
                root_cfg=self.root_cfg,
            )
        )

        if params and not slang_handles_params(opts):
            for key, value in params.items():
                lines.append(f"chparam -set {key} {value} {top}")

        synth_cmd = f"synth -top {top}"
        eff_synth = self.effort_cfg.get_yosys_synth_args()
        if eff_synth:
            synth_cmd += f" {eff_synth}"
        lines.append(synth_cmd)

        if lib_paths:
            for lib in lib_paths:
                lines.append(f"dfflibmap -liberty {lib}")
            abc_cmd = f'abc -liberty {lib_paths[0]} -script "+{_ABC_SCRIPT_AREA}"'
            lines.append(abc_cmd)
            lines.append(f"write_verilog {self._yosys_netlist_path()}")
            lines.append(f"stat -liberty {lib_paths[0]}")
        else:
            lines.append(
                f"write_rtlil {os.path.join(self.artefact_dir, 'synth.rtlil')}"
            )

        script = "\n".join(lines) + "\n"
        script_path = self._yosys_script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _parse_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"Chip area for module[^:]*:\s*([\d.]+)", log_text)
        return float(m.group(1)) if m else None

    def _parse_gate_count(self, log_text: str) -> int | None:
        matches = re.findall(
            r"^\s+(\d+)\s+(?:[\d.]+(?:[Ee][+-]?\d+)?\s+)?cells$", log_text, re.MULTILINE
        )
        return int(matches[-1]) if matches else None

    def _run_yosys_stage(self, fl_path: str) -> tuple[int | None, bool]:
        """Run Yosys stage. Returns (gate_count, success)."""
        lib_paths = self._resolve_lib_paths()
        if not lib_paths:
            log_event(
                logger,
                logging.ERROR,
                "synth.openroad.no_library",
                synth=self.synth_cfg.get_name(),
            )
            return None, False

        script_path = self._write_yosys_script(fl_path)
        log_path = self._yosys_log_path()

        cmd = [self.yosys_executable, "-s", script_path]
        log_event(
            logger,
            logging.DEBUG,
            "synth.run_cmd",
            synth=self.synth_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"synth {self.synth_cfg.get_name()} [yosys]"):
            with open(log_path, "w") as log_f:
                result = subprocess.run(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

        if result.returncode != 0:
            return None, False

        try:
            with open(log_path) as f:
                log_text = f.read()
        except OSError:
            return None, False

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("ERROR:")]
        if error_lines:
            return None, False

        return self._parse_gate_count(log_text), True

    # ------------------------------------------------------------------
    # Stage 2: OpenROAD — timing analysis with native multi-clock SDC
    # ------------------------------------------------------------------

    def _write_or_blackbox_stubs(self) -> list[str]:
        """Write OpenROAD-compatible copies of Yosys blackbox stub files.

        Yosys omits blackbox module definitions from write_verilog output.
        OpenROAD link_design fails if it encounters an instance whose module is
        undefined. We find source files containing (* blackbox *), generate a
        port-only stub (header + endmodule, no body), and write it into the
        artefact directory for use in the OR Tcl script. The body is stripped
        because OpenSTA's gate-level reader only accepts a tiny subset of
        Verilog — `reg` arrays, `always` blocks, `initial`, attributes other
        than `keep` etc. all break parsing, and the body has no semantic role
        for STA (cell timing comes from the Liberty). Returns the list of
        cleaned stub paths.
        """
        try:
            candidates = self._source_files_from_filelist(self._filelist_path())
        except OSError:
            return []
        # Match a module header (with its port list, possibly multi-line),
        # capture from the (* blackbox *) attribute through the closing );
        # of the port list, then everything up to endmodule is dropped.
        bb_re = re.compile(
            r"\(\*\s*blackbox\s*\*\)\s*"
            r"(module\s+\w+\s*(?:#\([^)]*\)\s*)?\([^;]*\);)"
            r".*?"
            r"endmodule",
            re.DOTALL,
        )
        result = []
        for src in candidates:
            try:
                with open(src) as f:
                    content = f.read()
                if "(* blackbox *)" not in content:
                    continue
                cleaned = bb_re.sub(r"\1\nendmodule", content)
                # OpenROAD's gate-level reader does not accept SV `logic`;
                # replace with `wire` for port declarations.
                cleaned = cleaned.replace("  input  logic ", "  input  wire  ")
                cleaned = cleaned.replace("  output logic ", "  output wire  ")
                stub_name = os.path.basename(src)
                stub_path = os.path.join(self.artefact_dir, f"or_{stub_name}")
                with open(stub_path, "w") as f:
                    f.write(cleaned)
                result.append(stub_path)
            except OSError:
                pass
        return result

    def _write_or_script(self, lef_paths: list[str], lib_paths: list[str]) -> str:
        top = self.synth_cfg.get_top()
        constraints = self.synth_cfg.get_constraints()
        opts = self.tool_cfg.get_opts(
            self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        lines = []
        for lef in lef_paths:
            lines.append(f"read_lef {lef}")
        for lib in lib_paths:
            lines.append(f"read_liberty {lib}")
        lines.append(f"read_verilog {self._yosys_netlist_path()}")
        # Read cleaned blackbox stubs so OpenROAD link_design can resolve them
        for bb_stub in self._write_or_blackbox_stubs():
            lines.append(f"read_verilog {bb_stub}")
        lines.append(f"link_design {top}")

        if constraints:
            lines.append(f"read_sdc {constraints}")

        # Effort-defined pre-STA Tcl snippet (e.g. floorplan + global_placement
        # + estimate_parasitics for more realistic pre-layout RC numbers).
        pre_sta_tcl = self.effort_cfg.get_openroad_pre_sta_tcl()
        if pre_sta_tcl:
            lines.append(pre_sta_tcl.rstrip())

        if opts.strategy.upper() in ("TIMING", "TIMING_ANNEAL"):
            lines.append("resynth_annealing")
        elif opts.strategy.upper() == "TIMING_GENETIC":
            lines.append("resynth_genetic")

        lines.append("report_design_area")
        if constraints:
            # report_checks emits per-group path reports for readability;
            # report_worst_slack -max emits the single authoritative WNS
            # across all path groups so the summary table reflects the
            # true worst, not just whichever group OpenROAD printed first.
            lines.append("report_checks -path_delay max -digits 3")
            lines.append("report_worst_slack -max -digits 3")
            lines.append("report_tns")

        script = "\n".join(lines) + "\n"
        script_path = self._or_script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _parse_or_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"^Design area\s+([\d.]+)\s+um\^2", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _parse_or_wns_ns(self, log_text: str) -> float | None:
        # Prefer the single authoritative line from `report_worst_slack -max`:
        #     "worst slack max -0.431"
        # That's the true WNS across every path group OpenROAD checked.
        m = re.search(r"^worst slack\s+max\s+([-\d.]+)", log_text, re.MULTILINE)
        if m:
            return float(m.group(1))
        # Fallback for legacy logs without report_worst_slack: scan every
        # path-report summary line and take the minimum.
        # `report_checks -path_delay max` emits one timing report per group,
        # each ending with "   6.754   slack (MET)" or "  -0.123   slack (VIOLATED)".
        # `re.search` would only grab the first; the summary needs the worst,
        # so collect them all and return the min.
        matches = re.findall(
            r"^\s+([-\d.]+)\s+slack\s+\((?:MET|VIOLATED)\)", log_text, re.MULTILINE
        )
        if not matches:
            return None
        return min(float(s) for s in matches)

    def _parse_or_tns_ns(self, log_text: str) -> float | None:
        m = re.search(r"^tns\s+(?:max|min)?\s*([-\d.]+)", log_text, re.MULTILINE)
        return float(m.group(1)) if m else None

    def _run_or_stage(
        self, gate_count: int | None, lef_paths: list[str], lib_paths: list[str]
    ) -> SynthResults:
        script_path = self._write_or_script(lef_paths, lib_paths)
        log_path = self._or_log_path()

        cmd = [self.tool_cfg.get_executable(), "-exit", script_path]
        log_event(
            logger,
            logging.DEBUG,
            "synth.run_cmd",
            synth=self.synth_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"synth {self.synth_cfg.get_name()} [openroad]"):
            with open(log_path, "w") as log_f:
                result = subprocess.run(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "synth.failed",
                synth=self.synth_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=f"OpenROAD exited with code {result.returncode}",
            )

        try:
            with open(log_path) as f:
                log_text = f.read()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            log_event(
                logger,
                logging.WARNING,
                "synth.errors_in_log",
                synth=self.synth_cfg.get_name(),
                count=len(error_lines),
                log=log_path,
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in OpenROAD log",
            )

        area_um2 = self._parse_or_area_um2(log_text)
        wns_ns = self._parse_or_wns_ns(log_text)
        tns_ns = self._parse_or_tns_ns(log_text)

        wns_ps = wns_ns * 1000.0 if wns_ns is not None else None
        tns_ps = tns_ns * 1000.0 if tns_ns is not None else None

        log_event(
            logger,
            logging.INFO,
            "synth.passed",
            synth=self.synth_cfg.get_name(),
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            tns_ps=tns_ps,
            log=log_path,
        )
        return SynthPassResults(
            name=self.name + "/results",
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            tns_ps=tns_ps,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> SynthResults:
        log_event(
            logger,
            logging.INFO,
            "synth.start",
            synth=self.synth_cfg.get_name(),
            tool=self.tool_cfg.get_executable(),
            top=self.synth_cfg.get_top(),
        )

        lib_paths = self._resolve_lib_paths()
        lef_paths = self._resolve_lef_paths()

        if not lib_paths:
            return SynthFailResults(
                name=self.name + "/results",
                desc=(
                    "OpenROAD backend requires Liberty — set a platform "
                    "(cfg-synth-platforms -> cfg-pdks corner) or add lib-paths "
                    "to the synth.yaml entry"
                ),
            )

        if not lef_paths:
            log_event(
                logger,
                logging.WARNING,
                "synth.openroad.no_lef",
                synth=self.synth_cfg.get_name(),
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=(
                    "OpenROAD backend requires LEF — the platform PDK must "
                    "provide tech-lef/macro-lef, or add lef-paths to the "
                    "synth.yaml entry"
                ),
            )

        fl_path = self._filelist_path()
        try:
            vlog_fl = VlogFilelist(
                name=self.name + "/filelist",
                model_cfg=self.synth_cfg.get_model(),
                output_path=fl_path,
            )
            vlog_fl.write_output(
                output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
            )
        except FilelistError as e:
            log_event(
                logger,
                logging.ERROR,
                "synth.filelist_failed",
                synth=self.synth_cfg.get_name(),
                error=str(e),
            )
            return SynthFailResults(
                name=self.name + "/results", desc=f"Filelist error: {e}"
            )

        gate_count, yosys_ok = self._run_yosys_stage(fl_path)
        if not yosys_ok:
            log_event(
                logger,
                logging.WARNING,
                "synth.failed",
                synth=self.synth_cfg.get_name(),
                returncode=-1,
                log=self._yosys_log_path(),
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc="Yosys stage failed; see synth_yosys.log",
            )

        return self._run_or_stage(gate_count, lef_paths, lib_paths)
