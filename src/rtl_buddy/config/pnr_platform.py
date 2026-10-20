import copy
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from serde import serde, field

from ..errors import FatalRtlBuddyError
from .pdk import (
    PLACEMENT_SPEC,
    DEFAULT_PLACEMENT_DENSITY,
    DEFAULT_PLACEMENT_MACRO_HALO,
    DEFAULT_PLACEMENT_MACRO_CELL_HALO,
    DEFAULT_PLACEMENT_PADDING,
    DEFAULT_PLACEMENT_TIE_SEPARATION,
    DEFAULT_ROTATED_MACRO_FOOTPRINT,
    MacroPinLimitsFile,
    PlacementFile,
    _validate_dont_use_cells,
    merge_dont_use_cells,
    validate_placement,
)
from .pnr_repair import (
    GLOBAL_ROUTE_REPAIR_SPEC,
    POST_ROUTE_REPAIR_SPEC,
    UNSET,
    GlobalRouteRepair,
    PostRouteRepair,
    read_global_route_repair,
    read_hold_repair_shorthand,
    read_post_route_repair,
    resolve_global_route_repair,
    resolve_post_route_repair,
)
from .pnr_signoff import SIGNOFF_SPEC, Signoff, read_signoff, resolve_signoff
from .unknown_keys import KeySpec

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


def _validate_port_buffer(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.split()) != 1:
        raise FatalRtlBuddyError(
            f"{where}: port-buffer must be one buffer cell name, got {value!r}"
        )
    return value


def _validate_cts_buffer(value, where: str) -> list[str]:
    cells = _as_cell_list(value) if isinstance(value, (str, list)) else None
    if not cells or any(not isinstance(c, str) or len(c.split()) != 1 for c in cells):
        raise FatalRtlBuddyError(
            f"{where}: cts-buffer must be one buffer cell name or a list of them, "
            f"got {value!r}"
        )
    return cells


#: Characters a `cts-clock-nets` entry cannot hold: it is written inside a braced Tcl list.
_TCL_LIST_BREAKERS = frozenset("{}\\")


def _validate_cts_clock_nets(value, where: str) -> tuple[str, ...]:
    nets = [value] if isinstance(value, str) else value
    if (
        not isinstance(nets, list)
        or not nets
        or any(
            not isinstance(n, str)
            or len(n.split()) != 1
            or n != n.strip()
            or _TCL_LIST_BREAKERS & set(n)
            for n in nets
        )
    ):
        raise FatalRtlBuddyError(
            f"{where}: cts-clock-nets must be one clock net name or a list of them, "
            "without whitespace, braces or backslashes, "
            f"got {value!r}"
        )
    return tuple(nets)


def _validate_tcl_path(key: str, value, where: str, config_dir: str) -> str | None:
    """Return a Tcl hook path resolved from `config_dir`, or `None` when unset."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise FatalRtlBuddyError(
            f"{where}: {key} must be a path to a Tcl file, got {value!r}"
        )
    return os.path.normpath(os.path.join(config_dir, value))


@serde
class PnrRoutingLayersFile:
    signal: str = ""
    clock: str = ""


@dataclass(frozen=True)
class PnrRunOverrides:
    """A P&R run's own values of its platform's routing, CTS, repair, tap-cell and signoff knobs; `None` (`UNSET` for `signoff`) keeps the platform's.

    Apply them with `PnrPlatformConfig.with_run_overrides`. `cts_clock_nets` exists on runs only.
    """

    signal_layers: str | None = None
    clock_layers: str | None = None
    cts_buffers: tuple[str, ...] | None = None
    cts_clock_nets: tuple[str, ...] = ()
    cts_repair_clock_nets: bool | None = None
    cts_latency_balancing: bool | None = None
    post_cts_setup_repair: bool | None = None
    routing_layer_adjustment: float | None = None
    port_buffer: str | None = None
    tapcell_tcl: str | None = None
    # The run's `signoff:` layer (see `pnr_signoff`): `UNSET` when absent, `None` when `null`.
    signoff: Any = UNSET

    def is_empty(self) -> bool:
        return self == PnrRunOverrides()


def read_run_overrides(
    where: str,
    *,
    routing_layers: "PnrRoutingLayersFile | None",
    cts_buffer,
    cts_clock_nets,
    cts_repair_clock_nets: bool | None,
    cts_latency_balancing: bool | None,
    post_cts_setup_repair: bool | None,
    routing_layer_adjustment,
    port_buffer,
    tapcell_tcl,
    config_dir: str,
    signoff=UNSET,
) -> PnrRunOverrides:
    """Validate a run's overrides of its platform's knobs; `where` names the run."""
    layers = routing_layers or PnrRoutingLayersFile()
    return PnrRunOverrides(
        signal_layers=layers.signal or None,
        clock_layers=layers.clock or None,
        cts_buffers=(
            tuple(_validate_cts_buffer(cts_buffer, where))
            if cts_buffer is not None
            else None
        ),
        cts_clock_nets=(
            _validate_cts_clock_nets(cts_clock_nets, where)
            if cts_clock_nets is not None
            else ()
        ),
        cts_repair_clock_nets=cts_repair_clock_nets,
        cts_latency_balancing=cts_latency_balancing,
        post_cts_setup_repair=post_cts_setup_repair,
        routing_layer_adjustment=_validate_layer_adjustment(
            routing_layer_adjustment, where
        ),
        port_buffer=_validate_port_buffer(port_buffer, where),
        tapcell_tcl=_validate_tcl_path("tapcell-tcl", tapcell_tcl, where, config_dir),
        signoff=read_signoff(where, signoff),
    )


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
    # `false` adds `clock_tree_synthesis -no_insertion_delay`.
    cts_latency_balancing: bool = field(rename="cts-latency-balancing", default=True)
    # `repair_clock_nets` after CTS.
    cts_repair_clock_nets: bool = field(rename="cts-repair-clock-nets", default=False)
    # Runs `repair_timing -setup` before the post-CTS hold repair.
    post_cts_setup_repair: bool = field(rename="post-cts-setup-repair", default=False)
    # Repairs hold again after global route, on `estimate_parasitics -global_routing`, as ORFS does: shorthand for `global-route-repair: {hold-margin: 0}`.
    global_route_hold_repair: bool = field(
        rename="global-route-hold-repair", default=False
    )
    # Repair after global route and after detailed route; a run overrides them field by field. Read by `pnr_repair`.
    global_route_repair: Any = field(rename="global-route-repair", default=UNSET)
    post_route_repair: Any = field(rename="post-route-repair", default=UNSET)
    # `set_global_routing_layer_adjustment` over the signal layers; `None` leaves the router's default.
    routing_layer_adjustment: float | None = field(
        rename="routing-layer-adjustment", default=None
    )
    # `clock_tree_synthesis -apply_ndr`; `None` leaves the tool's default.
    cts_apply_ndr: str | None = field(rename="cts-apply-ndr", default=None)
    # `buffer_ports -buffer_cell` for runs that buffer their ports; `None` lets OpenROAD pick. Typed loosely so the validator names a bad value.
    port_buffer: str | list | None = field(rename="port-buffer", default=None)
    # `set_max_fanout` after each `read_sdc`; `None` leaves the Liberty/SDC/tool default. `float` is admitted so the validator, not pyserde, refuses a fraction.
    max_fanout: int | float | None = field(rename="max-fanout", default=None)
    routing_layers: PnrRoutingLayersFile = field(
        rename="routing-layers", default_factory=PnrRoutingLayersFile
    )
    # Overrides the PDK's `placement:` block field by field.
    placement: PlacementFile = field(default_factory=PlacementFile)
    # Added to the PDK's `dont-use-cells`, not replacing them.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)
    # Replaces the PDK's `tapcell-tcl`; relative to the root config.
    tapcell_tcl: str | None = field(rename="tapcell-tcl", default=None)
    # Signoff corners and gates; a run overrides them field by field. Read by `pnr_signoff`.
    signoff: Any = UNSET


#: The keys of one `cfg-pnr-platforms` entry, for the unknown-key check.
PNR_PLATFORM_SPEC = KeySpec(
    PnrPlatformConfigFile,
    {
        "routing-layers": KeySpec(PnrRoutingLayersFile),
        "placement": PLACEMENT_SPEC,
        "global-route-repair": GLOBAL_ROUTE_REPAIR_SPEC,
        "post-route-repair": POST_ROUTE_REPAIR_SPEC,
        "signoff": SIGNOFF_SPEC,
    },
)


class PnrPlatformConfig:
    """A PDK plus STA corner selection and P&R knobs (CTS buffer, routing layers, placement)."""

    def __init__(self, cfg: PnrPlatformConfigFile, pdk_lookup, config_dir: str = ""):
        self._name = cfg.name
        self._pdk_name = cfg.pdk
        self._pdk = pdk_lookup(cfg.pdk)
        self._sta_corners = self._resolve_sta_corners(cfg)
        self._sta_corner = self._sta_corners[0]
        self._signoff_layer = read_signoff(f"pnr platform '{self._name}'", cfg.signoff)
        self._signoff = resolve_signoff(
            [self._signoff_layer], self._sta_corners, self._name
        )
        self._cts_buffers = _as_cell_list(cfg.cts_buffer)
        self._cts_sink_clustering = cfg.cts_sink_clustering
        self._cts_latency_balancing = cfg.cts_latency_balancing
        self._cts_repair_clock_nets = cfg.cts_repair_clock_nets
        self._cts_clock_nets: tuple[str, ...] = ()
        self._tapcell_tcl = _validate_tcl_path(
            "tapcell-tcl", cfg.tapcell_tcl, f"pnr platform '{self._name}'", config_dir
        )
        self._post_cts_setup_repair = cfg.post_cts_setup_repair
        self._read_repairs(cfg)
        self._routing_layer_adjustment = _validate_layer_adjustment(
            cfg.routing_layer_adjustment, f"pnr platform '{self._name}'"
        )
        self._cts_apply_ndr = _validate_cts_apply_ndr(
            cfg.cts_apply_ndr, f"pnr platform '{self._name}'"
        )
        self._port_buffer = _validate_port_buffer(
            cfg.port_buffer, f"pnr platform '{self._name}'"
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

        self._platform_placement = validate_placement(
            cfg.placement, f"pnr platform '{self._name}'"
        )
        self._resolve_placement(
            [self._platform_placement],
            f"pnr platform '{self._name}'",
            "set on the platform or its PDK",
        )

    def _read_repairs(self, cfg: PnrPlatformConfigFile) -> None:
        """Read the platform's repair blocks into layers a run's blocks go over. They are resolved per run (see `with_run_repairs`), so a partial platform block fails only the runs that leave it partial."""
        where = f"pnr platform '{self._name}'"
        gr_layer = read_global_route_repair(where, cfg.global_route_repair)
        self._global_route_repair_layer = read_hold_repair_shorthand(
            where, cfg.global_route_hold_repair or None, gr_layer
        )
        self._post_route_repair_layer = read_post_route_repair(
            where, cfg.post_route_repair
        )
        # Resolved on first use, or by `with_run_repairs`.
        self._repairs: (
            tuple[GlobalRouteRepair | None, PostRouteRepair | None] | None
        ) = None

    def _resolve_repairs(self, gr_run, pr_run, where: str) -> None:
        self._repairs = (
            resolve_global_route_repair(
                where, [self._global_route_repair_layer, gr_run]
            ),
            resolve_post_route_repair(where, [self._post_route_repair_layer, pr_run]),
        )

    def with_run_repairs(
        self, global_route_repair, post_route_repair, run: str
    ) -> "PnrPlatformConfig":
        """Return this platform with a P&R run's repair layers (see `pnr_repair`) over its own, field by field; raise when the merged block is incomplete."""
        resolved = copy.copy(self)
        resolved._resolve_repairs(
            global_route_repair,
            post_route_repair,
            f"pnr run '{run}' on platform '{self._name}'",
        )
        return resolved

    def _resolved_repairs(self):
        if self._repairs is None:
            self._resolve_repairs(UNSET, UNSET, f"pnr platform '{self._name}'")
        return self._repairs

    def _resolve_placement(
        self, layers: list[PlacementFile], where: str, levels: str
    ) -> None:
        """Resolve each `placement:` key from `layers` (highest first), then the PDK, then the default."""

        def _pick(key: str, pdk_value, default):
            for layer in layers:
                value = getattr(layer, key)
                if value is not None:
                    return value
            return pdk_value if pdk_value is not None else default

        pdk = self._pdk
        self._placement_density = _pick(
            "density", pdk.get_placement_density(), DEFAULT_PLACEMENT_DENSITY
        )
        self._placement_padding = _pick(
            "padding", pdk.get_placement_padding(), DEFAULT_PLACEMENT_PADDING
        )
        self._placement_macro_halo = _pick(
            "macro_halo", pdk.get_placement_macro_halo(), DEFAULT_PLACEMENT_MACRO_HALO
        )
        self._placement_macro_cell_halo = _pick(
            "macro_cell_halo",
            pdk.get_placement_macro_cell_halo(),
            DEFAULT_PLACEMENT_MACRO_CELL_HALO,
        )
        self._placement_reference_hpwl = _pick(
            "reference_hpwl", pdk.get_placement_reference_hpwl(), None
        )
        self._placement_tie_separation = _pick(
            "tie_separation",
            pdk.get_placement_tie_separation(),
            DEFAULT_PLACEMENT_TIE_SEPARATION,
        )
        self._placement_routability_driven = _pick(
            "routability_driven", pdk.get_placement_routability_driven(), False
        )
        self._placement_routability_use_grt = _pick(
            "routability_use_grt", pdk.get_placement_routability_use_grt(), False
        )
        self._placement_timing_driven = _pick(
            "timing_driven", pdk.get_placement_timing_driven(), False
        )
        self._placement_rotated_macro_footprint = _pick(
            "rotated_macro_footprint",
            pdk.get_placement_rotated_macro_footprint(),
            DEFAULT_ROTATED_MACRO_FOOTPRINT.value,
        )
        # A whole block: a higher level's `macro-pin-limits` replaces a lower one's.
        self._placement_macro_pin_limits = _pick(
            "macro_pin_limits", pdk.get_placement_macro_pin_limits(), None
        )
        # The placer reads `-routability_use_grt` only in routability mode, so alone it would be silently ignored.
        if (
            self._placement_routability_use_grt
            and not self._placement_routability_driven
        ):
            raise FatalRtlBuddyError(
                f"{where}: placement.routability-use-grt needs "
                f"placement.routability-driven: true ({levels}); "
                "global_placement ignores it otherwise"
            )

    def with_run_overrides(
        self, overrides: PnrRunOverrides | None
    ) -> "PnrPlatformConfig":
        """Return this platform with a P&R run's routing, CTS, repair and tap-cell values over its own, field by field.

        `overrides` must already be validated. Without any the platform itself is returned. A run's `signoff:` naming a corner the platform does not analyse raises.
        """
        if overrides is None or overrides.is_empty():
            return self
        resolved = copy.copy(self)
        o = overrides
        if o.signoff is not UNSET:
            resolved._signoff = resolve_signoff(
                [self._signoff_layer, o.signoff], self._sta_corners, self._name
            )
        for attr, value in (
            ("_signal_layers", o.signal_layers),
            ("_clock_layers", o.clock_layers),
            ("_cts_buffers", list(o.cts_buffers) if o.cts_buffers else None),
            ("_cts_clock_nets", o.cts_clock_nets or None),
            ("_cts_repair_clock_nets", o.cts_repair_clock_nets),
            ("_cts_latency_balancing", o.cts_latency_balancing),
            ("_post_cts_setup_repair", o.post_cts_setup_repair),
            ("_routing_layer_adjustment", o.routing_layer_adjustment),
            ("_port_buffer", o.port_buffer),
            ("_tapcell_tcl", o.tapcell_tcl),
        ):
            if value is not None:
                setattr(resolved, attr, value)
        return resolved

    def with_run_placement(
        self, placement: PlacementFile | None, run: str
    ) -> "PnrPlatformConfig":
        """Return this platform with a P&R run's `placement:` block over its own, field by field.

        `placement` must already be validated. Without one the platform itself is returned.
        """
        if placement is None:
            return self
        resolved = copy.copy(self)
        resolved._resolve_placement(
            [placement, self._platform_placement],
            f"pnr run '{run}' on platform '{self._name}'",
            "set on the run, the platform or its PDK",
        )
        return resolved

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

    def get_placement_routability_driven(self) -> bool:
        """Whether global placement runs `-routability_driven`, platform over PDK, default `False`."""
        return self._placement_routability_driven

    def get_placement_routability_use_grt(self) -> bool:
        """Whether global placement passes `-routability_use_grt`, platform over PDK, default `False`."""
        return self._placement_routability_use_grt

    def get_placement_timing_driven(self) -> bool:
        """Whether global placement runs `-timing_driven`, run over platform over PDK, default `False`."""
        return self._placement_timing_driven

    def get_placement_rotated_macro_footprint(self) -> str:
        """How the flow handles a macro rotated by 90 degrees (`block`, `diamond-legalizer` or `none`), platform over PDK, default `block`."""
        return self._placement_rotated_macro_footprint

    def get_placement_macro_pin_limits(self) -> MacroPinLimitsFile | None:
        """`placement.macro-pin-limits`, run over platform over PDK, or `None` when the step is off."""
        return self._placement_macro_pin_limits

    def get_dont_use_cells(self) -> list[str]:
        """The PDK's excluded cells plus this platform's, PDK first."""
        return list(self._dont_use_cells)

    def get_cts_sink_clustering(self) -> bool:
        return self._cts_sink_clustering

    def get_cts_latency_balancing(self) -> bool:
        """Whether CTS balances latency between clock branches; `False` adds `-no_insertion_delay`."""
        return self._cts_latency_balancing

    def get_cts_repair_clock_nets(self) -> bool:
        """Whether `repair_clock_nets` runs after CTS."""
        return self._cts_repair_clock_nets

    def get_cts_clock_nets(self) -> tuple[str, ...]:
        """The clock nets CTS is restricted to (`-clk_nets`), or `()` for every clock."""
        return self._cts_clock_nets

    def get_tapcell_tcl(self) -> str:
        """The tap and endcap Tcl: the run's, the platform's or the PDK's, resolved; `""` when none."""
        if self._tapcell_tcl is not None:
            return self._tapcell_tcl
        return self._pdk.get_tapcell_tcl()

    def get_tcl_hooks(self) -> dict[str, str]:
        """The PDK's Tcl hooks as `{yaml-key: resolved path}`, with this platform's or run's `tapcell-tcl` in place of the PDK's; unset ones omitted, in flow order."""
        hooks = dict(self._pdk.get_tcl_hooks())
        # `tapcell-tcl` is the last hook in flow order, so a new entry keeps the order.
        if self._tapcell_tcl is not None:
            hooks["tapcell-tcl"] = self._tapcell_tcl
        return hooks

    def get_signoff(self) -> Signoff | None:
        """The signoff corners and gates, run over platform, or None when neither sets `signoff:`."""
        return self._signoff

    def get_post_cts_setup_repair(self) -> bool:
        """Whether post-CTS repair fixes setup before hold."""
        return self._post_cts_setup_repair

    def get_global_route_repair(self) -> GlobalRouteRepair | None:
        """The repair after global route, platform under run, or None when off."""
        return self._resolved_repairs()[0]

    def get_post_route_repair(self) -> PostRouteRepair | None:
        """The repair rounds after detailed route, platform under run, or None when off."""
        return self._resolved_repairs()[1]

    def get_routing_layer_adjustment(self) -> float | None:
        """Global-route capacity adjustment, 0 to 1, or `None` for the router's default."""
        return self._routing_layer_adjustment

    def get_cts_apply_ndr(self) -> str | None:
        """The `clock_tree_synthesis -apply_ndr` value, or `None` for the tool's default."""
        return self._cts_apply_ndr

    def get_port_buffer(self) -> str | None:
        """The `buffer_ports -buffer_cell` cell, or `None` for OpenROAD's choice."""
        return self._port_buffer

    def get_max_fanout(self) -> int | None:
        """The design max fanout set after `read_sdc`, or `None` to leave it unset."""
        return self._max_fanout

    def get_signal_layers(self) -> str:
        return self._signal_layers

    def get_clock_layers(self) -> str:
        return self._clock_layers
