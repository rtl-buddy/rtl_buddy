"""rtl-buddy-view tool wrapper.

Drives the standalone ``rtl-buddy-view`` CLI: hands it a generated
filelist for a model from ``models.yaml`` and forwards renderer
options. Same subprocess-granularity integration as
:mod:`tools.cdc_rtl_buddy` — rtl_buddy is not tied to the viewer's
Python API, and a viewer release can be picked up via ``uv sync``
without code changes here.

The viewer's stdout is streamed through to the user's stdout when
``-o`` is not supplied, so ``rb hier <model> --format dot | dot ...``
keeps working. Its stderr is captured into ``artefacts/hier/<model>/
hier.log`` alongside the generated filelist.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .vlog_filelist import VlogFilelist
from ..config.model import ModelConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)


class RtlBuddyView:
    """Generates a filelist + invokes ``rtl-buddy-view``.

    Single-shot. Constructed per ``rb hier`` invocation.
    """

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        format: str = "tree",
        output: str | None = None,
        frontend: str | None = None,
        cdc_annotations: str | None = None,
        rdc_annotations: str | None = None,
        axi_perf_annotations: str | None = None,
        clock_legend: bool = False,
        executable: str = "rtl-buddy-view",
    ):
        self.name = name
        self.model_cfg = model_cfg
        self.format = format
        self.output = output
        self.frontend = frontend
        self.cdc_annotations = cdc_annotations
        self.rdc_annotations = rdc_annotations
        # Path to an ``axi-perf.json`` (the ``rb axi-profile run``
        # output for a given test). When set, rtl-buddy-view bakes
        # the per-bundle/interconnect throughput overlay AND emits a
        # top-level ``axi_perf.{source,test,suite_dir}`` block that
        # the SPA's "Open in marimo" button uses to skip its prompt
        # (Phase 2.5 of the marimo umbrella). Passed via the new
        # ``--overlay axi-perf=PATH`` form.
        self.axi_perf_annotations = axi_perf_annotations
        self.clock_legend = clock_legend
        self.executable = executable

        artefact_root = Path(suite_dir) / "artefacts" / "hier" / model_cfg.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.log")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        # rtl-buddy-view rejects +incdir+/-y/-f, so strip everything
        # down to plain source paths.
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _build_cmd(self, fl_path: str) -> list[str]:
        cmd = [
            self.executable,
            "--top",
            self.model_cfg.name,
            "--filelist",
            fl_path,
            "--format",
            self.format,
        ]
        if self.output is not None:
            cmd += ["--output", self.output]
        if self.frontend is not None:
            cmd += ["--frontend", self.frontend]
        if self.cdc_annotations is not None:
            cmd += ["--cdc-annotations", self.cdc_annotations]
        if self.rdc_annotations is not None:
            cmd += ["--rdc-annotations", self.rdc_annotations]
        if self.axi_perf_annotations is not None:
            cmd += ["--overlay", f"axi-perf={self.axi_perf_annotations}"]
        if self.clock_legend:
            cmd += ["--clock-legend"]
        return cmd

    def run(self) -> int:
        # Resolve the viewer up-front. Bare names (no '/') go through
        # PATH lookup; an absolute or relative path is checked for
        # existence + executability. Without this, a missing binary
        # surfaces as an unhandled Python traceback from subprocess.
        if os.sep in self.executable or (os.altsep and os.altsep in self.executable):
            if not (
                os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)
            ):
                raise FatalRtlBuddyError(
                    f"hier: rtl-buddy-view not found or not executable: "
                    f"{self.executable}"
                )
        elif shutil.which(self.executable) is None:
            raise FatalRtlBuddyError(
                f"hier: '{self.executable}' not found on PATH; install rtl-buddy-view "
                f"into the active venv or pass --tool to point at the binary"
            )

        if self.cdc_annotations is not None and not os.path.isfile(
            self.cdc_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: cdc-annotations file not found: {self.cdc_annotations}"
            )
        if self.axi_perf_annotations is not None and not os.path.isfile(
            self.axi_perf_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: axi-perf annotations file not found: "
                f"{self.axi_perf_annotations}"
            )

        if self.rdc_annotations is not None and not os.path.isfile(
            self.rdc_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: rdc-annotations file not found: {self.rdc_annotations}"
            )

        fl_path = self._write_filelist()
        cmd = self._build_cmd(fl_path)
        log_path = self._log_path()

        with task_status(f"Running hier {self.model_cfg.name}"):
            log_event(
                logger,
                logging.INFO,
                "hier.start",
                model=self.model_cfg.name,
                tool=self.executable,
                format=self.format,
            )
            with open(log_path, "w") as logf:
                logf.write("$ " + " ".join(cmd) + "\n")
                logf.flush()
                # Let the renderer's stdout pass through to the user's
                # terminal when --output is not used; capture stderr in
                # the log for diagnosis.
                stdout = subprocess.DEVNULL if self.output is not None else None
                proc = run_managed_process(
                    cmd,
                    stdout=stdout,
                    stderr=logf,
                )

        log_event(
            logger,
            logging.INFO,
            "hier.done",
            model=self.model_cfg.name,
            returncode=proc.returncode,
        )
        return proc.returncode
