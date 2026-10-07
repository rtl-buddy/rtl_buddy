import logging
import os

from serde import serde, field

from ..errors import FatalRtlBuddyError

logger = logging.getLogger(__name__)


def _as_path_list(value: str | list[str]) -> list[str]:
    """A path-valued key as a list, given a string or a list.

    Paths are never split on whitespace. Empty entries are dropped, since the default `""` means not configured.
    """
    if isinstance(value, str):
        return [value] if value else []
    return [p for p in value if p]


@serde
class PdkPinLayersFile:
    horizontal: str = "metal3"
    vertical: str = "metal2"


#: Placement defaults used when neither the PDK nor the P&R platform sets a value.
DEFAULT_PLACEMENT_DENSITY = 0.7
DEFAULT_PLACEMENT_PADDING = 1

#: Minimum channel in microns between two macros and between a macro and each core edge.
#: It must be wide enough for `pdngen` to repair the channel, or the run fails at PDN-0179; sky130hd needs about 19.2 um.
DEFAULT_PLACEMENT_MACRO_HALO = 20.0

#: Standard-cell keep-out in microns around each placed macro; 0 places no blockage.
#: Without it, cells abut the macro and the detailed router reports Metal Spacing violations at the shared edge.
DEFAULT_PLACEMENT_MACRO_CELL_HALO = 1.0

#: Distance in microns from each constant-driven load to the tie cell `repair_tie_fanout` gives it.
DEFAULT_PLACEMENT_TIE_SEPARATION = 0.0


@serde
class PlacementFile:
    """Global-placement and macro-placement tuning as written in YAML.

    Unset fields are `None`, so a P&R platform can override one and inherit the rest from the PDK.
    """

    density: float | None = None
    padding: int | None = None
    macro_halo: float | None = field(rename="macro-halo", default=None)
    macro_cell_halo: float | None = field(rename="macro-cell-halo", default=None)
    tie_separation: float | None = field(rename="tie-separation", default=None)
    # `global_placement -reference_hpwl`; `None` leaves the placer's own reference.
    reference_hpwl: float | None = field(rename="reference-hpwl", default=None)
    # `global_placement -routability_driven` / `-routability_use_grt`; `None` inherits, unset means off.
    routability_driven: bool | None = field(rename="routability-driven", default=None)
    routability_use_grt: bool | None = field(rename="routability-use-grt", default=None)


def validate_placement(placement: PlacementFile, where: str) -> PlacementFile:
    """Range-check a `placement:` block; ``where`` names it in errors.

    The `bool` guard exists because pyserde's `int` accepts `padding: true`.
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
    macro_cell_halo = placement.macro_cell_halo
    if macro_cell_halo is not None:
        macro_cell_halo = float(macro_cell_halo)
        if not 0.0 <= macro_cell_halo < float("inf"):
            raise FatalRtlBuddyError(
                f"{where}: placement.macro-cell-halo must be >= 0, "
                f"got {macro_cell_halo}"
            )
    tie_separation = placement.tie_separation
    if tie_separation is not None:
        tie_separation = float(tie_separation)
        if not 0.0 <= tie_separation < float("inf"):
            raise FatalRtlBuddyError(
                f"{where}: placement.tie-separation must be >= 0, got {tie_separation}"
            )
    reference_hpwl = placement.reference_hpwl
    if reference_hpwl is not None:
        if not 0.0 < float(reference_hpwl) < float("inf"):
            raise FatalRtlBuddyError(
                f"{where}: placement.reference-hpwl must be a positive number, "
                f"got {reference_hpwl!r}"
            )
        reference_hpwl = float(reference_hpwl)
    return PlacementFile(
        density=density,
        padding=padding,
        macro_halo=macro_halo,
        macro_cell_halo=macro_cell_halo,
        tie_separation=tie_separation,
        reference_hpwl=reference_hpwl,
        routability_driven=placement.routability_driven,
        routability_use_grt=placement.routability_use_grt,
    )


_TCL_METACHARACTERS = frozenset('[]{}$"\\;')


def _validate_dont_use_cells(cells: list[str], where: str) -> list[str]:
    """Check a `dont-use-cells:` list; ``where`` names it in errors.

    Each entry becomes one Tcl list element and one Yosys `-dont_use` argument, so whitespace would split it into two patterns.
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
        # Spliced unquoted into a Tcl `[list ...]`: a metacharacter would be executed, not matched.
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
    """A platform's `dont-use-cells` appended to its PDK's, duplicates removed.

    A platform can only add exclusions. The PDK's entries come first.
    """
    return list(dict.fromkeys([*pdk_cells, *platform_cells]))


@serde
class PdkConfigFile:
    name: str
    site: str = ""
    # Each corner is one Liberty path or a list of them (cell libraries split across files).
    corners: dict[str, str | list[str]] = field(default_factory=dict)
    tech_lef: str = field(rename="tech-lef", default="")
    macro_lef: str = field(rename="macro-lef", default="")
    # One path or a list of paths.
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
    # Cell patterns that `rb synth` and `rb pnr` must not map to or repair with.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)
    # Tcl snippet defining the power grid; the flow sources it and calls `pdngen`.
    pdn_config: str = field(rename="pdn-config", default="")
    # OpenRCX rules file. When set, `rb pnr` writes `<top>.routed.spef`, which `rb power` with `netlist-source: pnr` reads.
    rcx_rules: str = field(rename="rcx-rules", default="")
    # Platform Tcl hooks (ORFS `MAKE_TRACKS`, `SET_RC_TCL`, `TAPCELL_TCL`, `PLATFORM_TCL`); `rb pnr` sources each where ORFS does.
    tracks_tcl: str = field(rename="tracks-tcl", default="")
    layer_rc_tcl: str = field(rename="layer-rc-tcl", default="")
    tapcell_tcl: str = field(rename="tapcell-tcl", default="")
    platform_tcl: str = field(rename="platform-tcl", default="")


class PdkConfig:
    def __init__(self, cfg: PdkConfigFile, root_cfg_path: str):
        cfg_dir = os.path.dirname(root_cfg_path)

        def _resolve(p: str) -> str:
            return os.path.normpath(os.path.join(cfg_dir, p)) if p else ""

        self._name = cfg.name
        self._site = cfg.site
        self._corners = {
            k: [_resolve(p) for p in _as_path_list(v)]
            for k, v in (cfg.corners or {}).items()
        }
        for corner, paths in self._corners.items():
            if not paths:
                raise FatalRtlBuddyError(
                    f"PDK '{cfg.name}': corner '{corner}' names no Liberty file"
                )
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
        self._tcl_hooks = {
            "platform-tcl": _resolve(cfg.platform_tcl),
            "layer-rc-tcl": _resolve(cfg.layer_rc_tcl),
            "tracks-tcl": _resolve(cfg.tracks_tcl),
            "tapcell-tcl": _resolve(cfg.tapcell_tcl),
        }

    def get_name(self) -> str:
        return self._name

    def get_site(self) -> str:
        return self._site

    def get_corners(self) -> list[str]:
        return list(self._corners.keys())

    def get_corner_paths(self, corner: str) -> list[str]:
        """The corner's standard-cell Liberty files, in config order."""
        paths = self._corners.get(corner)
        if paths is None:
            raise FatalRtlBuddyError(
                f"PDK '{self._name}' has no corner '{corner}'; "
                f"available: {sorted(self._corners)}"
            )
        return list(paths)

    def get_default_corner(self) -> str:
        if not self._corners:
            raise FatalRtlBuddyError(f"PDK '{self._name}' declares no corners")
        return next(iter(self._corners))

    def get_tech_lef(self) -> str:
        return self._tech_lef

    def get_macro_lef(self) -> str:
        return self._macro_lef

    def get_cell_gds(self) -> str:
        """The first configured cell GDS, or `""`; use :meth:`get_cell_gds_paths` to stream layout."""
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

    def get_placement_macro_cell_halo(self) -> float | None:
        """Configured macro row keep-out in microns, or `None` when unset."""
        return self._placement.macro_cell_halo

    def get_placement_tie_separation(self) -> float | None:
        """Configured tie-cell separation in microns, or `None` when unset."""
        return self._placement.tie_separation

    def get_placement_reference_hpwl(self) -> float | None:
        """Configured global-placement reference HPWL, or `None` when unset."""
        return self._placement.reference_hpwl

    def get_placement_routability_driven(self) -> bool | None:
        """Configured `global_placement -routability_driven`, or `None` when unset."""
        return self._placement.routability_driven

    def get_placement_routability_use_grt(self) -> bool | None:
        """Configured `global_placement -routability_use_grt`, or `None` when unset."""
        return self._placement.routability_use_grt

    def get_dont_use_cells(self) -> list[str]:
        return list(self._dont_use_cells)

    def get_pdn_config(self) -> str:
        """Resolved path to the PDN Tcl snippet, or `""` when unset."""
        return self._pdn_config

    def get_rcx_rules(self) -> str:
        """Resolved path to the OpenRCX rules file, or `""` when unset."""
        return self._rcx_rules

    def get_tracks_tcl(self) -> str:
        """Resolved path to the routing-track Tcl that replaces `make_tracks`, or `""` when unset."""
        return self._tcl_hooks["tracks-tcl"]

    def get_layer_rc_tcl(self) -> str:
        """Resolved path to the layer-RC Tcl (`set_layer_rc` / `set_wire_rc`), or `""` when unset."""
        return self._tcl_hooks["layer-rc-tcl"]

    def get_tapcell_tcl(self) -> str:
        """Resolved path to the tap/endcap insertion Tcl, or `""` when unset."""
        return self._tcl_hooks["tapcell-tcl"]

    def get_platform_tcl(self) -> str:
        """Resolved path to the Tcl sourced before Liberty is read, or `""` when unset."""
        return self._tcl_hooks["platform-tcl"]

    def get_tcl_hooks(self) -> dict[str, str]:
        """The configured Tcl hooks as `{yaml-key: resolved path}`, unset ones omitted, in flow order."""
        return {k: v for k, v in self._tcl_hooks.items() if v}
