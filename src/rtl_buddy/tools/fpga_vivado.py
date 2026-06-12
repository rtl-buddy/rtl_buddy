import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.fpga import FpgaConfig
from ..errors import FilelistError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.fpga_results import (
    FpgaFailResults,
    FpgaPassResults,
    FpgaResults,
    FpgaSkipResults,
)
from .fpga_base import BaseFpga, resolve_target
from .fpga_vivado_flow import REPORT_FILES, render_flow_tcl
from .fpga_vivado_reports import (
    parse_drc,
    parse_methodology,
    parse_power,
    parse_timing_summary,
    parse_utilization,
)

# Vivado error lines look like `ERROR: [Synth 8-439] module ...` — match
# on the bracketed message-id form so the word ERROR inside user puts
# output doesn't false-positive.
_VIVADO_ERROR_RE = re.compile(r"^ERROR: \[")


class VivadoFpga(BaseFpga):
    """Vivado-driven FPGA implementation backend.

    Renders the non-project batch-Tcl flow from
    :mod:`.fpga_vivado_flow` (read sources/XDC -> ``synth_design`` ->
    ``opt_design`` -> ``place_design`` -> ``route_design`` -> reports
    -> optional ``write_bitstream``), runs::

        vivado -mode batch -source flow.tcl -nojournal -log vivado.log

    with ``cwd=artefacts/<run>/``, and parses the post-route reports
    (:mod:`.fpga_vivado_reports`) into the results dataclasses.
    """

    def __init__(
        self,
        name: str,
        fpga_cfg: FpgaConfig,
        suite_dir: str,
        root_cfg,
        executable: str = "vivado",
        emit_bitstream: bool = False,
    ):
        super().__init__(
            name=name,
            fpga_cfg=fpga_cfg,
            suite_dir=suite_dir,
            root_cfg=root_cfg,
            executable=executable,
            emit_bitstream=emit_bitstream,
        )

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "flow.tcl")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "vivado.log")

    def _bitstream_path(self) -> str:
        return os.path.join(self.artefact_dir, f"{self.fpga_cfg.get_top()}.bit")

    # ------------------------------------------------------------------
    # Tcl script generation
    # ------------------------------------------------------------------

    def _write_script(self, fl_path: str) -> str:
        top = self.fpga_cfg.get_top()
        # Platform vs inline part is resolved behind this seam; the
        # effective XDC list is platform defaults first, run files after
        # (later read_xdc wins in Vivado).
        target = resolve_target(self.fpga_cfg, self.root_cfg)
        script = render_flow_tcl(
            top=top,
            part=target.part,
            verilog_sources=self._source_files_from_filelist(fl_path),
            xdc_files=list(target.xdc_files),
            bitstream=f"{top}.bit",
            emit_bitstream=self.emit_bitstream,
        )
        script_path = self._script_path()
        Path(script_path).write_text(script)
        return script_path

    # ------------------------------------------------------------------
    # Report parsing
    # ------------------------------------------------------------------

    def _parse_reports(self) -> dict:
        """Read + parse the post-route reports from the run dir.

        Raises:
          RuntimeError: when a report is missing or unparsable — the
            caller maps this to a FAIL with the message as desc.
        """
        parsers = {
            "utilization": parse_utilization,
            "timing_summary": parse_timing_summary,
            "power": parse_power,
            "drc": parse_drc,
            "methodology": parse_methodology,
        }
        parsed: dict = {}
        for key, filename in REPORT_FILES.items():
            path = os.path.join(self.artefact_dir, filename)
            if not os.path.isfile(path):
                raise RuntimeError(f"report '{filename}' not produced at {path}")
            try:
                parsed[key] = parsers[key](Path(path).read_text())
            except (OSError, ValueError) as e:
                raise RuntimeError(f"failed to parse report '{filename}': {e}") from e
        return parsed

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> FpgaResults:
        # Resolve up front: an unknown `platform:` ref is a config error
        # (FatalRtlBuddyError, exit 2), even when vivado is absent.
        target = resolve_target(self.fpga_cfg, self.root_cfg)
        log_event(
            logger,
            logging.INFO,
            "fpga.start",
            fpga=self.fpga_cfg.get_name(),
            tool=self.executable,
            top=self.fpga_cfg.get_top(),
            part=target.part,
            bitstream=self.emit_bitstream,
        )

        if not shutil.which(self.executable):
            log_event(
                logger,
                logging.WARNING,
                "fpga.no_vivado",
                fpga=self.fpga_cfg.get_name(),
                exe=self.executable,
            )
            return FpgaSkipResults(
                name=self.name + "/results",
                desc=(
                    f"{self.executable!r} not found — run "
                    "`rb tool-check --explain vivado` for install instructions"
                ),
            )

        try:
            fl_path = self._write_filelist()
        except FilelistError as e:
            log_event(
                logger,
                logging.ERROR,
                "fpga.filelist_failed",
                fpga=self.fpga_cfg.get_name(),
                error=str(e),
            )
            return FpgaFailResults(
                name=self.name + "/results", desc=f"Filelist error: {e}"
            )

        try:
            script_path = self._write_script(fl_path)
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpga.script_failed",
                fpga=self.fpga_cfg.get_name(),
                error=str(e),
            )
            return FpgaFailResults(
                name=self.name + "/results", desc=f"script generation error: {e}"
            )

        # Relative paths: the process runs with cwd=artefacts/<run>/ so
        # Vivado's own side-files (.Xil/, webtalk) stay inside the run dir.
        cmd = [
            self.executable,
            "-mode",
            "batch",
            "-source",
            os.path.basename(script_path),
            "-nojournal",
            "-log",
            os.path.basename(self._log_path()),
        ]
        log_event(
            logger,
            logging.DEBUG,
            "fpga.run_cmd",
            fpga=self.fpga_cfg.get_name(),
            cmd=" ".join(cmd),
            cwd=self.artefact_dir,
        )

        with task_status(f"fpga {self.fpga_cfg.get_name()} [vivado]"):
            result = run_managed_process(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                cwd=self.artefact_dir,
            )

        log_path = self._log_path()
        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "fpga.failed",
                fpga=self.fpga_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return FpgaFailResults(
                name=self.name + "/results",
                desc=f"Vivado exited with code {result.returncode}",
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if _VIVADO_ERROR_RE.match(ln)]
        if error_lines:
            log_event(
                logger,
                logging.WARNING,
                "fpga.errors_in_log",
                fpga=self.fpga_cfg.get_name(),
                count=len(error_lines),
                first=error_lines[0],
                log=log_path,
            )
            return FpgaFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in Vivado log",
            )

        try:
            reports = self._parse_reports()
        except RuntimeError as e:
            return FpgaFailResults(name=self.name + "/results", desc=str(e))

        bitstream: str | None = None
        if self.emit_bitstream:
            bit_path = self._bitstream_path()
            if not os.path.isfile(bit_path):
                return FpgaFailResults(
                    name=self.name + "/results",
                    desc=f"bitstream not produced at {bit_path}",
                )
            bitstream = bit_path

        util = reports["utilization"]
        timing = reports["timing_summary"]
        power = reports["power"]
        drc = reports["drc"]
        methodology = reports["methodology"]

        log_event(
            logger,
            logging.INFO,
            "fpga.passed",
            fpga=self.fpga_cfg.get_name(),
            wns_ns=timing.get("wns_ns"),
            tns_ns=timing.get("tns_ns"),
            whs_ns=timing.get("whs_ns"),
            timing_met=timing.get("timing_met"),
            total_power_w=power.get("total_on_chip_w"),
            drc_violations=drc.get("total_violations"),
            methodology_warnings=methodology.get("total_warnings"),
            bitstream=bitstream,
            log=log_path,
        )
        return FpgaPassResults(
            name=self.name + "/results",
            lut=util.get("lut"),
            ff=util.get("ff"),
            bram=util.get("bram"),
            dsp=util.get("dsp"),
            wns_ns=timing.get("wns_ns"),
            tns_ns=timing.get("tns_ns"),
            whs_ns=timing.get("whs_ns"),
            timing_met=timing.get("timing_met"),
            failing_endpoints=timing.get("failing_endpoints"),
            # Omitted entirely when there are none — agents key off
            # timing_met first, then dig into the paths.
            failing_paths=timing.get("failing_paths") or None,
            total_power_w=power.get("total_on_chip_w"),
            dynamic_power_w=power.get("dynamic_w"),
            static_power_w=power.get("static_w"),
            drc_violations=drc.get("total_violations"),
            drc_by_severity=drc.get("by_severity"),
            methodology_warnings=methodology.get("warnings"),
            bitstream=bitstream,
        )
