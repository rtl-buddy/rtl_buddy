import logging
import os

from serde import serde, field

from ..errors import FatalRtlBuddyError

logger = logging.getLogger(__name__)


@serde
class PdkConfigFile:
    name: str
    site: str = ""
    corners: dict[str, str] = field(default_factory=dict)
    tech_lef: str = field(rename="tech-lef", default="")
    macro_lef: str = field(rename="macro-lef", default="")
    cell_gds: str = field(rename="cell-gds", default="")
    klayout_tech: str = field(rename="klayout-tech", default="")
    klayout_props: str = field(rename="klayout-props", default="")
    tie_hi: str = field(rename="tie-hi", default="")
    tie_lo: str = field(rename="tie-lo", default="")
    fill_cells: list[str] = field(rename="fill-cells", default_factory=list)


class PdkConfig:
    def __init__(self, cfg: PdkConfigFile, root_cfg_path: str):
        cfg_dir = os.path.dirname(root_cfg_path)

        def _resolve(p: str) -> str:
            return os.path.normpath(os.path.join(cfg_dir, p)) if p else ""

        self._name = cfg.name
        self._site = cfg.site
        self._corners = {k: _resolve(v) for k, v in (cfg.corners or {}).items()}
        self._tech_lef = _resolve(cfg.tech_lef)
        self._macro_lef = _resolve(cfg.macro_lef)
        self._cell_gds = _resolve(cfg.cell_gds)
        self._klayout_tech = _resolve(cfg.klayout_tech)
        self._klayout_props = _resolve(cfg.klayout_props)
        self._tie_hi = cfg.tie_hi
        self._tie_lo = cfg.tie_lo
        self._fill_cells = list(cfg.fill_cells)

    def get_name(self) -> str:
        return self._name

    def get_site(self) -> str:
        return self._site

    def get_corners(self) -> list[str]:
        return list(self._corners.keys())

    def get_corner_path(self, corner: str) -> str:
        path = self._corners.get(corner)
        if path is None:
            raise FatalRtlBuddyError(
                f"PDK '{self._name}' has no corner '{corner}'; "
                f"available: {sorted(self._corners)}"
            )
        return path

    def get_default_corner(self) -> str:
        if not self._corners:
            raise FatalRtlBuddyError(f"PDK '{self._name}' declares no corners")
        return next(iter(self._corners))

    def get_tech_lef(self) -> str:
        return self._tech_lef

    def get_macro_lef(self) -> str:
        return self._macro_lef

    def get_cell_gds(self) -> str:
        return self._cell_gds

    def get_klayout_tech(self) -> str:
        return self._klayout_tech

    def get_klayout_props(self) -> str:
        return self._klayout_props

    def get_tie_hi(self) -> str:
        return self._tie_hi

    def get_tie_lo(self) -> str:
        return self._tie_lo

    def get_fill_cells(self) -> list[str]:
        return list(self._fill_cells)
