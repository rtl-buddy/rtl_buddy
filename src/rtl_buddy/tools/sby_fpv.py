"""SymbiYosys (``sby``) tool wrapper for ``rb fpv``.

Generates a ``fpv.sby`` config from the per-run :class:`FpvConfig`,
shells out to ``sby -f -d <workdir>``, then reads the workdir
``status`` file plus the process exit code to populate
:class:`FpvResults`. Counterexample VCDs (when the proof fails) stay
inside the engine subdirectory of the workdir for the user to inspect.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from ..config.fpv import FpvConfig, FpvToolConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.fpv_results import FpvFailResults, FpvPassResults, FpvResults
from .fpv_vacuity import (
    VacuityCandidate,
    extract_candidates,
    parse_vacuity_log,
    write_vacuity_module,
)
from .fpv_coi import run_coi_analysis


# Sby's compatibility check requires a minimum yosys; we surface the
# version we probe for the same reason `pnr_openroad.py` does — gives
# users a clear signal when their toolchain is older than what we test
# against.
MIN_SBY_VERSION = "0.40"


_INCDIR_PREFIX = "+incdir+"
_LIBEXT_PREFIX = "+libext+"
_SOURCE_OPT_PREFIX = "-v "
_FILELIST_SKIP_PREFIXES = ("-y ", "-F ", "-f ")


class SbyFpv:
    def __init__(
        self,
        name: str,
        fpv_cfg: FpvConfig,
        tool_cfg: FpvToolConfig,
        suite_dir: str,
        root_cfg=None,
    ):
        self.name = name
        self.fpv_cfg = fpv_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg

        artefact_root = Path(suite_dir) / "artefacts" / fpv_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # --- artefact paths -----------------------------------------------------

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.f")

    def _sby_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.sby")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.log")

    def _workdir_path(self) -> str:
        # Sby creates this dir; we hand it `-f` to overwrite on rerun.
        return os.path.join(self.artefact_dir, "sby_workdir")

    def _vacuity_sv_path(self) -> str:
        return os.path.join(self.artefact_dir, "vacuity_covers.sv")

    def _vacuity_sby_path(self) -> str:
        return os.path.join(self.artefact_dir, "vacuity.sby")

    def _vacuity_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "vacuity.log")

    def _vacuity_workdir_path(self) -> str:
        return os.path.join(self.artefact_dir, "vacuity_workdir")

    def _resolve_plugin_path(self, plugin_path: str | None) -> str | None:
        """Resolve a yosys plugin path against the project root.

        Mirrors :func:`tools.synth_yosys.resolve_plugin_path`. Absolute
        paths pass through; relative paths are taken relative to the
        project root (the directory containing ``root_config.yaml``).
        ``None`` returns ``None`` so callers can distinguish
        unconfigured from configured-but-empty.
        """
        if not plugin_path:
            return None
        p = Path(plugin_path)
        if p.is_absolute():
            return str(p)
        if self.root_cfg is None:
            return str(p.resolve())
        return str((Path(self.root_cfg.get_project_rootdir()) / p).resolve())

    def _coi_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "coi.ys")

    def _coi_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "coi.log")

    # --- helpers ------------------------------------------------------------

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.fpv_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _parse_filelist(self, fl_path: str) -> tuple[list[str], list[str]]:
        """Return (source paths, include dirs) from a stripped filelist."""
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        sources: list[str] = []
        incdirs: list[str] = []
        with open(fl_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("//"):
                    continue
                if line.startswith(_INCDIR_PREFIX):
                    inc = line[len(_INCDIR_PREFIX) :]
                    incdirs.append(os.path.normpath(os.path.join(fl_dir, inc)))
                    continue
                if line.startswith(_LIBEXT_PREFIX):
                    continue
                if any(line.startswith(opt) for opt in _FILELIST_SKIP_PREFIXES):
                    continue
                if line.startswith(_SOURCE_OPT_PREFIX):
                    line = line[len(_SOURCE_OPT_PREFIX) :]
                sources.append(os.path.normpath(os.path.join(fl_dir, line)))
        return sources, incdirs

    def _probe_sby_version(self, executable: str) -> str | None:
        try:
            res = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_event(
                logger,
                logging.WARNING,
                "fpv.sby_version_probe_failed",
                tool=executable,
                error=str(e),
            )
            return None
        out = (res.stdout or "") + (res.stderr or "")
        # Output looks like "sby 0.42+12 (yosys-0.51) ..."
        m = re.search(r"sby\s+(\S+)", out)
        return m.group(1) if m else None

    def _write_sby_file(self, sources: list[str], incdirs: list[str]) -> str:
        """Render the ``fpv.sby`` config that sby consumes."""
        return self._render_sby(
            output_path=self._sby_path(),
            sources=sources,
            incdirs=incdirs,
            mode=self.fpv_cfg.get_mode(),
            extra_property_files=[],
        )

    def _render_sby(
        self,
        *,
        output_path: str,
        sources: list[str],
        incdirs: list[str],
        mode: str,
        extra_property_files: list[str],
    ) -> str:
        cfg = self.fpv_cfg
        opts = self.tool_cfg.get_opts(
            cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        lines: list[str] = []

        # [options]
        lines.append("[options]")
        lines.append(f"mode {mode}")
        lines.append(f"depth {cfg.get_depth()}")
        if opts.timeout is not None:
            lines.append(f"timeout {opts.timeout}")
        lines.append("")

        # [engines]
        lines.append("[engines]")
        for engine in cfg.get_engines():
            lines.append(engine)
        lines.append("")

        # [script]
        # Order: design sources -> constraints (assumes in scope first) ->
        # properties (asserts that depend on those assumes).
        lines.append("[script]")
        frontend = cfg.get_frontend()
        if frontend == "slang":
            plugin = self._resolve_plugin_path(opts.plugin_path)
            if not plugin:
                raise FatalRtlBuddyError(
                    f"{cfg.get_name()}: fpv frontend=slang requires "
                    f"`cfg-fpv-tools[].opts.plugin-path` to point at the "
                    f"built yosys-slang shared library"
                )
            # `plugin -i` is idempotent within a yosys session — only
            # emit the directive when slang is actually used so the
            # default verilog path stays plugin-free.
            lines.append(f"plugin -i {plugin}")
        for inc in incdirs:
            if frontend == "slang":
                # slang's preprocessor uses --include-directory; we
                # accept the same incdirs the filelist already parsed.
                lines.append(f"verilog_defaults -add -I {inc}")
            else:
                lines.append(f"verilog_defaults -add -I {inc}")
        constraints = cfg.get_constraints()
        constraint_files = [constraints] if constraints else []
        all_sources = (
            list(sources)
            + constraint_files
            + list(cfg.get_properties())
            + list(extra_property_files)
        )
        if frontend == "slang":
            # slang elaborates eagerly and handles SV `bind` directives,
            # concurrent SVA implications, and full sequence operators
            # that the native verilog frontend rejects. The `--top`
            # arg is required for `bind` to resolve — slang's
            # elaborator only pulls in bound modules under the
            # designated top. All files are read in one invocation so
            # bind statements at compilation-unit scope see every
            # declared module.
            #
            # `--no-synthesis-define -DFORMAL=1` mirrors what the
            # verilog path's `read -formal` does: yosys's verilog
            # frontend replaces its implicit SYNTHESIS=1 define with
            # FORMAL=1 in formal mode, while yosys-slang defaults to
            # SYNTHESIS=1. Without this, in-RTL asserts guarded by
            # `ifdef FORMAL are preprocessed away and the proof
            # passes vacuously (#246).
            src_args = " ".join(os.path.basename(s) for s in all_sources)
            lines.append(
                f"read_slang --top {cfg.get_top()} "
                f"--no-synthesis-define -DFORMAL=1 {src_args}"
            )
        else:
            for src in all_sources:
                # Use basename — files are dropped into the sby workdir under [files].
                lines.append(f"read -sv -formal {os.path.basename(src)}")
        lines.append(f"prep -top {cfg.get_top()}")
        lines.append("")

        # [files]
        lines.append("[files]")
        for src in all_sources:
            lines.append(src)
        lines.append("")

        with open(output_path, "w") as f:
            f.write("\n".join(lines))
        return output_path

    # --- run ----------------------------------------------------------------

    def run(self) -> FpvResults:
        cfg = self.fpv_cfg

        fl_path = self._write_filelist()
        sources, incdirs = self._parse_filelist(fl_path)
        if not sources and not cfg.get_properties():
            raise FatalRtlBuddyError(
                f"{cfg.get_name()}: filelist {fl_path} produced no sources and "
                f"no `properties:` entries are listed"
            )

        # Validate that all explicit property files exist before we
        # bother spinning up sby.
        for prop in cfg.get_properties():
            if not os.path.isfile(prop):
                raise FatalRtlBuddyError(
                    f"{cfg.get_name()}: property file not found: {prop}"
                )

        constraints = cfg.get_constraints()
        if constraints is not None and not os.path.isfile(constraints):
            raise FatalRtlBuddyError(
                f"{cfg.get_name()}: constraints file not found: {constraints}"
            )

        opts = self.tool_cfg.get_opts(
            cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )
        if opts.solver_versions:
            # Raises FatalRtlBuddyError on any mismatch so the user
            # sees the drift before sby produces a wrong-looking
            # PASS or timeout on a different solver version.
            from .fpv_solver_pin import check_solver_pins

            resolved = check_solver_pins(opts.solver_versions)
            log_event(
                logger,
                logging.INFO,
                "fpv.solver_pins_resolved",
                verification=cfg.get_name(),
                resolved=resolved,
            )

        sby_path = self._write_sby_file(sources, incdirs)
        log_path = self._log_path()
        workdir = self._workdir_path()
        executable = self.tool_cfg.get_executable() or "sby"

        # Surface the sby version once per run so the log captures it.
        version = self._probe_sby_version(executable)
        if version is not None:
            log_event(
                logger,
                logging.INFO,
                "fpv.sby_version",
                tool=executable,
                version=version,
            )

        # `-f` overwrites the workdir if it already exists; `-d <path>`
        # selects the workdir location so we keep all artefacts under
        # `<suite>/artefacts/<run>/sby_workdir/`.
        cmd = [executable, "-f", "-d", workdir, sby_path]

        with task_status(f"Running FPV {cfg.get_name()}"):
            log_event(
                logger,
                logging.INFO,
                "fpv.start",
                verification=cfg.get_name(),
                tool=executable,
                top=cfg.get_top(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
            )
            start = time.monotonic()
            proc = self._run(cmd, log_path)
            runtime_s = time.monotonic() - start

        status = self._read_status(workdir)
        per_engine = self._read_per_engine(workdir)

        # Optional secondary sby pass: cover-mode reachability check for
        # every `|->` antecedent so the user learns when a proved
        # property is vacuously true. Only run when the primary pass
        # *succeeded* — failure already carries actionable signal.
        vacuity = None
        if cfg.vacuity_enabled() and (
            status == "PASS" or (status is None and proc.returncode == 0)
        ):
            vacuity = self._run_vacuity(executable, sources, incdirs)

        # Cone-of-influence coverage: structural-only yosys walk, runs
        # regardless of the primary verdict because the coverage signal
        # is just as actionable on a failing run ("the assertion that
        # caught this only sees X% of the design") as on a passing one.
        coi = None
        if cfg.coi_enabled():
            # The COI walk needs the same frontend the proof used —
            # mixing verilog + slang frontends in the same yosys
            # invocation produces inconsistent `$check` cell sets.
            opts_for_coi = self.tool_cfg.get_opts(
                cfg.get_tool_overrides_for(self.tool_cfg.get_name())
            )
            coi = run_coi_analysis(
                name=cfg.get_name(),
                yosys_exe="yosys",
                sources=sources,
                incdirs=incdirs,
                properties=list(cfg.get_properties()),
                constraints=cfg.get_constraints(),
                top=cfg.get_top(),
                script_path=self._coi_script_path(),
                log_path=self._coi_log_path(),
                frontend=cfg.get_frontend(),
                plugin_path=self._resolve_plugin_path(opts_for_coi.plugin_path),
            )

        # Sby exit code conventions:
        #   0 -> PASS, 1 -> FAIL, 2 -> UNKNOWN/timeout, other -> ERROR.
        # We prefer the workdir `status` file when present; the exit
        # code is the fallback signal when sby crashed before writing.
        if status == "PASS" or (status is None and proc.returncode == 0):
            result = FpvPassResults(
                name=cfg.get_name(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
                engines=cfg.get_engines(),
                runtime_s=round(runtime_s, 2),
                per_engine=per_engine,
            )
            self._merge_extras(result, vacuity=vacuity, coi=coi)
            return result

        if status == "FAIL":
            result = FpvFailResults(
                name=cfg.get_name(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
                engines=cfg.get_engines(),
                runtime_s=round(runtime_s, 2),
                desc=self._counterexample_desc(workdir),
                per_engine=per_engine,
            )
            self._merge_extras(result, vacuity=vacuity, coi=coi)
            return result

        desc_status = status or f"sby exit code {proc.returncode}"
        result = FpvFailResults(
            name=cfg.get_name(),
            mode=cfg.get_mode(),
            depth=cfg.get_depth(),
            engines=cfg.get_engines(),
            runtime_s=round(runtime_s, 2),
            desc=f"sby reported {desc_status} (see {log_path})",
            per_engine=per_engine,
        )
        self._merge_extras(result, vacuity=vacuity, coi=coi)
        return result

    # --- vacuity pass -------------------------------------------------------

    def _run_vacuity(
        self,
        executable: str,
        sources: list[str],
        incdirs: list[str],
    ) -> dict | None:
        """Run a secondary sby cover-mode pass for `|->` antecedents.

        Returns the structured vacuity summary (`candidates`, per-cover
        reachability, `vacuous` count) or None when there's nothing to
        check or sby couldn't run.
        """
        cfg = self.fpv_cfg
        candidates: list[VacuityCandidate] = extract_candidates(cfg.get_properties())
        if not candidates:
            log_event(
                logger,
                logging.DEBUG,
                "fpv.vacuity_skip_no_implications",
                verification=cfg.get_name(),
            )
            return None

        # Bind the synthesized covers into the DUT so they see clk /
        # rst_n / signal ports by name — needed for slang (which does
        # not infer free identifiers) and harmless for the verilog
        # frontend.
        vacuity_sv = write_vacuity_module(
            candidates,
            self._vacuity_sv_path(),
            bind_to=cfg.get_top(),
        )
        sby_path = self._render_sby(
            output_path=self._vacuity_sby_path(),
            sources=sources,
            incdirs=incdirs,
            mode="cover",
            extra_property_files=[vacuity_sv],
        )
        workdir = self._vacuity_workdir_path()
        log_path = self._vacuity_log_path()
        cmd = [executable, "-f", "-d", workdir, sby_path]

        log_event(
            logger,
            logging.INFO,
            "fpv.vacuity_start",
            verification=cfg.get_name(),
            candidates=len(candidates),
        )
        self._run(cmd, log_path)

        log_text = Path(log_path).read_text() if os.path.isfile(log_path) else ""
        # sby cover mode also writes per-engine output into the workdir
        # logfile; fall back to that when the user-facing log is empty.
        if not log_text:
            wd_log = os.path.join(workdir, "logfile.txt")
            if os.path.isfile(wd_log):
                log_text = Path(wd_log).read_text()

        reachable = parse_vacuity_log(log_text)
        results: list[dict] = []
        vacuous_count = 0
        for index, c in enumerate(candidates, start=1):
            cover_name = c.cover_name(index)
            # Default to "unknown" when sby's output didn't tag this
            # cover either way — surfaces honestly rather than guessing.
            status: str
            if cover_name in reachable:
                status = "reachable" if reachable[cover_name] else "unreachable"
            else:
                status = "unknown"
            if status == "unreachable":
                vacuous_count += 1
            results.append(
                {
                    "cover": cover_name,
                    "label": c.label,
                    "source": f"{os.path.relpath(c.source_file, self.artefact_dir)}:{c.source_line}",
                    "operator": c.operator,
                    "antecedent": c.antecedent,
                    "status": status,
                }
            )

        if vacuous_count:
            log_event(
                logger,
                logging.WARNING,
                "fpv.vacuity_unreached",
                verification=cfg.get_name(),
                vacuous=vacuous_count,
                total=len(candidates),
            )

        return {
            "candidates": len(candidates),
            "vacuous": vacuous_count,
            "covers": results,
            "log": log_path,
        }

    @staticmethod
    def _merge_extras(
        result: FpvResults,
        *,
        vacuity: dict | None,
        coi: dict | None,
    ) -> None:
        if vacuity is not None:
            result.results["vacuity"] = vacuity
        if coi is not None:
            result.results["coi"] = coi

    # --- result helpers -----------------------------------------------------

    @staticmethod
    def _read_status(workdir: str) -> str | None:
        """Return the contents of ``<workdir>/status`` if present.

        Sby writes one of ``PASS``, ``FAIL``, ``UNKNOWN``, or ``ERROR``
        to that file after each run. The file is missing when sby died
        before completing setup.
        """
        path = os.path.join(workdir, "status")
        if not os.path.isfile(path):
            return None
        text = Path(path).read_text().strip()
        # Status lines look like "PASS" or "PASS (engine_0)" — keep the
        # first whitespace-delimited token.
        return text.split()[0] if text else None

    @staticmethod
    def _read_per_engine(workdir: str) -> list[dict]:
        """Parse per-engine status from ``<workdir>/logfile.txt``.

        Returns an empty list when the logfile is missing (sby died
        before producing one) — callers treat that as "no engine data
        available" and surface the overall verdict only.
        """
        from .fpv_log_parse import parse_engine_summary, read_workdir_log

        log_text = read_workdir_log(workdir)
        if log_text is None:
            return []
        return parse_engine_summary(log_text)

    @staticmethod
    def _counterexample_desc(workdir: str) -> str:
        """Compose a short failure description pointing at the trace dir."""
        engine_dir = None
        for entry in sorted(os.listdir(workdir)) if os.path.isdir(workdir) else []:
            if entry.startswith("engine_") and os.path.isdir(
                os.path.join(workdir, entry)
            ):
                engine_dir = entry
                break
        if engine_dir is None:
            return "property disproved (no counterexample dir)"
        trace = os.path.join(workdir, engine_dir, "trace.vcd")
        if os.path.isfile(trace):
            return f"property disproved (counterexample: {trace})"
        return f"property disproved (engine dir: {os.path.join(workdir, engine_dir)})"

    # --- subprocess helper --------------------------------------------------

    def _run(self, cmd: list[str], log_path: str):
        with open(log_path, "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            return run_managed_process(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=self.artefact_dir,
            )
