"""rtl-buddy-axi-profiler tool wrapper.

Drives the standalone ``axi-profiler`` CLI: hands it a generated
filelist for a model from ``models.yaml`` and runs the discover
stage to produce ``axi-bundles.yaml``. Same subprocess-granularity
integration as :mod:`tools.hier_rtl_buddy_view` — rtl_buddy is not
tied to the profiler's Python API, and a profiler release can be
picked up via ``uv sync`` (or by re-installing the standalone
binary) without code changes here.

The remaining subcommands (``run``, ``gen-monitor``) land once the
profiler's ingest stages graduate from #3 / #4. For now, ``rb
axi-profile <model>`` runs ``axi-profiler discover`` and writes the
manifest into ``artefacts/axi/<model>/axi-bundles.yaml``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .vlog_filelist import VlogFilelist
from ..config.model import ModelConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)


class RtlBuddyAxiProfile:
    """Generates a filelist + invokes ``axi-profiler discover``.

    Single-shot. Constructed per ``rb axi-profile`` invocation.
    """

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        output: str | None = None,
        amend: str | None = None,
        executable: str = "axi-profiler",
    ):
        self.name = name
        self.model_cfg = model_cfg
        self.output = output
        self.amend = amend
        self.executable = executable

        artefact_root = Path(suite_dir) / "artefacts" / "axi" / model_cfg.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi.f")

    def _default_output_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-bundles.yaml")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-profile.log")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        # axi-profiler accepts plain source-file lists; strip
        # +incdir+ / -y / -f directives like rb hier does.
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _build_cmd(self, fl_path: str, out_path: str) -> list[str]:
        cmd = [
            self.executable,
            "discover",
            "--filelist",
            fl_path,
            "--top",
            self.model_cfg.name,
            "--output",
            out_path,
        ]
        if self.amend:
            cmd += ["--amend", self.amend]
        return cmd

    def run(self) -> int:
        if os.sep in self.executable or (os.altsep and os.altsep in self.executable):
            if not (
                os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)
            ):
                raise FatalRtlBuddyError(
                    f"axi-profile: axi-profiler not found or not executable: "
                    f"{self.executable}"
                )
        elif shutil.which(self.executable) is None:
            raise FatalRtlBuddyError(
                f"axi-profile: '{self.executable}' not found on PATH; "
                f"install rtl-buddy-axi-profiler "
                f"(e.g. `uv tool install rtl-buddy-axi-profiler`)."
            )

        fl_path = self._write_filelist()
        out_path = self.output or self._default_output_path()
        cmd = self._build_cmd(fl_path, out_path)
        log_event(
            logger,
            logging.INFO,
            "axi_profile.run",
            model=self.model_cfg.name,
            cmd=" ".join(cmd),
            output=out_path,
        )

        log_path = self._log_path()
        with task_status(f"axi-profile {self.model_cfg.name}"):
            with open(log_path, "w") as log_f:
                log_f.write("$ " + " ".join(cmd) + "\n")
                log_f.flush()
                proc = run_managed_process(cmd, stdout=None, stderr=log_f)

        log_event(
            logger,
            logging.INFO,
            "axi_profile.done",
            model=self.model_cfg.name,
            output=out_path,
            returncode=proc.returncode,
        )
        return proc.returncode
