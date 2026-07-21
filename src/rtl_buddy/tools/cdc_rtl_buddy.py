"""rtl-buddy-cdc tool wrapper.

Drives the standalone ``rtl-buddy-cdc lint`` CLI: hands it the model's
filelist of SystemVerilog sources, the SDC, and an optional waiver
file, then parses the JSON report it emits to populate
:class:`CdcResults`. Keeping the integration at subprocess granularity
means rtl_buddy isn't tied to the analyzer's Python API and can pick
up new releases via ``uv sync`` without code changes here.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from ..config.cdc import CdcConfig, CdcToolConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.cdc_results import CdcFailResults, CdcPassResults, CdcResults


_FILELIST_SKIP_PREFIXES = ("+incdir+", "+libext+", "-y ", "-F ", "-f ")
_FILELIST_SOURCE_PREFIX = "-v "


@functools.lru_cache(maxsize=None)
def _lint_supports_project_root(executable: str) -> bool:
    """Whether ``<executable> lint`` accepts ``--project-root`` (rtl-buddy-cdc#245).

    The analyzer is resolved off PATH / the tool config and is *not*
    pinned by rtl_buddy, and its ``version`` command reports a static
    string — so the only reliable capability signal is the ``--help``
    surface. We probe once per executable (cached) and degrade to
    ``False`` on any failure (missing binary, timeout, non-zero exit), so
    an older analyzer that predates the flag keeps working instead of
    hard-failing on an unknown option.
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
        # When set, additionally request the structured clock-domain and
        # reset-domain maps (the inputs for `rb cdc --emit-constraints`, #291).
        self.emit_maps = emit_maps
        # The cdc.yaml's directory (see ``_do_cdc_suite``). Used as the
        # analyzer's ``--project-root`` so relative paths in a config's
        # ``extra_args`` resolve against the config — not against the
        # nested artefact cwd we run the subprocess from (#245).
        self.suite_dir = suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / cdc_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # --- artefact paths -----------------------------------------------------

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
        """Return the (domain_map, reset_map) dicts produced by an
        ``emit_maps`` run, or ``(None, None)`` if they were not written."""

        def _load(path):
            try:
                return json.loads(Path(path).read_text())
            except (OSError, json.JSONDecodeError):
                return None

        return _load(self._domain_map_path()), _load(self._reset_map_path())

    def read_report(self) -> dict:
        """Return the parsed cdc.json report (with ``violations`` /
        ``suppressed`` / ``crossings``), or ``{}`` if not produced."""
        try:
            return json.loads(Path(self._report_path("json")).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    # --- helpers ------------------------------------------------------------

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
        """Return absolute source file paths from a stripped filelist.

        Mirrors the helper in :mod:`tools.synth_yosys` rather than
        importing it, because the synth tool's helper is private. If we
        grow more tool wrappers that need this, factor it out.
        """
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

    # --- run ----------------------------------------------------------------

    def run(self) -> CdcResults:
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

        # Always emit JSON so we can parse violation counts; also keep a
        # human-readable text report alongside for the user.
        json_report = self._report_path("json")
        text_report = self._report_path("txt")
        log_path = self._log_path()

        executable = self.tool_cfg.get_executable() or "rtl-buddy-cdc"
        opts = self.tool_cfg.get_opts(
            self.cdc_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        # Anchor the analyzer's relative path args (chiefly any in
        # ``extra_args`` — `--yosys-plugin` / `--emit-*`) to the cdc.yaml
        # dir, matching how `constraints:` / `waivers:` already resolve
        # (#245). Skipped (with a debug note) when the installed analyzer
        # predates the flag, so we never hard-fail an older tool.
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
            for module in self.cdc_cfg.blackbox:
                # Repeated `--blackbox <module>` (rtl-buddy-cdc#259). An
                # empty list adds nothing.
                cmd += ["--blackbox", module]
            if self.emit_maps:
                cmd += [
                    "--emit-domain-map",
                    self._domain_map_path(),
                    "--emit-reset-domain-map",
                    self._reset_map_path(),
                ]
            if opts.extra_args:
                # After project_root_args so a config can still override
                # the anchor in its own extra_args if it must.
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
            # Run twice: once for human-readable text, once for JSON we
            # parse below. Both invocations elaborate the design; if
            # this becomes a hotspot, switch to running once with JSON
            # and rendering the text from the parsed payload.
            text_proc = self._run(cmd_text, log_path)
            json_proc = self._run(cmd_json, log_path, append=True)

        # Either invocation succeeding (exit 0) or returning the rule-
        # violation exit code (1) is a successful run; anything else
        # (typically 2 = elaboration failure) is a hard fail.
        for proc in (text_proc, json_proc):
            if proc.returncode not in (0, 1):
                return CdcFailResults(
                    name=self.cdc_cfg.get_name(),
                    violations=0,
                    desc=(
                        f"rtl-buddy-cdc exited with code {proc.returncode} "
                        f"(see {log_path})"
                    ),
                )

        if not os.path.isfile(json_report):
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"no JSON report produced (see {log_path})",
            )

        try:
            payload = json.loads(Path(json_report).read_text())
        except json.JSONDecodeError as e:
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"could not parse JSON report: {e}",
            )

        summary = payload.get("summary", {})
        violations = int(summary.get("violations", 0))
        suppressed = int(summary.get("suppressed", 0))
        crossings = summary.get("crossings")
        crossings = int(crossings) if crossings is not None else None

        # Best-effort hub publish. When a hub is running for this
        # project, push the violations as a `diagnostics_set` event so
        # the SPA's badge layer + nvim diagnostics namespace light up
        # immediately. Silently no-ops when no hub is reachable, and
        # is wrapped in a broad except so a sidecar UI bug can never
        # fail the CDC analysis itself.
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
