import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.power import PowerConfig
from ..logging_utils import log_event, task_status
from ..phys.publish import publish_power
from ..runner.power_results import PowerFailResults, PowerPassResults, PowerResults
from .artifact_paths import clear_stale_artefacts
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

    def _instances_report_path(self) -> str:
        """`report_power -instances`' output: one line per leaf cell (#558)."""
        return os.path.join(self.artefact_dir, "power_instances.rpt")

    def _instances_cells_path(self) -> str:
        """The `<instance path> <liberty cell>` sidecar (#558).

        `report_power` prints the path and the four powers, never the master
        the instance is an instance *of* — so the hierarchy walk that feeds
        it writes the mapping out alongside. Without this the model's power
        rows have no module column and cannot be joined to the synth half.
        """
        return os.path.join(self.artefact_dir, "power_instances.cells")

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

    def _emit_per_instance_cmds(self) -> list[str]:
        """Tcl that attributes the run's power to individual leaf instances.

        This is rtl-buddy/rtl_buddy#114 delivered where the OpenROAD session
        already lives, rather than as the stand-alone `emit_phys.tcl` that
        issue predates `rb power` by (#558).

        Three properties the shape is chosen for:

        **One analysis, not one per instance.** `report_power` takes a *list*
        of instances and prints one line each, so the whole design costs a
        single extra call on top of the design-total report above it. The
        obvious `foreach ... {report_power -instances $inst}` spelling reruns
        the propagation per cell and turns a minute into an afternoon on
        anything real.

        **Leaf cells only.** `get_cells -hierarchical *` returns the leaves —
        the instances that have a Liberty cell and therefore a power number.
        Roll-up to the enclosing modules is the model consumer's job.

        **It cannot fail the run.** Everything here is inside a `catch`: the
        design totals have already been written by the time this executes, so
        a `get_cells` that finds nothing, or an OpenSTA without the
        `-instances` form, must cost the run its per-instance detail and
        nothing else. A Tcl error escaping to the top level would abort the
        script and take the exit code with it.
        """
        return [
            "catch {",
            "  set rb_insts [get_cells -hierarchical *]",
            "  if {[llength $rb_insts] > 0} {",
            f"    set rb_fh [open {self._instances_cells_path()} w]",
            "    foreach rb_inst $rb_insts {",
            '      puts $rb_fh "[get_full_name $rb_inst] '
            '[get_property $rb_inst ref_name]"',
            "    }",
            "    close $rb_fh",
            f"    report_power -instances $rb_insts > {self._instances_report_path()}",
            "  }",
            "}",
        ]

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
        lines.extend(self._emit_per_instance_cmds())
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

    def _clear_stale_report(self) -> None:
        """Remove the previous run's `power.rpt` and its per-instance half.

        The per-instance report and its cell sidecar are read back inside
        this same `run()` to build the phys model, so they take the same
        treatment as the report they accompany: an OpenROAD that exits 0
        without reaching the `catch` block must not have the last run's
        per-instance watts published as this one's (#469, #558).
        """
        stale = clear_stale_artefacts(
            [
                self._report_path(),
                self._instances_report_path(),
                self._instances_cells_path(),
            ],
            owner=self.power_cfg.get_name(),
        )
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "power.stale_artefacts_removed",
                power=self.power_cfg.get_name(),
                paths=stale,
            )

    def _fail_after_openroad(self, desc: str) -> PowerFailResults:
        """Fail a run that has already invoked OpenROAD, publishing no report.

        `report_power`'s output file is written before the script ends, so
        OpenROAD can exit non-zero — or log an `[ERROR ...]`, or leave a
        report this wrapper cannot read or parse — with `power.rpt` on disk
        at the fixed path the next run would otherwise quote (#469). Every
        post-OpenROAD failure return goes through here.
        """
        self._clear_stale_report()
        return PowerFailResults(name=self.name + "/results", desc=desc)

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

        # Ahead of the "openroad not found" return below: `_write_script`
        # is where this flow validates its configuration — the SDC, the
        # platform's tech-lef, the upstream netlist or routed ODB. An
        # analysis pointing at a missing input is broken on every machine,
        # and reporting it as "openroad not found" on a box that merely
        # lacks the tool sends the user after the wrong problem. A config
        # error is a failed run, so it clears on the way out (#469).
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
            self._clear_stale_report()
            return PowerFailResults(
                name=self.name + "/results", desc=f"script generation error: {e}"
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

        # Everything past the "openroad not found" return above is a run of
        # this entry, however it ends — including the script-generation
        # failure just below. `report_power`'s output file is read back off a
        # fixed path and OpenROAD exiting 0 with no [ERROR] does not prove it
        # rewrote it, so clear here and the "power report not produced" path
        # stays reachable instead of quoting a previous run's watts (#469).
        self._clear_stale_report()

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
            return self._fail_after_openroad(
                f"OpenROAD exited with code {result.returncode}"
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("[ERROR ")]
        if error_lines:
            return self._fail_after_openroad(
                f"{len(error_lines)} ERROR(s) in OpenROAD log"
            )

        report_path = self._report_path()
        if not os.path.isfile(report_path):
            return self._fail_after_openroad(
                f"power report not produced at {report_path}"
            )

        try:
            report_text = Path(report_path).read_text()
        except OSError as e:
            return self._fail_after_openroad(f"failed to read power report: {e}")

        parsed = self._parse_report(report_text)
        if parsed is None:
            return self._fail_after_openroad(
                "could not parse Total line from report_power output"
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
        phys_model = self._publish_phys_model(parsed)
        return PowerPassResults(
            name=self.name + "/results",
            mode=self.power_cfg.get_mode(),
            netlist_source=self.power_cfg.get_netlist_source(),
            total_w=parsed["total_w"],
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            activity_source=activity_source,
            phys_model=phys_model,
        )

    def _publish_phys_model(self, parsed: dict) -> str | None:
        """Write `phys-model.json` + its manifest for a run that passed (#558).

        Never fails the power analysis. The design totals are already parsed
        and already reported by the time this runs; the per-instance rows are
        the by-product, and an OpenSTA that skipped or garbled them costs the
        model its `instances` half and earns a warning.

        The top comes from `_resolve_inputs` rather than the run name because
        the model is keyed on the *design*: it is what decides whether a
        synthesis' module rows already in this directory describe the same
        thing and may be merged forward.
        """
        try:
            top = self._resolve_inputs()["top"]
        except Exception:  # noqa: BLE001 - resolution already succeeded once
            top = None
        published = publish_power(
            artefact_dir=self.artefact_dir,
            top=top,
            backend="openroad",
            run=self.power_cfg.get_name(),
            netlist_source=self.power_cfg.get_netlist_source(),
            report_path=self._report_path(),
            instances_path=self._instances_report_path(),
            cells_path=self._instances_cells_path(),
            log_path=self._log_path(),
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            total_w=parsed["total_w"],
        )
        if published["error"] is not None or published["rows"] is None:
            log_event(
                logger,
                logging.WARNING,
                "power.phys_model_incomplete",
                power=self.power_cfg.get_name(),
                instances=self._instances_report_path(),
                error=published["error"],
            )
        return published["model"]
