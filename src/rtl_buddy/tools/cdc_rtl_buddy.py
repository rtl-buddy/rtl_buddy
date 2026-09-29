"""Wrapper around the standalone ``rtl-buddy-cdc lint`` CLI.

Passes it the model's sources, the SDC and an optional waiver file, and parses its JSON
report into :class:`CdcResults`.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .artifact_paths import clear_stale_artefacts
from .vlog_filelist import VlogFilelist, incdirs_from_filelist
from ..config.cdc import CdcConfig, CdcToolConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.cdc_results import (
    CdcFailResults,
    CdcPassResults,
    CdcResults,
    CdcSkipResults,
)


_FILELIST_SKIP_PREFIXES = ("+incdir+", "+libext+", "+define+", "-y ", "-F ", "-f ")


def warn_unsupported_incdirs(analysis: str, fl_path: str) -> None:
    """Warn that filelist ``+incdir+`` entries are not passed to rtl-buddy-cdc, which has no include-path option."""
    incdirs = incdirs_from_filelist(fl_path)
    if not incdirs:
        return
    log_event(
        logger,
        logging.WARNING,
        "cdc.filelist_incdirs_unsupported",
        analysis=analysis,
        incdirs=incdirs,
        count=len(incdirs),
    )


_FILELIST_SOURCE_PREFIX = "-v "


@functools.lru_cache(maxsize=None)
def _lint_supports_project_root(executable: str) -> bool:
    """Whether ``<executable> lint --help`` lists ``--project-root``; False on any probe failure.

    The analyzer version is not pinned and its ``version`` output is static, so
    ``--help`` is the only capability signal.
    """
    try:
        proc = subprocess.run(
            [executable, "lint", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--project-root" in (proc.stdout + proc.stderr)


@functools.lru_cache(maxsize=None)
def _lint_supports_single_unit(executable: str) -> bool:
    """Whether ``<executable> lint --help`` lists ``--single-unit``; False on any probe failure."""
    try:
        proc = subprocess.run(
            [executable, "lint", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--single-unit" in (proc.stdout + proc.stderr)


class RtlBuddyCdc:
    def __init__(
        self,
        name: str,
        cdc_cfg: CdcConfig,
        tool_cfg: CdcToolConfig,
        suite_dir: str,
        root_cfg=None,
        emit_maps: bool = False,
    ):
        self.name = name
        self.cdc_cfg = cdc_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg
        # Also request the domain and reset maps that `rb cdc --emit-constraints` reads.
        self.emit_maps = emit_maps
        # The cdc.yaml directory; passed as ``--project-root`` so relative ``extra_args`` paths
        # resolve against it, not the artefact cwd.
        self.suite_dir = suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / cdc_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "cdc.f")

    def _report_path(self, fmt: str) -> str:
        return os.path.join(self.artefact_dir, f"cdc.{fmt}")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "cdc.log")

    def _domain_map_path(self) -> str:
        return os.path.join(self.artefact_dir, "domain_map.json")

    def _reset_map_path(self) -> str:
        return os.path.join(self.artefact_dir, "reset_map.json")

    def read_emitted_maps(self) -> tuple[dict | None, dict | None]:
        """Return the (domain_map, reset_map) dicts from an ``emit_maps`` run; None for a missing map."""

        def _load(path):
            try:
                return json.loads(Path(path).read_text())
            except (OSError, json.JSONDecodeError):
                return None

        return _load(self._domain_map_path()), _load(self._reset_map_path())

    def read_report(self) -> dict:
        """Return the parsed cdc.json report, or ``{}`` if it was not produced."""
        try:
            return json.loads(Path(self._report_path("json")).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.cdc_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
        )
        return fl_path

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        """Return absolute source file paths from a stripped filelist."""
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        paths: list[str] = []
        with open(fl_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("//"):
                    continue
                if any(line.startswith(opt) for opt in _FILELIST_SKIP_PREFIXES):
                    continue
                if line.startswith(_FILELIST_SOURCE_PREFIX):
                    line = line[len(_FILELIST_SOURCE_PREFIX) :]
                paths.append(os.path.normpath(os.path.join(fl_dir, line)))
        return paths

    def _clear_stale_outputs(self) -> list[str]:
        """Delete the reports and domain maps written by a previous run and return the removed paths.

        Exit 1 means "violations found", so a crash exiting 1 would otherwise be read
        against a stale ``cdc.json``. The maps are cleared even when this run does not
        emit them, because ``--emit-constraints`` and ``--check-xdc`` read them from
        fixed paths. ``cdc.log`` is truncated by :meth:`_run`.
        """
        return clear_stale_artefacts(
            [
                self._report_path("json"),
                self._report_path("txt"),
                self._domain_map_path(),
                self._reset_map_path(),
            ],
            owner=self.cdc_cfg.get_name(),
        )

    def _fail_after_analyzer(self, desc: str) -> CdcFailResults:
        """Clear the analyzer's outputs and return a tool-stage FAIL.

        A failed analyzer run can leave a partial ``cdc.json`` or maps behind.
        """
        self._clear_stale_outputs()
        # fail_stage="tool": an xfail marker does not excuse an analyzer failure.
        return CdcFailResults(
            name=self.cdc_cfg.get_name(), violations=0, desc=desc, fail_stage="tool"
        )

    def run(self) -> CdcResults:
        # Validate the config before checking for the analyzer, so a broken analysis is not
        # reported as "analyzer not installed". A config error clears the previous outputs.
        try:
            fl_path = self._write_filelist()
            sources = self._source_files_from_filelist(fl_path)
            if not sources:
                raise FatalRtlBuddyError(
                    f"{self.cdc_cfg.get_name()}: filelist {fl_path} produced no sources"
                )

            sdc_path = self.cdc_cfg.get_constraints()
            if not os.path.isfile(sdc_path):
                raise FatalRtlBuddyError(
                    f"{self.cdc_cfg.get_name()}: SDC not found: {sdc_path}"
                )
            waivers_path = self.cdc_cfg.get_waivers()
            if waivers_path is not None and not os.path.isfile(waivers_path):
                raise FatalRtlBuddyError(
                    f"{self.cdc_cfg.get_name()}: waivers file not found: {waivers_path}"
                )
        except Exception:
            # Catch everything: `_write_filelist` raises FilelistError, which is not a
            # FatalRtlBuddyError. Re-raised, so nothing is masked.
            self._clear_stale_outputs()
            raise

        executable = self.tool_cfg.get_executable() or "rtl-buddy-cdc"
        if not shutil.which(executable):
            # Skip before clearing: a host without the analyzer must not delete reports.
            log_event(
                logger,
                logging.WARNING,
                "cdc.no_analyzer",
                analysis=self.cdc_cfg.get_name(),
                exe=executable,
            )
            return CdcSkipResults(
                name=self.cdc_cfg.get_name(),
                desc=(
                    f"{executable!r} not found — run "
                    "`rb tool-check --explain rtl-buddy-cdc` for install "
                    "instructions"
                ),
            )

        warn_unsupported_incdirs(self.cdc_cfg.get_name(), fl_path)

        stale = self._clear_stale_outputs()
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "cdc.stale_artefacts_removed",
                analysis=self.cdc_cfg.get_name(),
                paths=stale,
            )

        # JSON is parsed for the verdict; the text report is for the user.
        json_report = self._report_path("json")
        text_report = self._report_path("txt")
        log_path = self._log_path()

        opts = self.tool_cfg.get_opts(
            self.cdc_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        if self.cdc_cfg.single_unit and not _lint_supports_single_unit(executable):
            raise FatalRtlBuddyError(
                "CDC analysis "
                f"'{self.cdc_cfg.get_name()}' sets single_unit: true, but "
                f"'{executable} lint --help' does not advertise --single-unit; "
                "install an rtl-buddy-cdc build containing rtl-buddy-cdc#277"
            )

        # Older analyzers lack --project-root; run without it.
        if _lint_supports_project_root(executable):
            project_root_args = ["--project-root", self.suite_dir]
        else:
            project_root_args = []
            log_event(
                logger,
                logging.DEBUG,
                "cdc.project_root.unsupported",
                analysis=self.cdc_cfg.get_name(),
                tool=executable,
            )

        def _build_cmd(fmt: str, report: str) -> list[str]:
            cmd = [
                executable,
                "lint",
                "--top",
                self.cdc_cfg.get_top(),
                "--sdc",
                sdc_path,
                "--format",
                fmt,
                "--output",
                report,
                *project_root_args,
            ]
            if waivers_path is not None:
                cmd += ["--waivers", waivers_path]
            if opts.sync_depth is not None:
                cmd += ["--sync-depth", str(opts.sync_depth)]
            if self.cdc_cfg.frontend is not None:
                cmd += ["--frontend", self.cdc_cfg.frontend]
            if self.cdc_cfg.single_unit:
                cmd.append("--single-unit")
            for module in self.cdc_cfg.blackbox:
                cmd += ["--blackbox", module]
            if self.emit_maps:
                cmd += [
                    "--emit-domain-map",
                    self._domain_map_path(),
                    "--emit-reset-domain-map",
                    self._reset_map_path(),
                ]
            if opts.extra_args:
                # After project_root_args, so extra_args can override it.
                cmd += opts.extra_args.split()
            cmd += sources
            return cmd

        cmd_text = _build_cmd("text", text_report)
        cmd_json = _build_cmd("json", json_report)

        with task_status(f"Running CDC {self.cdc_cfg.get_name()}"):
            log_event(
                logger,
                logging.INFO,
                "cdc.start",
                analysis=self.cdc_cfg.get_name(),
                tool=executable,
                top=self.cdc_cfg.get_top(),
            )
            # Two invocations, so the design is elaborated twice.
            text_proc = self._run(cmd_text, log_path)
            json_proc = self._run(cmd_json, log_path, append=True)

        # Exit 0 (clean) and 1 (violations) are successful runs; 2 is an elaboration failure.
        for proc in (text_proc, json_proc):
            if proc.returncode not in (0, 1):
                return self._fail_after_analyzer(
                    f"rtl-buddy-cdc exited with code {proc.returncode} (see {log_path})"
                )

        if not os.path.isfile(json_report):
            return self._fail_after_analyzer(
                f"no JSON report produced (see {log_path})"
            )

        try:
            payload = json.loads(Path(json_report).read_text())
            # Shape checks stay inside this guard so a malformed report goes through
            # _fail_after_analyzer.
            if not isinstance(payload, dict):
                raise ValueError(
                    f"expected a JSON object at the top level, got "
                    f"{type(payload).__name__}"
                )
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                raise ValueError(
                    f"expected an object for 'summary', got {type(summary).__name__}"
                )
            violations = int(summary.get("violations", 0))
            suppressed = int(summary.get("suppressed", 0))
            crossings = summary.get("crossings")
            crossings = int(crossings) if crossings is not None else None
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
            return self._fail_after_analyzer(f"could not parse JSON report: {e}")

        # Best-effort hub publish; a hub bug must never fail the analysis.
        try:
            from .cdc_publisher import publish_cdc_report

            publish_cdc_report(
                analysis_name=self.cdc_cfg.get_name(),
                json_report_path=json_report,
            )
        except Exception:  # noqa: BLE001 — best-effort side effect
            logger.debug("cdc.publish.unexpected_error", exc_info=True)

        if violations == 0:
            return CdcPassResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                suppressed=suppressed,
                crossings=crossings,
            )
        return CdcFailResults(
            name=self.cdc_cfg.get_name(),
            violations=violations,
            suppressed=suppressed,
            crossings=crossings,
        )

    def _run(self, cmd: list[str], log_path: str, *, append: bool = False):
        mode = "a" if append else "w"
        with open(log_path, mode) as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            return run_managed_process(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=self.artefact_dir,
            )
