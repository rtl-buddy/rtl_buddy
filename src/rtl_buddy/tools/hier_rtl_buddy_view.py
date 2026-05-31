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
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)


def _is_non_source_filelist_line(line: str) -> bool:
    """Return True when ``line`` is a filelist directive that doesn't
    name a source file (include dirs, lib dirs, lib files) or names a
    file that isn't parseable HDL (Verilator config/waiver files).
    These must not survive into rtl-buddy-view's filelist because the
    renderer expects bare source paths and ``strip=True`` would emit
    the trailing argument as one — turning ``+incdir+../../common``
    into the bare directory ``../../common`` and crashing the
    parser with IsADirectoryError on the conventional testbench
    layout.
    """
    s = line.strip()
    if s.startswith("+incdir+") or s.startswith("+libext+"):
        return True
    # ``-y <dir>`` / ``-v <file>`` use a space (or tab) separator.
    for prefix in ("-y", "-v"):
        if s.startswith(prefix) and len(s) > len(prefix) and s[len(prefix)].isspace():
            return True
    # Verilator config/waiver files (``*.vlt``) are commonly listed
    # alongside sources in a testbench filelist (lint waivers scoped to
    # vendor code). They are not HDL — Verible's ``verible-verilog-syntax``
    # exits non-zero on them — so drop them before the merge.
    if s.endswith(".vlt"):
        return True
    return False


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
        test_cfg: TestConfig | None = None,
        test_suite_dir: str | None = None,
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
        # Optional test that pins the TB top + TB filelist for the
        # TB-rooted view (#99 / 6b). When set, the generated filelist
        # is DUT+TB merged and rtl-buddy-view is invoked with both
        # ``--top <model>`` AND ``--tb-top <tb.toplevel>`` so the
        # rendered tree is rooted at the TB with the DUT recorded for
        # the SPA's dashed-boundary overlay. When None, today's
        # DUT-only invocation is byte-identical (no behavioural change
        # for the unconditional ``rb hier <model>`` callers).
        self.test_cfg = test_cfg
        # Directory the test's ``tests.yaml`` lives in. The TB filelist
        # entries (e.g. ``tb_axi_2x2.sv``, ``+incdir+../../common``) are
        # declared relative to it, so it must anchor their resolution.
        # ``suite_dir`` above is the *artefact* root (the hub passes the
        # project root there) and is the wrong base for the TB filelist
        # — without this, the merge resolves TB sources against the hub
        # process cwd and fails with ``FilelistError: <tb> does not
        # exist``. ``None`` falls back to cwd, the legacy behaviour for
        # callers that run from the suite dir.
        self.test_suite_dir = test_suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / "hier" / model_cfg.name
        if test_cfg is not None:
            # Cache key for TB mode is (model, tb_name). Two tests
            # sharing the same TB share the artefact — the test's
            # other parameters (plusargs, sweep) don't affect the
            # elaborated hierarchy, only its top + filelist do.
            artefact_root = artefact_root / "tb" / test_cfg.tb.name
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
        #
        # TB mode merges the test's TB filelist on top of the model
        # filelist via VlogFilelist's existing test_filelist parameter
        # (the same merge the compile flow uses). Order is DUT first,
        # TB second — the TB modules instantiate the DUT, not the
        # other way around, and Verible elaborates from the
        # ``--tb-top`` regardless of file order.
        #
        # Drop non-source entries (``+incdir+...``, ``-y .../``,
        # ``+libext+...``) from the TB filelist before merging. With
        # ``strip=True`` VlogFilelist would emit them as bare paths
        # which rtl-buddy-view then tries to open as source files,
        # producing ``IsADirectoryError`` on the typical
        # ``+incdir+../../common`` testbench convention. rtl-buddy-view
        # works on absolute source paths and does not need include
        # directories — the TB's compile-time options are not relevant
        # to its CST walk.
        test_filelist = None
        if self.test_cfg is not None:
            test_filelist = [
                line
                for line in self.test_cfg.tb.get_filelist()
                if not _is_non_source_filelist_line(line)
            ]
        vlog_fl.write_output(
            output_filepath=fl_path,
            unroll=True,
            strip=True,
            deduplicate=True,
            test_filelist=test_filelist,
            suite_dir=self.test_suite_dir,
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
        if self.test_cfg is not None:
            # ``--tb-top`` is independent of ``--top`` (rtl-buddy-view
            # #99 / 6a). When both are supplied, the renderer elaborates
            # from --tb-top and records the DUT name in
            # ``view.json::dut_top`` so the SPA can mark the DUT
            # subtree with a dashed boundary.
            #
            # ``toplevel`` is the explicit top override — set for cocotb
            # / SystemC harnesses, but conventionally unset for a plain
            # SystemVerilog testbench (Verilator auto-detects the top at
            # sim time). The view has no elaboration to auto-detect from,
            # so fall back to the testbench config name, which by
            # convention is the TB's top module name (e.g. ``tb_axi_2x2``).
            # Without this fallback a plain-SV testbench silently rendered
            # DUT-rooted, so clicking "TB" in the SPA showed the DUT view
            # with no AXI overlay. rtl-buddy-view fails loudly with a
            # "top module not found" error if the convention doesn't hold,
            # so a mismatch surfaces as a clear 500 rather than a silent
            # wrong render.
            tb_top = self.test_cfg.tb.toplevel or self.test_cfg.tb.name
            cmd += ["--tb-top", tb_top]
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
                    cwd=self.artefact_dir,
                )

        log_event(
            logger,
            logging.INFO,
            "hier.done",
            model=self.model_cfg.name,
            returncode=proc.returncode,
        )
        return proc.returncode
