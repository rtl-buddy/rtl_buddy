import logging
import os
import re
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.pnr import PnrConfig
from ..logging_utils import log_event, task_status
from ..runner.pnr_results import PnrFailResults, PnrPassResults, PnrResults


_TEMPLATE_PACKAGE = "rtl_buddy.pnr"
_TEMPLATE_FILE = "flow.tcl.template"


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
    ):
        self.name = name
        self.pnr_cfg = pnr_cfg
        self.root_cfg = root_cfg
        self.openroad_executable = openroad_executable

        artefact_root = Path(suite_dir) / "artefacts" / pnr_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "pnr.tcl")

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

    def _load_template(self) -> str:
        return files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_FILE).read_text()

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

        substitutions = {
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
            "cts_buf": platform.get_cts_buffer(),
            "signal_layers": platform.get_signal_layers(),
            "clock_layers": platform.get_clock_layers(),
            "fill_cells": fill_cells,
            "out_dir": self.artefact_dir,
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

    def _count_drcs(self) -> int:
        drc_path = os.path.join(self.artefact_dir, "route.drc.rpt")
        if not os.path.isfile(drc_path):
            return 0
        try:
            with open(drc_path) as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> PnrResults:
        log_event(
            logger,
            logging.INFO,
            "pnr.start",
            pnr=self.pnr_cfg.get_name(),
            tool=self.openroad_executable,
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
                desc=f"{self.openroad_executable!r} not found on PATH",
            )

        try:
            platform = self.root_cfg.get_pnr_platform_cfg(self.pnr_cfg.get_platform())
        except Exception as e:
            return PnrFailResults(
                name=self.name + "/results", desc=f"platform lookup failed: {e}"
            )

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
                name=self.name + "/results", desc=f"template error: {e}"
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
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
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
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"OpenROAD exited with code {result.returncode}",
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            return PnrFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in OpenROAD log",
            )

        area = self._parse_area_um2(log_text)
        cells = self._parse_cell_count(log_text)
        wns_setup = self._parse_wns(log_text, "max")
        wns_hold = self._parse_wns(log_text, "min")
        tns = self._parse_tns(log_text)
        drcs = self._count_drcs()

        log_event(
            logger,
            logging.INFO,
            "pnr.passed",
            pnr=self.pnr_cfg.get_name(),
            area_um2=area,
            cell_count=cells,
            wns_setup_ps=wns_setup * 1000.0 if wns_setup is not None else None,
            wns_hold_ps=wns_hold * 1000.0 if wns_hold is not None else None,
            tns_ps=tns * 1000.0 if tns is not None else None,
            drc_count=drcs,
            log=log_path,
        )
        return PnrPassResults(
            name=self.name + "/results",
            area_um2=area,
            cell_count=cells,
            wns_setup_ps=wns_setup * 1000.0 if wns_setup is not None else None,
            wns_hold_ps=wns_hold * 1000.0 if wns_hold is not None else None,
            tns_ps=tns * 1000.0 if tns is not None else None,
            drc_count=drcs,
        )
