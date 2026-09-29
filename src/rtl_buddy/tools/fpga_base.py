"""Base class and target resolution shared by FPGA implementation backends.

A backend subclasses `BaseFpga`, implements `run()` and is registered in
`runner/fpga_runner.py::_FPGA_BACKENDS`.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config.fpga import FpgaConfig
from ..errors import FatalRtlBuddyError
from ..runner.fpga_results import FpgaResults
from .vlog_filelist import VlogFilelist, incdirs_from_filelist


@dataclass(frozen=True)
class FpgaTarget:
    """Target part and constraint files for one fpga run.

    ``xdc_files`` is in read order: platform files first, then per-run files,
    so run-level constraints override platform ones.
    """

    part: str
    xdc_files: tuple[str, ...]


def resolve_target(fpga_cfg: FpgaConfig, root_cfg) -> FpgaTarget:
    """Resolve the part and constraint set, applying ``cfg-fpga-platforms`` when referenced.

    Backends call this instead of reading the part and XDC files from ``fpga_cfg``.

    Raises:
      FatalRtlBuddyError: the run names a platform that is unknown or that has no root config.
    """
    platform_name = fpga_cfg.get_platform()
    if not platform_name:
        return FpgaTarget(
            part=fpga_cfg.get_part(),
            xdc_files=tuple(fpga_cfg.get_xdc_files()),
        )
    if root_cfg is None:
        raise FatalRtlBuddyError(
            f"fpga run '{fpga_cfg.get_name()}': platform "
            f"'{platform_name}' requires a root_config.yaml with a "
            "cfg-fpga-platforms section"
        )
    platform = root_cfg.get_fpga_platform_cfg(platform_name)
    return FpgaTarget(
        part=platform.get_part(),
        xdc_files=tuple(platform.get_xdc_files() + fpga_cfg.get_xdc_files()),
    )


class BaseFpga(ABC):
    def __init__(
        self,
        name: str,
        fpga_cfg: FpgaConfig,
        suite_dir: str,
        root_cfg,
        executable: str,
        emit_bitstream: bool = False,
    ):
        self.name = name
        self.fpga_cfg = fpga_cfg
        self.suite_dir = suite_dir
        self.root_cfg = root_cfg
        self.executable = executable
        self.emit_bitstream = emit_bitstream
        artefact_root = Path(suite_dir) / "artefacts" / fpga_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpga.f")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.fpga_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
        )
        return fl_path

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        """Return absolute source file paths from a generated filelist."""
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

    def _incdirs_from_filelist(self, fl_path: str) -> list[str]:
        """The filelist's ``+incdir+`` directories, for the tool's include option."""
        return incdirs_from_filelist(fl_path)

    @abstractmethod
    def run(self) -> FpgaResults:  # pragma: no cover
        ...
