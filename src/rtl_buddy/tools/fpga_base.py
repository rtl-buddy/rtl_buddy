"""Abstract contract for FPGA implementation backends.

Adding a new backend (openXC7, Quartus, ...) is:
  1. Subclass `BaseFpga` and implement `run()` returning a `FpgaResults`.
  2. Register the class in `runner/fpga_runner.py::_FPGA_BACKENDS`.

Shared resolution logic (target part + effective XDC set) lives here so
every backend agrees on what device the user asked for and only
diverges on tool-specific command emission.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config.fpga import FpgaConfig
from ..errors import FatalRtlBuddyError
from ..runner.fpga_results import FpgaResults


@dataclass(frozen=True)
class FpgaTarget:
    """Resolved implementation target for one fpga run.

    ``xdc_files`` is the effective constraint list in read order:
    platform defaults first, per-run files after — later XDC commands
    win in Vivado, so run-level constraints override platform defaults.
    """

    part: str
    xdc_files: tuple[str, ...]


def resolve_target(fpga_cfg: FpgaConfig, root_cfg) -> FpgaTarget:
    """Resolve the target device + constraint set for one fpga run.

    This is the single seam where the platform abstraction
    (``cfg-fpga-platforms``, issue #286) plugs in — backends must go
    through it rather than reading ``fpga_cfg.get_part()`` /
    ``get_xdc_files()`` directly, so platform-referencing and
    inline-part runs look identical downstream.

    Raises:
      FatalRtlBuddyError: when the run references an unknown platform
        (or references one with no RootConfig available).
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

    @abstractmethod
    def run(self) -> FpgaResults:  # pragma: no cover - abstract
        ...
