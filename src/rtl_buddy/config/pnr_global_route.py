"""The `global-route:` block of `rb pnr`: `global_route` options set on a P&R platform and overridden by a run field by field.

OpenROAD resets `-grid_origin` and `-allow_congestion` on every `global_route` call, incremental ones included, so the flow passes them to each call it makes. The other options apply to full routes only.
"""

from dataclasses import dataclass, fields
from typing import Any

from serde import field, serde

from ..errors import FatalRtlBuddyError
from .unknown_keys import KeySpec


# Typed loosely so `read_global_route` names a bad value.
@serde
class PnrGlobalRouteFile:
    congestion_iterations: Any = field(rename="congestion-iterations", default=None)
    congestion_report_iter_step: Any = field(
        rename="congestion-report-iter-step", default=None
    )
    grid_origin: Any = field(rename="grid-origin", default=None)
    layer_adjustment: Any = field(rename="layer-adjustment", default=None)
    allow_congestion: Any = field(rename="allow-congestion", default=None)
    critical_nets_percentage: Any = field(
        rename="critical-nets-percentage", default=None
    )
    resistance_aware: Any = field(rename="resistance-aware", default=None)


#: The keys of the block, for the unknown-key check.
GLOBAL_ROUTE_SPEC = KeySpec(PnrGlobalRouteFile)

#: The congestion iterations of a full route when `congestion-iterations` is unset: ORFS's setting.
DEFAULT_CONGESTION_ITERATIONS = 30

#: What these fields route as when unset; the digest leaves such a value out.
_ROUTE_DEFAULTS = {
    "allow_congestion": False,
    "resistance_aware": False,
}

#: Fields that change only what global route reports, left out of the digest.
_REPORTING_ONLY = ("congestion_report_iter_step",)

#: Characters a layer name cannot hold: it is written as a bare Tcl word.
_TCL_WORD_BREAKERS = frozenset('{}[]$"\\;')


@dataclass(frozen=True)
class GlobalRoute:
    """A level's `global-route:` values, or the merged values a run uses; `None` is unset."""

    congestion_iterations: int | None = None
    congestion_report_iter_step: int | None = None
    grid_origin: tuple[int, int] | None = None
    # Layer name -> fraction of its capacity withheld, in the order written.
    layer_adjustment: tuple[tuple[str, float], ...] | None = None
    allow_congestion: bool | None = None
    critical_nets_percentage: float | None = None
    resistance_aware: bool | None = None

    def is_empty(self) -> bool:
        return self == GlobalRoute()

    def over(self, base: "GlobalRoute") -> "GlobalRoute":
        """These values over `base`'s, field by field."""
        return GlobalRoute(
            **{
                f.name: (
                    getattr(self, f.name)
                    if getattr(self, f.name) is not None
                    else getattr(base, f.name)
                )
                for f in fields(self)
            }
        )

    def effective_congestion_iterations(self) -> int:
        """`congestion-iterations`, or the default when unset."""
        return (
            self.congestion_iterations
            if self.congestion_iterations is not None
            else DEFAULT_CONGESTION_ITERATIONS
        )

    def full_options(self) -> str:
        """The options of a full `global_route` other than its report file and `-verbose`, each with a leading space."""
        options = f" -congestion_iterations {self.effective_congestion_iterations()}"
        if self.congestion_report_iter_step is not None:
            options += (
                f" -congestion_report_iter_step {self.congestion_report_iter_step}"
            )
        if self.critical_nets_percentage is not None:
            options += (
                f" -critical_nets_percentage {_fmt(self.critical_nets_percentage)}"
            )
        if self.resistance_aware:
            options += " -resistance_aware"
        return options + self.every_call_options()

    def every_call_options(self) -> str:
        """The options every `global_route` call needs, incremental ones included, since OpenROAD resets them per call."""
        options = ""
        if self.grid_origin is not None:
            options += f" -grid_origin {{{self.grid_origin[0]} {self.grid_origin[1]}}}"
        if self.allow_congestion:
            options += " -allow_congestion"
        return options

    def digest(self) -> dict:
        """The values that change the route, by YAML key, for a hardened block's config digest.

        The report step changes only what is reported, and a value equal to rb's default routes as leaving it unset does, so neither is in it. The congestion iterations are always in it, at their effective value.
        """
        out: dict = {"congestion-iterations": self.effective_congestion_iterations()}
        for f in fields(self):
            value = getattr(self, f.name)
            if (
                value is None
                or f.name == "congestion_iterations"
                or f.name in _REPORTING_ONLY
                or (f.name in _ROUTE_DEFAULTS and value == _ROUTE_DEFAULTS[f.name])
            ):
                continue
            if f.name == "grid_origin":
                value = list(value)
            elif f.name == "layer_adjustment":
                value = dict(value)
            out[f.name.replace("_", "-")] = value
        return out


def _fmt(value: float) -> str:
    return f"{value:.10g}"


def _int(key: str, value, where: str, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        kind = "a positive integer" if minimum > 0 else "a non-negative integer"
        raise FatalRtlBuddyError(
            f"{where}: global-route.{key} must be {kind}, got {value!r}"
        )
    return value


def _bool(key: str, value, where: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise FatalRtlBuddyError(
        f"{where}: global-route.{key} must be true or false, got {value!r}"
    )


def _fraction(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _grid_origin(value, where: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in value)
    ):
        raise FatalRtlBuddyError(
            f"{where}: global-route.grid-origin must be [x, y], two non-negative "
            f"integers in DBU, got {value!r}"
        )
    return (value[0], value[1])


def _layer_adjustment(value, where: str) -> tuple[tuple[str, float], ...] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FatalRtlBuddyError(
            f"{where}: global-route.layer-adjustment must map routing layer names "
            f"to numbers from 0 to 1, got {value!r}"
        )
    out = []
    for layer, adjustment in value.items():
        fraction = _fraction(adjustment)
        if (
            not isinstance(layer, str)
            or len(layer.split()) != 1
            or layer != layer.strip()
            or _TCL_WORD_BREAKERS & set(layer)
            or fraction is None
        ):
            raise FatalRtlBuddyError(
                f"{where}: global-route.layer-adjustment must map routing layer "
                f"names to numbers from 0 to 1, got {layer!r}: {adjustment!r}"
            )
        out.append((layer, fraction))
    return tuple(out)


def read_global_route(where: str, block: PnrGlobalRouteFile | None) -> GlobalRoute:
    """Validate one level's `global-route:` block; `where` names the platform or run."""
    if block is None:
        return GlobalRoute()
    percentage = block.critical_nets_percentage
    if percentage is not None and (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not 0 <= percentage <= 100
    ):
        raise FatalRtlBuddyError(
            f"{where}: global-route.critical-nets-percentage must be a number from "
            f"0 to 100, got {percentage!r}"
        )
    return GlobalRoute(
        congestion_iterations=_int(
            "congestion-iterations", block.congestion_iterations, where, 1
        ),
        congestion_report_iter_step=_int(
            "congestion-report-iter-step", block.congestion_report_iter_step, where, 0
        ),
        grid_origin=_grid_origin(block.grid_origin, where),
        layer_adjustment=_layer_adjustment(block.layer_adjustment, where),
        allow_congestion=_bool("allow-congestion", block.allow_congestion, where),
        critical_nets_percentage=(
            float(percentage) if percentage is not None else None
        ),
        resistance_aware=_bool("resistance-aware", block.resistance_aware, where),
    )
