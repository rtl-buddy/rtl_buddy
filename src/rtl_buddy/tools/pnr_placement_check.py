"""Check the routed DEF against the macros and pins the floorplan fixed.

`macros.tcl` and `pins.tcl` print each located macro and exact pin as placed, in DBU, on tagged log lines. After OpenROAD succeeds, `rb pnr` reads the routed DEF's COMPONENTS and PINS sections and compares them at full precision, so a step that moved a fixed macro or pin fails the run instead of passing with a different floorplan.
"""

from dataclasses import dataclass
from fractions import Fraction

#: Printed by `macros.tcl` and the pins block: the database units per micron the tags are in.
DBU_TAG = "RB-FLOORPLAN-DBU:"
#: `RB-FLOORPLAN-MACRO: x y orientation name`, the located macro's lower-left corner in DBU.
MACRO_TAG = "RB-FLOORPLAN-MACRO:"
#: `RB-FLOORPLAN-PIN: x0 y0 x1 y1 layer name`, the exact pin's box in DBU.
PIN_TAG = "RB-FLOORPLAN-PIN:"
#: `RB-PINS-COMPLETE: class count ports...`, one line per `floorplan.pins-complete` error class.
PINS_COMPLETE_TAG = "RB-PINS-COMPLETE:"

#: OpenDB orientation to DEF orientation.
DEF_ORIENT = {
    "R0": "N",
    "R90": "W",
    "R180": "S",
    "R270": "E",
    "MY": "FN",
    "MX": "FS",
    "MXR90": "FW",
    "MYR90": "FE",
}

#: What each `RB-PINS-COMPLETE` class means, for the failure description.
PINS_COMPLETE_CLASSES = {
    "duplicate-exact": "with more than one exact entry",
    "several-regions": "with no exact entry, matched by several side or group rules",
    "uncovered": "matched by no entry",
}

# How many differences a failure description names.
_SHOWN = 3


@dataclass(frozen=True)
class FloorplanTags:
    """The located macros and exact pins the flow reported, in database units."""

    dbu: int | None
    # name -> (x, y, OpenDB orientation)
    macros: dict[str, tuple[int, int, str]]
    # name -> (layer, (x0, y0, x1, y1))
    pins: dict[str, tuple[str, tuple[int, int, int, int]]]


def parse_tags(log_text: str) -> FloorplanTags:
    """Return the floorplan tags in an OpenROAD log; a later line for the same name wins, as the later placement does."""
    dbu = None
    macros: dict[str, tuple[int, int, str]] = {}
    pins: dict[str, tuple[str, tuple[int, int, int, int]]] = {}
    for line in log_text.splitlines():
        if not line.startswith("RB-"):
            continue
        if line.startswith(MACRO_TAG):
            parts = line[len(MACRO_TAG) :].split(maxsplit=3)
            if len(parts) == 4:
                x, y, orient, name = parts
                macros[name] = (int(x), int(y), orient)
        elif line.startswith(PIN_TAG):
            parts = line[len(PIN_TAG) :].split(maxsplit=5)
            if len(parts) == 6:
                x0, y0, x1, y1, layer, name = parts
                pins[name] = (layer, (int(x0), int(y0), int(x1), int(y1)))
        elif line.startswith(DBU_TAG):
            dbu = int(line[len(DBU_TAG) :].strip())
    return FloorplanTags(dbu=dbu, macros=macros, pins=pins)


def parse_pins_complete(log_text: str) -> list[tuple[str, int, list[str]]]:
    """Return the `floorplan.pins-complete` error classes in an OpenROAD log as `(class, count, ports shown)`."""
    found = []
    for line in log_text.splitlines():
        if line.startswith(PINS_COMPLETE_TAG):
            parts = line[len(PINS_COMPLETE_TAG) :].split()
            if len(parts) >= 2 and parts[1].isdigit():
                found.append((parts[0], int(parts[1]), parts[2:]))
    return found


def describe_pins_complete(classes: list[tuple[str, int, list[str]]]) -> str:
    """One sentence naming each class's count and the ports shown."""
    parts = []
    for cls, count, shown in classes:
        more = f", +{count - len(shown)} more" if count > len(shown) else ""
        meaning = PINS_COMPLETE_CLASSES.get(cls, cls)
        parts.append(f"{count} port(s) {meaning} ({', '.join(shown)}{more})")
    return "floorplan.pins-complete: " + "; ".join(parts)


def _key(name: str) -> str:
    """A DEF or OpenDB name without escapes, so `u_a\\/u_ram` and `u_a/u_ram` compare equal."""
    return name.replace("\\", "")


def _transform(orient: str, x: int, y: int) -> tuple[int, int]:
    """A pin shape point relative to the pin origin, turned by its DEF orientation."""
    match orient:
        case "W":
            return -y, x
        case "S":
            return -x, -y
        case "E":
            return y, -x
        case "FN":
            return -x, y
        case "FS":
            return x, -y
        case "FW":
            return y, x
        case "FE":
            return -y, -x
    return x, y


def _statements(lines, section: str):
    """Yield the token lists of the `- ... ;` statements in one DEF section, from its header to `END <section>`."""
    tokens: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("END ") and stripped.split()[1:2] == [section]:
            return
        if not stripped or stripped.startswith("#"):
            continue
        tokens.extend(stripped.split())
        if tokens and tokens[-1] == ";":
            if tokens[0] == "-":
                yield tokens
            tokens = []
        elif ";" in tokens:
            # A statement ends inside the line; DEF writers do not do this, but split it.
            while ";" in tokens:
                end = tokens.index(";")
                if tokens[0] == "-":
                    yield tokens[: end + 1]
                tokens = tokens[end + 1 :]


def _component(tokens: list[str]) -> tuple[str, tuple[int, int, str] | None]:
    name = tokens[1]
    for i, token in enumerate(tokens):
        if token in ("PLACED", "FIXED", "COVER") and i > 0 and tokens[i - 1] == "+":
            # + FIXED ( x y ) orient
            return name, (int(tokens[i + 2]), int(tokens[i + 3]), tokens[i + 5])
    return name, None


def _pin_shapes(tokens: list[str]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Every `(layer, absolute box)` of a DEF pin, each port's shapes moved by that port's placement."""
    shapes = []
    pending: list[tuple[str, int, int, int, int]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "PORT" and tokens[i - 1] == "+":
            pending = []
        elif token == "LAYER" and tokens[i - 1] == "+":
            layer = tokens[i + 1]
            j = tokens.index("(", i)
            x0, y0 = int(tokens[j + 1]), int(tokens[j + 2])
            x1, y1 = int(tokens[j + 5]), int(tokens[j + 6])
            pending.append((layer, x0, y0, x1, y1))
            i = j + 7
            continue
        elif token in ("PLACED", "FIXED", "COVER") and tokens[i - 1] == "+":
            ox, oy, orient = int(tokens[i + 2]), int(tokens[i + 3]), tokens[i + 5]
            for layer, x0, y0, x1, y1 in pending:
                ax, ay = _transform(orient, x0, y0)
                bx, by = _transform(orient, x1, y1)
                shapes.append(
                    (
                        layer,
                        (
                            ox + min(ax, bx),
                            oy + min(ay, by),
                            ox + max(ax, bx),
                            oy + max(ay, by),
                        ),
                    )
                )
            pending = []
            i += 6
            continue
        i += 1
    return shapes


def read_def_placement(path: str, macros, pins) -> tuple[int | None, dict, dict]:
    """Return `(units, components, pins)` for the named macros and pins of a DEF, streaming COMPONENTS and PINS only.

    `components` maps a wanted name to `(x, y, orientation)` or None when unplaced; `pins` maps a wanted name to its shapes. Names compare without escapes.
    """
    want_macros = {_key(n) for n in macros}
    want_pins = {_key(n) for n in pins}
    units = None
    components: dict[str, tuple[int, int, str] | None] = {}
    def_pins: dict[str, list] = {}
    with open(path) as f:
        for line in f:
            stripped = line.lstrip()
            if stripped.startswith("UNITS DISTANCE MICRONS"):
                units = int(stripped.split()[3])
            elif stripped.startswith("COMPONENTS ") and want_macros:
                for tokens in _statements(f, "COMPONENTS"):
                    if _key(tokens[1]) in want_macros:
                        name, placed = _component(tokens)
                        components[_key(name)] = placed
            elif stripped.startswith("PINS ") and want_pins:
                for tokens in _statements(f, "PINS"):
                    if _key(tokens[1]) in want_pins:
                        def_pins[_key(tokens[1])] = _pin_shapes(tokens)
            elif stripped.startswith("END PINS") or (
                stripped.startswith(("NETS ", "SPECIALNETS ")) and units is not None
            ):
                break
    return units, components, def_pins


def check_routed_def(tags: FloorplanTags, def_path: str) -> list[str]:
    """Return a description of each difference between the tagged floorplan and the routed DEF; empty when they agree."""
    if not tags.macros and not tags.pins:
        return []
    units, components, def_pins = read_def_placement(def_path, tags.macros, tags.pins)
    if units is None or tags.dbu is None:
        return [f"{def_path} has no UNITS DISTANCE MICRONS, or the log no {DBU_TAG}"]
    scale = Fraction(units, tags.dbu)
    # A pin shape is its placement plus an offset, each truncated on its own.
    pin_slack = 0 if scale.denominator == 1 else 1

    def _def_units(value: int) -> int:
        # `write_def` truncates toward zero when DEF units are coarser than the database's.
        return int(value * scale)

    problems = []
    for name, (x, y, orient) in tags.macros.items():
        placed = components.get(_key(name))
        want = (_def_units(x), _def_units(y), DEF_ORIENT.get(orient, orient))
        if placed is None:
            problems.append(f"macro {name} is not placed in the routed DEF")
        elif tuple(placed) != want:
            problems.append(
                f"macro {name} is at ({placed[0]}, {placed[1]}) {placed[2]} in the "
                f"routed DEF, not ({want[0]}, {want[1]}) {want[2]} "
                f"as the floorplan placed it (DEF units)"
            )
    for name, (layer, box) in tags.pins.items():
        shapes = def_pins.get(_key(name))
        want_box = tuple(_def_units(v) for v in box)
        if not shapes:
            problems.append(f"pin {name} has no shape in the routed DEF")
            continue
        if not any(
            shape_layer == layer
            and all(abs(v - w) <= pin_slack for v, w in zip(shape, want_box))
            for shape_layer, shape in shapes
        ):
            got_layer, got = shapes[0]
            problems.append(
                f"pin {name} is {got_layer} {list(got)} in the routed DEF, not "
                f"{layer} {list(want_box)} as the floorplan placed it "
                "(DEF units)"
            )
    return problems


def describe(problems: list[str]) -> str:
    """The failure description: the first few differences and how many more."""
    shown = "; ".join(problems[:_SHOWN])
    more = f" (+{len(problems) - _SHOWN} more)" if len(problems) > _SHOWN else ""
    return f"routed DEF differs from the floorplan: {shown}{more}"
