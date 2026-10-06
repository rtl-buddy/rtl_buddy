"""Instances of `blocks:` modules in a mapped netlist: their checks, and the parameter overrides OpenROAD cannot read.

A parent instances a parameterised hardened block through a `(* blackbox *)` stub that declares the block's parameters. Yosys writes every parameter of such an instance into the netlist as a `#(...)` override, localparams included, and OpenROAD's structural `read_verilog` rejects the syntax (`STA-0171`). A hardened block has exactly one parameterisation, the one its abstract was built from, so the overrides carry no information OpenROAD needs once they are checked.

`clean_netlist` checks every instance of a block, then strips the overrides:

- **Ports.** Every connected port is a pin or bus of the block's abstract LEF with the same width, and every signal pin of the abstract is connected. An unconnected input or inout fails; an unconnected output is a warning.
- **Parameters.** Every override, named or positional, equals the value the block was elaborated with, as recorded in the abstract's `<top>.params.json` when it was hardened. Without a record (an abstract from an older rtl_buddy) only the parameters in the block synthesis's `params:` are compared, with a warning to re-harden. All instances of a block also carry the same overrides.
- **The strip itself.** The output is the input with exactly the `#(...)` lists removed, and stripping it again changes nothing.

The netlist is tokenised, not matched with a regular expression, so escaped identifiers, sized and signed literals, strings, attributes, comments and line breaks are handled. Connection widths come from the netlist's declarations, selects, concatenations and sized constants, the subset Yosys `write_verilog` produces; anything else fails closed with a message naming the connection.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

from ..logging_utils import log_event
from .artifact_paths import atomic_tmp_name

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<attr>\(\*(?!\))(?:"(?:\\.|[^"\\])*"|[^"])*?\*\))
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<escid>\\\S+)
  | (?P<num>
        (?:\d[\d_]*)?\s*'[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+
      | '[01xXzZ]
      | \d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?
    )
  | (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<punct>.)
    """,
    re.DOTALL | re.VERBOSE,
)

_TRIVIA = ("ws", "comment", "attr")


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int

    @property
    def name(self) -> str:
        """The identifier this token names, without an escaped identifier's backslash."""
        return self.text[1:] if self.kind == "escid" else self.text


def _tokens(text: str, *, attributes: bool = False) -> list[_Token]:
    """Return the significant tokens of Verilog `text`: no whitespace or comments, and attributes only when asked."""
    skip = ("ws", "comment") if attributes else _TRIVIA
    out = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind not in skip:
            out.append(_Token(kind, m.group(), m.start(), m.end()))
    return out


# A module's optional lifetime keyword: `module automatic m`.
_LIFETIMES = ("automatic", "static")

_BLACKBOX_ATTR_RE = re.compile(r"^\(\*.*\bblackbox\b.*\*\)$", re.DOTALL)


@dataclass(frozen=True)
class BlackboxModule:
    """A `(* blackbox *)` module in a source file, as character offsets into it."""

    name: str
    #: Start of the attribute.
    start: int
    #: Start of the `module` keyword.
    header_start: int
    #: Just past the `;` that ends the header.
    header_end: int
    #: Just past `endmodule`.
    end: int
    #: Whether the header has a parameter port list, `#(...)`.
    parameterised: bool = False


def blackbox_modules(text: str) -> list[BlackboxModule]:
    """Return the modules in Verilog `text` that a `(* blackbox *)` attribute marks.

    The header runs from `module` to the first `;` outside parentheses, so a parameter port list such as `#(parameter int W = 8, localparam int AW = $clog2(W))` is handled.
    """
    toks = _tokens(text, attributes=True)
    found = []
    i = 0
    while i < len(toks) - 2:
        attr = toks[i]
        n = i + 2
        if n < len(toks) and toks[n].text in _LIFETIMES:
            n += 1
        if not (
            attr.kind == "attr"
            and _BLACKBOX_ATTR_RE.match(attr.text)
            and toks[i + 1].text in ("module", "macromodule")
            and n < len(toks)
            and toks[n].kind in ("id", "escid")
        ):
            i += 1
            continue
        depth = 0
        j = n + 1
        while j < len(toks) and not (toks[j].text == ";" and depth == 0):
            if toks[j].text == "(":
                depth += 1
            elif toks[j].text == ")":
                depth -= 1
            j += 1
        k = j
        while k < len(toks) and toks[k].text != "endmodule":
            k += 1
        if k >= len(toks):
            break
        found.append(
            BlackboxModule(
                name=toks[n].name,
                start=attr.start,
                header_start=toks[i + 1].start,
                header_end=toks[j].end,
                end=toks[k].end,
                parameterised=n + 1 < len(toks) and toks[n + 1].text == "#",
            )
        )
        i = k + 1
    return found


@dataclass
class BlockInstance:
    """One instance of a block module that carries a parameter override list."""

    module: str
    instance: str
    line: int
    #: Parameter name to its value as written, in netlist order; positional ones are `#0`, `#1`, ...
    params: dict[str, str] = field(default_factory=dict)
    #: Port name to the width of its connection, or None for an empty `.port()`.
    connections: dict[str, int | None] = field(default_factory=dict)

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.instance} ({shown})"


class BlockParamError(Exception):
    """Raised when a block instance fails a check or cannot be read; the message says why."""


class StripSelfCheckError(RuntimeError):
    """Raised when stripping the overrides changed more than the override lists: an rtl_buddy bug."""


def _matching_paren(toks: list[_Token], open_idx: int) -> int:
    depth = 0
    for j in range(open_idx, len(toks)):
        if toks[j].text == "(":
            depth += 1
        elif toks[j].text == ")":
            depth -= 1
            if depth == 0:
                return j
    raise BlockParamError("unbalanced parentheses in a parameter override list")


def _parse_overrides(
    text: str, toks: list[_Token], open_idx: int, close_idx: int
) -> dict[str, str]:
    """Return `{name: value}` for the named overrides `.NAME(value), ...` between two parentheses."""
    params: dict[str, str] = {}
    i = open_idx + 1
    position = 0
    while i < close_idx:
        tok = toks[i]
        if tok.text == ",":
            i += 1
            continue
        if (
            tok.text == "."
            and i + 2 < close_idx
            and toks[i + 1].kind in ("id", "escid")
            and toks[i + 2].text == "("
        ):
            end = _matching_paren(toks, i + 2)
            value_toks = toks[i + 3 : end]
            value = text[value_toks[0].start : value_toks[-1].end] if value_toks else ""
            params[toks[i + 1].name] = " ".join(value.split())
            i = end + 1
            continue
        # A positional override runs to the next top-level comma.
        start = i
        depth = 0
        while i < close_idx and not (toks[i].text == "," and depth == 0):
            if toks[i].text == "(":
                depth += 1
            elif toks[i].text == ")":
                depth -= 1
            i += 1
        value = text[toks[start].start : toks[i - 1].end]
        params[f"#{position}"] = " ".join(value.split())
        position += 1
    return params


def strip_overrides(text: str, modules: set[str]) -> tuple[str, list[BlockInstance]]:
    """Remove the `#(...)` override list from every instance of `modules` in netlist `text`.

    Returns the rewritten text and the instances whose overrides were removed. A module header `module m #(...)` is not an instance and is left alone.

    The result is checked before it is returned: it must be the input with exactly the recorded spans removed, each span must be whitespace and one `#(...)` list, and stripping the result again must change nothing. Anything else raises StripSelfCheckError.
    """
    cleaned, found, cuts = _strip(text, modules)
    _verify_strip(text, cleaned, cuts, modules)
    return cleaned, found


def _verify_strip(text, cleaned, cuts, modules) -> None:
    rebuilt = []
    last = 0
    for start, end in cuts:
        removed = text[start:end]
        body = removed.lstrip()
        if not (body.startswith("#") and body.endswith(")")):
            raise StripSelfCheckError(
                f"stripped span {removed!r} is not one #(...) list"
            )
        rebuilt.append(text[last:start])
        if end < len(text) and not text[end].isspace():
            rebuilt.append(" ")
        last = end
    rebuilt.append(text[last:])
    if "".join(rebuilt) != cleaned:
        raise StripSelfCheckError(
            "the stripped netlist differs from the input outside the override lists"
        )
    again, _, again_cuts = _strip(cleaned, modules)
    if again_cuts or again != cleaned:
        raise StripSelfCheckError("stripping the stripped netlist again changed it")


def _strip(
    text: str, modules: set[str]
) -> tuple[str, list[BlockInstance], list[tuple[int, int]]]:
    toks = _tokens(text)
    found: list[BlockInstance] = []
    cuts: list[tuple[int, int]] = []
    i = 0
    while i < len(toks) - 2:
        tok = toks[i]
        if (
            tok.kind in ("id", "escid")
            and tok.name in modules
            and toks[i + 1].text == "#"
            and toks[i + 2].text == "("
            and not (i > 0 and toks[i - 1].text in ("module", "macromodule"))
        ):
            close = _matching_paren(toks, i + 2)
            params = _parse_overrides(text, toks, i + 2, close)
            inst_tok = toks[close + 1] if close + 1 < len(toks) else None
            instance = (
                inst_tok.name if inst_tok and inst_tok.kind in ("id", "escid") else "?"
            )
            found.append(
                BlockInstance(
                    module=tok.name,
                    instance=instance,
                    line=text.count("\n", 0, tok.start) + 1,
                    params=params,
                )
            )
            cuts.append((tok.end, toks[close].end))
            i = close + 1
            continue
        i += 1
    if not cuts:
        return text, found, cuts
    pieces = []
    last = 0
    for start, end in cuts:
        pieces.append(text[last:start])
        # Keep the module name and instance name apart, which an escaped name needs.
        if end < len(text) and not text[end].isspace():
            pieces.append(" ")
        last = end
    pieces.append(text[last:])
    return "".join(pieces), found, cuts


_BASED_RE = re.compile(
    r"^(?P<width>\d+)?'(?P<signed>[sS])?(?P<base>[bBoOdDhH])(?P<digits>[0-9a-fA-FxXzZ?]+)$"
)
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}


def parse_value(text: str):
    """Return a Verilog parameter value as an int, float or str, or None when it is not a literal.

    A sized literal is returned as `(value, width)`; a negative or signed one is sign-extended. Literals with x or z bits are returned as their normalised text, so they compare only with the same text.
    """
    raw = str(text).strip()
    # A string's contents are compared verbatim; only numbers are normalised.
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    s = "".join(raw.split()).replace("_", "")
    negative = False
    if s.startswith(("-", "+")):
        negative = s[0] == "-"
        s = s[1:]
    if s.startswith('"'):
        return None
    m = _BASED_RE.match(s)
    if m:
        digits = m.group("digits")
        if any(c in "xXzZ?" for c in digits):
            return s.lower()
        value = int(digits, _BASES[m.group("base").lower()])
        width = int(m.group("width")) if m.group("width") else None
        if m.group("signed") and width and value >= 1 << (width - 1):
            value -= 1 << width
        if negative:
            value = -value
        return (value, width)
    try:
        value = int(s, 10)
    except ValueError:
        pass
    else:
        return (-value if negative else value, None)
    try:
        number = float(s)
    except ValueError:
        return None
    return -number if negative else number


def values_equal(netlist_value: str, configured) -> bool:
    """Return whether a netlist override equals a `params:` value from YAML.

    Integers compare modulo the literal's width, so `32'd4294967295` equals -1. A YAML string is read as a Verilog literal when it parses as one (`"8'hff"`), and otherwise compared with a string literal's contents.
    """
    got = parse_value(netlist_value)
    if isinstance(configured, bool):
        configured = int(configured)
    if isinstance(configured, str):
        want = parse_value(configured)
        if want is None:
            return isinstance(got, str) and got == configured
        if isinstance(want, str) and isinstance(got, str):
            return want == got
        configured = want[0] if isinstance(want, tuple) else want
    if isinstance(configured, int):
        if not isinstance(got, tuple):
            return isinstance(got, float) and got == configured
        value, width = got
        if width:
            mask = (1 << width) - 1
            return (value & mask) == (configured & mask)
        return value == configured
    if isinstance(configured, float):
        number = got[0] if isinstance(got, tuple) else got
        return isinstance(number, (int, float)) and float(number) == configured
    return False


# --- Abstract pins -------------------------------------------------------------------------

_SUPPLY_USES = ("POWER", "GROUND")
_BUS_PIN_RE = re.compile(r"^(?P<base>.+)\[(?P<index>\d+)\]$")


@dataclass
class AbstractPin:
    """A pin or bus of a block's abstract LEF."""

    name: str
    width: int
    direction: str
    use: str

    @property
    def supply(self) -> bool:
        return self.use in _SUPPLY_USES


def abstract_pins(lef_path: str, macro: str) -> dict[str, AbstractPin]:
    """Return the pins of `macro` in an abstract LEF, with bus bits `d[0]` ... `d[15]` folded into one 16-bit `d`.

    The LEF is the view `write_abstract_lef` produces for `harden: true`, and the one OpenROAD binds each instance to. Raises BlockParamError when the macro is missing or a bus has gaps.
    """
    try:
        with open(lef_path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise BlockParamError(f"cannot read block abstract {lef_path}: {e}") from None
    in_macro = False
    pin = None
    bits: dict[str, list[int]] = {}
    pins: dict[str, AbstractPin] = {}
    for line in lines:
        words = line.replace(";", " ; ").split()
        if not words:
            continue
        if words[0] == "MACRO" and len(words) > 1:
            in_macro = words[1] == macro
            continue
        if not in_macro:
            continue
        if words[0] == "END" and len(words) > 1 and words[1] == macro:
            break
        if words[0] == "PIN" and len(words) > 1:
            name = words[1]
            m = _BUS_PIN_RE.match(name)
            base = m.group("base") if m else name
            if m:
                bits.setdefault(base, []).append(int(m.group("index")))
            pin = pins.setdefault(base, AbstractPin(base, 0, "INOUT", "SIGNAL"))
            pin.width += 1
            continue
        if pin is not None and words[0] == "DIRECTION" and len(words) > 1:
            pin.direction = words[1]
        elif pin is not None and words[0] == "USE" and len(words) > 1:
            pin.use = words[1]
        elif pin is not None and words[0] == "END" and len(words) > 1:
            pin = None
    if not pins:
        raise BlockParamError(f"block abstract {lef_path} has no pins for {macro!r}")
    for base, indices in bits.items():
        if sorted(indices) != list(range(min(indices), max(indices) + 1)):
            raise BlockParamError(
                f"block abstract {lef_path}: bus {base!r} of {macro!r} has gaps"
            )
    return pins


# --- Netlist instances and connection widths ----------------------------------------------

_DECL_KEYWORDS = {
    "input",
    "output",
    "inout",
    "wire",
    "reg",
    "logic",
    "tri",
    "wand",
    "wor",
    "supply0",
    "supply1",
}
_DECL_MODIFIERS = {"signed", "unsigned", "wire", "reg", "logic", "var"}
_SIZED_RE = re.compile(r"^(\d+)\s*'")


def _int_at(toks: list[_Token], i: int) -> tuple[int, int]:
    """Read a decimal integer, optionally negative, at `i`; return (value, next index)."""
    sign = 1
    if toks[i].text == "-":
        sign, i = -1, i + 1
    tok = toks[i]
    if tok.kind != "num" or not tok.text.replace("_", "").isdigit():
        raise ValueError(tok.text)
    return sign * int(tok.text.replace("_", "")), i + 1


def _range_width(toks: list[_Token], i: int) -> tuple[int, int]:
    """Read `[a:b]` or `[a]` at `i`; return (width, index past `]`)."""
    a, i = _int_at(toks, i + 1)
    if toks[i].text == "]":
        return 1, i + 1
    if toks[i].text != ":":
        raise ValueError(toks[i].text)
    b, i = _int_at(toks, i + 1)
    if toks[i].text != "]":
        raise ValueError(toks[i].text)
    return abs(a - b) + 1, i + 1


def _module_bodies(toks: list[_Token]):
    """Yield (name, first token index, endmodule index) for each module."""
    i = 0
    while i < len(toks):
        if toks[i].text in ("module", "macromodule"):
            n = i + 1
            if n < len(toks) and toks[n].text in _LIFETIMES:
                n += 1
            end = n
            while end < len(toks) and toks[end].text != "endmodule":
                end += 1
            yield toks[n].name, n + 1, end
            i = end + 1
            continue
        i += 1


def _declarations(toks: list[_Token], lo: int, hi: int) -> dict[str, int | None]:
    """Return net and port widths declared in a module; None for a declaration rb cannot size."""
    widths: dict[str, int | None] = {}
    i = lo
    while i < hi:
        if toks[i].text not in _DECL_KEYWORDS or toks[i - 1].text not in (
            ";",
            ")",
            "(",
            ",",
        ):
            i += 1
            continue
        j = i + 1
        while j < hi and toks[j].text in _DECL_MODIFIERS:
            j += 1
        # One packed dimension at most; more is beyond what rb sizes, and fails closed on use.
        dims = 0
        width: int | None = 1
        while j < hi and toks[j].text == "[":
            dims += 1
            try:
                width, j = _range_width(toks, j)
            except (ValueError, IndexError):
                width = None
                while j < hi and toks[j].text != "]":
                    j += 1
                j += 1
        if dims > 1:
            width = None
        depth = 0
        expect_name = True
        while j < hi and toks[j].text != ";":
            t = toks[j]
            if depth == 0 and t.text in _DECL_KEYWORDS:
                break  # the next declaration of an ANSI port list
            if t.text in ("(", "{", "["):
                depth += 1
            elif t.text in (")", "}", "]"):
                depth -= 1
                if depth < 0:
                    break  # the end of an ANSI port list
            elif depth == 0 and t.text == ",":
                expect_name = True
            elif depth == 0 and t.text == "=":
                expect_name = False
            elif depth == 0 and expect_name and t.kind in ("id", "escid"):
                # An unpacked dimension after the name makes it an array, which no port takes.
                unpacked = j + 1 < hi and toks[j + 1].text == "["
                widths[t.name] = None if unpacked else width
                expect_name = False
            j += 1
        i = j if j < hi and toks[j].text != ";" else j + 1
    return widths


def _expression_width(toks: list[_Token], widths: dict[str, int | None]) -> int:
    """Return the bit width of a connection expression, or raise ValueError naming what is not understood."""

    def item(i: int) -> tuple[int, int]:
        t = toks[i]
        if t.text == "{":
            # `{n{...}}` replication, or `{a, b, ...}` concatenation.
            if (
                i + 2 < len(toks)
                and toks[i + 1].kind == "num"
                and toks[i + 2].text == "{"
            ):
                count, _ = _int_at(toks, i + 1)
                inner, j = item(i + 2)
                if toks[j].text != "}":
                    raise ValueError(toks[j].text)
                return count * inner, j + 1
            total = 0
            j = i + 1
            while True:
                w, j = item(j)
                total += w
                if toks[j].text == "}":
                    return total, j + 1
                if toks[j].text != ",":
                    raise ValueError(toks[j].text)
                j += 1
        if t.kind == "num":
            m = _SIZED_RE.match(t.text)
            if not m:
                raise ValueError(f"unsized constant {t.text}")
            return int(m.group(1)), i + 1
        if t.kind in ("id", "escid"):
            if i + 1 < len(toks) and toks[i + 1].text == "[":
                return _range_width(toks, i + 1)
            if t.name not in widths:
                raise ValueError(f"undeclared net {t.name}")
            width = widths[t.name]
            if width is None:
                raise ValueError(f"net {t.name} has a declaration rb cannot size")
            return width, i + 1
        raise ValueError(t.text)

    width, end = item(0)
    if end != len(toks):
        raise ValueError(toks[end].text)
    return width


def block_instances(text: str, modules: set[str], where: str) -> list[BlockInstance]:
    """Return every instance of `modules` in netlist `text`, with its overrides and the width of each port connection.

    Raises BlockParamError, naming the instance and port, for a connection it cannot size, a positional port list or an instance array.
    """
    toks = _tokens(text)
    found: list[BlockInstance] = []
    for _parent, lo, hi in _module_bodies(toks):
        widths = None
        i = lo
        while i < hi:
            tok = toks[i]
            if not (tok.kind in ("id", "escid") and tok.name in modules) or (
                toks[i - 1].text in (".", "module", "macromodule")
            ):
                i += 1
                continue
            j = i + 1
            params: dict[str, str] = {}
            if toks[j].text == "#":
                close = _matching_paren(toks, j + 1)
                params = _parse_overrides(text, toks, j + 1, close)
                j = close + 1
            if toks[j].kind not in ("id", "escid") or toks[j + 1].text not in (
                "(",
                "[",
            ):
                i += 1
                continue
            inst = BlockInstance(
                module=tok.name,
                instance=toks[j].name,
                line=text.count("\n", 0, tok.start) + 1,
                params=params,
            )
            context = f"{where}: instance {inst.instance!r} (line {inst.line}) of block {inst.module!r}"
            if toks[j + 1].text == "[":
                raise BlockParamError(f"{context}: instance arrays are not supported")
            if widths is None:
                widths = _declarations(toks, lo, hi)
            open_idx = j + 1
            close = _matching_paren(toks, open_idx)
            k = open_idx + 1
            while k < close:
                if toks[k].text == ",":
                    k += 1
                    continue
                if not (
                    toks[k].text == "."
                    and toks[k + 1].kind in ("id", "escid")
                    and toks[k + 2].text == "("
                ):
                    raise BlockParamError(
                        f"{context}: positional port connections are not supported"
                    )
                port = toks[k + 1].name
                end = _matching_paren(toks, k + 2)
                expr = toks[k + 3 : end]
                if not expr:
                    inst.connections[port] = None
                else:
                    try:
                        inst.connections[port] = _expression_width(expr, widths)
                    except (ValueError, IndexError) as e:
                        shown = " ".join(text[expr[0].start : expr[-1].end].split())
                        raise BlockParamError(
                            f"{context}: cannot tell the width of port {port!r} "
                            f"connected to `{shown}` ({e})"
                        ) from None
                k = end + 1
            found.append(inst)
            i = close + 1
    return found


def check_ports(instances: list[BlockInstance], blocks, where: str) -> list[str]:
    """Raise BlockParamError unless every instance's connections match its block's abstract pins.

    Returns warnings: an unconnected output pin is allowed.
    """
    by_name = {b.ref.name: b for b in blocks}
    pins_of: dict[str, dict[str, AbstractPin]] = {}
    warnings = []
    for inst in instances:
        if inst.module not in pins_of:
            block = by_name[inst.module]
            pins_of[inst.module] = abstract_pins(block.lef, block.ref.name)
        pins = pins_of[inst.module]
        context = f"{where}: instance {inst.instance!r} (line {inst.line}) of block {inst.module!r}"
        for port, width in inst.connections.items():
            pin = pins.get(port)
            if pin is None:
                raise BlockParamError(
                    f"{context}: port {port!r} is not a pin of the block's abstract "
                    f"({', '.join(sorted(p for p in pins if not pins[p].supply))})"
                )
            if pin.supply:
                continue
            if width is not None and width != pin.width:
                raise BlockParamError(
                    f"{context}: port {port!r} is connected to {width} bit(s), but "
                    f"the hardened block's pin is {pin.width} bit(s) wide — the "
                    "instance's parameters differ from the ones the block was "
                    "hardened with, or the stub's ports differ from the block's"
                )
        for name, pin in sorted(pins.items()):
            if pin.supply or inst.connections.get(name) is not None:
                continue
            message = (
                f"{context}: {pin.direction.lower()} pin {name!r} is not connected"
            )
            if pin.direction == "OUTPUT":
                warnings.append(message)
            else:
                raise BlockParamError(message)
    return warnings


# --- Elaborated parameter records ----------------------------------------------------------

#: Bumped on an incompatible change to a parameter record.
PARAM_RECORD_SCHEMA = 1


@dataclass
class ParamRecord:
    """The parameter values a block's top was elaborated with, written when it was hardened."""

    module: str
    frontend: str
    #: True when localparams are included (slang); the native frontend reports parameters only.
    complete: bool
    #: Name to a Verilog literal: `32'b...` for bits, `"..."` for a string.
    parameters: dict[str, str]
    #: The overridable parameters in declaration order, for positional overrides.
    order: list[str]

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "schema_version": PARAM_RECORD_SCHEMA,
                    "module": self.module,
                    "frontend": self.frontend,
                    "complete": self.complete,
                    "parameters": self.parameters,
                    "order": self.order,
                },
                indent=2,
            )
            + "\n"
        )


def yosys_json_literal(value: str) -> str:
    """Return a Yosys `write_json` parameter value as a Verilog literal.

    Bits are a string of 0/1/x/z; a string that would look like bits carries one trailing space.
    """
    if value and set(value) <= set("01xz"):
        return f"{len(value)}'b{value}"
    if value.endswith(" "):
        value = value[:-1]
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_param_record(path: str) -> ParamRecord | None:
    """Return the record at `path`, or None when it is missing, unreadable or of an unknown schema."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != PARAM_RECORD_SCHEMA:
        return None
    return ParamRecord(
        module=data.get("module", ""),
        frontend=data.get("frontend", ""),
        complete=bool(data.get("complete")),
        parameters=dict(data.get("parameters") or {}),
        order=list(data.get("order") or []),
    )


def parameter_port_names(text: str, module: str) -> list[str] | None:
    """Return the overridable parameters of `module` in declaration order, or None when it is not declared in `text`.

    These are the `parameter` entries of the `#(...)` header list, in which a `localparam` keyword stays in force until the next keyword; without a header list, the body's `parameter` declarations.
    """
    toks = _tokens(text)
    for name, lo, hi in _module_bodies(toks):
        if name != module:
            continue
        if toks[lo].text != "#":
            items = []
            i = lo
            while i < hi:
                if toks[i].text == "parameter":
                    start = i + 1
                    i = start
                    while i < hi and toks[i].text != ";":
                        i += 1
                    items.append(toks[start:i])
                i += 1
            return [n for item in items for n in _item_names(item)]
        close = _matching_paren(toks, lo + 1)
        names: list[str] = []
        local = False
        depth = 0
        item: list[_Token] = []
        for t in toks[lo + 2 : close + 1]:
            if t is toks[close] or (depth == 0 and t.text == ","):
                if item and item[0].text in ("parameter", "localparam"):
                    local = item[0].text == "localparam"
                    item = item[1:]
                if item and not local:
                    names.extend(_item_names(item)[:1])
                item = []
                continue
            if t.text in ("(", "[", "{"):
                depth += 1
            elif t.text in (")", "]", "}"):
                depth -= 1
            item.append(t)
        return names
    return None


def _item_names(item: list[_Token]) -> list[str]:
    """Return the names declared by `type name = value, name = value` tokens."""
    names = []
    depth = 0
    last = None
    for t in item + [_Token("punct", ",", 0, 0)]:
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and t.text in ("=", ","):
            if last is not None:
                names.append(last)
            last = None
            if t.text == "=":
                depth += 1000  # skip the value up to the next top-level comma
            continue
        elif depth == 1000 and t.text == ",":
            depth = 0
            continue
        if depth == 0 and t.kind in ("id", "escid"):
            last = t.name
    return names


def literals_equal(a: str, b: str) -> bool:
    """Return whether two Verilog literals denote the same parameter value.

    Integers compare modulo the narrower width; strings compare verbatim.
    """
    pa, pb = parse_value(a), parse_value(b)
    if isinstance(pa, tuple) and isinstance(pb, tuple):
        widths = [w for w in (pa[1], pb[1]) if w]
        if widths:
            mask = (1 << min(widths)) - 1
            return (pa[0] & mask) == (pb[0] & mask)
        return pa[0] == pb[0]
    if isinstance(pa, (tuple, float)) and isinstance(pb, (tuple, float)):
        na = pa[0] if isinstance(pa, tuple) else pa
        nb = pb[0] if isinstance(pb, tuple) else pb
        return float(na) == float(nb)
    return pa is not None and pa == pb


def _named(
    inst: BlockInstance, order: list[str] | None, context: str
) -> dict[str, str]:
    """Return the instance's overrides by parameter name, mapping positional and Yosys `$N` ones through `order`."""
    named: dict[str, str] = {}
    for key, value in inst.params.items():
        position = None
        if key.startswith("#"):
            position = int(key[1:])
        elif key.startswith("$") and key[1:].isdigit():
            position = int(key[1:]) - 1
        if position is None:
            named[key] = value
            continue
        if not order or position >= len(order):
            raise BlockParamError(
                f"{context}: positional override {position + 1} ({value}) cannot be "
                "matched to a parameter name; the block's parameter order is unknown "
                "— override by name"
            )
        named[order[position]] = value
    return named


def check_params(instances: list[BlockInstance], blocks, where: str) -> list[str]:
    """Raise BlockParamError unless every override matches the value the block was elaborated with.

    Returns warnings: a block with no parameter record (hardened by an older rtl_buddy) falls back to its synthesis's `params:`, and an incomplete record (the native frontend) cannot check localparams.
    """
    by_name = {b.ref.name: b for b in blocks}
    records = {name: block_param_record(b) for name, b in by_name.items()}
    warnings: list[str] = []
    warned: set[str] = set()
    named_overrides: dict[int, dict[str, str]] = {}
    for inst in instances:
        context = f"{where}: instance {inst.instance!r} (line {inst.line}) of block {inst.module!r}"
        record = records.get(inst.module)
        named = _named(inst, record.order if record else None, context)
        named_overrides[id(inst)] = named
        if record is None:
            if inst.module not in warned:
                warned.add(inst.module)
                warnings.append(
                    f"block {inst.module!r}: its abstract records no elaborated "
                    "parameters, so only the synthesis's params: are checked — "
                    "re-harden the block with this rtl_buddy"
                )
            synth, params = block_expectations([by_name[inst.module]])[inst.module]
            for key, configured in params.items():
                if key in named and not values_equal(named[key], configured):
                    raise BlockParamError(
                        f"{context} sets {key}={named[key]}, but the block was "
                        f"synthesised with {key}={configured!r} ({synth}) — make "
                        "the instance match, or re-harden the block with the "
                        "parameters the parent needs"
                    )
            continue
        mismatched = []
        for key, value in named.items():
            if key not in record.parameters:
                if record.complete:
                    raise BlockParamError(
                        f"{context} sets {key}={value}, but the hardened block has "
                        f"no parameter {key!r} (it has "
                        f"{', '.join(sorted(record.parameters)) or 'none'})"
                    )
                warnings.append(
                    f"{context}: {key}={value} is not checked; the block's record "
                    f"({record.frontend} frontend) has no localparams — harden it "
                    "with frontend: slang for a complete record"
                )
                continue
            if not literals_equal(value, record.parameters[key]):
                mismatched.append(key)
        if mismatched:
            got = ", ".join(f"{k}={named[k]}" for k in mismatched)
            want = ", ".join(f"{k}={_shown(record.parameters[k])}" for k in mismatched)
            raise BlockParamError(
                f"{context} sets {got}, but the block was hardened with {want} — "
                "make the instance match, or re-harden the block with the "
                "parameters the parent needs"
            )
    # All instances of one block share one parameterisation.
    by_module: dict[str, list[BlockInstance]] = {}
    for inst in instances:
        by_module.setdefault(inst.module, []).append(inst)
    for module, insts in by_module.items():
        first = insts[0]
        a = named_overrides[id(first)]
        for other in insts[1:]:
            b = named_overrides[id(other)]
            differ = sorted(
                k
                for k in set(a) | set(b)
                if k not in a or k not in b or not literals_equal(a[k], b[k])
            )
            if differ:
                raise BlockParamError(
                    f"{where}: instances of block {module!r} have different "
                    f"parameters ({', '.join(differ)}): {first.describe()} vs "
                    f"{other.describe()} — a hardened block has one "
                    "parameterisation; harden one block per parameter set"
                )
    return warnings


def _shown(literal: str) -> str:
    """Return a recorded literal as a reader would write it: `32'd16` rather than 32 bits."""
    value = parse_value(literal)
    if isinstance(value, tuple) and value[1]:
        return f"{value[1]}'d{value[0]}"
    return literal


def block_param_record(block) -> ParamRecord | None:
    """Return the parameter record published with a resolved block's abstract, or None."""
    return read_param_record(
        os.path.join(block.abstract_dir, f"{block.ref.name}{PARAM_RECORD_SUFFIX}")
    )


#: The parameter record's name in an abstract directory: `<top>.params.json`.
PARAM_RECORD_SUFFIX = ".params.json"


def block_expectations(blocks) -> dict[str, tuple[str, dict]]:
    """Return each resolved block's synthesis name and `params:`, keyed by module."""
    expected: dict[str, tuple[str, dict]] = {}
    for block in blocks:
        try:
            synth_cfg = block.run_cfg.resolve_synth_cfg()
        except Exception:  # an unloadable synthesis leaves only the consistency check
            expected[block.ref.name] = ("synthesis not loadable", {})
            continue
        expected[block.ref.name] = (
            f"synth run {synth_cfg.get_name()!r}",
            dict(synth_cfg.get_params() or {}),
        )
    return expected


# --- Checking and stripping a netlist -----------------------------------------------------


def clean_text(text: str, blocks, where: str) -> tuple[str, list[BlockInstance]]:
    """Check every `blocks:` instance in netlist `text` and strip their overrides.

    Returns the stripped text (unchanged when no instance carries an override) and the instances whose overrides were removed. Raises BlockParamError when a check fails; warnings are logged as `blocks.check_warning`.
    """
    modules = {b.ref.name for b in blocks}
    if not modules:
        return text, []
    instances = block_instances(text, modules, where)
    # Parameters first: a mismatch there is the cause of any port width mismatch.
    warnings = check_params(instances, blocks, where)
    warnings += check_ports(instances, blocks, where)
    for message in warnings:
        log_event(
            logger,
            logging.WARNING,
            "blocks.check_warning",
            netlist=where,
            warning=message,
        )
    cleaned, stripped = strip_overrides(text, modules)
    checked = {(i.instance, i.line) for i in instances}
    unchecked = [i for i in stripped if (i.instance, i.line) not in checked]
    if unchecked:
        raise BlockParamError(
            f"{where}: instance {unchecked[0].instance!r} (line {unchecked[0].line}) "
            f"of block {unchecked[0].module!r} carries overrides but could not be "
            "read as an instance, so it cannot be checked"
        )
    return cleaned, stripped


def clean_netlist(
    source: str, destination: str, blocks, *, where: str | None = None
) -> tuple[str, list[BlockInstance]]:
    """Return the netlist path OpenROAD should read, and the instances whose overrides were stripped.

    Every instance of a block is checked. When one carries overrides, a stripped copy is written to `destination`, which is returned; `destination` may be `source` itself. Otherwise `source` is returned and any stale `destination` is removed. `where` names the netlist in an error, by default `source`. Raises BlockParamError on a failed check and OSError when the netlist cannot be read or written.
    """
    in_place = os.path.abspath(destination) == os.path.abspath(source)
    if not blocks:
        if not in_place:
            _unlink(destination)
        return source, []
    with open(source) as f:
        text = f.read()
    cleaned, instances = clean_text(text, blocks, where or source)
    if not instances:
        if not in_place:
            _unlink(destination)
        return source, []
    staging = atomic_tmp_name(destination)
    try:
        with open(staging, "w") as f:
            f.write(cleaned)
        os.replace(staging, destination)
    except OSError:
        _unlink(staging)
        raise
    return destination, instances


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
