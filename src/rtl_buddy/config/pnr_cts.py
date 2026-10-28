"""The mapping forms of two CTS keys of `rb pnr`: `cts-sink-clustering` and `cts-repair-clock-nets`.

Each key is a boolean or a mapping of options. A platform or a run value is read whole: a run's value replaces the platform's, options included.
"""

import math
from dataclasses import dataclass
from typing import Any

from serde import field, serde

from ..errors import FatalRtlBuddyError
from .unknown_keys import KeySpec


# Typed loosely so the readers name a bad value.
@serde
class PnrCtsSinkClusteringFile:
    enable: Any = None
    size: Any = None
    max_diameter: Any = field(rename="max-diameter", default=None)


@serde
class PnrCtsRepairClockNetsFile:
    max_wire_length: Any = field(rename="max-wire-length", default=None)


#: The keys of the two blocks, for the unknown-key check.
CTS_SINK_CLUSTERING_SPEC = KeySpec(PnrCtsSinkClusteringFile)
CTS_REPAIR_CLOCK_NETS_SPEC = KeySpec(PnrCtsRepairClockNetsFile)


@dataclass(frozen=True)
class CtsSinkClustering:
    """`cts-sink-clustering` read whole: `size` and `max_diameter` (microns) are `None` when CTS picks them."""

    enable: bool = True
    size: int | None = None
    max_diameter: float | None = None

    def options(self) -> str:
        """The `clock_tree_synthesis` options, without a leading space; `""` when clustering is off."""
        if not self.enable:
            return ""
        options = "-sink_clustering_enable"
        if self.size is not None:
            options += f" -sink_clustering_size {self.size}"
        if self.max_diameter is not None:
            options += f" -sink_clustering_max_diameter {fmt_number(self.max_diameter)}"
        return options

    def digest(self) -> bool | dict | None:
        """The value a hardened block's config digest records, or `None` for the platform default (enabled, CTS picks the sizes), which it leaves out."""
        if not self.enable:
            return False
        sizes = {
            key: value
            for key, value in (
                ("size", self.size),
                ("max_diameter", self.max_diameter),
            )
            if value is not None
        }
        return sizes or None


@dataclass(frozen=True)
class CtsRepairClockNets:
    """`cts-repair-clock-nets` read whole: `max_wire_length` (microns) is `None` for `repair_clock_nets` without options."""

    enable: bool = False
    max_wire_length: float | None = None

    def command(self) -> str:
        """The `repair_clock_nets` command, with its options."""
        if self.max_wire_length is None:
            return "repair_clock_nets"
        return f"repair_clock_nets -max_wire_length {fmt_number(self.max_wire_length)}"


def fmt_number(value: float) -> str:
    """Format a validated number as a Tcl word."""
    return f"{value:.10g}"


def _number(value) -> float | None:
    """`value` as a positive finite float, or `None` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def read_cts_sink_clustering(where: str, value) -> CtsSinkClustering | None:
    """Validate a `cts-sink-clustering` value, or `None` when unset; `where` names the platform or run."""
    if value is None:
        return None
    if isinstance(value, bool):
        return CtsSinkClustering(enable=value)
    if not isinstance(value, dict):
        raise FatalRtlBuddyError(
            f"{where}: cts-sink-clustering must be true, false or a mapping of "
            f"enable, size and max-diameter, got {value!r}"
        )
    enable = value.get("enable", True)
    if not isinstance(enable, bool):
        raise FatalRtlBuddyError(
            f"{where}: cts-sink-clustering.enable must be true or false, got {enable!r}"
        )
    size = value.get("size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size < 1
    ):
        raise FatalRtlBuddyError(
            f"{where}: cts-sink-clustering.size must be a positive integer, got {size!r}"
        )
    diameter = value.get("max-diameter")
    max_diameter = _number(diameter)
    if diameter is not None and max_diameter is None:
        raise FatalRtlBuddyError(
            f"{where}: cts-sink-clustering.max-diameter must be a positive number "
            f"of microns, got {diameter!r}"
        )
    return CtsSinkClustering(enable=enable, size=size, max_diameter=max_diameter)


def read_cts_repair_clock_nets(where: str, value) -> CtsRepairClockNets | None:
    """Validate a `cts-repair-clock-nets` value, or `None` when unset; `where` names the platform or run."""
    if value is None:
        return None
    if isinstance(value, bool):
        return CtsRepairClockNets(enable=value)
    if not isinstance(value, dict):
        raise FatalRtlBuddyError(
            f"{where}: cts-repair-clock-nets must be true, false or a mapping of "
            f"max-wire-length, got {value!r}"
        )
    length = value.get("max-wire-length")
    max_wire_length = _number(length)
    if length is not None and max_wire_length is None:
        raise FatalRtlBuddyError(
            f"{where}: cts-repair-clock-nets.max-wire-length must be a positive "
            f"number of microns, got {length!r}"
        )
    return CtsRepairClockNets(enable=True, max_wire_length=max_wire_length)
