"""Abstract contract for FPGA implementation backends.

Adding a new backend (openXC7, Quartus, ...) is:
  1. Subclass `BaseFpga` and implement `run()` returning a `FpgaResults`.
  2. Register the class in `runner/fpga_runner.py::_FPGA_BACKENDS`.

Shared resolution logic (target-part selection) lives here so every
backend agrees on what device the user asked for and only diverges on
tool-specific command emission.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config.fpga import FpgaConfig
from ..runner.fpga_results import FpgaResults


def resolve_part(fpga_cfg: FpgaConfig, root_cfg) -> str:
    """Resolve the target device part for one fpga run.

    Today the part comes straight from the run's ``part:`` field in
    fpga.yaml. This function is the seam where the platform abstraction
    (``cfg-fpga-platforms``, issue #286) plugs in — backends must go
    through it rather than reading ``fpga_cfg.get_part()`` directly.
    """
    return fpga_cfg.get_part()


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

    @abstractmethod
    def run(self) -> FpgaResults:  # pragma: no cover - abstract
        ...
