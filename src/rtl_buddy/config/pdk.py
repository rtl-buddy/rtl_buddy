import logging
import os

from serde import serde, field

from ..errors import FatalRtlBuddyError

logger = logging.getLogger(__name__)


def _as_path_list(value: str | list[str]) -> list[str]:
    """A path-valued key as a list, whether it was written as one or many.

    `cell-gds` took a single string before #617 and still does; a YAML list
    is taken entry by entry, so a path is never split on whitespace and a
    path containing spaces survives. Empty entries are dropped — the key's
    own default is `""`, which means "not configured", not "one empty path".
    """
    if isinstance(value, str):
        return [value] if value else []
    return [p for p in value if p]


@serde
class PdkPinLayersFile:
    horizontal: str = "metal3"
    vertical: str = "metal2"


#: What the flow asks for when neither the PDK nor the P&R platform says.
#: Both are FreePDK45-derived: 0.7 utilisation targets and one site of cell
#: padding on each side are what `flow.tcl.template` used to spell out.
DEFAULT_PLACEMENT_DENSITY = 0.7
DEFAULT_PLACEMENT_PADDING = 1

#: Minimum channel, in microns, the macro packer keeps between two macros and
#: between a macro and each core edge (#626). It has to be wide enough for
#: `pdngen` to repair the channel, or the run dies at PDN-0179 with a
#: placement that was otherwise legal. What a repair needs is two straps and
#: the spacing between them, inside whatever halo the PDN's own macro grid
#: reserves: on sky130hd (the ORFS values the project template uses) that is
#: met4/met5 straps 1.6 um wide whose default spacing is half the 27.14 um
#: pitch less the width, ~11.97 um, plus 2 um of macro-grid halo on each side
#: — about 19.2 um. 20 um clears that, and a 12 um channel measurably does
#: not. Nangate45 ships no `pdn-config`, so there the halo is placement cost
#: only: ~14 standard-cell rows at its 1.4 um site height.
DEFAULT_PLACEMENT_MACRO_HALO = 20.0


@serde
class PlacementFile:
    """Global-placement and macro-placement tuning, as written in YAML.

    Every field is `None` when unset, which is what lets a P&R platform
    override one of them and inherit the others from the PDK.
    """

    density: float | None = None
    padding: int | None = None
    macro_halo: float | None = field(rename="macro-halo", default=None)


def validate_placement(placement: PlacementFile, where: str) -> PlacementFile:
    """Range-check a `placement:` block, naming the block that carries it.

    The types are pyserde's to enforce; the ranges are not, and a density
    of 0 or 7 reaches OpenROAD as a placement that cannot converge. The
    `bool` guard is here because pyserde's `int` accepts `padding: true`.
    """
    density = placement.density
    if density is not None:
        density = float(density)
        if not 0.0 < density <= 1.0:
            raise FatalRtlBuddyError(
                f"{where}: placement.density must be > 0 and <= 1, got {density}"
            )
    padding = placement.padding
    if padding is not None:
        if isinstance(padding, bool):
            raise FatalRtlBuddyError(
                f"{where}: placement.padding must be a non-negative integer, "
                f"got {padding!r}"
            )
        if padding < 0:
            raise FatalRtlBuddyError(
                f"{where}: placement.padding must be >= 0, got {padding}"
            )
    macro_halo = placement.macro_halo
    if macro_halo is not None:
        macro_halo = float(macro_halo)
        if macro_halo < 0.0:
            raise FatalRtlBuddyError(
                f"{where}: placement.macro-halo must be >= 0, got {macro_halo}"
            )
    return PlacementFile(density=density, padding=padding, macro_halo=macro_halo)


_TCL_METACHARACTERS = frozenset('[]{}$"\\;')


def _validate_dont_use_cells(cells: list[str], where: str) -> list[str]:
    """Check a `dont-use-cells:` list, naming the block that carries it.

    Each entry becomes one element of a Tcl list and one `-dont_use`
    argument to Yosys, so an entry carrying whitespace would silently
    become two patterns. Reject it here rather than in a tool log.
    """
    validated = []
    for cell in cells:
        if not isinstance(cell, str) or not cell.strip():
            raise FatalRtlBuddyError(
                f"{where}: dont-use-cells entries must be non-empty cell-name "
                f"patterns, got {cell!r}"
            )
        if len(cell.split()) > 1:
            raise FatalRtlBuddyError(
                f"{where}: dont-use-cells entry {cell!r} contains whitespace; "
                "write one pattern per list entry"
            )
        # The entry is spliced into a Tcl `[list ...]` unquoted, so a Tcl
        # metacharacter would be run, not matched — `probe[c]*` dies as
        # `invalid command name "c"` minutes into the flow. OpenSTA's
        # matcher only knows `*` and `?` anyway (#656).
        bad = sorted(set(cell) & _TCL_METACHARACTERS)
        if bad:
            raise FatalRtlBuddyError(
                f"{where}: dont-use-cells entry {cell!r} contains "
                f"{' '.join(bad)}; patterns support only the `*` and `?` "
                "wildcards"
            )
        validated.append(cell)
    return validated


def merge_dont_use_cells(pdk_cells: list[str], platform_cells: list[str]) -> list[str]:
    """A platform's `dont-use-cells` added to its PDK's, in a stable order.

    Additive, never a replacement (#656): a platform can only exclude more,
    so a PDK-level exclusion — a cell the process cannot legalise — cannot
    be dropped by a platform that forgets to repeat it. The PDK's entries
    come first and a pattern named by both is kept once, so a platform that
    adds nothing renders exactly the list the PDK alone did.
    """
    return list(dict.fromkeys([*pdk_cells, *platform_cells]))


@serde
class PdkConfigFile:
    name: str
    site: str = ""
    corners: dict[str, str] = field(default_factory=dict)
    tech_lef: str = field(rename="tech-lef", default="")
    macro_lef: str = field(rename="macro-lef", default="")
    # One path or a list of them: standard cells plus whatever else the
    # stream-out has to read from the PDK (#617).
    cell_gds: str | list[str] = field(rename="cell-gds", default="")
    klayout_tech: str = field(rename="klayout-tech", default="")
    klayout_props: str = field(rename="klayout-props", default="")
    tie_hi: str = field(rename="tie-hi", default="")
    tie_lo: str = field(rename="tie-lo", default="")
    fill_cells: list[str] = field(rename="fill-cells", default_factory=list)
    pin_layers: PdkPinLayersFile = field(
        rename="pin-layers", default_factory=PdkPinLayersFile
    )
    placement: PlacementFile = field(default_factory=PlacementFile)
    # Cell names or patterns the flow must not map to or repair with. One
    # list, read by both `rb synth` and `rb pnr`.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)
    # Path to a Tcl snippet that defines the power grid. The flow sources it
    # and calls `pdngen` itself, as ORFS does with `PDN_TCL`.
    pdn_config: str = field(rename="pdn-config", default="")
    # Path to an OpenRCX extraction-rules file (ORFS `RCX_RULES`). When set,
    # `rb pnr` extracts the routed design and writes `<top>.routed.spef`,
    # and `rb power` with `netlist-source: pnr` reads that SPEF instead of
    # re-estimating parasitics from the global routes (#101, #104).
    rcx_rules: str = field(rename="rcx-rules", default="")


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
        self._cell_gds = [_resolve(p) for p in _as_path_list(cfg.cell_gds)]
        self._klayout_tech = _resolve(cfg.klayout_tech)
        self._klayout_props = _resolve(cfg.klayout_props)
        self._tie_hi = cfg.tie_hi
        self._tie_lo = cfg.tie_lo
        self._fill_cells = list(cfg.fill_cells)
        self._pin_layer_horizontal = cfg.pin_layers.horizontal
        self._pin_layer_vertical = cfg.pin_layers.vertical
        self._placement = validate_placement(cfg.placement, f"PDK '{cfg.name}'")
        self._dont_use_cells = _validate_dont_use_cells(
            cfg.dont_use_cells, f"PDK '{cfg.name}'"
        )
        self._pdn_config = _resolve(cfg.pdn_config)
        self._rcx_rules = _resolve(cfg.rcx_rules)

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
        """The first configured cell GDS, or `""` when none is.

        Kept for callers written against the single-valued key; anything
        that streams layout wants :meth:`get_cell_gds_paths` (#617).
        """
        return self._cell_gds[0] if self._cell_gds else ""

    def get_cell_gds_paths(self) -> list[str]:
        """Every configured cell GDS, resolved, in config order."""
        return list(self._cell_gds)

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

    def get_pin_layer_horizontal(self) -> str:
        return self._pin_layer_horizontal

    def get_pin_layer_vertical(self) -> str:
        return self._pin_layer_vertical

    def get_placement_density(self) -> float | None:
        """Configured global-placement density, or `None` when unset."""
        return self._placement.density

    def get_placement_padding(self) -> int | None:
        """Configured global-placement cell padding, or `None` when unset."""
        return self._placement.padding

    def get_placement_macro_halo(self) -> float | None:
        """Configured macro halo in microns, or `None` when unset."""
        return self._placement.macro_halo

    def get_dont_use_cells(self) -> list[str]:
        return list(self._dont_use_cells)

    def get_pdn_config(self) -> str:
        """Resolved path to the PDN Tcl snippet, or `""` when unset."""
        return self._pdn_config

    def get_rcx_rules(self) -> str:
        """Resolved path to the OpenRCX rules file, or `""` when unset."""
        return self._rcx_rules
