import logging
import re

from serde import serde, field

from ..errors import FatalRtlBuddyError
from .pdk import (
    DEFAULT_PLACEMENT_DENSITY,
    DEFAULT_PLACEMENT_MACRO_HALO,
    DEFAULT_PLACEMENT_PADDING,
    PlacementFile,
    _validate_dont_use_cells,
    merge_dont_use_cells,
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


#: What a corner name has to look like once it is one of several (#104,
#: #105). Each name becomes an OpenSTA scene name, a word in a Tcl list and
#: part of a report file name (`power.<corner>.rpt`), so it is held to the
#: characters all three take unquoted. A single `corner:` is only ever a key
#: into `cfg-pdks.corners` and keeps accepting whatever the PDK spells.
_CORNER_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


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
    # Multi-corner signoff (#104, #105): several names from the PDK's
    # `corners:`, analysed together in one OpenROAD session. Mutually
    # exclusive with `corner:`; the first entry is the primary corner.
    # `None` (the key absent) is kept apart from `[]` so an empty list is
    # an error rather than a silent single-corner run.
    # `str` ahead of the list so a scalar `corners: ss` arrives as the
    # string it is, to be refused, rather than as pyserde's ['s', 's'].
    sta_corners: str | list[str] | None = field(rename="corners", default=None)
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
    # Cells this platform excludes on top of the PDK's `dont-use-cells`
    # (#656). Added to the PDK list, never replacing it.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)


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
        self._sta_corners = self._resolve_sta_corners(cfg)
        self._sta_corner = self._sta_corners[0]
        self._cts_buffers = _as_cell_list(cfg.cts_buffer)
        self._cts_sink_clustering = cfg.cts_sink_clustering
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

    def _resolve_sta_corners(self, cfg: PnrPlatformConfigFile) -> list[str]:
        """The analysis corners, primary first, validated against the PDK.

        `corner:` and `corners:` are two spellings of one choice, so a
        platform that writes both is refused rather than having one of them
        silently win. A one-entry `corners:` is the same run as `corner:`
        with that name — it renders the same Tcl and reports the same
        fields — so a list is only "multi-corner" from two entries up.
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

    def get_sta_lib_path(self) -> str:
        """The primary corner's Liberty."""
        return self._pdk.get_corner_path(self._sta_corner)

    def get_sta_corners(self) -> list[str]:
        """Every analysis corner, primary first. One entry for a single-corner
        platform."""
        return list(self._sta_corners)

    def is_multi_corner(self) -> bool:
        """Whether the flows analyse more than one corner (#104, #105)."""
        return len(self._sta_corners) > 1

    def get_sta_corner_lib_paths(self) -> dict[str, str]:
        """Each analysis corner's Liberty, in `get_sta_corners` order."""
        return {c: self._pdk.get_corner_path(c) for c in self._sta_corners}

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

    def get_dont_use_cells(self) -> list[str]:
        """The PDK's excluded cells plus this platform's, PDK first (#656)."""
        return list(self._dont_use_cells)

    def get_cts_sink_clustering(self) -> bool:
        return self._cts_sink_clustering

    def get_signal_layers(self) -> str:
        return self._signal_layers

    def get_clock_layers(self) -> str:
        return self._clock_layers
