"""`signoff:` of `rb pnr`: the corners each timing side is judged at and the gates that fail a routed run, set on a P&R platform and overridden by a run field by field.

:func:`read_signoff` turns one level's YAML into a layer: the keys it writes, validated, with `null` kept as a value (that key's default), or `None` for a block written as `null` (no signoff). :func:`resolve_signoff` merges the platform's layer and the run's and checks the corners against the platform's.
"""

from dataclasses import dataclass, field as dc_field
from typing import Any

from serde import field, serde

from ..errors import FatalRtlBuddyError
from .pnr_repair import UNSET
from .unknown_keys import KeySpec

#: The gates `fail-on` can name, in the order a result and a description list them.
SIGNOFF_GATES = ("timing", "drc", "electrical")


@serde
class SignoffFile:
    setup_corners: Any = field(rename="setup-corners", default=UNSET)
    hold_corners: Any = field(rename="hold-corners", default=UNSET)
    fail_on: Any = field(rename="fail-on", default=UNSET)


#: The keys of the block, for the unknown-key check.
SIGNOFF_SPEC = KeySpec(SignoffFile)

_KEYS = {
    "setup-corners": "setup_corners",
    "hold-corners": "hold_corners",
    "fail-on": "fail_on",
}


@dataclass(frozen=True)
class SignoffLayer:
    """One level's `signoff:` keys: `UNSET` where it does not write one, `None` where it writes `null` (the default). `where` names the level in errors."""

    setup_corners: Any = UNSET
    hold_corners: Any = UNSET
    fail_on: Any = UNSET
    where: str = dc_field(default="", compare=False)


@dataclass(frozen=True)
class Signoff:
    """A run's resolved signoff: the corners its setup and hold are judged at, in the platform's order, and the gates in `SIGNOFF_GATES` order that fail it."""

    setup_corners: tuple[str, ...]
    hold_corners: tuple[str, ...]
    fail_on: tuple[str, ...] = ()

    def corners(self, side: str) -> tuple[str, ...]:
        """The corners `side` (`setup` or `hold`) is judged at."""
        return self.setup_corners if side == "setup" else self.hold_corners


def _names(where: str, key: str, value, what: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise FatalRtlBuddyError(f"{where}.{key} is a list, e.g. {key}: [{value}]")
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(v, str) or not v for v in value)
    ):
        raise FatalRtlBuddyError(
            f"{where}.{key} must be a non-empty list of {what}, got {value!r}"
        )
    repeated = sorted({v for v in value if value.count(v) > 1})
    if repeated:
        raise FatalRtlBuddyError(f"{where}.{key} lists {', '.join(repeated)} twice")
    return tuple(value)


def effective_gates(
    signoff: "Signoff | None", fail_on_electrical: bool
) -> tuple[str, ...]:
    """The gates that fail a routed run, in `SIGNOFF_GATES` order: `signoff.fail-on`, with `electrical` added by `fail-on-electrical: true`."""
    gates = set(signoff.fail_on) if signoff else set()
    if fail_on_electrical:
        gates.add("electrical")
    return tuple(g for g in SIGNOFF_GATES if g in gates)


def read_signoff(where: str, raw):
    """Return one level's `signoff:` as a layer: `UNSET` when absent, `None` when `null`, else a `SignoffLayer` of the keys it writes, validated. `where` names the level."""
    if raw is UNSET:
        return UNSET
    if raw is None:
        return None
    where = f"{where}: signoff"
    if not isinstance(raw, dict):
        raise FatalRtlBuddyError(
            f"{where} must be a mapping of {', '.join(_KEYS)} or null, got {raw!r}"
        )
    layer = {}
    for key, attr in _KEYS.items():
        value = raw.get(key, UNSET)
        if value is UNSET or value is None:
            layer[attr] = value
        elif attr == "fail_on":
            if value == []:
                layer[attr] = ()
                continue
            gates = _names(where, key, value, "gates")
            unknown = [g for g in gates if g not in SIGNOFF_GATES]
            if unknown:
                raise FatalRtlBuddyError(
                    f"{where}.{key} names unknown gate(s) {', '.join(unknown)}; "
                    f"the gates are {', '.join(SIGNOFF_GATES)}"
                )
            layer[attr] = gates
        else:
            layer[attr] = _names(where, key, value, "corner names")
    return SignoffLayer(**layer, where=where)


def resolve_signoff(layers, corners: list[str], platform: str) -> Signoff | None:
    """The signoff `layers` (platform first, then run) configure for a platform of `corners`, or None when no layer sets one or the last to set the block writes `null`.

    A later layer's keys win field by field. Corners default to every corner, `fail-on` to no gate. A corner the platform does not analyse is an error naming it.
    """
    merged: dict | None = None
    for layer in layers:
        if layer is UNSET:
            continue
        if layer is None:
            merged = None
            continue
        merged = dict(merged or {})
        for attr in _KEYS.values():
            value = getattr(layer, attr)
            if value is not UNSET:
                merged[attr] = (value, layer.where)
    if merged is None:
        return None
    resolved = {}
    for side in ("setup", "hold"):
        value, where = merged.get(f"{side}_corners", (None, ""))
        if value is None:
            resolved[f"{side}_corners"] = tuple(corners)
            continue
        unknown = [c for c in value if c not in corners]
        if unknown:
            raise FatalRtlBuddyError(
                f"{where}.{side}-corners names corner(s) {', '.join(unknown)}, "
                f"which platform '{platform}' does not analyse; its corners: "
                f"{', '.join(corners)}"
            )
        resolved[f"{side}_corners"] = tuple(c for c in corners if c in value)
    gates = merged.get("fail_on", (None, ""))[0] or ()
    return Signoff(
        **resolved, fail_on=tuple(g for g in SIGNOFF_GATES if g in set(gates))
    )
