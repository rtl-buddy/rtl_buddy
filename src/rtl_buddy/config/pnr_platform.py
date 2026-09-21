import logging

from serde import serde, field

from ..errors import FatalRtlBuddyError
from .pdk import (
    DEFAULT_PLACEMENT_DENSITY,
    DEFAULT_PLACEMENT_PADDING,
    PlacementFile,
    validate_placement,
)

logger = logging.getLogger(__name__)


def _as_cell_list(value: str | list[str]) -> list[str]:
    """A cell-name key as a list, whether it was written as one or many.

    `cts-buffer` took a single name before the buffer-list support and
    still does; a YAML list is taken entry by entry. Empty entries are
    dropped — the key's own default is `""`, which means "not
    configured", not "one nameless buffer".
    """
    if isinstance(value, str):
        return [value] if value else []
    return [c for c in value if c]


def _first_set(*values):
    """The first value that is not `None` — platform, then PDK, then default."""
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
    # One buffer name or a list of them. With a list, CTS is given every
    # entry as its buffer list and the first as the root buffer.
    cts_buffer: str | list[str] = field(rename="cts-buffer", default="")
    cts_sink_clustering: bool = field(rename="cts-sink-clustering", default=True)
    routing_layers: PnrRoutingLayersFile = field(
        rename="routing-layers", default_factory=PnrRoutingLayersFile
    )
    # Per-platform override of the PDK's `placement:` block, field by
    # field: the platform wins where it says something, the PDK where it
    # does not.
    placement: PlacementFile = field(default_factory=PlacementFile)


class PnrPlatformConfig:
    """A P&R-side view of a PDK + STA corner selection.

    Wraps a PdkConfig with P&R-specific knobs (CTS buffer, routing
    layer ranges). Floorplan-level details like die size / utilization
    live on the per-run pnr.yaml, not here.
    """

    def __init__(self, cfg: PnrPlatformConfigFile, pdk_lookup):
        self._name = cfg.name
        self._pdk_name = cfg.pdk
        self._pdk = pdk_lookup(cfg.pdk)
        self._sta_corner = cfg.sta_corner or self._pdk.get_default_corner()
        if self._sta_corner not in self._pdk.get_corners():
            raise FatalRtlBuddyError(
                f"pnr platform '{self._name}': PDK '{self._pdk_name}' "
                f"has no corner '{self._sta_corner}'; "
                f"available: {self._pdk.get_corners()}"
            )
        self._cts_buffers = _as_cell_list(cfg.cts_buffer)
        self._cts_sink_clustering = cfg.cts_sink_clustering
        self._signal_layers = cfg.routing_layers.signal
        self._clock_layers = cfg.routing_layers.clock

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

    def get_name(self) -> str:
        return self._name

    def get_pdk(self):
        return self._pdk

    def get_pdk_name(self) -> str:
        return self._pdk_name

    def get_sta_corner(self) -> str:
        return self._sta_corner

    def get_sta_lib_path(self) -> str:
        return self._pdk.get_corner_path(self._sta_corner)

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

    def get_cts_sink_clustering(self) -> bool:
        return self._cts_sink_clustering

    def get_signal_layers(self) -> str:
        return self._signal_layers

    def get_clock_layers(self) -> str:
        return self._clock_layers
