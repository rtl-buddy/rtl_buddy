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
_KLAYOUT_PACKAGE = "rtl_buddy.pnr.klayout"

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
        png_width: int = 2048,
        png_height: int = 2048,
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

        # Design-specific macro libraries and LEFs (e.g. SRAM macros).
        extra_lines = []
        for lib in self.pnr_cfg.get_lib_paths():
            extra_lines.append(f"read_liberty {lib}")
        for lef in self.pnr_cfg.get_lef_paths():
            extra_lines.append(f"read_lef     {lef}")
        extra_libs_lefs = "\n".join(extra_lines)

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

    def _run_def2stream(self, platform, design: str) -> str | None:
        pdk = platform.get_pdk()
        tech = pdk.get_klayout_tech()
        if not tech:
            log_event(
                logger,
                logging.WARNING,
                "pnr.gds_no_klayout_tech",
                pnr=self.pnr_cfg.get_name(),
                pdk=pdk.get_name(),
            )
            return None
        klayout = _resolve_klayout_exe()
        if not klayout:
            log_event(
                logger,
                logging.WARNING,
                "pnr.no_klayout",
                pnr=self.pnr_cfg.get_name(),
            )
            return None
        in_def = os.path.join(self.artefact_dir, f"{design}.def")
        out_gds = os.path.join(self.artefact_dir, f"{design}.gds")
        cell_gds = pdk.get_cell_gds()
        script = self._klayout_script_path("def2stream.py")
        cmd = [
            klayout,
            "-zz",
            "-nc",
            "-rd",
            f"tech_file={tech}",
            "-rd",
            "layer_map=",
            "-rd",
            f"in_def={in_def}",
            "-rd",
            f"design_name={design}",
            "-rd",
            f"in_files={cell_gds}",
            "-rd",
            "seal_file=",
            "-rd",
            f"out_file={out_gds}",
            "-r",
            script,
        ]
        log_path = os.path.join(self.artefact_dir, "klayout.def2stream.log")
        with task_status(f"pnr {self.pnr_cfg.get_name()} [klayout gds]"):
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        Path(log_path).write_text((r.stdout or "") + (r.stderr or ""))
        # def2stream is treated as failed only when no GDS file was
        # produced. Some platforms ship LEF-only macros (e.g. ORFS
        # fakeram45) for early-flow verification; KLayout emits an
        # [ERROR] for each such cell and exits non-zero, but the GDS
        # is still streamed (with the macro as an empty placeholder)
        # and downstream gds2png works fine. Treating non-zero exit
        # as fatal here unnecessarily skipped the PNG render step.
        if not os.path.isfile(out_gds) or os.path.getsize(out_gds) == 0:
            log_event(
                logger,
                logging.WARNING,
                "pnr.gds_failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                log=log_path,
            )
            return None
        if r.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "pnr.gds_warnings",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                log=log_path,
            )
        return out_gds

    def _run_gds2png(self, platform, gds_path: str, design: str) -> str | None:
        klayout = _resolve_klayout_exe()
        if not klayout:
            return None
        lyp = platform.get_pdk().get_klayout_props()
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
        with task_status(f"pnr {self.pnr_cfg.get_name()} [klayout png]"):
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        Path(log_path).write_text((r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0 or not os.path.isfile(out_png):
            log_event(
                logger,
                logging.WARNING,
                "pnr.png_failed",
                pnr=self.pnr_cfg.get_name(),
                returncode=r.returncode,
                log=log_path,
            )
            return None
        return out_png

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
                desc=f"{self.openroad_executable!r} not found",
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

        gds_path: str | None = None
        png_path: str | None = None
        if self.emit_gds:
            design = self.pnr_cfg.resolve_synth_cfg().get_top()
            gds_path = self._run_def2stream(platform, design)
            if gds_path and self.emit_png:
                png_path = self._run_gds2png(platform, gds_path, design)

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
            gds_path=gds_path,
            png_path=png_path,
        )
