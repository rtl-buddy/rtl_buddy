import logging

from serde import serde, field

from ..errors import FatalRtlBuddyError

logger = logging.getLogger(__name__)


@serde
class PnrRoutingLayersFile:
    signal: str = ""
    clock: str = ""


@serde
class PnrPlatformConfigFile:
    name: str
    pdk: str
    sta_corner: str = field(rename="corner", default="")
    cts_buffer: str = field(rename="cts-buffer", default="")
    routing_layers: PnrRoutingLayersFile = field(
        rename="routing-layers", default_factory=PnrRoutingLayersFile
    )


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
        self._cts_buffer = cfg.cts_buffer
        self._signal_layers = cfg.routing_layers.signal
        self._clock_layers = cfg.routing_layers.clock

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
        return self._cts_buffer

    def get_signal_layers(self) -> str:
        return self._signal_layers

    def get_clock_layers(self) -> str:
        return self._clock_layers
