import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.power import PowerConfig
from ..logging_utils import log_event, task_status
from ..runner.power_results import PowerFailResults, PowerPassResults, PowerResults
from .power_base import BasePower


class OpenRoadPower(BasePower):
    """OpenROAD-driven power-analysis backend.

    Reads the upstream `rb synth` artefact (tech-mapped netlist) together
    with the platform Liberty + tech/macro LEFs + SDC, applies a
    switching-activity model (synthetic global activity, SAIF file, or
    VCD file), and parses OpenROAD's `report_power` output for
    total/internal/switching/leakage.

    LEF is required even though `report_power` itself only needs Liberty
    — OpenROAD's gate-level `read_verilog` builds an in-memory database
    that requires a technology view (`[ERROR ORD-2010] no technology has
    been read.` otherwise).
    """

    def __init__(
        self,
        name: str,
        power_cfg: PowerConfig,
        suite_dir: str,
        root_cfg,
        executable: str = "openroad",
    ):
        super().__init__(
            name=name,
            power_cfg=power_cfg,
            suite_dir=suite_dir,
            root_cfg=root_cfg,
            executable=executable,
        )
        artefact_root = Path(suite_dir) / "artefacts" / power_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.tcl")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.log")

    def _report_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.rpt")

    # ------------------------------------------------------------------
    # Inputs resolution
    # ------------------------------------------------------------------

    def _resolve_inputs(self) -> dict:
        """Resolve netlist / ODB / SDC paths per `netlist-source`.

        Dispatches on `power_cfg.get_netlist_source()`:

        - "synth" (default): post-synth tech-mapped netlist (Liberty +
          LEF supply the technology view; switching power is
          under-estimated because there are no real parasitics and no
          CTS-buffered clock tree).
        - "pnr": post-PnR OpenROAD binary DB (`<top>.routed.odb`) +
          post-CTS SDC. The .odb encapsulates placement + routing so
          rerunning `estimate_parasitics -global_routing` reflects the
          CTS-buffered clock tree and routed wire capacitance.
          Stand-alone SPEF is not used — OpenROAD's RCX extractor is
          not wired into the PnR flow, so `write_spef` would produce
          an empty file.

        Returns a dict with keys: netlist (None for pnr), odb (None for
        synth), sdc, top.
        """
        if self.power_cfg.get_netlist_source() == "pnr":
            pnr_cfg = self.power_cfg.resolve_pnr_cfg()
            top = pnr_cfg.resolve_synth_cfg().get_top()
            pnr_suite_path = self.power_cfg.get_pnr_suite_path()
            assert pnr_suite_path is not None
            pnr_suite_dir = os.path.dirname(pnr_suite_path)
            pnr_artefact = os.path.join(pnr_suite_dir, "artefacts", pnr_cfg.get_name())
            odb = os.path.join(pnr_artefact, f"{top}.routed.odb")
            sdc = self.power_cfg.get_constraints() or os.path.join(
                pnr_artefact, f"{top}.routed.sdc"
            )
            return {"netlist": None, "odb": odb, "sdc": sdc, "top": top}

        synth_cfg = self.power_cfg.resolve_synth_cfg()
        top = synth_cfg.get_top()
        synth_suite_path = self.power_cfg.get_synth_suite_path()
        assert synth_suite_path is not None
        synth_suite_dir = os.path.dirname(synth_suite_path)
        netlist = os.path.join(
            synth_suite_dir, "artefacts", synth_cfg.get_name(), "synth_netlist.v"
        )
        return {
            "netlist": netlist,
            "odb": None,
            "sdc": self.power_cfg.get_constraints(),
            "top": top,
        }

    def _resolve_platform(self):
        """Resolve to a PnrPlatformConfig (provides Liberty path)."""
        return self.root_cfg.get_pnr_platform_cfg(self.power_cfg.get_platform())

    # ------------------------------------------------------------------
    # Tcl script generation
    # ------------------------------------------------------------------

    def _emit_activity_cmds(self) -> list[str]:
        """Translate the resolved activity source into OpenROAD Tcl.

        The *decision* of which source to use lives on PowerConfig
        (`get_activity_source()`); this backend just emits the
        corresponding `read_saif` / `read_power_activities` /
        `set_power_activity` command.
        """
        source = self.power_cfg.get_activity_source()
        activity = self.power_cfg.get_activity()
        if source == "saif":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_saif{scope_arg} {activity.saif}"]
        if source == "vcd":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_power_activities{scope_arg} -vcd {activity.vcd}"]
        if source == "synthetic":
            return [
                f"set_power_activity -global "
                f"-activity {activity.default_toggle_rate} "
                f"-duty {activity.default_static_prob}"
            ]
        return []  # "default" → static, no activity commands

    def _write_script(self) -> str:
        platform = self._resolve_platform()
        pdk = platform.get_pdk()
        liberty = platform.get_sta_lib_path()
        tech_lef = pdk.get_tech_lef()
        macro_lef = pdk.get_macro_lef()
        inputs = self._resolve_inputs()
        netlist = inputs["netlist"]
        sdc = inputs["sdc"]
        odb = inputs["odb"]
        top = inputs["top"]
        source = self.power_cfg.get_netlist_source()

        if not sdc:
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': "
                "constraints (SDC path) is required"
            )
        if not tech_lef:
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': "
                f"pdk '{pdk.get_name()}' has no tech-lef configured"
            )
        if source == "pnr":
            if not os.path.isfile(odb):
                raise RuntimeError(
                    f"power run '{self.power_cfg.get_name()}': "
                    f"routed ODB not found at {odb} — re-run `rb pnr` "
                    "(older runs predate the .routed.odb artefact)"
                )
        else:
            if not os.path.isfile(netlist):
                raise RuntimeError(
                    f"power run '{self.power_cfg.get_name()}': "
                    f"upstream netlist not found at {netlist} — run `rb synth` first"
                )

        lines = [
            "# Generated by rtl_buddy power flow",
            f"read_liberty {liberty}",
            f"read_lef {tech_lef}",
        ]
        if macro_lef:
            lines.append(f"read_lef {macro_lef}")
        if source == "pnr":
            # ODB encapsulates placement + routing. Reading it
            # repopulates OpenROAD's DB at the post-route state;
            # estimate_parasitics then derives wire-cap from the global
            # routes so the CTS-buffered clock tree contributes
            # realistically to switching power.
            lines.append(f"read_db {odb}")
            lines.append(f"read_sdc {sdc}")
            lines.append("estimate_parasitics -global_routing")
        else:
            lines.extend(
                [
                    f"read_verilog {netlist}",
                    f"link_design {top}",
                    f"read_sdc {sdc}",
                ]
            )
        lines.extend(self._emit_activity_cmds())
        lines.append(f"report_power > {self._report_path()}")
        lines.append("exit")
        lines.append("")

        script_path = self._script_path()
        Path(script_path).write_text("\n".join(lines))
        return script_path

    # ------------------------------------------------------------------
    # Report parsing
    # ------------------------------------------------------------------

    # report_power output for the Total line looks like:
    #   Total                 1.50e-04   2.30e-05   8.00e-06   1.81e-04
    _TOTAL_LINE_RE = re.compile(
        r"^\s*Total\s+"
        r"([-\d.eE+]+)\s+"  # internal
        r"([-\d.eE+]+)\s+"  # switching
        r"([-\d.eE+]+)\s+"  # leakage
        r"([-\d.eE+]+)",  # total
        re.MULTILINE,
    )

    def _parse_report(self, report_text: str) -> dict | None:
        m = self._TOTAL_LINE_RE.search(report_text)
        if not m:
            return None
        try:
            return {
                "internal_w": float(m.group(1)),
                "switching_w": float(m.group(2)),
                "leakage_w": float(m.group(3)),
                "total_w": float(m.group(4)),
            }
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> PowerResults:
        log_event(
            logger,
            logging.INFO,
            "power.start",
            power=self.power_cfg.get_name(),
            tool=self.executable,
            mode=self.power_cfg.get_mode(),
            netlist_source=self.power_cfg.get_netlist_source(),
        )

        if not shutil.which(self.executable):
            log_event(
                logger,
                logging.WARNING,
                "power.no_openroad",
                power=self.power_cfg.get_name(),
                exe=self.executable,
            )
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"{self.executable!r} not found",
            )

        try:
            script_path = self._write_script()
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "power.script_failed",
                power=self.power_cfg.get_name(),
                error=str(e),
            )
            return PowerFailResults(
                name=self.name + "/results", desc=f"script generation error: {e}"
            )

        log_path = self._log_path()
        env = os.environ.copy()
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

        cmd = [
            self.executable,
            "-no_init",
            "-exit",
            "-log",
            log_path,
            script_path,
        ]
        log_event(
            logger,
            logging.DEBUG,
            "power.run_cmd",
            power=self.power_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"power {self.power_cfg.get_name()} [openroad]"):
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
                "power.failed",
                power=self.power_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"OpenROAD exited with code {result.returncode}",
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in OpenROAD log",
            )

        report_path = self._report_path()
        if not os.path.isfile(report_path):
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"power report not produced at {report_path}",
            )

        try:
            report_text = Path(report_path).read_text()
        except OSError as e:
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"failed to read power report: {e}",
            )

        parsed = self._parse_report(report_text)
        if parsed is None:
            return PowerFailResults(
                name=self.name + "/results",
                desc="could not parse Total line from report_power output",
            )

        activity_source = self.power_cfg.get_activity_source()
        log_event(
            logger,
            logging.INFO,
            "power.passed",
            power=self.power_cfg.get_name(),
            mode=self.power_cfg.get_mode(),
            activity_source=activity_source,
            total_w=parsed["total_w"],
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            log=log_path,
            report=report_path,
        )
        return PowerPassResults(
            name=self.name + "/results",
            mode=self.power_cfg.get_mode(),
            total_w=parsed["total_w"],
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            activity_source=activity_source,
        )
