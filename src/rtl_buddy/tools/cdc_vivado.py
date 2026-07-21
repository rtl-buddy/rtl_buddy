"""Vivado ``report_cdc`` backend for ``rb cdc`` (#287).

A second-opinion CDC backend: elaborate the model with ``synth_design``
(no place/route), run ``report_cdc -details``, and parse the report into
the same :class:`CdcResults` shape the rtl-buddy-cdc backend produces.
Vivado's findings are surfaced verbatim — rule id (``CDC-1`` ...),
severity, and description — tagged with the backend name; rtl_buddy does
NOT adopt Vivado's rule taxonomy as its own canonical ruleset.

Severity mapping to rtl_buddy's pass/fail surface: ``Critical`` and
``Warning`` findings count as violations (a non-zero count is a FAIL),
``Info`` findings are informational only. Every finding, whatever its
severity, rides along in the ``findings`` payload.

The target part for elaboration comes from the ``cfg-cdc-tools`` vivado
entry's ``opts.part`` (overridable per analysis via
``tool_overrides.vivado.part``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from ..config.cdc import CdcConfig, CdcToolConfig
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.cdc_results import (
    CdcFailResults,
    CdcPassResults,
    CdcResults,
    CdcSkipResults,
)

BACKEND_NAME = "vivado"

# Vivado error lines look like `ERROR: [Synth 8-439] module ...` — match
# on the bracketed message-id form (same scan as the rb fpga backend).
_VIVADO_ERROR_RE = re.compile(r"^ERROR: \[")

# Of Vivado's CDC severities (Critical / Warning / Info), the ones
# rtl_buddy counts as violations. Info findings (e.g. CDC-3, a properly
# ASYNC_REG-synchronized crossing) are informational.
_VIOLATION_SEVERITIES = frozenset({"Critical", "Warning"})

_FILELIST_SKIP_PREFIXES = ("+incdir+", "+libext+", "-y ", "-F ", "-f ")
_FILELIST_SOURCE_PREFIX = "-v "


# ---------------------------------------------------------------------------
# report_cdc parsing
# ---------------------------------------------------------------------------


def _column_spans(underline: str) -> list[tuple[int, int]]:
    """Column extents from a ``---  -----  ---`` underline row.

    The last span is open-ended (the description/destination column may
    exceed its dashes when values are wider than the header).
    """
    spans = [(m.start(), m.end()) for m in re.finditer(r"-+", underline)]
    if spans:
        spans[-1] = (spans[-1][0], 10**9)
    return spans


def parse_report_cdc(text: str) -> dict:
    """Parse a Vivado ``report_cdc -details`` report.

    Returns::

        {
          "by_id": {"CDC-1": {"severity", "count", "description"}, ...},
          "findings": [{"id", "severity", "description", "depth",
                        "exception", "source", "destination",
                        "source_clock", "destination_clock"}, ...],
          "crossings": int,        # total summary count
          "violations": int,       # Critical + Warning summary count
        }

    The summary table ("ID  Severity  Count  Description") provides the
    per-rule counts; the per-clock-pair detail tables provide one finding
    per crossing endpoint, kept verbatim. Raises :class:`ValueError`
    when the text is not a Vivado CDC report.
    """
    if "CDC Report" not in text:
        raise ValueError("not a Vivado CDC report")

    lines = text.splitlines()

    def _is_underline(idx: int) -> bool:
        return idx < len(lines) and bool(
            re.fullmatch(r"\s*-+(\s+-+)*\s*", lines[idx]) and "-" in lines[idx]
        )

    by_id: dict[str, dict] = {}
    findings: list[dict] = []
    source_clock: str | None = None
    destination_clock: str | None = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("Source Clock:"):
            source_clock = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Destination Clock:"):
            destination_clock = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("ID") and _is_underline(i + 1):
            # Summary table: ID  Severity  Count  Description
            spans = _column_spans(lines[i + 1])
            j = i + 2
            while j < len(lines) and lines[j].strip():
                row = lines[j]
                cells = [row[a:b].strip() for a, b in spans]
                if len(cells) >= 4 and cells[0]:
                    by_id[cells[0]] = {
                        "severity": cells[1],
                        "count": int(cells[2]) if cells[2].isdigit() else 0,
                        "description": cells[3],
                    }
                j += 1
            i = j
            continue
        elif stripped.startswith("Row") and _is_underline(i + 1):
            # Details table: Row ID Severity Description Depth Exception
            #                Source (From) Destination (To)
            header = lines[i]
            spans = _column_spans(lines[i + 1])
            names = [header[a:b].strip() for a, b in spans]
            j = i + 2
            while j < len(lines) and lines[j].strip():
                row = lines[j]
                cells = dict(
                    zip(names, (row[a:b].strip() for a, b in spans), strict=False)
                )
                if cells.get("ID"):
                    findings.append(
                        {
                            "id": cells.get("ID"),
                            "severity": cells.get("Severity"),
                            "description": cells.get("Description"),
                            "depth": cells.get("Depth"),
                            "exception": cells.get("Exception"),
                            "source": cells.get("Source (From)"),
                            "destination": cells.get("Destination (To)"),
                            "source_clock": source_clock,
                            "destination_clock": destination_clock,
                        }
                    )
                j += 1
            i = j
            continue
        i += 1

    crossings = sum(entry["count"] for entry in by_id.values())
    violations = sum(
        entry["count"]
        for entry in by_id.values()
        if entry["severity"] in _VIOLATION_SEVERITIES
    )
    return {
        "by_id": by_id,
        "findings": findings,
        "crossings": crossings,
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# Tcl script
# ---------------------------------------------------------------------------


def render_cdc_tcl(
    *,
    top: str,
    part: str,
    verilog_sources: list[str],
    sdc_file: str,
    report_file: str = "cdc.rpt",
) -> str:
    """Render the batch-Tcl script for one Vivado CDC analysis.

    Elaboration only — ``synth_design`` then ``report_cdc -details``;
    no place/route. The SDC is read via ``read_xdc`` (``create_clock``
    et al. are valid XDC constraints).
    """
    if not top:
        raise RuntimeError("vivado cdc: top module name is required")
    if not part:
        raise RuntimeError("vivado cdc: part name is required")
    if not verilog_sources:
        raise RuntimeError("vivado cdc: at least one HDL source is required")

    lines = [
        "# Vivado batch CDC analysis -- templated by rb cdc.",
        'puts ">>> Reading sources"',
    ]
    for src in verilog_sources:
        lower = src.lower()
        if lower.endswith(".sv"):
            lines.append(f"read_verilog -sv {src}")
        elif lower.endswith((".vhd", ".vhdl")):
            lines.append(f"read_vhdl {src}")
        else:
            lines.append(f"read_verilog {src}")
    lines += [
        'puts ">>> Reading constraints"',
        f"read_xdc {sdc_file}",
        'puts ">>> Stage: synth"',
        f"synth_design -top {top} -part {part}",
        'puts ">>> Report: cdc"',
        f"report_cdc -details -file {report_file}",
        'puts ">>> DONE"',
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class VivadoCdc:
    """``report_cdc``-driven CDC backend (second opinion, not authority)."""

    def __init__(
        self,
        name: str,
        cdc_cfg: CdcConfig,
        tool_cfg: CdcToolConfig,
        suite_dir: str,
        root_cfg=None,
    ):
        self.name = name
        self.cdc_cfg = cdc_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg
        self.suite_dir = suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / cdc_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # --- artefact paths -----------------------------------------------------

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "cdc.f")

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "cdc.tcl")

    def _report_path(self) -> str:
        return os.path.join(self.artefact_dir, "cdc.rpt")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "vivado.log")

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
        """Return absolute source file paths from a generated filelist."""
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

    def _resolve_part(self) -> str:
        opts = self.tool_cfg.get_opts(
            self.cdc_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )
        if not opts.part:
            raise FatalRtlBuddyError(
                f"{self.cdc_cfg.get_name()}: the vivado CDC backend needs a "
                "device part — set opts.part on the cfg-cdc-tools vivado "
                "entry (or override per analysis via tool_overrides.vivado.part)"
            )
        return opts.part

    # --- run ----------------------------------------------------------------

    def run(self) -> CdcResults:
        # Resolve up front: a missing part is a config error (exit 2),
        # even when vivado is absent.
        part = self._resolve_part()
        executable = self.tool_cfg.get_executable() or "vivado"

        log_event(
            logger,
            logging.INFO,
            "cdc.start",
            analysis=self.cdc_cfg.get_name(),
            tool=executable,
            top=self.cdc_cfg.get_top(),
            part=part,
        )

        if not shutil.which(executable):
            log_event(
                logger,
                logging.WARNING,
                "cdc.no_vivado",
                analysis=self.cdc_cfg.get_name(),
                exe=executable,
            )
            return CdcSkipResults(
                name=self.cdc_cfg.get_name(),
                desc=(
                    f"{executable!r} not found — run "
                    "`rb tool-check --explain vivado` for install instructions"
                ),
            )

        sdc_path = self.cdc_cfg.get_constraints()
        if not os.path.isfile(sdc_path):
            raise FatalRtlBuddyError(
                f"{self.cdc_cfg.get_name()}: SDC not found: {sdc_path}"
            )
        if self.cdc_cfg.get_waivers() is not None:
            # rtl-buddy-cdc waiver files don't translate to Vivado; the
            # finding payload still carries everything so consumers can
            # filter downstream.
            log_event(
                logger,
                logging.WARNING,
                "cdc.vivado_waivers_unsupported",
                analysis=self.cdc_cfg.get_name(),
                waivers=self.cdc_cfg.get_waivers(),
            )

        try:
            fl_path = self._write_filelist()
        except FilelistError as e:
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"Filelist error: {e}",
            )
        sources = self._source_files_from_filelist(fl_path)
        if not sources:
            raise FatalRtlBuddyError(
                f"{self.cdc_cfg.get_name()}: filelist {fl_path} produced no sources"
            )

        script = render_cdc_tcl(
            top=self.cdc_cfg.get_top(),
            part=part,
            verilog_sources=sources,
            sdc_file=sdc_path,
        )
        script_path = self._script_path()
        Path(script_path).write_text(script)

        log_path = self._log_path()
        cmd = [
            executable,
            "-mode",
            "batch",
            "-source",
            os.path.basename(script_path),
            "-nojournal",
            "-log",
            os.path.basename(log_path),
        ]

        with task_status(f"Running CDC {self.cdc_cfg.get_name()} [vivado]"):
            result = run_managed_process(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                cwd=self.artefact_dir,
            )

        if result.returncode != 0:
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=(f"vivado exited with code {result.returncode} (see {log_path})"),
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""
        error_lines = [ln for ln in log_text.splitlines() if _VIVADO_ERROR_RE.match(ln)]
        if error_lines:
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"{len(error_lines)} ERROR(s) in Vivado log (see {log_path})",
            )

        report_path = self._report_path()
        if not os.path.isfile(report_path):
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"no CDC report produced (see {log_path})",
            )
        try:
            parsed = parse_report_cdc(Path(report_path).read_text())
        except (OSError, ValueError) as e:
            return CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                desc=f"could not parse CDC report: {e}",
            )

        violations = parsed["violations"]
        log_event(
            logger,
            logging.INFO,
            "cdc.vivado_done",
            analysis=self.cdc_cfg.get_name(),
            violations=violations,
            crossings=parsed["crossings"],
            findings=len(parsed["findings"]),
        )

        if violations == 0:
            res = CdcPassResults(
                name=self.cdc_cfg.get_name(),
                violations=0,
                suppressed=0,
                crossings=parsed["crossings"],
            )
        else:
            res = CdcFailResults(
                name=self.cdc_cfg.get_name(),
                violations=violations,
                suppressed=0,
                crossings=parsed["crossings"],
            )
        # Vivado's findings ride along verbatim, tagged with the backend
        # name — second opinion, not rtl_buddy's canonical taxonomy.
        res.results["backend"] = BACKEND_NAME
        res.results["findings"] = parsed["findings"]
        return res
