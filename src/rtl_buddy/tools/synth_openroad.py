import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from . import block_params
from .artifact_paths import clear_stale_artefacts
from .liberty_units import LibertyTimeUnitError, liberty_time_unit_ps, open_liberty
from .vlog_filelist import VlogFilelist, incdirs_from_filelist
from .synth_yosys import (
    MAX_EVENT_FINDINGS,
    clean_block_netlist,
    dont_use_args,
    elaboration_defines,
    liberty_args,
    library_fingerprint,
    elaboration_fingerprint,
    emit_frontend_read_cmds,
    describe_unresolved_interfaces,
    find_conflicting_driver_warnings,
    find_unresolved_interface_warnings,
    lifetime_scan_inputs,
    mapped_abc_script,
    parse_area_um2,
    parse_gate_count,
    resolve_dont_use_cells,
    slang_handles_params,
    validate_frontend,
    warn_mapped_abc_args,
    yosys_env,
    yosys_read_lib_paths,
)
from .sv_lifetime_scan import LifetimeFinding, describe_findings, scan_files
from ..config.synth import (
    SynthConfig,
    SynthToolConfig,
    SynthToolOpts,
    SynthEffortConfig,
    default_effort_config,
    resolve_conflicting_drivers_mode,
    resolve_static_functions_mode,
    resolve_unresolved_interfaces_mode,
)
from ..config.openroad_threads import ThreadPlan, parse_reported_threads, plan_threads
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..phys.provenance import text_sha256
from ..phys.publish import (
    confirm_digest,
    invalidate_half,
    publish_synth,
    sha256_of,
    withdrawal_failure_desc,
)
from ..runner.synth_results import SynthFailResults, SynthPassResults, SynthResults


class OpenRoadSynth:
    """Two-stage synthesis: Yosys maps RTL to a gate-level netlist, then OpenROAD applies the SDC and reports area, WNS and TNS."""

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
        self._yosys_opts: SynthToolOpts | None = None
        self._ps_per_unit: float | None = None
        self._or_opts: SynthToolOpts | None = None
        # The `-D` table `_write_yosys_script` fed the frontend.
        self._script_defines: dict[str, str | None] | None = None
        # SDC hash taken at stage 1 script generation and confirmed at run end; None before the run or with no SDC.
        self._constraints_sha256: str | None = None
        self.static_function_findings = 0
        self.unresolved_interfaces = 0
        # Thread plan resolved by `_write_or_script`; None until stage 2 is scripted.
        self._thread_plan: ThreadPlan | None = None
        # Resolved `blocks:` abstracts, set by the runner.
        self.blocks: list = []

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.f")

    def _yosys_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.ys")

    def _yosys_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth_yosys.log")

    def _yosys_netlist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth_netlist.v")

    def _stats_path(self) -> str:
        """Yosys' per-module `stat -json` dump."""
        return os.path.join(self.artefact_dir, "synth_stat.json")

    def _or_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.tcl")

    def _or_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.log")

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        _SKIP = ("+incdir+", "+libext+", "+define+", "-y ", "-F ", "-f ")
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
        return self.root_cfg.get_synth_platform_cfg(platform).get_paths() + extras

    def _resolve_cell_lib_paths(self) -> list[str]:
        """The standard-cell Liberty files mapping and `stat` use: the platform corner's, or `lib-paths` without a platform."""
        platform = self.synth_cfg.get_platform()
        if not platform or self.root_cfg is None:
            return list(self.synth_cfg.get_lib_paths())
        return self.root_cfg.get_synth_platform_cfg(platform).get_paths()

    def _time_unit_ps(self) -> float:
        """Return the Liberty `time_unit` in picoseconds, the unit OpenSTA reports slack in.

        Raises :class:`LibertyTimeUnitError` when the libraries disagree.
        """
        if self._ps_per_unit is None:
            self._ps_per_unit = liberty_time_unit_ps(self._resolve_lib_paths())
        return self._ps_per_unit

    def _resolve_lef_paths(self) -> list[str]:
        platform = self.synth_cfg.get_platform()
        extras = list(self.synth_cfg.get_lef_paths())
        if not platform or self.root_cfg is None:
            return extras
        platform_lefs = self.root_cfg.get_synth_platform_cfg(platform).get_lef_paths()
        return list(platform_lefs) + extras

    def _resolve_yosys_opts(self) -> SynthToolOpts:
        """Return the options for the Yosys elaboration stage.

        Elaboration always uses Yosys, so its opts come from the yosys tool config plus any
        `tool_overrides.yosys`, falling back to the openroad opts only when no yosys tool config
        exists. The effort's `abc-args` and `abc-script` apply unless those overrides set them.
        Memoised because resolving overrides emits validation warnings.
        """
        if self._yosys_opts is not None:
            return self._yosys_opts
        overrides = self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        opts = self.tool_cfg.get_opts(overrides)
        if self.root_cfg is not None:
            try:
                yosys_tool_cfg = self.root_cfg.get_synth_tool_cfg("yosys")
            except FatalRtlBuddyError:
                # No `yosys` entry under cfg-synth-tools. Only the lookup is guarded: a config error while
                # resolving the opts must surface, not silently downgrade the frontend to "verilog".
                yosys_tool_cfg = None
            if yosys_tool_cfg is not None:
                overrides = self.synth_cfg.get_tool_overrides_for("yosys")
                opts = yosys_tool_cfg.get_opts(overrides)
        if not overrides or "abc_args" not in overrides:
            opts.abc_args = self.effort_cfg.get_yosys_abc_args() or opts.abc_args
        if not overrides or "abc_script" not in overrides:
            opts.abc_script = self.effort_cfg.get_yosys_abc_script() or opts.abc_script
        self._yosys_opts = opts
        return opts

    def _scan_static_lifetimes(
        self, fl_path: str, opts: SynthToolOpts
    ) -> list[LifetimeFinding]:
        """Run the pre-elaboration static-lifetime gate; see YosysSynth."""
        incdirs, defines = lifetime_scan_inputs(
            fl_path,
            self.synth_cfg.get_name(),
            self.synth_cfg.get_defines(),
            opts.frontend,
        )
        if resolve_static_functions_mode(opts) == "allow":
            return []
        return scan_files(
            self._source_files_from_filelist(fl_path),
            incdirs=incdirs,
            defines=defines,
            # Only slang honours --single-unit; the verilog frontend ignores it with a warning.
            single_unit=opts.single_unit and opts.frontend == "slang",
            # slang's `undefineall` keeps the -D macros; Yosys read_verilog drops them.
            undefineall_keeps_predefines=opts.frontend == "slang",
        )

    def _stat_json_cmd(self, liberty_args: str) -> str:
        """Return the `stat -json` line that feeds the phys model's module rows.

        `tee -o` writes the JSON to a file and `-q` keeps it out of `synth_yosys.log`, which stage 1
        scrapes for its cell count and `ERROR:` lines.
        """
        return f"tee -q -o {self._stats_path()} stat -json{liberty_args}"

    def _write_yosys_script(self, fl_path: str) -> str:
        top = self.synth_cfg.get_top()
        lib_paths = self._resolve_lib_paths()
        params = self.synth_cfg.get_params()
        defines = elaboration_defines(fl_path, self.synth_cfg.get_defines())
        self._script_defines = defines
        self._hash_constraints()
        incdirs = incdirs_from_filelist(fl_path)
        opts = self._resolve_yosys_opts()

        source_files = self._source_files_from_filelist(fl_path)
        lines = []
        for lib in yosys_read_lib_paths(
            lib_paths, self.blocks, source_files, self.synth_cfg.get_name()
        ):
            lines.append(f"read_liberty -lib {lib}")

        lines.extend(
            emit_frontend_read_cmds(
                opts=opts,
                source_files=source_files,
                top=top,
                defines=defines,
                params=params,
                root_cfg=self.root_cfg,
                incdirs=incdirs,
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
        # Strip formal cells ($assert/$assume/$cover from unguarded immediate assertions): OpenROAD's structural `read_verilog` rejects them.
        lines.append("chformal -remove")

        if lib_paths:
            dont_use = dont_use_args(
                resolve_dont_use_cells(self.synth_cfg, self.root_cfg)
            )
            cell_libs = liberty_args(self._resolve_cell_lib_paths())
            lines.append(f"dfflibmap{dont_use}{cell_libs}")
            lines.append(
                f'abc{cell_libs}{dont_use} -script "+{mapped_abc_script(opts)}"'
            )
            warn_mapped_abc_args(opts, self.synth_cfg.get_name())
            lines.append(f"write_verilog {self._yosys_netlist_path()}")
            lines.append(f"stat{cell_libs}")
            lines.append(self._stat_json_cmd(cell_libs))
        else:
            lines.append(
                f"write_rtlil {os.path.join(self.artefact_dir, 'synth.rtlil')}"
            )
            lines.append(self._stat_json_cmd(""))

        script = "\n".join(lines) + "\n"
        script_path = self._yosys_script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _parse_area_um2(self, log_text: str, top: str | None = None) -> float | None:
        return parse_area_um2(log_text, top)

    def _parse_gate_count(self, log_text: str, top: str | None = None) -> int | None:
        return parse_gate_count(log_text, top)

    def _run_yosys_stage(self, fl_path: str) -> tuple[int | None, bool, str | None]:
        """Run the Yosys stage and return (gate_count, success, failure description).

        The description is None when the log holds all the detail. The correctness gates supply one
        because their finding is absent from the Yosys log (lifetime scan) or buried in it
        (conflicting drivers).
        """
        lib_paths = self._resolve_lib_paths()
        if not lib_paths:
            log_event(
                logger,
                logging.ERROR,
                "synth.openroad.no_library",
                synth=self.synth_cfg.get_name(),
            )
            return None, False, None

        # Same gates as YosysSynth: stage 1 elaborates with the same frontend.
        opts = self._resolve_yosys_opts()
        static_mode = resolve_static_functions_mode(opts)
        conflicting_mode = resolve_conflicting_drivers_mode(opts)
        interfaces_mode = resolve_unresolved_interfaces_mode(opts)
        findings = self._scan_static_lifetimes(fl_path, opts)
        self.static_function_findings = len(findings)
        if findings:
            if static_mode == "error":
                log_event(
                    logger,
                    logging.ERROR,
                    "synth.static_functions",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    count=len(findings),
                    findings=[f.describe() for f in findings[:MAX_EVENT_FINDINGS]],
                    truncated=max(0, len(findings) - MAX_EVENT_FINDINGS),
                )
                return (
                    None,
                    False,
                    (
                        f"{len(findings)} subroutine(s) declared without an "
                        f"explicit automatic lifetime: {describe_findings(findings)}"
                    ),
                )
            for finding in findings:
                log_event(
                    logger,
                    logging.WARNING,
                    "synth.static_function",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    path=finding.path,
                    line=finding.line,
                    kind=finding.kind,
                    subroutine=finding.name,
                )

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
                    env=yosys_env(self.artefact_dir),
                )

        if result.returncode != 0:
            return None, False, None

        try:
            with open(log_path) as f:
                log_text = f.read()
        except OSError:
            return None, False, None

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("ERROR:")]
        if error_lines:
            return None, False, None

        conflicting = find_conflicting_driver_warnings(log_text)
        if conflicting and conflicting_mode == "error":
            log_event(
                logger,
                logging.ERROR,
                "synth.conflicting_drivers",
                synth=self.synth_cfg.get_name(),
                count=len(conflicting),
                log=log_path,
            )
            # Drop this run's netlist too, so stage 2 and `rb pnr` / `rb power` cannot read a design that
            # folded to x. `_fail_after_yosys` clears again and reports a withdrawal this could not make.
            self._clear_stale_netlists()
            return (
                None,
                False,
                (
                    f"{len(conflicting)} 'multiple conflicting drivers' "
                    f"warning(s) in {log_path}"
                ),
            )

        if interfaces_mode != "allow":
            unbound = find_unresolved_interface_warnings(log_text)
            self.unresolved_interfaces = len(unbound)
            if unbound and interfaces_mode == "error":
                log_event(
                    logger,
                    logging.ERROR,
                    "synth.unresolved_interfaces",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    count=len(unbound),
                    instances=[
                        f"{module}.{inst}"
                        for inst, module in unbound[:MAX_EVENT_FINDINGS]
                    ],
                    truncated=max(0, len(unbound) - MAX_EVENT_FINDINGS),
                    log=log_path,
                )
                # Stage 1 has written a netlist that lacks the interface instances' port connections; drop it.
                self._clear_stale_netlists()
                return (
                    None,
                    False,
                    (
                        f"{len(unbound)} unbound interface instance(s): "
                        f"{describe_unresolved_interfaces(unbound)}"
                    ),
                )
            for inst, module in unbound:
                log_event(
                    logger,
                    logging.WARNING,
                    "synth.unresolved_interface",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    instance=inst,
                    module=module,
                    log=log_path,
                )

        return self._parse_gate_count(log_text, self.synth_cfg.get_top()), True, None

    def _masters_from_lef_and_liberty(
        self, lef_paths: list[str], lib_paths: list[str]
    ) -> set[str]:
        """Return the names OpenROAD already has a master for, from the LEFs and Liberties.

        A LEF `MACRO` or Liberty `cell` is a complete master for link_design, so these modules must
        not also be declared in Verilog (see `_write_or_blackbox_stubs`). The files are scanned line
        by line, not parsed, because they can be tens of MB. The LEF is the reliable source: the
        OpenROAD backend refuses a platform with no LEF.
        """
        names: set[str] = set()
        macro_re = re.compile(r"^\s*MACRO\s+(\S+)")
        cell_re = re.compile(r'^\s*cell\s*\(\s*"?([^"\s()]+)"?\s*\)')
        # `cell` and its parenthesised name split across two lines.
        cell_open_re = re.compile(r"^\s*cell\s*$")
        name_only_re = re.compile(r'^\s*\(\s*"?([^"\s()]+)"?\s*\)')
        for path, is_lef in [(p, True) for p in lef_paths] + [
            (p, False) for p in lib_paths
        ]:
            try:
                with open(path) if is_lef else open_liberty(path) as f:
                    pending_cell = False
                    for line in f:
                        if is_lef:
                            m = macro_re.match(line)
                            if m:
                                names.add(m.group(1))
                            continue
                        if pending_cell:
                            pending_cell = False
                            m = name_only_re.match(line)
                            if m:
                                names.add(m.group(1))
                                continue
                        m = cell_re.match(line)
                        if m:
                            names.add(m.group(1))
                        elif cell_open_re.match(line):
                            pending_cell = True
            except (OSError, EOFError):
                pass
        return names

    def _write_or_blackbox_stubs(self, known_masters: set[str]) -> list[str]:
        """Write OpenROAD-compatible port-only copies of Yosys blackbox stub files and return their paths.

        Yosys omits blackbox definitions from write_verilog output, and link_design fails on an
        instance of an undefined module. Files containing (* blackbox *) are reduced to module
        headers with no body, because OpenSTA's gate-level reader accepts only a small Verilog
        subset and takes cell timing from the Liberty.

        A blackbox named in `known_masters` is dropped rather than stubbed. Such a macro's LEF and
        Liberty are read by the same script (`lef-paths` / `lib-paths` in synth.yaml), and also
        declaring it in Verilog can displace that master. link_design then binds every instance to
        a zero-area module, so the macros vanish from the area report and the timing graph, the run
        still exits 0, and WNS is optimistic.
        """
        try:
            candidates = self._source_files_from_filelist(self._filelist_path())
        except OSError:
            return []
        module_re = re.compile(r"^\s*module\s+\w+", re.MULTILINE)
        shadowed: list[str] = []

        def _stub_or_drop(content: str) -> str:
            # Each module is cut from the (* blackbox *) attribute to endmodule. The header, through the `;` after its port list, is kept; the body is dropped.
            pieces = []
            last = 0
            for bb in block_params.blackbox_modules(content):
                pieces.append(content[last : bb.start])
                if bb.name in known_masters:
                    shadowed.append(bb.name)
                else:
                    pieces.append(
                        f"{content[bb.header_start : bb.header_end]}\nendmodule"
                    )
                last = bb.end
            pieces.append(content[last:])
            return "".join(pieces)

        result = []
        for src in candidates:
            try:
                with open(src) as f:
                    content = f.read()
                if "(* blackbox *)" not in content:
                    continue
                cleaned = _stub_or_drop(content)
                if not module_re.search(cleaned):
                    # Every blackbox in this file has a LEF/Liberty master; nothing left to read.
                    continue
                # OpenROAD's gate-level reader rejects SV `logic`; use `wire` for ports.
                cleaned = cleaned.replace("  input  logic ", "  input  wire  ")
                cleaned = cleaned.replace("  output logic ", "  output wire  ")
                stub_name = os.path.basename(src)
                stub_path = os.path.join(self.artefact_dir, f"or_{stub_name}")
                with open(stub_path, "w") as f:
                    f.write(cleaned)
                result.append(stub_path)
            except OSError:
                pass
        if shadowed:
            log_event(
                logger,
                logging.DEBUG,
                "synth.openroad.blackbox_master_from_lib",
                synth=self.synth_cfg.get_name(),
                modules=" ".join(sorted(set(shadowed))),
            )
        return result

    def _resolve_or_opts(self) -> SynthToolOpts:
        """Return the options for the mapping stage, from this backend's own tool config.

        Unlike `_resolve_yosys_opts`, this supplies `strategy` (AREA, TIMING, TIMING_GENETIC ...).
        Memoised because resolving overrides emits validation warnings.
        """
        if self._or_opts is None:
            self._or_opts = self.tool_cfg.get_opts(
                self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
            )
        return self._or_opts

    def _resynth_cmd(self) -> str | None:
        """Return the stage-2 command that `strategy` selects, or None for no resynthesis.

        Shared by `_write_or_script` and the phys model's config fingerprint, which records the
        command rather than the strategy string: `TIMING` and `TIMING_ANNEAL` map to one command,
        and `AREA` and unknown values both mean no resynthesis.
        """
        strategy = self._resolve_or_opts().strategy.upper()
        if strategy in ("TIMING", "TIMING_ANNEAL"):
            return "resynth_annealing"
        if strategy == "TIMING_GENETIC":
            return "resynth_genetic"
        return None

    def _write_or_script(self, lef_paths: list[str], lib_paths: list[str]) -> str:
        top = self.synth_cfg.get_top()
        constraints = self.synth_cfg.get_constraints()

        lines = []
        # First, and absent when `threads:` is unset.
        self._thread_plan = plan_threads(
            self.synth_cfg.get_threads(), flow="synth", run=self.synth_cfg.get_name()
        )
        if self._thread_plan.emit:
            lines.append(self._thread_plan.tcl())
        for lef in lef_paths:
            lines.append(f"read_lef {lef}")
        for lib in lib_paths:
            lines.append(f"read_liberty {lib}")
        lines.append(f"read_verilog {self._yosys_netlist_path()}")
        # Read the cleaned blackbox stubs so link_design can resolve them.
        known_masters = self._masters_from_lef_and_liberty(lef_paths, lib_paths)
        for bb_stub in self._write_or_blackbox_stubs(known_masters):
            lines.append(f"read_verilog {bb_stub}")
        lines.append(f"link_design {top}")
        # The resynthesis strategies pick library cells, so the PDK's exclusions must hold. Runs after
        # `link_design`: OpenROAD 26Q2's `set_dont_use` fails with "no network has been linked" before it.
        or_dont_use = resolve_dont_use_cells(self.synth_cfg, self.root_cfg)
        if or_dont_use:
            lines.append(f"set_dont_use [list {' '.join(or_dont_use)}]")

        if constraints:
            lines.append(f"read_sdc {constraints}")

        # Effort-defined pre-STA Tcl (e.g. floorplan, global_placement, estimate_parasitics).
        pre_sta_tcl = self.effort_cfg.get_openroad_pre_sta_tcl()
        if pre_sta_tcl:
            lines.append(pre_sta_tcl.rstrip())

        resynth = self._resynth_cmd()
        if resynth:
            lines.append(resynth)

        lines.append("report_design_area")
        if constraints:
            # report_checks prints per-group reports; report_worst_slack -max gives the single WNS across all groups.
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

    def _parse_or_wns(self, log_text: str) -> float | None:
        # Prefer the single line from `report_worst_slack -max`: "worst slack max -0.431".
        m = re.search(r"^worst slack\s+max\s+([-\d.]+)", log_text, re.MULTILINE)
        if m:
            return float(m.group(1))
        # Fallback: `report_checks` prints one report per group, each ending with a "slack (MET)" or
        # "slack (VIOLATED)" line; take the minimum over all of them.
        matches = re.findall(
            r"^\s+([-\d.]+)\s+slack\s+\((?:MET|VIOLATED)\)", log_text, re.MULTILINE
        )
        if not matches:
            return None
        return min(float(s) for s in matches)

    def _parse_or_tns(self, log_text: str) -> float | None:
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
        wns = self._parse_or_wns(log_text)
        tns = self._parse_or_tns(log_text)

        ps_per_unit = self._time_unit_ps()
        wns_ps = wns * ps_per_unit if wns is not None else None
        tns_ps = tns * ps_per_unit if tns is not None else None

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
        phys_model = self._publish_phys_model(area_um2=area_um2, gate_count=gate_count)
        return SynthPassResults(
            name=self.name + "/results",
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            tns_ps=tns_ps,
            static_function_findings=self.static_function_findings or None,
            unresolved_interfaces=self.unresolved_interfaces or None,
            phys_model=phys_model,
        )

    def _phys_options(self) -> dict:
        """Return the effective options the config fingerprint is digested over.

        These are the inputs the two generated scripts consume, not the resolved `SynthToolOpts`:

        - `elaborate`: the shared frontend subset (:func:`elaboration_fingerprint`) plus `synth_args`
          from `effort_cfg.get_yosys_synth_args()`. `opts.synth_args` and `abc-args` are ignored on
          this backend, so they are not digested.
        - `abc_script`: the stage 1 ABC script (:func:`mapped_abc_script`).
        - `map`: `resynth` (from `_resynth_cmd`), the sha256 of the effort's pre-STA Tcl (stripped as
          the script writer strips it), and the resolved `lefs`.
        - `params` and `defines`: the elaboration values. `defines` is the merged table given to the
          frontend (:func:`elaboration_defines`), not `synth.yaml`'s field alone.
        - `libs`: the resolved Liberty set (:func:`library_fingerprint`), which both stages read. It
          includes the config's own `lib-paths` / `lef-paths` on top of the platform's, so the
          platform name alone is not enough.
        """
        return {
            "tool": self.tool_cfg.get_name(),
            "elaborate": dict(
                elaboration_fingerprint(self._resolve_yosys_opts(), self.root_cfg),
                synth_args=self.effort_cfg.get_yosys_synth_args(),
            ),
            "abc_script": mapped_abc_script(self._resolve_yosys_opts()),
            "map": {
                "resynth": self._resynth_cmd(),
                "pre_sta_tcl_sha256": text_sha256(
                    self.effort_cfg.get_openroad_pre_sta_tcl().rstrip()
                ),
                "lefs": library_fingerprint(self._resolve_lef_paths(), self.root_cfg),
            },
            "libs": library_fingerprint(self._resolve_lib_paths(), self.root_cfg),
            # Both stages read the PDK's excluded cells; different exclusions are different experiments.
            "dont_use": resolve_dont_use_cells(self.synth_cfg, self.root_cfg),
            "params": self.synth_cfg.get_params(),
            "defines": self._digested_defines(),
        }

    def _digested_defines(self) -> dict:
        """Return the macro table the generated script fed the frontend.

        Recorded by `_write_yosys_script`, because `synth.f` may have been rewritten since.
        """
        if self._script_defines is not None:
            return dict(self._script_defines)
        return self.synth_cfg.get_defines()

    def _publish_phys_model(
        self, *, area_um2: float | None, gate_count: int | None
    ) -> str | None:
        """Write `phys-model.json` and its manifest for a passing run.

        Stage 1 supplies the per-module breakdown and stage 2 the design totals. Never fails the
        synthesis. The identity fields cover both stages (see `_phys_options`); the SDC hash is the
        one taken at stage 1 script generation and confirmed here.
        """
        self._confirm_constraints_unchanged()
        published = publish_synth(
            artefact_dir=self.artefact_dir,
            top=self.synth_cfg.get_top(),
            backend="openroad",
            run=self.synth_cfg.get_name(),
            stats_path=self._stats_path(),
            netlist_path=self._yosys_netlist_path(),
            log_path=self._or_log_path(),
            area_um2=area_um2,
            gate_count=gate_count,
            platform=self.synth_cfg.get_platform(),
            effort=self.effort_cfg.get_name(),
            constraints=self.synth_cfg.get_constraints(),
            constraints_sha256=self._constraints_sha256,
            options=self._phys_options(),
        )
        if published["error"] is not None or published["rows"] is None:
            log_event(
                logger,
                logging.WARNING,
                "synth.phys_model_incomplete",
                synth=self.synth_cfg.get_name(),
                stats=self._stats_path(),
                error=published["error"],
            )
        return published["model"]

    def _hash_constraints(self) -> None:
        """Hash the SDC bytes as of stage 1 script generation.

        Hashing at publish time would be wrong: a file edited during the run would be recorded as
        the constraints the netlist was timed against. `_confirm_constraints_unchanged` checks it
        at run end.
        """
        self._constraints_sha256 = sha256_of(self.synth_cfg.get_constraints())

    def _confirm_constraints_unchanged(self) -> None:
        """Withdraw the SDC hash if the file changed during the run.

        Uses :func:`~rtl_buddy.phys.publish.confirm_digest`. A mismatch records ``null`` and
        warns, so a null does not read as "this run had no constraints".
        """
        self._constraints_sha256, changed = confirm_digest(
            self.synth_cfg.get_constraints(), self._constraints_sha256
        )
        if changed:
            log_event(
                logger,
                logging.WARNING,
                "synth.constraints_changed_during_run",
                synth=self.synth_cfg.get_name(),
                constraints=self.synth_cfg.get_constraints(),
            )

    def _clear_stale_netlists(self) -> str | None:
        """Remove the previous run's stage-1 netlists and `synth_stat.json` before anything returns.

        Called first in `run()` so no early return leaves a stale netlist for stage 2, `rb pnr` or
        `rb power` to read. This flow's half of the phys model is nulled out, but the model and
        manifest stay because the other flow's half may be in them.

        :returns: ``None``, or the reason the withdrawal failed; every caller turns that into a
            failed run. See :func:`~rtl_buddy.phys.publish.withdrawal_failure_desc`.
        """
        stale = clear_stale_artefacts(
            [
                self._yosys_netlist_path(),
                os.path.join(self.artefact_dir, "synth.rtlil"),
                self._stats_path(),
            ],
            owner=self.synth_cfg.get_name(),
        )
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "synth.stale_artefacts_removed",
                synth=self.synth_cfg.get_name(),
                paths=stale,
            )
        return self._invalidate_phys_half()

    def _invalidate_phys_half(self) -> str | None:
        """Null this flow's half of any model and manifest already present; the power half is untouched.

        :returns: ``None`` on success, else `invalidate_half`'s ``error``.
        """
        result = invalidate_half(self.artefact_dir, "modules")
        if result["error"]:
            log_event(
                logger,
                logging.WARNING,
                "synth.phys_half_stale",
                synth=self.synth_cfg.get_name(),
                error=result["error"],
            )
            return result["error"]
        if result["model"] or result["manifest"]:
            log_event(
                logger,
                logging.DEBUG,
                "synth.phys_half_invalidated",
                synth=self.synth_cfg.get_name(),
                model=result["model"],
                manifest=result["manifest"],
            )
        return None

    def _fail_after_yosys(self, desc: str) -> SynthFailResults:
        """Fail a run that has already invoked Yosys, leaving no netlist published.

        Stage 1 writes the netlist before its trailing `stat`, so a later Yosys error or a stage 2
        failure would otherwise leave a netlist where `rb pnr` and `rb power` read it. Every
        post-Yosys failure return goes through here. A withdrawal that could not be made is added
        to the failure description.
        """
        stale_error = self._clear_stale_netlists()
        if stale_error is not None:
            desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
        return SynthFailResults(name=self.name + "/results", desc=desc)

    def run(self) -> SynthResults:
        # If the withdrawal failed, stop: the rows it could not clear would describe a design this directory no longer holds.
        stale_error = self._clear_stale_netlists()
        if stale_error is not None:
            return SynthFailResults(
                name=self.name + "/results",
                desc=withdrawal_failure_desc(stale_error),
                fail_stage="setup",
            )
        log_event(
            logger,
            logging.INFO,
            "synth.start",
            synth=self.synth_cfg.get_name(),
            tool=self.tool_cfg.get_executable(),
            top=self.synth_cfg.get_top(),
        )

        # Resolve both gate modes before the Liberty, LEF and filelist returns so a misspelled value is fatal on every run, as in YosysSynth.run().
        opts = self._resolve_yosys_opts()
        resolve_static_functions_mode(opts)
        resolve_conflicting_drivers_mode(opts)
        # An unknown frontend or missing slang plugin is a config error too; stage 1's gates return before _write_yosys_script() would check.
        validate_frontend(opts, self.root_cfg)

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
                fail_stage="setup",
            )

        try:
            self._time_unit_ps()
        except LibertyTimeUnitError as e:
            log_event(
                logger,
                logging.ERROR,
                "synth.liberty_time_unit_error",
                synth=self.synth_cfg.get_name(),
                error=str(e),
                units=e.units,
            )
            return SynthFailResults(
                name=self.name + "/results", desc=str(e), fail_stage="setup"
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
                fail_stage="setup",
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
                name=self.name + "/results",
                desc=f"Filelist error: {e}",
                fail_stage="setup",
            )

        gate_count, yosys_ok, yosys_desc = self._run_yosys_stage(fl_path)
        if not yosys_ok:
            log_event(
                logger,
                logging.WARNING,
                "synth.failed",
                synth=self.synth_cfg.get_name(),
                returncode=-1,
                log=self._yosys_log_path(),
            )
            # Stage 1 writes the netlist before its trailing `stat`; a crash or ERROR can leave one behind.
            return self._fail_after_yosys(
                yosys_desc or "Yosys stage failed; see synth_yosys.log"
            )

        # Stage 2 and every later reader need the `#(...)` gone from parameterised block instances.
        block_error = clean_block_netlist(
            self._yosys_netlist_path(), self.blocks, self.synth_cfg.get_name()
        )
        if block_error is not None:
            return self._fail_after_yosys(block_error)

        result = self._run_or_stage(gate_count, lef_paths, lib_paths)
        if isinstance(result, SynthFailResults):
            # Stage 1 published a netlist; stage 2 then failed.
            return self._with_threads(self._fail_after_yosys(result.results["desc"]))
        return self._with_threads(result)

    def _with_threads(self, res: SynthResults) -> SynthResults:
        """Record stage 2's OpenROAD thread provenance on ``res``, once stage 2 was scripted.

        The thread count OpenROAD logged wins over the one requested.
        """
        if self._thread_plan is not None:
            try:
                reported = parse_reported_threads(Path(self._or_log_path()).read_text())
            except OSError:
                reported = None
            res.results["openroad_threads"] = self._thread_plan.fields(reported)
        return res
