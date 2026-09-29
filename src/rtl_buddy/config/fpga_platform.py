import logging
import os

from serde import serde, field

logger = logging.getLogger(__name__)


@serde
class FpgaPlatformConfigFile:
    name: str
    part: str
    board: str = ""
    package: str = ""
    xdc: list[str] = field(default_factory=list)


class FpgaPlatformConfig:
    """A reusable FPGA target from ``cfg-fpga-platforms``: a device part plus default constraints.

    ``board`` and ``package`` are informational; ``package`` is never appended to ``part``.
    ``xdc`` files resolve relative to ``root_config.yaml``; per-run ``xdc:`` entries in ``fpga.yaml`` extend them.
    """

    def __init__(self, cfg: FpgaPlatformConfigFile, root_cfg_path: str):
        cfg_dir = os.path.dirname(root_cfg_path)
        self._name = cfg.name
        self._part = cfg.part
        self._board = cfg.board
        self._package = cfg.package
        self._xdc_files = [os.path.normpath(os.path.join(cfg_dir, p)) for p in cfg.xdc]

    def get_name(self) -> str:
        return self._name

    def get_part(self) -> str:
        return self._part

    def get_board(self) -> str:
        return self._board

    def get_package(self) -> str:
        return self._package

    def get_xdc_files(self) -> list[str]:
        return list(self._xdc_files)
