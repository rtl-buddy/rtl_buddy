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
    """A reusable FPGA target: a device part plus its default constraints.

    Parallel to ``cfg-pnr-platforms`` for ASIC P&R: the platform lifts
    the device choice out of individual ``fpga.yaml`` runs so one suite
    can sweep the same RTL across several parts via ``platform:`` refs.

    ``board`` and ``package`` are informational only. Vivado part names
    already encode the package (e.g. ``ffvc1156`` inside
    ``xczu7ev-ffvc1156-2-e``), so ``package`` is never re-attached to
    the part string — it exists for documentation and for backends
    whose part naming splits device and package.

    ``xdc`` lists the platform's default constraint files (board clocks,
    pinout), resolved relative to ``root_config.yaml`` — the file that
    owns the platform definition — following the same anchoring
    convention as ``cfg-pdks`` asset paths. Per-run ``xdc:`` entries in
    ``fpga.yaml`` extend (not replace) this set.
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
