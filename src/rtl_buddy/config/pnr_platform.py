import logging
import re

from serde import serde, field

from ..errors import FatalRtlBuddyError
from .pdk import (
    DEFAULT_PLACEMENT_DENSITY,
    DEFAULT_PLACEMENT_MACRO_HALO,
    DEFAULT_PLACEMENT_MACRO_CELL_HALO,
    DEFAULT_PLACEMENT_PADDING,
    DEFAULT_PLACEMENT_TIE_SEPARATION,
    PlacementFile,
    _validate_dont_use_cells,
    merge_dont_use_cells,
    validate_placement,
)

logger = logging.getLogger(__name__)


def _as_cell_list(value: str | list[str]) -> list[str]:
    """Return a cell-name key as a list; empty entries are dropped (the default `""` means not configured)."""
    if isinstance(value, str):
        return [value] if value else []
    return [c for c in value if c]


#: Valid name for a corner in `corners:`: it becomes an OpenSTA scene name, a Tcl list word and part of a report file name. A single `corner:` is not held to this.
_CORNER_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _validate_layer_adjustment(value, where: str) -> float | None:
    """Range-check `routing-layer-adjustment`, the fraction of each layer's routing capacity the global router withholds."""
    if value is None:
        return None
    if not 0.0 <= float(value) <= 1.0:
        raise FatalRtlBuddyError(
            f"{where}: routing-layer-adjustment must be a number from 0 to 1, "
            f"got {value!r}"
        )
    return float(value)


#: `clock_tree_synthesis -apply_ndr` values.
CTS_APPLY_NDR_VALUES = ("none", "root_only", "half", "full")


def _validate_cts_apply_ndr(value, where: str) -> str | None:
    if value is None:
        return None
    if value not in CTS_APPLY_NDR_VALUES:
        raise FatalRtlBuddyError(
            f"{where}: cts-apply-ndr must be one of "
            f"{', '.join(CTS_APPLY_NDR_VALUES)}, got {value!r}"
        )
    return value


def _validate_max_fanout(value, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FatalRtlBuddyError(
            f"{where}: max-fanout must be a positive integer, got {value!r}"
        )
    return value


def _first_set(*values):
    """Return the first value that is not `None`."""
    return next(v for v in values if v is not None)


@serde
class PnrRoutingLayersFile:
    signal: str = ""
    clock: str = ""


@serde
class PnrPlatformConfigFile:
    name: str
    pdk: str
    sta_corner: str = field(rename="corner", default="")
    # Exclusive with `corner:`; the first entry is primary. `None` (absent) differs from `[]` (an error). `str` is listed first so a scalar `corners: ss` is refused instead of becoming pyserde's ['s', 's'].
    sta_corners: str | list[str] | None = field(rename="corners", default=None)
    # With a list, CTS gets every entry as its buffer list and the first as the root buffer.
    cts_buffer: str | list[str] = field(rename="cts-buffer", default="")
    cts_sink_clustering: bool = field(rename="cts-sink-clustering", default=True)
    # Runs `repair_timing -setup` before the post-CTS hold repair.
    post_cts_setup_repair: bool = field(rename="post-cts-setup-repair", default=False)
    # Repairs hold again after global route, on `estimate_parasitics -global_routing`, as ORFS does.
    global_route_hold_repair: bool = field(
        rename="global-route-hold-repair", default=False
    )
    # `set_global_routing_layer_adjustment` over the signal layers; `None` leaves the router's default.
    routing_layer_adjustment: float | None = field(
        rename="routing-layer-adjustment", default=None
    )
    # `clock_tree_synthesis -apply_ndr`; `None` leaves the tool's default.
    cts_apply_ndr: str | None = field(rename="cts-apply-ndr", default=None)
    # `set_max_fanout` after each `read_sdc`; `None` leaves the Liberty/SDC/tool default. `float` is admitted so the validator, not pyserde, refuses a fraction.
    max_fanout: int | float | None = field(rename="max-fanout", default=None)
    routing_layers: PnrRoutingLayersFile = field(
        rename="routing-layers", default_factory=PnrRoutingLayersFile
    )
    # Overrides the PDK's `placement:` block field by field.
    placement: PlacementFile = field(default_factory=PlacementFile)
    # Added to the PDK's `dont-use-cells`, not replacing them.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)


class PnrPlatformConfig:
    """A PDK plus STA corner selection and P&R knobs (CTS buffer, routing layers, placement)."""

    def __init__(self, cfg: PnrPlatformConfigFile, pdk_lookup):
        self._name = cfg.name
        self._pdk_name = cfg.pdk
        self._pdk = pdk_lookup(cfg.pdk)
        self._sta_corners = self._resolve_sta_corners(cfg)
        self._sta_corner = self._sta_corners[0]
        self._cts_buffers = _as_cell_list(cfg.cts_buffer)
        self._cts_sink_clustering = cfg.cts_sink_clustering
        self._post_cts_setup_repair = cfg.post_cts_setup_repair
        self._global_route_hold_repair = cfg.global_route_hold_repair
        self._routing_layer_adjustment = _validate_layer_adjustment(
            cfg.routing_layer_adjustment, f"pnr platform '{self._name}'"
        )
        self._cts_apply_ndr = _validate_cts_apply_ndr(
            cfg.cts_apply_ndr, f"pnr platform '{self._name}'"
        )
        self._max_fanout = _validate_max_fanout(
            cfg.max_fanout, f"pnr platform '{self._name}'"
        )
        self._signal_layers = cfg.routing_layers.signal
        self._clock_layers = cfg.routing_layers.clock
        self._dont_use_cells = merge_dont_use_cells(
            self._pdk.get_dont_use_cells(),
            _validate_dont_use_cells(
                cfg.dont_use_cells, f"pnr platform '{self._name}'"
            ),
        )

        placement = validate_placement(cfg.placement, f"pnr platform '{self._name}'")
        self._placement_density = _first_set(
            placement.density,
            self._pdk.get_placement_density(),
            DEFAULT_PLACEMENT_DENSITY,
        )
        self._placement_padding = _first_set(
            placement.padding,
            self._pdk.get_placement_padding(),
            DEFAULT_PLACEMENT_PADDING,
        )
        self._placement_macro_halo = _first_set(
            placement.macro_halo,
            self._pdk.get_placement_macro_halo(),
            DEFAULT_PLACEMENT_MACRO_HALO,
        )
        self._placement_macro_cell_halo = _first_set(
            placement.macro_cell_halo,
            self._pdk.get_placement_macro_cell_halo(),
            DEFAULT_PLACEMENT_MACRO_CELL_HALO,
        )
        self._placement_reference_hpwl = (
            placement.reference_hpwl
            if placement.reference_hpwl is not None
            else self._pdk.get_placement_reference_hpwl()
        )
        self._placement_tie_separation = _first_set(
            placement.tie_separation,
            self._pdk.get_placement_tie_separation(),
            DEFAULT_PLACEMENT_TIE_SEPARATION,
        )

    def _resolve_sta_corners(self, cfg: PnrPlatformConfigFile) -> list[str]:
        """Return the analysis corners, primary first, validated against the PDK.

        Setting both `corner:` and `corners:` is an error. A one-entry `corners:` behaves like `corner:`.
        """
        where = f"pnr platform '{self._name}'"
        available = self._pdk.get_corners()
        if cfg.sta_corners is None:
            corner = cfg.sta_corner or self._pdk.get_default_corner()
            if corner not in available:
                raise FatalRtlBuddyError(
                    f"{where}: PDK '{self._pdk_name}' "
                    f"has no corner '{corner}'; "
                    f"available: {available}"
                )
            return [corner]

        if cfg.sta_corner:
            raise FatalRtlBuddyError(
                f"{where}: set either 'corner' or 'corners', not both "
                "(the first entry of 'corners' is the primary corner)"
            )
        corners = cfg.sta_corners
        if isinstance(corners, str):
            raise FatalRtlBuddyError(
                f"{where}: 'corners' is a list, e.g. corners: [{corners}]; "
                f"for one corner write corner: {corners}"
            )
        if not corners:
            raise FatalRtlBuddyError(
                f"{where}: 'corners' must name at least one corner of PDK "
                f"'{self._pdk_name}'; available: {available}"
            )
        seen: set[str] = set()
        for corner in corners:
            if not isinstance(corner, str) or not _CORNER_NAME_RE.fullmatch(corner):
                raise FatalRtlBuddyError(
                    f"{where}: corner name {corner!r} in 'corners' must be "
                    "letters, digits, '_', '.' or '-' (it becomes an OpenSTA "
                    "scene name and part of a report file name)"
                )
            if corner not in available:
                raise FatalRtlBuddyError(
                    f"{where}: PDK '{self._pdk_name}' "
                    f"has no corner '{corner}'; "
                    f"available: {available}"
                )
            if corner in seen:
                raise FatalRtlBuddyError(
                    f"{where}: corner '{corner}' is listed twice in 'corners'"
                )
            seen.add(corner)
        return list(corners)

    def get_name(self) -> str:
        return self._name

    def get_pdk(self):
        return self._pdk

    def get_pdk_name(self) -> str:
        return self._pdk_name

    def get_sta_corner(self) -> str:
        """The primary corner: `corner:`, or the first entry of `corners:`."""
        return self._sta_corner

    def get_sta_lib_paths(self) -> list[str]:
        """The primary corner's standard-cell Liberty files."""
        return self._pdk.get_corner_paths(self._sta_corner)

    def get_sta_corners(self) -> list[str]:
        """Every analysis corner, primary first."""
        return list(self._sta_corners)

    def is_multi_corner(self) -> bool:
        """Whether the flows analyse more than one corner."""
        return len(self._sta_corners) > 1

    def get_sta_corner_lib_paths(self) -> dict[str, list[str]]:
        """Each analysis corner's Liberty files, in `get_sta_corners` order."""
        return {c: self._pdk.get_corner_paths(c) for c in self._sta_corners}

    def get_cts_buffer(self) -> str:
        """The root clock buffer: the configured name, or the first of a list."""
        return self._cts_buffers[0] if self._cts_buffers else ""

    def get_cts_buffers(self) -> list[str]:
        """Every configured clock buffer, in config order."""
        return list(self._cts_buffers)

    def get_placement_density(self) -> float:
        """Global-placement target density, after platform/PDK/default."""
        return self._placement_density

    def get_placement_padding(self) -> int:
        """Global-placement cell padding, after platform/PDK/default."""
        return self._placement_padding

    def get_placement_macro_halo(self) -> float:
        """Macro halo in microns, after platform/PDK/default."""
        return self._placement_macro_halo

    def get_placement_macro_cell_halo(self) -> float:
        """Macro-to-row keep-out in microns, after platform/PDK/default."""
        return self._placement_macro_cell_halo

    def get_placement_tie_separation(self) -> float:
        """Tie-cell separation in microns, after platform/PDK/default."""
        return self._placement_tie_separation

    def get_placement_reference_hpwl(self) -> float | None:
        """Global-placement reference HPWL, platform over PDK, or `None` for the placer's own."""
        return self._placement_reference_hpwl

    def get_dont_use_cells(self) -> list[str]:
        """The PDK's excluded cells plus this platform's, PDK first."""
        return list(self._dont_use_cells)

    def get_cts_sink_clustering(self) -> bool:
        return self._cts_sink_clustering

    def get_post_cts_setup_repair(self) -> bool:
        """Whether post-CTS repair fixes setup before hold."""
        return self._post_cts_setup_repair

    def get_global_route_hold_repair(self) -> bool:
        """Whether hold is repaired again on global-route parasitics, before detail route."""
        return self._global_route_hold_repair

    def get_routing_layer_adjustment(self) -> float | None:
        """Global-route capacity adjustment, 0 to 1, or `None` for the router's default."""
        return self._routing_layer_adjustment

    def get_cts_apply_ndr(self) -> str | None:
        """The `clock_tree_synthesis -apply_ndr` value, or `None` for the tool's default."""
        return self._cts_apply_ndr

    def get_max_fanout(self) -> int | None:
        """The design max fanout set after `read_sdc`, or `None` to leave it unset."""
        return self._max_fanout

    def get_signal_layers(self) -> str:
        return self._signal_layers

    def get_clock_layers(self) -> str:
        return self._clock_layers
