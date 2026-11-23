"""Forwarded clocks in a hardened block's timing model (`abstract: {forwarded-clocks: arc}`).

A block that forwards its clock from an input port to an output port declares it in its SDC as a generated clock of ratio 1 on the output port. OpenSTA's `write_timing_model` writes that output's arcs as clock-to-output arcs (`rising_edge`, `falling_edge`), related to the output itself with the generated clock and to the input without it. Static timing does not propagate a clock through either, so in a parent the blocks after the first are unclocked (rtl_buddy#1080).

With `arc`, the harden step writes a second model with those generated clocks deleted (`<top>.forwarded.lib`). Each qualifying output pin's timing groups in `<top>.lib` are replaced by one `combinational` arc from the source port, `positive_unate` (`negative_unate` with `-invert`), whose tables come from the second model's edge arcs: the rise from the `rising_edge` group and the fall from the `falling_edge` group (crossed over for `-invert`). Every other group of `<top>.lib` is kept byte for byte, so the data arcs and the source pin's clock-tree paths are unchanged.

The generated clocks are read from the routed SDC, which OpenSTA's `write_sdc` writes in one canonical form. Liberty is handled as text: only the groups the rewrite touches are parsed.
"""

import os
import re
from dataclasses import dataclass

from ..constraints.tcl_tokenizer import _extract_names, _tokenize
from .artifact_paths import atomic_tmp_name

#: The second timing model the harden step writes beside `<top>.lib`; read and removed before publishing.
FORWARDED_VIEW = "forwarded.lib"
#: Prefix of the table templates copied from the second model, so they cannot collide with `<top>.lib`'s own.
_TEMPLATE_PREFIX = "rb_forwarded_"


class ForwardedClockError(Exception):
    """A forwarded clock's arc could not be written; the message says why."""


@dataclass(frozen=True)
class ForwardedClock:
    """A generated clock of ratio 1 from input port `source` to output port `target`."""

    name: str
    source: str
    target: str
    invert: bool = False

    def sense(self) -> str:
        return "negative_unate" if self.invert else "positive_unate"

    def record(self) -> dict:
        """The abstract manifest's record of the arc."""
        return {"from": self.source, "to": self.target, "sense": self.sense()}


#: `create_generated_clock` options that take a value, and those that take none; any other word is the clock's objects.
_VALUED = frozenset(
    {
        "-name",
        "-source",
        "-master_clock",
        "-divide_by",
        "-multiply_by",
        "-duty_cycle",
        "-edges",
        "-edge_shift",
        "-comment",
        "-pll_output",
        "-pll_feedback",
    }
)
_FLAGS = frozenset({"-invert", "-combinational", "-add"})
#: Options that make a generated clock's waveform other than its master's, or its master's inverted.
_SHAPING = ("-edges", "-edge_shift", "-duty_cycle")


def _ports(word: str | None) -> list[str]:
    """The port names of a `[get_ports {...}]` word; empty for anything else, such as `get_pins`."""
    if word is None or not word.startswith("[get_ports"):
        return []
    return _extract_names(word)[0]


def _options(words: list[str]) -> tuple[dict[str, str], set[str], list[str]]:
    """Split a tokenized `create_generated_clock`'s words after the command into valued options, flags and objects. A braced `-comment` is one word, so the option names it mentions are not options."""
    valued: dict[str, str] = {}
    flags: set[str] = set()
    objects: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _VALUED and i + 1 < len(words):
            valued[word] = words[i + 1]
            i += 2
            continue
        if word in _FLAGS:
            flags.add(word)
        else:
            objects.append(word)
        i += 1
    return valued, flags, objects


def _is_one(value: str) -> bool:
    try:
        return float(value) == 1.0
    except ValueError:
        return False


def forwarded_clocks(
    sdc: str, inputs: set[str], outputs: set[str]
) -> tuple[list[ForwardedClock], list[tuple[str, str]]]:
    """Return the generated clocks of the routed SDC text `sdc` that forward an input port to an output port at ratio 1, and `(name, reason)` for each other generated clock on an output port.

    `inputs` and `outputs` are the block's port names. A clock qualifies with one source port among `inputs`, one target among `outputs`, `-divide_by 1` or `-multiply_by 1`, or `-combinational` without either, optionally `-invert`, and none of `-edges`, `-edge_shift` and `-duty_cycle`. Two qualifying clocks on one output that disagree on `-invert` are both left out, as is a qualifying clock on an output that also has one that does not qualify, so the output keeps its original arcs. The SDC is tokenized as Tcl, so a braced `-comment` is one word.
    """
    found: list[ForwardedClock] = []
    skipped: list[tuple[str, str]] = []
    # Outputs with a generated clock that is not forwarded.
    kept_as_written: set[str] = set()
    for words in _tokenize(sdc):
        if words[0] != "create_generated_clock":
            continue
        valued, flags, objects = _options(words[1:])
        name = valued.get("-name", "?")
        targets = [t for word in objects for t in _ports(word)]
        if not any(t in outputs for t in targets):
            continue
        sources = _ports(valued.get("-source"))
        ratios = [valued[k] for k in ("-divide_by", "-multiply_by") if k in valued]
        if len(targets) != 1:
            reason = "it is defined on more than one port"
        elif len(sources) != 1 or sources[0] not in inputs:
            reason = "its -source is not one input port"
        elif any(option in valued for option in _SHAPING):
            reason = "it changes the waveform"
        elif (
            not all(_is_one(r) for r in ratios)
            if ratios
            else "-combinational" not in flags
        ):
            reason = "its ratio is not 1"
        else:
            found.append(
                ForwardedClock(name, sources[0], targets[0], "-invert" in flags)
            )
            continue
        skipped.append((name, reason))
        kept_as_written.update(t for t in targets if t in outputs)
    by_target: dict[str, list[ForwardedClock]] = {}
    for clock in found:
        by_target.setdefault(clock.target, []).append(clock)
    kept = []
    for target, clocks in by_target.items():
        if target in kept_as_written:
            skipped.extend(
                (c.name, f"another generated clock on {target} is not forwarded")
                for c in clocks
            )
        elif len({(c.source, c.invert) for c in clocks}) > 1:
            skipped.extend(
                (c.name, f"another generated clock on {target} differs from it")
                for c in clocks
            )
        else:
            kept.append(clocks[0])
    return kept, skipped


@dataclass
class _Group:
    """A Liberty group in `text`: `name(args) { body }`, from `start` (its name) to `end` (after its closing brace); its body lies between `open` and `close`."""

    name: str
    args: str
    start: int
    open: int
    close: int
    end: int


_HEADER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)\s*\{")


def _closing(text: str, open_brace: int) -> int:
    """The index of the brace that closes the one at `open_brace`, skipping quoted strings and comments."""
    depth = 0
    i = open_brace
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i = text.index('"', i + 1) + 1
            continue
        if text.startswith("/*", i):
            i = text.index("*/", i + 2) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ForwardedClockError("unbalanced braces")


def _groups(text: str, start: int, end: int) -> list[_Group]:
    """The groups directly inside `text[start:end]`."""
    groups = []
    i = start
    while True:
        m = _HEADER.search(text, i, end)
        if not m:
            return groups
        close = _closing(text, m.end() - 1)
        groups.append(
            _Group(
                m.group(1),
                m.group(2).strip().strip('"'),
                m.start(),
                m.end() - 1,
                close,
                close + 1,
            )
        )
        i = close + 1


def _attribute(text: str, group: _Group, name: str) -> str | None:
    """A simple attribute `name : value;` of `group`, outside its subgroups, unquoted."""
    body = text[group.open + 1 : group.close]
    for sub in reversed(_groups(text, group.open + 1, group.close)):
        offset = group.open + 1
        body = body[: sub.start - offset] + body[sub.end - offset :]
    m = re.search(rf"(?m)^\s*{name}\s*:\s*\"?([^\";]*)\"?\s*;", body)
    return m.group(1).strip() if m else None


def _only(groups: list[_Group], name: str, what: str) -> _Group:
    found = [g for g in groups if g.name == name]
    if len(found) != 1:
        raise ForwardedClockError(
            f"{what}: expected one {name} group, found {len(found)}"
        )
    return found[0]


def _cell(text: str, path: str) -> _Group:
    library = _only(_groups(text, 0, len(text)), "library", path)
    return _only(_groups(text, library.open + 1, library.close), "cell", path)


def _pin(text: str, cell: _Group, name: str, path: str) -> _Group:
    pins = [
        g
        for g in _groups(text, cell.open + 1, cell.close)
        if g.name == "pin" and g.args == name
    ]
    if len(pins) != 1:
        raise ForwardedClockError(f"{path} has no pin {name}")
    return pins[0]


def _templates(text: str) -> dict[str, str]:
    """The library's table templates by name, each as written."""
    library = _only(_groups(text, 0, len(text)), "library", "timing model")
    return {
        g.args: text[g.start : g.end]
        for g in _groups(text, library.open + 1, library.close)
        if g.name.endswith("_template")
    }


def _value_numbers(table: str) -> list[float]:
    values = re.search(r"values\s*\((.*?)\)\s*;", table, re.S)
    if not values:
        return []
    return [
        float(v) for v in re.findall(r"-?[0-9.]+(?:[eE][-+]?[0-9]+)?", values.group(1))
    ]


def _edge_tables(text: str, pin: _Group, source: str, path: str) -> dict[str, dict]:
    """The `rising_edge` and `falling_edge` groups of `pin` related to `source`: timing type -> table name -> table text."""
    edges: dict[str, dict] = {}
    for timing in _groups(text, pin.open + 1, pin.close):
        if timing.name != "timing":
            continue
        kind = _attribute(text, timing, "timing_type")
        if kind not in ("rising_edge", "falling_edge"):
            continue
        if _attribute(text, timing, "related_pin") != source:
            continue
        edges[kind] = {
            g.name: text[g.start : g.end]
            for g in _groups(text, timing.open + 1, timing.close)
        }
    for kind in ("rising_edge", "falling_edge"):
        if kind not in edges:
            raise ForwardedClockError(
                f"{path}: pin {pin.args} has no {kind} arc from {source}"
            )
    return edges


def _arc(
    clock: ForwardedClock, edges: dict[str, dict], indent: str
) -> tuple[str, set[str]]:
    """The combinational timing group of `clock` from its edge arcs, and the templates it names."""
    rise_from, fall_from = (
        ("falling_edge", "rising_edge")
        if clock.invert
        else ("rising_edge", "falling_edge")
    )
    tables = []
    templates = set()
    for table, kind in (
        ("cell_rise", rise_from),
        ("rise_transition", rise_from),
        ("cell_fall", fall_from),
        ("fall_transition", fall_from),
    ):
        written = edges[kind].get(table)
        if written is None:
            raise ForwardedClockError(
                f"pin {clock.target}: its {kind} arc has no {table} table"
            )
        values = _value_numbers(written)
        if not values or min(values) < 0:
            raise ForwardedClockError(
                f"pin {clock.target}: its {kind} {table} has a negative or no value, "
                f"so {clock.target} does not follow {clock.source} "
                f"{'inverted' if clock.invert else 'uninverted'}; check the generated "
                "clock's -invert"
            )
        head = re.match(r"(\w+)\s*\(\s*([^()]*?)\s*\)", written)
        template = head.group(2)
        if template != "scalar":
            templates.add(template)
            written = f"{table}({_TEMPLATE_PREFIX}{template})" + written[head.end() :]
        tables.append(written)
    body = "".join(f"\n{indent}  {t}" for t in tables)
    return (
        f"{indent}timing() {{\n"
        f'{indent}  related_pin : "{clock.source}";\n'
        f"{indent}  timing_sense : {clock.sense()};\n"
        f"{indent}  timing_type : combinational;"
        f"{body}\n{indent}}}\n",
        templates,
    )


def rewrite(lib: str, forwarded_lib: str, clocks: list[ForwardedClock]) -> None:
    """Replace each of `clocks`' target pin's timing groups in the timing model `lib` by its combinational arc, built from the edge arcs of `forwarded_lib`; raise `ForwardedClockError` naming what is missing or does not fit.

    The file is replaced atomically, and only when every clock's arc could be built.
    """
    with open(lib, encoding="utf-8") as f:
        text = f.read()
    with open(forwarded_lib, encoding="utf-8") as f:
        forwarded = f.read()
    source_cell = _cell(forwarded, os.path.basename(forwarded_lib))
    source_templates = _templates(forwarded)
    edits: list[tuple[int, int, str]] = []
    needed: set[str] = set()
    cell = _cell(text, os.path.basename(lib))
    for clock in clocks:
        pin = _pin(text, cell, clock.target, os.path.basename(lib))
        edges = _edge_tables(
            forwarded,
            _pin(forwarded, source_cell, clock.target, os.path.basename(forwarded_lib)),
            clock.source,
            os.path.basename(forwarded_lib),
        )
        line_start = text.rfind("\n", 0, pin.start) + 1
        indent = text[line_start : pin.start] + "  "
        arc, templates = _arc(clock, edges, indent)
        needed |= templates
        timings = [
            g for g in _groups(text, pin.open + 1, pin.close) if g.name == "timing"
        ]
        for timing in timings:
            # The group with its indentation and line break.
            start = text.rfind("\n", 0, timing.start) + 1
            end = (
                timing.end + 1
                if text[timing.end : timing.end + 1] == "\n"
                else timing.end
            )
            edits.append((start, end, ""))
        closing_line = text.rfind("\n", 0, pin.close) + 1
        edits.append((closing_line, closing_line, arc))
    missing = sorted(t for t in needed if t not in source_templates)
    if missing:
        raise ForwardedClockError(
            f"{os.path.basename(forwarded_lib)} has no table template {missing[0]}"
        )
    if needed:
        cell_line = text.rfind("\n", 0, cell.start) + 1
        indent = text[cell_line : cell.start]
        copied = "".join(
            indent
            + source_templates[name].replace(
                f"({name})", f"({_TEMPLATE_PREFIX}{name})", 1
            )
            + "\n"
            for name in sorted(needed)
        )
        edits.append((cell_line, cell_line, copied))
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:start] + new + text[end:]
    tmp = atomic_tmp_name(lib)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, lib)


def port_names(lib: str) -> tuple[set[str], set[str]]:
    """The input and output pins of the one cell of the timing model `lib`; bus members are left out."""
    with open(lib, encoding="utf-8") as f:
        text = f.read()
    cell = _cell(text, os.path.basename(lib))
    inputs, outputs = set(), set()
    for pin in _groups(text, cell.open + 1, cell.close):
        if pin.name != "pin":
            continue
        direction = _attribute(text, pin, "direction")
        if direction == "input":
            inputs.add(pin.args)
        elif direction == "output":
            outputs.add(pin.args)
    return inputs, outputs
