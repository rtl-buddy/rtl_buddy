import contextlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.power import PowerConfig
from ..logging_utils import log_event, task_status
from ..phys.provenance import activity_block
from ..phys.publish import invalidate_half, publish_power, sha256_of
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
        # The upstream netlist this run measures, and the hash of the
        # private copy OpenROAD is actually given; see
        # `_snapshot_netlist`. Both `None` until the run resolves them,
        # and for a `netlist-source: pnr` run that reads a routed
        # database and never a netlist at all.
        self._netlist_source_path: str | None = None
        self._netlist_sha256: str | None = None

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

    def _netlist_snapshot_path(self) -> str:
        """This run's own copy of the netlist it hands OpenROAD (#560).

        The analysis reads a netlist another command wrote, in another
        artefact directory, and records its sha256 as the evidence that
        these watts and the module rows beside them describe one design.
        Hashing the upstream path leaves a window however tightly it is
        drawn: `rb synth` rewriting that file between the hash and
        OpenROAD's `read_verilog` would have the model name bytes the
        analysis never measured, and the provenance gate would then read
        a real mismatch as a match.

        So the netlist is *snapshotted* instead: copied here, hashed
        here, and read from here. The hash and the bytes OpenROAD parses
        are then the same file, which no concurrent writer can reach —
        the upstream directory is not this run's, and this one is.

        The cost is one copy of a netlist that may be megabytes, per
        power run, in the directory that already holds the run's log and
        reports; the stale-clear removes it exactly as it removes them.
        """
        return os.path.join(self.artefact_dir, "power_netlist.v")

    def _snapshot_netlist(self) -> str | None:
        """Copy the upstream netlist in, hash the copy, or say why not.

        Called between the stale-clear (which removes the previous run's
        copy) and OpenROAD, so the file the script names is written once
        and read once, by this run. Copy-then-rename via a `.tmp`
        sibling: a crash mid-copy leaves the staging file, never a short
        `power_netlist.v` that the next reader would take for a netlist.

        A `netlist-source: pnr` run resolves no netlist at all — it reads
        a routed database — so there is nothing to snapshot and nothing
        to hash, which is what it recorded before this existed.

        :returns: ``None`` on success (or when there is nothing to do),
            else a description of the failure. A netlist that cannot be
            copied into the artefact directory is not a by-product
            failure to warn about and continue past: the generated
            script names the copy, so there would be nothing for
            `read_verilog` to read.
        """
        source = self._netlist_source_path
        self._netlist_sha256 = None
        if source is None:
            return None
        snapshot = Path(self._netlist_snapshot_path())
        staging = snapshot.with_name(snapshot.name + ".tmp")
        try:
            shutil.copyfile(source, staging)
            os.replace(staging, snapshot)
        except OSError as e:
            with contextlib.suppress(OSError):
                staging.unlink()
            return f"could not copy {source} to {snapshot}: {e}"
        # Of the copy, not of the source: these are the bytes OpenROAD
        # is about to read, and nothing else writes this path.
        self._netlist_sha256 = sha256_of(snapshot)
        return None

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

    # Printed by the generated script between the design-total `report_power`
    # and the per-instance block below it. It splits `power.log` into the half
    # that decides the run and the half that only decides the by-product: see
    # `_fatal_log_region` (#558).
    _DETAIL_MARKER = "RB_PHYS_DETAIL_BEGIN"

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

        The `catch` alone is not enough, though: an OpenSTA that rejects
        `-instances` prints an `[ERROR ...]` diagnostic *before* raising, and
        the post-run log gate fails any run whose log carries one. So the
        block opens with a marker line naming where the by-product begins —
        `_fatal_log_region` scans only what precedes it, and this contract
        holds without the gate having to guess which diagnostics are benign.
        """
        return [
            f'puts "{self._DETAIL_MARKER}"',
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
            # The script reads this run's own copy, not the upstream
            # path: `_snapshot_netlist` writes it after the stale-clear
            # below and hashes what it wrote, so the bytes the model
            # names and the bytes OpenROAD parses are one file that no
            # concurrent `rb synth` can reach (#560). Recorded here so
            # that step knows what to copy.
            self._netlist_source_path = netlist

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
                    f"read_verilog {self._netlist_snapshot_path()}",
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

        The netlist snapshot goes with them (#560). It is the largest
        thing this flow writes, nothing reads it after OpenROAD has, and
        a failed run that left it behind would leave a copy of a netlist
        no artefact here still describes. `run()` therefore clears
        *before* `_snapshot_netlist` takes this run's copy, never after.

        The model and its manifest stay -- a synthesis may have merged its
        own half into them -- but this flow's half is nulled out, because
        publication happens only on a pass and a failed rerun would otherwise
        leave the previous run's per-instance watts discoverable with the
        report behind them already deleted (#558).
        """
        stale = clear_stale_artefacts(
            [
                self._report_path(),
                self._instances_report_path(),
                self._instances_cells_path(),
                self._netlist_snapshot_path(),
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
        self._invalidate_phys_half()

    def _invalidate_phys_half(self) -> None:
        """Null this flow's half of any model + manifest already here (#558).

        The counterpart of `_publish_phys_model`, called from the clear so a
        run that never reaches publication withdraws the previous one's
        per-instance rows rather than leaving them over a deleted report. The
        synthesis half is untouched.
        """
        result = invalidate_half(self.artefact_dir, "instances")
        if result["model"] or result["manifest"] or result["error"]:
            log_event(
                logger,
                logging.DEBUG,
                "power.phys_half_invalidated",
                power=self.power_cfg.get_name(),
                model=result["model"],
                manifest=result["manifest"],
                error=result["error"],
            )

    def _fatal_log_region(self, log_text: str) -> str:
        """The part of `power.log` whose `[ERROR ...]` lines fail the run.

        Everything the generated script prints after `_DETAIL_MARKER` belongs
        to the per-instance block, which is a by-product: it is `catch`ed, and
        by the time it runs the design totals are already in `power.rpt`. An
        `[ERROR ...]` down there (an OpenSTA without `report_power
        -instances`, say) must cost the model its `instances` half and nothing
        more — failing the run on it would delete totals this wrapper had
        already parsed.

        A log with no marker is scanned whole: older scripts predate it, and
        so does a run that died before reaching the totals.
        """
        marker_at = log_text.find(self._DETAIL_MARKER)
        return log_text if marker_at < 0 else log_text[:marker_at]

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

        # After the clear, before OpenROAD: the script names this run's
        # own copy of the netlist, and the hash recorded beside the watts
        # is of that copy (#560).
        snapshot_error = self._snapshot_netlist()
        if snapshot_error is not None:
            log_event(
                logger,
                logging.WARNING,
                "power.netlist_snapshot_failed",
                power=self.power_cfg.get_name(),
                error=snapshot_error,
            )
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"could not stage the netlist for OpenROAD: {snapshot_error}",
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
            return self._fail_after_openroad(
                f"OpenROAD exited with code {result.returncode}"
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [
            ln
            for ln in self._fatal_log_region(log_text).splitlines()
            if ln.startswith("[ERROR ")
        ]
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

        The netlist this run read is identified by the hash
        `_snapshot_netlist` took of the private copy it gave OpenROAD, not
        by re-reading the upstream path now: it is what a later synthesis
        into this directory tests its own output against before carrying
        these per-instance rows forward, and what this publish tests before
        carrying any module rows already here forward (#558), so it has to
        be of the bytes the analysis actually measured -- which is why the
        analysis reads a copy nothing else can rewrite (#560). A
        `netlist-source: pnr` run resolves no netlist at all -- it reads the
        routed ODB -- so it records none, and nothing is inherited in either
        direction.

        The mode and the activity go in beside them (#568). Without them
        the model records a µW figure with no statement of what it is a
        figure OF: static leakage-plus-internal and a SAIF-driven dynamic
        total print in the same column, and two runs of one design that
        differ only in their stimulus are one document read twice. Both
        are the resolved values this run actually dispatched on -- the
        same `get_mode()` / `get_activity_source()` pair
        `_emit_activity_cmds` branches on -- so the record cannot claim a
        source the Tcl did not use.
        """
        try:
            inputs = self._resolve_inputs()
        except Exception:  # noqa: BLE001 - resolution already succeeded once
            inputs = {}
        activity = self.power_cfg.get_activity()
        source = self.power_cfg.get_activity_source()
        published = publish_power(
            artefact_dir=self.artefact_dir,
            top=inputs.get("top"),
            backend="openroad",
            run=self.power_cfg.get_name(),
            netlist_source=self.power_cfg.get_netlist_source(),
            netlist_sha256=self._netlist_sha256,
            mode=self.power_cfg.get_mode(),
            activity=activity_block(
                source=source,
                trace=activity.saif or activity.vcd,
                scope=activity.scope,
                toggle_rate=activity.default_toggle_rate,
                duty=activity.default_static_prob,
            ),
            platform=self.power_cfg.get_platform(),
            constraints=self.power_cfg.get_constraints(),
            options={
                "tool": self.power_cfg.get_tool_name(),
                "netlist_source": self.power_cfg.get_netlist_source(),
                "mode": self.power_cfg.get_mode(),
                "activity_source": source,
                "reglvl": self.power_cfg.get_reglvl(self.power_cfg.get_tool_name()),
                "tool_overrides": self.power_cfg.tool_overrides,
            },
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
