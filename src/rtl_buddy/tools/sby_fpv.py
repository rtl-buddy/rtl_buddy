"""SymbiYosys (``sby``) wrapper for ``rb fpv``.

Writes ``fpv.sby`` from the run's :class:`FpvConfig`, runs ``sby -f -d <workdir>``, and fills
:class:`FpvResults` from the workdir ``status`` file and the exit code.
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
from .fpv_coi import render_chparam, render_slang_read, run_coi_analysis
from .synth_yosys import SLANG_PLUGIN_ENV, resolve_plugin_path


# Oldest sby version tested against.
MIN_SBY_VERSION = "0.40"


_INCDIR_PREFIX = "+incdir+"
_LIBEXT_PREFIX = "+libext+"
_DEFINE_PREFIX = "+define+"
_SOURCE_OPT_PREFIX = "-v "
_FILELIST_SKIP_PREFIXES = ("-y ", "-F ", "-f ")

# rtl-buddy owns `FORMAL`. A user `+define+FORMAL=...` is dropped with a warning because the two
# frontends resolve duplicate -D differently (verilog: last wins, slang: first wins).
_RESERVED_DEFINE_NAMES = ("FORMAL",)


# Every formal-cell flavor: modern yosys folds them into `$check`, older yosys emits `$assert` etc.
_FORMAL_CELL_SELECTOR = "t:$assert t:$assume t:$cover t:$live t:$check"

# Stable leading phrase of the yosys `select -assert-min 1` empty-selection message.
_EMPTY_FORMAL_SELECTION_MARKER = "selection contains 0 elements"


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

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.f")

    def _sby_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.sby")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.log")

    def _workdir_path(self) -> str:
        # `-f` makes sby overwrite the workdir on rerun.
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
        """Resolve the yosys plugin path via :func:`tools.synth_yosys.resolve_plugin_path`."""
        return resolve_plugin_path(plugin_path, self.root_cfg)

    def _coi_script_path(self) -> str:
        return os.path.join(self.artefact_dir, "coi.ys")

    def _coi_log_path(self) -> str:
        return os.path.join(self.artefact_dir, "coi.log")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.fpv_cfg.get_model(),
            output_path=fl_path,
        )
        # strip=False keeps the +incdir+/+define+/-v markers that _parse_filelist dispatches on.
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
        )
        return fl_path

    def _parse_filelist(self, fl_path: str) -> tuple[list[str], list[str], list[str]]:
        """Return (source paths, include dirs, defines) from the model filelist.

        Defines are raw ``NAME[=VALUE]`` tokens. These are dropped with a warning, so the proof,
        the vacuity pass and the COI walk see the same set:

        * names in `_RESERVED_DEFINE_NAMES`;
        * tokens containing whitespace, which cannot be quoted into a yosys script line;
        * earlier definitions of a repeated name (last wins).
        """
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        sources: list[str] = []
        incdirs: list[str] = []
        defines: list[str] = []
        with open(fl_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("//"):
                    continue
                if line.startswith(_INCDIR_PREFIX):
                    inc = line[len(_INCDIR_PREFIX) :]
                    incdirs.append(os.path.normpath(os.path.join(fl_dir, inc)))
                    continue
                if line.startswith(_DEFINE_PREFIX):
                    # Split `+define+A+B=C` again for filelists read without VlogFilelist.
                    for token in line[len(_DEFINE_PREFIX) :].split("+"):
                        if not token:
                            continue
                        name = token.split("=", 1)[0]
                        if name in _RESERVED_DEFINE_NAMES:
                            log_event(
                                logger,
                                logging.WARNING,
                                "fpv.filelist_define_reserved",
                                verification=self.fpv_cfg.get_name(),
                                define=token,
                                name=name,
                            )
                            continue
                        if token.split() != [token]:
                            log_event(
                                logger,
                                logging.WARNING,
                                "fpv.filelist_define_unquotable",
                                verification=self.fpv_cfg.get_name(),
                                define=token,
                                name=name,
                            )
                            continue
                        defines.append(token)
                    continue
                if line.startswith(_LIBEXT_PREFIX):
                    continue
                if any(line.startswith(opt) for opt in _FILELIST_SKIP_PREFIXES):
                    continue
                if line.startswith(_SOURCE_OPT_PREFIX):
                    line = line[len(_SOURCE_OPT_PREFIX) :]
                sources.append(os.path.normpath(os.path.join(fl_dir, line)))
        return sources, incdirs, self._last_definition_wins(defines)

    def _last_definition_wins(self, defines: list[str]) -> list[str]:
        """Keep only the last definition of each repeated define name, warning on each.

        Order is preserved, with each name at the position of its first appearance.
        """
        latest: dict[str, str] = {}
        order: list[str] = []
        for token in defines:
            name = token.split("=", 1)[0]
            if name in latest:
                if latest[name] != token:
                    log_event(
                        logger,
                        logging.WARNING,
                        "fpv.filelist_define_redefined",
                        verification=self.fpv_cfg.get_name(),
                        name=name,
                        dropped=latest[name],
                        kept=token,
                    )
            else:
                order.append(name)
            latest[name] = token
        return [latest[name] for name in order]

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
        m = re.search(r"sby\s+(\S+)", out)
        return m.group(1) if m else None

    def _write_sby_file(
        self,
        sources: list[str],
        incdirs: list[str],
        defines: list[str] | None = None,
    ) -> str:
        """Render the ``fpv.sby`` config that sby consumes."""
        return self._render_sby(
            output_path=self._sby_path(),
            sources=sources,
            incdirs=incdirs,
            defines=defines or [],
            mode=self.fpv_cfg.get_mode(),
            extra_property_files=[],
            # Only bind-based `properties:` suites are guarded against a vacuous PASS.
            emit_formal_guard=bool(self.fpv_cfg.get_properties()),
        )

    def _render_sby(
        self,
        *,
        output_path: str,
        sources: list[str],
        incdirs: list[str],
        mode: str,
        extra_property_files: list[str],
        defines: list[str] | None = None,
        emit_formal_guard: bool = False,
        engines: list[str] | None = None,
    ) -> str:
        cfg = self.fpv_cfg
        opts = self.tool_cfg.get_opts(
            cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        lines: list[str] = []

        lines.append("[options]")
        lines.append(f"mode {mode}")
        lines.append(f"depth {cfg.get_depth()}")
        if opts.timeout is not None:
            lines.append(f"timeout {opts.timeout}")
        lines.append("")

        lines.append("[engines]")
        for engine in engines if engines is not None else cfg.get_engines():
            lines.append(engine)
        lines.append("")

        # Order matters: sources, then constraints (assumes), then properties (asserts).
        lines.append("[script]")
        frontend = cfg.get_frontend()
        if frontend == "slang":
            plugin = self._resolve_plugin_path(opts.plugin_path)
            if not plugin:
                raise FatalRtlBuddyError(
                    f"{cfg.get_name()}: fpv frontend=slang requires "
                    f"`cfg-fpv-tools[].opts.plugin-path` (or the "
                    f"{SLANG_PLUGIN_ENV} environment variable) to point "
                    f"at the built yosys-slang shared library"
                )
            # Emit `plugin -i` only for slang so the verilog path stays plugin-free.
            lines.append(f"plugin -i {plugin}")
        # slang ignores verilog_defaults -I; render_slang_read carries incdirs on the read_slang line.
        defines = list(defines or [])
        # The proof, vacuity pass and COI walk must all receive the same parameter overrides.
        params = cfg.get_param_tokens()
        if frontend != "slang":
            for inc in incdirs:
                lines.append(f"verilog_defaults -add -I {inc}")
            # `read -formal` already defines FORMAL.
            for define in defines:
                lines.append(f"verilog_defaults -add -D{define}")
        constraints = cfg.get_constraints()
        constraint_files = [constraints] if constraints else []
        all_sources = (
            list(sources)
            + constraint_files
            + list(cfg.get_properties())
            + list(extra_property_files)
        )
        if frontend == "slang":
            # One read_slang command covers the whole filelist; the COI walk shares render_slang_read.
            # Basenames: files are copied into the sby workdir under [files].
            slang_sources = [os.path.basename(s) for s in all_sources]
            lines.append(
                render_slang_read(
                    cfg.get_top(), incdirs, slang_sources, defines, params
                )
            )
        else:
            for src in all_sources:
                lines.append(f"read -sv -formal {os.path.basename(src)}")
            # chparam must come before `prep`; slang has no chparam and takes overrides as `-G`.
            lines.extend(render_chparam(cfg.get_top(), params))
        lines.append(f"prep -top {cfg.get_top()}")
        if emit_formal_guard:
            # Fail on a vacuous proof: zero formal cells means an unresolved `bind` was dropped.
            # The clear restores the full selection for sby's engine passes.
            lines.append(f"select -assert-min 1 {_FORMAL_CELL_SELECTOR}")
            lines.append("select -clear")
        lines.append("")

        lines.append("[files]")
        for src in all_sources:
            lines.append(src)
        lines.append("")

        with open(output_path, "w") as f:
            f.write("\n".join(lines))
        return output_path

    def run(self) -> FpvResults:
        cfg = self.fpv_cfg

        fl_path = self._write_filelist()
        sources, incdirs, defines = self._parse_filelist(fl_path)
        if not sources and not cfg.get_properties():
            raise FatalRtlBuddyError(
                f"{cfg.get_name()}: filelist {fl_path} produced no sources and "
                f"no `properties:` entries are listed"
            )

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
            from .fpv_solver_pin import check_solver_pins

            resolved = check_solver_pins(opts.solver_versions)
            log_event(
                logger,
                logging.INFO,
                "fpv.solver_pins_resolved",
                verification=cfg.get_name(),
                resolved=resolved,
            )

        sby_path = self._write_sby_file(sources, incdirs, defines)
        log_path = self._log_path()
        workdir = self._workdir_path()
        executable = self.tool_cfg.get_executable() or "sby"

        version = self._probe_sby_version(executable)
        if version is not None:
            log_event(
                logger,
                logging.INFO,
                "fpv.sby_version",
                tool=executable,
                version=version,
            )

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
                params=cfg.get_params() or None,
            )
            start = time.monotonic()
            proc = self._run(cmd, log_path)
            runtime_s = time.monotonic() - start

        status = self._read_status(workdir)
        per_engine = self._read_per_engine(workdir)

        # Secondary cover pass for `|->` antecedents; only after a passing primary proof.
        vacuity = None
        if cfg.vacuity_enabled() and (
            status == "PASS" or (status is None and proc.returncode == 0)
        ):
            vacuity = self._run_vacuity(executable, sources, incdirs, defines)

        # The COI walk runs whatever the primary verdict.
        coi = None
        if cfg.coi_enabled():
            # The COI walk must use the proof frontend; mixing frontends gives inconsistent `$check` cells.
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
                defines=defines,
                params=cfg.get_param_tokens(),
            )

        # Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN/timeout, other ERROR. The status file wins when present.
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
        hint = self._vacuous_guard_hint(log_path, workdir)
        result = FpvFailResults(
            name=cfg.get_name(),
            mode=cfg.get_mode(),
            depth=cfg.get_depth(),
            engines=cfg.get_engines(),
            runtime_s=round(runtime_s, 2),
            desc=f"sby reported {desc_status} (see {log_path}){hint}",
            per_engine=per_engine,
            # No verdict was reached, so an xfail marker has nothing to excuse.
            fail_stage="tool",
        )
        self._merge_extras(result, vacuity=vacuity, coi=coi)
        return result

    def _vacuous_guard_hint(self, log_path: str, workdir: str) -> str:
        """Return a hint when the formal-cell guard (``select -assert-min 1``) tripped, else "".

        The usual cause is a compilation-unit-scope `bind` dropped by the verilog frontend.
        """
        texts: list[str] = []
        if os.path.isfile(log_path):
            texts.append(Path(log_path).read_text())
        wd_log = os.path.join(workdir, "logfile.txt")
        if os.path.isfile(wd_log):
            texts.append(Path(wd_log).read_text())
        if not any(_EMPTY_FORMAL_SELECTION_MARKER in t for t in texts):
            return ""
        hint = (
            " — zero formal cells elaborated: the property set produced no "
            "assert/assume/cover cells, so the proof would otherwise have "
            "passed vacuously"
        )
        if self.fpv_cfg.get_frontend() != "slang":
            hint += (
                f" (frontend={self.fpv_cfg.get_frontend()!r} cannot resolve a "
                f"compilation-unit-scope `bind`; set `frontend: slang` for "
                f"bind-based property modules)"
            )
        return hint

    def _run_vacuity(
        self,
        executable: str,
        sources: list[str],
        incdirs: list[str],
        defines: list[str] | None = None,
    ) -> dict | None:
        """Run a secondary sby cover pass for `|->` antecedents.

        Returns the vacuity summary, or None if there is nothing to check or sby could not run.
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

        # Bind the covers into the DUT so they see clk/rst_n/ports by name; slang needs this.
        vacuity_sv = write_vacuity_module(
            candidates,
            self._vacuity_sv_path(),
            bind_to=cfg.get_top(),
        )
        sby_path = self._render_sby(
            output_path=self._vacuity_sby_path(),
            sources=sources,
            incdirs=incdirs,
            defines=defines,
            mode="cover",
            extra_property_files=[vacuity_sv],
            engines=cfg.get_vacuity_engines(),
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
            engines=",".join(cfg.get_vacuity_engines()),
        )
        self._run(cmd, log_path)

        log_text = Path(log_path).read_text() if os.path.isfile(log_path) else ""
        # Fall back to the workdir logfile when the user-facing log is empty.
        if not log_text:
            wd_log = os.path.join(workdir, "logfile.txt")
            if os.path.isfile(wd_log):
                log_text = Path(wd_log).read_text()

        reachable = parse_vacuity_log(log_text)
        results: list[dict] = []
        vacuous_count = 0
        for index, c in enumerate(candidates, start=1):
            cover_name = c.cover_name(index)
            # Untagged covers stay "unknown" rather than being guessed.
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

    @staticmethod
    def _read_status(workdir: str) -> str | None:
        """Return ``<workdir>/status`` (PASS, FAIL, UNKNOWN or ERROR), or None if sby died before writing it."""
        path = os.path.join(workdir, "status")
        if not os.path.isfile(path):
            return None
        text = Path(path).read_text().strip()
        return text.split()[0] if text else None

    @staticmethod
    def _read_per_engine(workdir: str) -> list[dict]:
        """Parse per-engine status from ``<workdir>/logfile.txt``; return [] if the logfile is missing."""
        from .fpv_log_parse import parse_engine_summary, read_workdir_log

        log_text = read_workdir_log(workdir)
        if log_text is None:
            return []
        return parse_engine_summary(log_text)

    @staticmethod
    def _counterexample_desc(workdir: str) -> str:
        """Return a short failure description pointing at the trace dir."""
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
