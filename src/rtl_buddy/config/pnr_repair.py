"""Timing-repair knobs of `rb pnr`: `global-route-repair` and `post-route-repair`, set on a P&R platform and overridden by a run field by field, and the run's `mode`.

A block is read in two steps. :func:`read_global_route_repair` and :func:`read_post_route_repair` turn one level's YAML into a layer: the keys it writes, validated, with `null` kept as a value (skip that repair), or `None` for a block written as `null` (off). :func:`resolve_global_route_repair` and :func:`resolve_post_route_repair` merge the platform's layer and the run's into the repair the flow runs. Rules about the whole block (it must repair something; several rounds need a margin) apply only there, to the merged block a run uses, so a platform may set part of a block that its runs complete.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from serde import field, serde

from ..errors import FatalRtlBuddyError
from .unknown_keys import KeySpec


class _Unset:
    """A key the YAML does not write, as opposed to one written as `null`."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


class PnrMode(StrEnum):
    """A run's `mode`: `closure` runs the whole flow; `probe` skips timing repair and stops after global route."""

    CLOSURE = "closure"
    PROBE = "probe"


class Reroute(StrEnum):
    """How `global-route-repair` reroutes after repair: the changed nets only, or a fresh global route."""

    INCREMENTAL = "incremental"
    FULL = "full"


# Every field is typed loosely with an `UNSET` default, so a key written as `null` differs from an absent one and the readers below name a bad value.
@serde
class RepairDesignFile:
    slew_margin: Any = field(rename="slew-margin", default=UNSET)
    cap_margin: Any = field(rename="cap-margin", default=UNSET)
    max_wire_length: Any = field(rename="max-wire-length", default=UNSET)


@serde
class GlobalRouteRepairFile:
    repair_design: Any = field(rename="repair-design", default=UNSET)
    setup_margin: Any = field(rename="setup-margin", default=UNSET)
    hold_margin: Any = field(rename="hold-margin", default=UNSET)
    reroute: Any = UNSET


@serde
class PostRouteRepairFile:
    repair_design: Any = field(rename="repair-design", default=UNSET)
    setup_margin: Any = field(rename="setup-margin", default=UNSET)
    hold_margin: Any = field(rename="hold-margin", default=UNSET)
    rounds: Any = UNSET
    margin_step: Any = field(rename="margin-step", default=UNSET)


#: The keys of each block, for the unknown-key check.
GLOBAL_ROUTE_REPAIR_SPEC = KeySpec(
    GlobalRouteRepairFile, {"repair-design": KeySpec(RepairDesignFile)}
)
POST_ROUTE_REPAIR_SPEC = KeySpec(
    PostRouteRepairFile, {"repair-design": KeySpec(RepairDesignFile)}
)


@dataclass(frozen=True)
class RepairDesign:
    """`repair_design` options; `None` leaves OpenROAD's default."""

    slew_margin: float | None = None
    cap_margin: float | None = None
    max_wire_length: float | None = None

    def options(self) -> str:
        """The `repair_design` options, each with a leading space."""
        return "".join(
            f" -{name} {_fmt(value)}"
            for name, value in (
                ("slew_margin", self.slew_margin),
                ("cap_margin", self.cap_margin),
                ("max_wire_length", self.max_wire_length),
            )
            if value is not None
        )


@dataclass(frozen=True)
class GlobalRouteRepair:
    """The repair after global route, on global-route parasitics. Margins are in ns; `None` skips that repair."""

    repair_design: RepairDesign | None = None
    setup_margin: float | None = None
    hold_margin: float | None = None
    reroute: Reroute = Reroute.INCREMENTAL


@dataclass(frozen=True)
class PostRouteRepair:
    """The repair rounds after detailed route, on extracted parasitics. Margins are in ns; `None` skips that repair."""

    repair_design: RepairDesign | None = None
    setup_margin: float | None = None
    hold_margin: float | None = None
    rounds: int = 1
    margin_step: float = 0.0

    def sides(self) -> tuple[str, ...]:
        """The timing sides the rounds repair, `setup` and `hold`, in that order: each whose margin is set."""
        return tuple(
            side
            for side, margin in (
                ("setup", self.setup_margin),
                ("hold", self.hold_margin),
            )
            if margin is not None
        )

    def margins(self, round_: int) -> tuple[float | None, float | None]:
        """The setup and hold margins of round `round_` (from 1): each set margin plus `margin-step` for every earlier round."""
        step = self.margin_step * (round_ - 1)
        return tuple(
            None if m is None else round(m + step, 12)
            for m in (self.setup_margin, self.hold_margin)
        )


def _fmt(value: float) -> str:
    """A number as Tcl reads it, without float noise."""
    return f"{value:.10g}"


def _number(
    where: str,
    key: str,
    value,
    *,
    low: float,
    high: float | None = None,
    nullable: bool = True,
):
    """Return `value` as a float in [low, high], or raise naming `key` and, when `nullable`, that `null` is accepted too."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < low
        or (high is not None and value > high)
    ):
        bound = f"from {low:g} to {high:g}" if high is not None else f">= {low:g}"
        alternative = " or null" if nullable else ""
        raise FatalRtlBuddyError(
            f"{where}.{key} must be a number {bound}{alternative}, got {value!r}"
        )
    return float(value)


def _block(where: str, raw, keys: dict):
    """Read one mapping into `{attr: value}`, `UNSET` for a key it does not write; `None` for `null`. Unknown keys were refused at load."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FatalRtlBuddyError(
            f"{where} must be a mapping of {', '.join(keys)} or null, got {raw!r}"
        )
    return {attr: raw.get(key, UNSET) for key, attr in keys.items()}


_REPAIR_DESIGN_KEYS = {
    "slew-margin": "slew_margin",
    "cap-margin": "cap_margin",
    "max-wire-length": "max_wire_length",
}


def _read_repair_design(where: str, raw):
    block = _block(where, raw, _REPAIR_DESIGN_KEYS)
    if block is None:
        return None
    layer = {}
    for key, attr in _REPAIR_DESIGN_KEYS.items():
        value = block[attr]
        if value is UNSET:
            continue
        if value is None:
            layer[attr] = None
        elif attr == "max_wire_length":
            layer[attr] = _number(where, key, value, low=0.0)
            if layer[attr] == 0.0:
                raise FatalRtlBuddyError(
                    f"{where}.max-wire-length must be a positive length in microns "
                    "or null, got 0"
                )
        else:
            layer[attr] = _number(where, key, value, low=0.0, high=100.0)
    return layer


def _read_margins(where: str, block: dict) -> dict:
    layer = {}
    if block["repair_design"] is not UNSET:
        layer["repair_design"] = _read_repair_design(
            f"{where}.repair-design", block["repair_design"]
        )
    for key, attr in (("setup-margin", "setup_margin"), ("hold-margin", "hold_margin")):
        value = block[attr]
        if value is UNSET:
            continue
        layer[attr] = None if value is None else _number(where, key, value, low=0.0)
    return layer


_GLOBAL_ROUTE_KEYS = {
    "repair-design": "repair_design",
    "setup-margin": "setup_margin",
    "hold-margin": "hold_margin",
    "reroute": "reroute",
}
_POST_ROUTE_KEYS = {
    "repair-design": "repair_design",
    "setup-margin": "setup_margin",
    "hold-margin": "hold_margin",
    "rounds": "rounds",
    "margin-step": "margin_step",
}


def read_global_route_repair(where: str, raw):
    """Return one level's `global-route-repair` as a layer: `UNSET` when absent, `None` when `null`, else the keys it writes, validated."""
    where = f"{where}: global-route-repair"
    if raw is UNSET:
        return UNSET
    block = _block(where, raw, _GLOBAL_ROUTE_KEYS)
    if block is None:
        return None
    layer = _read_margins(where, block)
    reroute = block["reroute"]
    if reroute is not UNSET:
        try:
            layer["reroute"] = Reroute(reroute)
        except ValueError:
            raise FatalRtlBuddyError(
                f"{where}.reroute must be one of "
                f"{', '.join(r.value for r in Reroute)}, got {reroute!r}"
            ) from None
    return layer


def read_post_route_repair(where: str, raw):
    """Return one level's `post-route-repair` as a layer: `UNSET` when absent, `None` when `null`, else the keys it writes, validated."""
    where = f"{where}: post-route-repair"
    if raw is UNSET:
        return UNSET
    block = _block(where, raw, _POST_ROUTE_KEYS)
    if block is None:
        return None
    layer = _read_margins(where, block)
    rounds = block["rounds"]
    if rounds is not UNSET:
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
            raise FatalRtlBuddyError(
                f"{where}.rounds must be a positive integer, got {rounds!r}"
            )
        layer["rounds"] = rounds
    step = block["margin_step"]
    if step is not UNSET:
        if step is None:
            raise FatalRtlBuddyError(
                f"{where}.margin-step must be a number >= 0, got null"
            )
        layer["margin_step"] = _number(
            where, "margin-step", step, low=0.0, nullable=False
        )
    return layer


def _merge(layers) -> dict | None:
    """Merge layers, lowest first: a `None` layer turns the block off, a later layer's keys win field by field, `repair_design` merging the same way."""
    merged = None
    for layer in layers:
        if layer is UNSET:
            continue
        if layer is None:
            merged = None
            continue
        merged = dict(merged or {})
        for attr, value in layer.items():
            if attr == "repair_design" and value is not None:
                value = {**(merged.get("repair_design") or {}), **value}
            merged[attr] = value
    return merged


def _repairs(where: str, block: str, merged: dict) -> RepairDesign | None:
    design = merged.get("repair_design")
    if (
        design is None
        and merged.get("setup_margin") is None
        and merged.get("hold_margin") is None
    ):
        raise FatalRtlBuddyError(
            f"{where}: {block} repairs nothing: set repair-design, setup-margin or "
            f"hold-margin, or write {block}: null to turn it off"
        )
    return RepairDesign(**design) if design is not None else None


def resolve_global_route_repair(where: str, layers) -> GlobalRouteRepair | None:
    """The global-route repair `layers` (platform first, then run) configure, or None when off."""
    merged = _merge(layers)
    if merged is None:
        return None
    return GlobalRouteRepair(
        repair_design=_repairs(where, "global-route-repair", merged),
        setup_margin=merged.get("setup_margin"),
        hold_margin=merged.get("hold_margin"),
        reroute=merged.get("reroute", Reroute.INCREMENTAL),
    )


def resolve_post_route_repair(where: str, layers) -> PostRouteRepair | None:
    """The post-route repair `layers` (platform first, then run) configure, or None when off.

    More than one round needs a setup or hold margin: the rounds stop when the sides they repair are met, and a block without either has no such side.
    """
    merged = _merge(layers)
    if merged is None:
        return None
    repair = PostRouteRepair(
        repair_design=_repairs(where, "post-route-repair", merged),
        setup_margin=merged.get("setup_margin"),
        hold_margin=merged.get("hold_margin"),
        rounds=merged.get("rounds", 1),
        margin_step=merged.get("margin_step", 0.0),
    )
    if repair.rounds > 1 and not repair.sides():
        raise FatalRtlBuddyError(
            f"{where}: post-route-repair.rounds is {repair.rounds}, but the block "
            "sets no setup-margin or hold-margin: the rounds stop once the sides "
            "they repair meet timing, so a repair-design-only block runs one round"
        )
    return repair


#: The layer `global-route-hold-repair: true` stands for: a hold-only repair with no margin.
HOLD_REPAIR_SHORTHAND = {"hold_margin": 0.0}


def read_mode(where: str, value) -> PnrMode:
    """Return a run's `mode`, default `closure`."""
    if value is None:
        return PnrMode.CLOSURE
    try:
        return PnrMode(value)
    except ValueError:
        raise FatalRtlBuddyError(
            f"{where}: mode must be one of {', '.join(m.value for m in PnrMode)}, "
            f"got {value!r}"
        ) from None
