"""Instances of `blocks:` modules in a mapped netlist: their checks, and the parameter overrides OpenROAD cannot read.

A parent instances a parameterised hardened block through a `(* blackbox *)` stub that declares the block's parameters. Yosys then writes the instance in one of two forms OpenROAD's structural `read_verilog` cannot use:

- `blk #(.AW(32'd4), .W(32'd16)) u_blk (...)`: slang, which lists every parameter, localparams included. OpenROAD rejects the syntax (`STA-0171`).
- `\\$paramod\\blk\\W=s32'0...10000  u_blk (...)`: the native `verilog` frontend, which derives a copy of the blackbox per parameter set. OpenROAD finds no such master. When the parameter list is long, Yosys names the copy by a hash, `\\$paramod$<sha1>\\blk`, and the values cannot be recovered.

A hardened block has exactly one parameterisation, the one its abstract was built from, so `clean_netlist` checks every instance of a block and then rewrites it as `blk u_blk (...)`:

- **Ports.** Every connected port is a pin or bus of the block's abstract LEF with the same width, and every signal pin of the abstract is connected. An unconnected input or inout fails; an unconnected output is a warning.
- **Parameters.** Every override, named or positional, is compared with the value recorded in the abstract's `<top>.params.json` when the block was hardened. See `check_params` for what a record cannot cover.
- **The rewrite itself.** The output's tokens are the input's minus exactly the `#(...)` lists, with each `$paramod` master renamed, and rewriting it again changes nothing.

The netlist is scanned once for comments, attributes, strings and escaped identifiers. Only the statements that instance a block, and the declarations of the module holding them, are tokenised, so the cost stays linear in the netlist's size. Connection widths come from declarations, selects, concatenations and sized constants, the subset Yosys `write_verilog` produces; anything else fails closed with a message naming the connection.
"""

import bisect
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import NamedTuple

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


class _Token(NamedTuple):
    kind: str
    text: str
    start: int
    end: int

    @property
    def name(self) -> str:
        """The identifier this token names, without an escaped identifier's backslash."""
        return self.text[1:] if self.kind == "escid" else self.text


def _tokens(
    text: str, *, attributes: bool = False, pos: int = 0, endpos: int | None = None
) -> list[_Token]:
    """Return the significant tokens of Verilog `text`: no whitespace or comments, and attributes only when asked."""
    skip = ("ws", "comment") if attributes else _TRIVIA
    finditer = (
        _TOKEN_RE.finditer(text, pos)
        if endpos is None
        else _TOKEN_RE.finditer(text, pos, endpos)
    )
    return [
        _Token(m.lastgroup, m.group(), m.start(), m.end())
        for m in finditer
        if m.lastgroup not in skip
    ]


# A module's optional lifetime keyword: `module automatic m`.
_LIFETIMES = ("automatic", "static")

_BLACKBOX_ATTR_RE = re.compile(r"^\(\*.*\bblackbox\b.*\*\)$", re.DOTALL)


def _header_start(toks: list[_Token], n: int) -> int:
    """Return the index past a module name at `n`: past any `import p::*, q::x;` declarations."""
    i = n + 1
    while i < len(toks) and toks[i].text == "import":
        while i < len(toks) and toks[i].text != ";":
            i += 1
        i += 1
    return i


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

    The header runs from `module` to the first `;` outside parentheses that follows any package imports, so a parameter port list such as `#(parameter int W = 8, localparam int AW = $clog2(W))` is handled.
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
        after = _header_start(toks, n)
        depth = 0
        j = after
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
                parameterised=after < len(toks) and toks[after].text == "#",
            )
        )
        i = k + 1
    return found


@dataclass
class BlockInstance:
    """One instance of a block module in a netlist."""

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
    """Raised when the rewrite changed more than the override lists and masters: an rtl_buddy bug."""


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
    """Return `{name: value}` for the overrides between two parentheses: `.NAME(value)`, or positional `#0`, `#1`, ..."""
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


# --- Literal values ---------------------------------------------------------------------------

_BASED_RE = re.compile(
    r"^(?P<width>\d+)?'(?P<signed>[sS])?(?P<base>[bBoOdDhH])(?P<digits>[0-9a-fA-FxXzZ?]+)$"
)
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}
_DIGIT_BITS = {"b": 1, "o": 3, "h": 4}
_REAL_RE = re.compile(r"^\d+(\.\d+)?([eE][+-]?\d+)?$")


def parse_value(text: str):
    """Return a Verilog parameter value as an int, float or str, or None when it is not a literal.

    A sized or based literal is returned as `(value, width)`: reduced modulo 2**width and sign-extended only when it is signed. A literal with x or z bits is returned as its bit string, `"4'b10x1"`, so that equal bits compare equal however they are written. A string literal is returned as its contents.
    """
    lit = _literal(text)
    if lit is None or isinstance(lit, (str, float)):
        return lit
    value, width, _signed, bits = lit
    if bits is not None:
        return f"{width}'b{bits}"
    return (value, width)


def is_number(text: str) -> bool:
    """Whether `text` is a numeric Verilog literal: decimal, based, x/z or real."""
    return isinstance(_literal(text), (tuple, float))


def _literal(text):
    """Parse `text` to a str, a float, or `(value, width, signed, xz_bits)`, or None."""
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
        digits = m.group("digits").lower().replace("?", "z")
        base = m.group("base").lower()
        width = int(m.group("width")) if m.group("width") else None
        # A leading minus does not make a literal signed: `-8'd1` is 8'd255.
        signed = bool(m.group("signed"))
        if any(c in "xz" for c in digits):
            if base == "d" or width is None:
                return None
            per = _DIGIT_BITS[base]
            bits = "".join(
                c * per if c in "xz" else format(int(c, 16), f"0{per}b") for c in digits
            )
            pad = bits[0] if bits[0] in "xz" else "0"
            bits = (pad * width + bits)[-width:]
            return (None, width, signed, bits)
        value = int(digits, _BASES[base])
        if negative:
            value = -value
        if width:
            value %= 1 << width
            if signed and value >= 1 << (width - 1):
                value -= 1 << width
        return (value, width, signed, None)
    try:
        value = int(s, 10)
    except ValueError:
        pass
    else:
        return (-value if negative else value, None, True, None)
    if not _REAL_RE.match(s):
        return None
    number = float(s)
    return -number if negative else number


def literals_equal(a: str, b: str) -> bool:
    """Return whether two Verilog literals denote the same parameter value.

    Integers compare as whole values, each reduced to its own width and sign-extended only when signed, so `12'h000` differs from `'h1000`. Two literals of the same width also compare equal when their bits are equal, because a recorded value carries no signedness. x/z literals compare by their bits; strings compare verbatim, except that a string reading as a real or unsized decimal number compares with a number as that number: Yosys records a `real` parameter as the string `"2.500000"` and writes it to a netlist as `2.500000`.
    """
    la, lb = _literal(a), _literal(b)
    if isinstance(la, str) != isinstance(lb, str):
        la, lb = _string_as_number(la), _string_as_number(lb)
    if la is None or lb is None:
        return False
    if isinstance(la, str) or isinstance(lb, str):
        return la == lb
    if isinstance(la, float) or isinstance(lb, float):
        na = la if isinstance(la, float) else la[0]
        nb = lb if isinstance(lb, float) else lb[0]
        return na is not None and nb is not None and float(na) == float(nb)
    va, wa, _, xa = la
    vb, wb, _, xb = lb
    if xa is not None or xb is not None:
        return _bits_of(la) == _bits_of(lb)
    if wa and wa == wb and va % (1 << wa) == vb % (1 << wb):
        return True
    return va == vb


def _string_as_number(lit):
    """Return a string literal's contents parsed as a real or an unsized decimal, or None when they are not one; any other literal unchanged."""
    if not isinstance(lit, str):
        return lit
    number = _literal(lit)
    if isinstance(number, float):
        return number
    if isinstance(number, tuple) and number[1] is None and number[3] is None:
        return number
    return None


def _bits_of(lit, width: int | None = None) -> str | None:
    value, w, _signed, bits = lit
    if bits is not None:
        return bits
    if w is None:
        return None
    return format(value % (1 << w), f"0{w}b")


def values_equal(netlist_value: str, configured) -> bool:
    """Return whether a netlist override equals a `params:` value from YAML.

    A YAML integer equals a literal of width W when they agree modulo 2**W and the integer fits W bits, signed or unsigned, so `32'd4294967295` equals -1 but `12'h000` does not equal 4096. A YAML string is read as a Verilog literal when it parses as one (`"8'hff"`), and otherwise compared with a string literal's contents. A quoted unsized number (`"-1"`, `"255"`) is the integer it spells, so it gets the same fits-the-width rule.
    """
    if isinstance(configured, bool):
        configured = int(configured)
    if isinstance(configured, str):
        want = _literal(configured)
        if want is None:
            return _literal(netlist_value) == configured
        if isinstance(want, tuple) and want[1] is None and want[3] is None:
            configured = want[0]
        else:
            return literals_equal(netlist_value, configured)
    got = _literal(netlist_value)
    if got is None or isinstance(got, str):
        return False
    if isinstance(configured, int):
        if isinstance(got, float):
            return got == configured
        value, width, _, bits = got
        if bits is not None:
            return False
        if width:
            fits = -(1 << (width - 1)) <= configured < (1 << width)
            return fits and configured % (1 << width) == value % (1 << width)
        return value == configured
    if isinstance(configured, float):
        number = got if isinstance(got, float) else got[0]
        return number is not None and float(number) == configured
    return False


# --- Abstract pins -------------------------------------------------------------------------

_SUPPLY_USES = ("POWER", "GROUND")


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

    The bus characters come from the LEF's `BUSBITCHARS` (default `[]`). An escaped bus character, `d\\[0\\]`, is part of a scalar pin's name. The LEF is the view `write_abstract_lef` produces for `harden: true`, and the one OpenROAD binds each instance to. Raises BlockParamError when the macro is missing or a bus has gaps.
    """
    try:
        with open(lef_path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise BlockParamError(f"cannot read block abstract {lef_path}: {e}") from None
    open_c, close_c = "[", "]"
    bus_re = None
    in_macro = False
    pin = None
    bits: dict[str, list[int]] = {}
    pins: dict[str, AbstractPin] = {}
    for line in lines:
        words = line.replace(";", " ; ").split()
        if not words:
            continue
        if words[0] == "BUSBITCHARS" and len(words) > 1:
            chars = words[1].strip('"')
            if len(chars) == 2:
                open_c, close_c = chars[0], chars[1]
            bus_re = None
            continue
        if words[0] == "MACRO" and len(words) > 1:
            in_macro = words[1] == macro
            continue
        if not in_macro:
            continue
        if words[0] == "END" and len(words) > 1 and words[1] == macro:
            break
        if words[0] == "PIN" and len(words) > 1:
            if bus_re is None:
                bus_re = re.compile(
                    rf"^(?P<base>.+?)(?<!\\){re.escape(open_c)}(?P<index>\d+)"
                    rf"(?<!\\){re.escape(close_c)}$"
                )
            name = words[1]
            m = bus_re.match(name)
            base = m.group("base") if m else name
            # An escaped bus character is literal: `d\[0\]` is the scalar `d[0]`.
            base = re.sub(r"\\(.)", r"\1", base)
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


# --- Scanning a netlist ---------------------------------------------------------------------

# Everything that can hide a module name or a keyword: comments, attributes and strings are masked; escaped identifiers are opaque.
_SCAN_RE = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<attr>\(\*(?!\))(?:"(?:\\.|[^"\\])*"|[^"])*?\*\))
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<escid>\\\S+)
    """,
    re.DOTALL | re.VERBOSE,
)
_MODULE_KW_RE = re.compile(r"(?<![\w$\\])(module|macromodule|endmodule)(?![\w$])")
_DECL_KEYWORDS = (
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
)
_DECL_KW_RE = re.compile(rf"(?<![\w$\\.])(?:{'|'.join(_DECL_KEYWORDS)})(?![\w$])")
# The one-name declaration Yosys writes, e.g. `wire [15:0] \a.b ;`, read without tokenising.
_FAST_DECL_RE = re.compile(
    rf"(?:{'|'.join(_DECL_KEYWORDS)})"
    r"(?:\s+(?:signed|unsigned|wire|reg|logic|var)(?![\w$]))*"
    r"\s*(?:\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\])?"
    r"\s*([A-Za-z_][\w$]*|\\\S+)\s*;"
)
_DECL_MODIFIERS = {"signed", "unsigned", "wire", "reg", "logic", "var"}
_SIZED_RE = re.compile(r"^(\d+)\s*'")
# A native-frontend derived blackbox: `$paramod\blk\W=s32'0...`, or `$paramod$<sha1>\blk`.
_PARAMOD_RE = re.compile(
    r"^\$paramod(?P<hash>\$[0-9a-f]+)?\\(?P<module>[^\\]+)(?P<params>\\.*)?$"
)
_PARAMOD_VALUE_RE = re.compile(
    r"^(?P<name>[\w$]+)=(?P<s>s?)(?P<w>\d+)'(?P<bits>[01xz]+)$"
)
_SIMPLE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class _Index:
    """Masked regions, escaped identifiers and module spans of a netlist, found in one pass."""

    def __init__(self, text: str):
        self.text = text
        mask_starts, mask_ends = [], []
        self.escids: list[tuple[int, int]] = []
        for m in _SCAN_RE.finditer(text):
            if m.lastgroup == "escid":
                self.escids.append((m.start(), m.end()))
            else:
                mask_starts.append(m.start())
                mask_ends.append(m.end())
        self._mask_starts, self._mask_ends = mask_starts, mask_ends
        self._mask_by_end = dict(zip(mask_ends, mask_starts))
        self._esc_starts = [s for s, _ in self.escids]
        modules = []
        open_at = None
        for m in _MODULE_KW_RE.finditer(text):
            if self.hidden(m.start()):
                continue
            if m.group(1) == "endmodule":
                if open_at is not None:
                    modules.append((open_at, m.start()))
                open_at = None
            else:
                open_at = m.end()
        self._module_starts = [s for s, _ in modules]
        self._modules = modules
        self._widths: dict[int, dict[str, int | None]] = {}
        self._line_pos = 0
        self._line_no = 1

    def _inside(self, starts, ends, pos: int) -> bool:
        i = bisect.bisect_right(starts, pos) - 1
        return i >= 0 and pos < ends[i]

    def masked(self, pos: int) -> bool:
        return self._inside(self._mask_starts, self._mask_ends, pos)

    def hidden(self, pos: int) -> bool:
        """Whether `pos` is in a comment, attribute, string or escaped identifier."""
        if self.masked(pos):
            return True
        i = bisect.bisect_right(self._esc_starts, pos) - 1
        return i >= 0 and pos < self.escids[i][1]

    def line(self, pos: int) -> int:
        """Return the 1-based line of `pos`; positions must be asked in increasing order."""
        if pos < self._line_pos:
            self._line_pos, self._line_no = 0, 1
        self._line_no += self.text.count("\n", self._line_pos, pos)
        self._line_pos = pos
        return self._line_no

    def previous_word(self, pos: int) -> tuple[str, int]:
        """Return the significant word or character before `pos`, skipping whitespace, comments and attributes, and where it starts."""
        text = self.text
        q = pos - 1
        while q >= 0:
            if text[q].isspace():
                q -= 1
            elif (q + 1) in self._mask_by_end:
                q = self._mask_by_end[q + 1] - 1
            else:
                break
        if q < 0:
            return "", 0
        j = q
        while j >= 0 and (text[j].isalnum() or text[j] in "_$"):
            j -= 1
        if j == q:
            return text[q], q
        return text[j + 1 : q + 1], j + 1

    def module_span(self, pos: int) -> tuple[int, int] | None:
        i = bisect.bisect_right(self._module_starts, pos) - 1
        if i >= 0 and pos < self._modules[i][1]:
            return self._modules[i]
        return None

    def statement(self, pos: int) -> list[_Token]:
        """Tokenise from `pos` through the `;` that ends the statement, or up to a `module`/`endmodule`."""
        toks = []
        depth = 0
        for m in _TOKEN_RE.finditer(self.text, pos):
            kind = m.lastgroup
            if kind in _TRIVIA:
                continue
            tok = _Token(kind, m.group(), m.start(), m.end())
            if kind == "id" and tok.text in ("module", "macromodule", "endmodule"):
                break
            toks.append(tok)
            if tok.text in ("(", "{", "["):
                depth += 1
            elif tok.text in (")", "}", "]"):
                depth -= 1
            elif tok.text == ";" and depth <= 0:
                break
        return toks

    def widths(self, span: tuple[int, int]) -> dict[str, int | None]:
        """Return the widths declared in a module, read once per module."""
        lo, hi = span
        if lo in self._widths:
            return self._widths[lo]
        widths: dict[str, int | None] = {}
        text = self.text
        for m in _DECL_KW_RE.finditer(text, lo, hi):
            if self.hidden(m.start()):
                continue
            fast = _FAST_DECL_RE.match(text, m.start(), hi)
            if fast:
                name = fast.group(3)
                name = name[1:] if name.startswith("\\") else name
                if fast.group(1) is None:
                    widths[name] = 1
                else:
                    widths[name] = abs(int(fast.group(1)) - int(fast.group(2))) + 1
                continue
            widths.update(_declaration(self.statement(m.start())))
        self._widths[lo] = widths
        return widths


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


def _declaration(toks: list[_Token]) -> dict[str, int | None]:
    """Return the names one declaration statement declares and their widths; None for one rb cannot size.

    It stops at the next declaration keyword or the `)` of an ANSI port list.
    """
    widths: dict[str, int | None] = {}
    j = 1
    hi = len(toks)
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
            break
        if t.text in ("(", "{", "["):
            depth += 1
        elif t.text in (")", "}", "]"):
            depth -= 1
            if depth < 0:
                break
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


@dataclass
class _Site:
    """One statement that instances a block, and how it is rewritten."""

    tokens: list[_Token]
    instance: BlockInstance | None
    #: Why it is not a readable instance; set when it must be rewritten but cannot be checked.
    problem: str | None = None
    #: Token index ranges to drop (`#(...)`) and the master token's new text.
    drop: list[tuple[int, int]] = field(default_factory=list)
    rename: str | None = None
    #: Index of the instance name token.
    inst_index: int = 0

    @property
    def start(self) -> int:
        return self.tokens[0].start

    @property
    def end(self) -> int:
        return self.tokens[-1].end

    def rewritten_tokens(self) -> list[tuple[str, str]]:
        """The (kind, text) stream the rewrite must produce for this statement."""
        dropped = {i for lo, hi in self.drop for i in range(lo, hi)}
        out = []
        for i, t in enumerate(self.tokens):
            if i in dropped:
                continue
            if i == 0 and self.rename is not None:
                kind = "id" if _SIMPLE_ID_RE.match(self.rename) else "escid"
                text = self.rename if kind == "id" else f"\\{self.rename}"
                out.append((kind, text))
            else:
                out.append((t.kind, t.text))
        return out


def _candidates(index: _Index, modules: set[str]):
    """Yield (position, module, paramod match) for each spelling of a block module that could start an instance."""
    text = index.text
    found = []
    if modules:
        plain = re.compile(
            r"(?<![\w$\\.])("
            + "|".join(re.escape(m) for m in sorted(modules))
            + r")(?![\w$])"
        )
        for m in plain.finditer(text):
            if not index.hidden(m.start()):
                found.append((m.start(), m.group(1), None))
    for s, e in index.escids:
        if index.masked(s):
            continue
        name = text[s + 1 : e]
        if name in modules:
            found.append((s, name, None))
        elif name.startswith("$paramod"):
            pm = _PARAMOD_RE.match(name)
            if pm and pm.group("module") in modules:
                found.append((s, pm.group("module"), pm))
    found.sort(key=lambda c: c[0])
    return found


def _paramod_of(tok: _Token):
    """Return the `$paramod` match of a master token, or None."""
    if tok.kind != "escid" or not tok.name.startswith("$paramod"):
        return None
    return _PARAMOD_RE.match(tok.name)


def _scan(text: str, modules: set[str], where: str) -> tuple[_Index, list[_Site]]:
    """Find every statement that instances a block module."""
    index = _Index(text)
    sites: list[_Site] = []
    for pos, module, paramod in _candidates(index, modules):
        word, wstart = index.previous_word(pos)
        if word == ".":
            continue
        if word in _LIFETIMES:
            word, _ = index.previous_word(wstart)
        if word in ("module", "macromodule"):
            continue
        toks = index.statement(pos)
        if not toks or toks[-1].text != ";":
            continue
        line = index.line(pos)
        sites.append(_site(text, index, toks, module, paramod, line, where))
    return index, sites


def _site(text, index, toks, module, paramod, line, where) -> _Site:
    """Read one candidate statement as an instance, or record why it cannot be."""
    i = 1
    params: dict[str, str] = {}
    drop: list[tuple[int, int]] = []
    rename = None
    context = f"{where}: instance at line {line} of block {module!r}"
    if paramod is not None:
        rename = module
        if paramod.group("hash"):
            site = _Site(toks, None, rename=rename)
            site.problem = (
                f"{context} is written as {toks[0].text.strip()}, a derived blackbox "
                "named by a hash from which rtl_buddy cannot recover the "
                "parameters — synthesise the parent with frontend: slang"
            )
            return site
        for segment in (paramod.group("params") or "")[1:].split("\\"):
            if not segment:
                continue
            mv = _PARAMOD_VALUE_RE.match(segment)
            if not mv:
                site = _Site(toks, None, rename=rename)
                site.problem = (
                    f"{context}: cannot read parameter {segment!r} of the derived "
                    f"blackbox {toks[0].text.strip()} — synthesise the parent with "
                    "frontend: slang"
                )
                return site
            params[mv.group("name")] = (
                f"{mv.group('w')}'{mv.group('s')}b{mv.group('bits')}"
            )
    if i < len(toks) and toks[i].text == "#":
        if i + 1 >= len(toks) or toks[i + 1].text != "(":
            return _Site(toks, None, problem=f"{context}: unreadable override list")
        close = _matching_paren(toks, i + 1)
        params.update(_parse_overrides(text, toks, i + 1, close))
        drop.append((i, close + 1))
        i = close + 1
    if not (
        i + 1 < len(toks)
        and toks[i].kind in ("id", "escid")
        and toks[i + 1].text in ("(", "[")
    ):
        site = _Site(toks, None, drop=drop, rename=rename)
        if drop or rename:
            site.problem = (
                f"{context} carries overrides but could not be read as an instance, "
                "so it cannot be checked"
            )
        return site
    inst = BlockInstance(module=module, instance=toks[i].name, line=line, params=params)
    return _Site(toks, inst, drop=drop, rename=rename, inst_index=i)


def _connections(site: _Site, index: _Index, where: str) -> None:
    """Fill the instance's port connection widths, or raise BlockParamError naming what cannot be read."""
    inst = site.instance
    toks = site.tokens
    context = f"{where}: instance {inst.instance!r} (line {inst.line}) of block {inst.module!r}"
    i = site.inst_index
    if toks[i + 1].text == "[":
        raise BlockParamError(f"{context}: instance arrays are not supported")
    span = index.module_span(site.start)
    widths = index.widths(span) if span else {}
    open_idx = i + 1
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
                shown = " ".join(index.text[expr[0].start : expr[-1].end].split())
                raise BlockParamError(
                    f"{context}: cannot tell the width of port {port!r} "
                    f"connected to `{shown}` ({e})"
                ) from None
        k = end + 1


def block_instances(text: str, modules: set[str], where: str) -> list[BlockInstance]:
    """Return every instance of `modules` in netlist `text`, with its overrides and the width of each port connection.

    Raises BlockParamError, naming the instance and port, for a connection it cannot size, a positional port list, an instance array, or an instance rb must rewrite but cannot read.
    """
    index, sites = _scan(text, modules, where)
    return _instances(index, sites, where)


def _instances(index: _Index, sites: list[_Site], where: str) -> list[BlockInstance]:
    found = []
    for site in sites:
        if site.problem:
            raise BlockParamError(site.problem)
        if site.instance is None:
            continue
        _connections(site, index, where)
        found.append(site.instance)
    return found


def strip_overrides(text: str, modules: set[str]) -> tuple[str, list[BlockInstance]]:
    """Rewrite every instance of `modules` in netlist `text` as `blk name (...)`: drop `#(...)` lists and rename `$paramod` masters.

    Returns the rewritten text and the instances that changed. A module header `module m #(...)` is not an instance and is left alone. The result is verified before it is returned (see `_verify`); a failure raises StripSelfCheckError.
    """
    _index, sites = _scan(text, modules, "netlist")
    cleaned, changed = _rewrite(text, sites)
    _verify(text, cleaned, changed, modules)
    return cleaned, [s.instance for s, _, _ in changed if s.instance is not None]


def _rewrite(text: str, sites: list[_Site]) -> tuple[str, list[tuple[_Site, int, int]]]:
    """Apply each site's drops and rename; return the text and (site, start, end) of each changed statement in it."""
    pieces = []
    changed = []
    last = 0
    out_len = 0
    for site in sites:
        if not site.drop and site.rename is None:
            continue
        toks = site.tokens
        # A list goes with the whitespace before it, unless a comment sits there.
        cuts = [
            (
                toks[lo - 1].end
                if text[toks[lo - 1].end : toks[lo].start].isspace()
                else toks[lo].start,
                toks[hi - 1].end,
                "",
            )
            for lo, hi in site.drop
        ]
        if site.rename is not None:
            name = (
                site.rename if _SIMPLE_ID_RE.match(site.rename) else f"\\{site.rename}"
            )
            cuts.append((toks[0].start, toks[0].end, name))
        cuts.sort()
        gap = text[last : site.start]
        pieces.append(gap)
        out_len += len(gap)
        region = []
        at = site.start
        for start, end, new in cuts:
            region.append(text[at:start])
            region.append(new)
            # Keep the tokens either side of a removed list apart.
            before = text[start - 1] if start > 0 else " "
            after = text[end] if end < len(text) else " "
            if not new and not before.isspace() and not after.isspace():
                region.append(" ")
            at = end
        region.append(text[at : site.end])
        body = "".join(region)
        pieces.append(body)
        changed.append((site, out_len, out_len + len(body)))
        out_len += len(body)
        last = site.end
    pieces.append(text[last:])
    return "".join(pieces), changed


def _verify(text, cleaned, changed, modules) -> None:
    """Check the rewrite by token streams, without reusing its span logic.

    Outside the changed statements the output is byte-identical to the input; each changed statement, tokenised afresh from the output, equals the input's tokens minus the dropped `#(...)` runs with the master renamed; and re-reading each rewritten statement from the output finds nothing left to rewrite.
    """
    at_in = at_out = 0
    for site, out_start, out_end in changed:
        if cleaned[at_out:out_start] != text[at_in : site.start]:
            raise StripSelfCheckError(
                "the rewritten netlist differs from the input outside the block instances"
            )
        got = [
            (t.kind, t.text) for t in _tokens(cleaned, pos=out_start, endpos=out_end)
        ]
        if got != site.rewritten_tokens():
            raise StripSelfCheckError(
                f"the rewrite of the statement at offset {site.start} changed more "
                "than its override list and master"
            )
        at_in, at_out = site.end, out_end
    if cleaned[at_out:] != text[at_in:]:
        raise StripSelfCheckError(
            "the rewritten netlist differs from the input after the last block instance"
        )
    # Re-reading each rewritten statement from the output must find nothing left to rewrite; the rest of the output is the input, which needed nothing.
    for site, out_start, out_end in changed:
        again = _tokens(cleaned, pos=out_start, endpos=out_end)
        module = site.rename or site.tokens[0].name
        redo = _site(cleaned, None, again, module, _paramod_of(again[0]), 0, "")
        if redo.drop or redo.rename is not None:
            raise StripSelfCheckError(
                "rewriting the rewritten netlist changed it again"
            )


def check_ports(instances: list[BlockInstance], blocks, where: str) -> list[str]:
    """Raise BlockParamError unless every instance's connections match its block's abstract pins.

    Returns warnings: an unconnected output pin is allowed, reported once per block and pin with an instance count.
    """
    by_name = {b.ref.name: b for b in blocks}
    pins_of: dict[str, dict[str, AbstractPin]] = {}
    unconnected: dict[tuple[str, str], list[BlockInstance]] = {}
    for inst in instances:
        if inst.module not in pins_of:
            block = by_name[inst.module]
            pins_of[inst.module] = abstract_pins(block.lef, block.ref.name)
        pins = pins_of[inst.module]
        context = (
            f"{where}: instance {inst.instance!r} (line {inst.line}) of block "
            f"{inst.module!r}"
        )
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
            if pin.direction == "OUTPUT":
                unconnected.setdefault((inst.module, name), []).append(inst)
                continue
            raise BlockParamError(
                f"{context}: {pin.direction.lower()} pin {name!r} is not connected"
            )
    return [
        f"{where}: output pin {pin!r} of block {module!r} is not connected on "
        + _count(insts)
        for (module, pin), insts in unconnected.items()
    ]


def _count(insts: list[BlockInstance]) -> str:
    first = insts[0]
    more = f" and {len(insts) - 1} more instance(s)" if len(insts) > 1 else ""
    return f"instance {first.instance!r} (line {first.line}){more}"


# --- Elaborated parameter records ----------------------------------------------------------

#: Bumped on an incompatible change to a parameter record.
PARAM_RECORD_SCHEMA = 1

#: The parameter record's name in an abstract directory: `<top>.params.json`.
PARAM_RECORD_SUFFIX = ".params.json"


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

    Bits are a string of 0/1/x/z. Yosys appends one space to a string that would otherwise read as bits, and only to such a string, so a real trailing space survives.
    """
    if value and set(value) <= set("01xz"):
        return f"{len(value)}'b{value}"
    if value.endswith(" ") and value[:-1] and set(value[:-1]) <= set("01xz"):
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


def _module_bodies(toks: list[_Token]):
    """Yield (name, index past the name and any imports, endmodule index) for each module."""
    i = 0
    while i < len(toks):
        if toks[i].text in ("module", "macromodule"):
            n = i + 1
            if n < len(toks) and toks[n].text in _LIFETIMES:
                n += 1
            end = n
            while end < len(toks) and toks[end].text != "endmodule":
                end += 1
            yield toks[n].name, _header_start(toks, n), end
            i = end + 1
            continue
        i += 1


def parameter_port_names(text: str, module: str) -> list[str] | None:
    """Return the overridable parameters of `module` in declaration order, or None when it is not declared in `text`.

    These are the `parameter` entries of the `#(...)` header list, in which a `localparam` keyword stays in force until the next keyword; without a header list, the body's `parameter` declarations. Package imports before the list are skipped.
    """
    toks = _tokens(text)
    for name, lo, hi in _module_bodies(toks):
        if name != module:
            continue
        if lo >= len(toks) or toks[lo].text != "#":
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
    in_value = False
    last = None
    for t in [*item, _Token("punct", ",", 0, 0)]:
        if t.text in ("(", "[", "{"):
            depth += 1
            continue
        if t.text in (")", "]", "}"):
            depth -= 1
            continue
        if depth:
            continue
        if t.text == ",":
            if last is not None:
                names.append(last)
            last, in_value = None, False
        elif t.text == "=":
            in_value = True
        elif not in_value and t.kind in ("id", "escid"):
            last = t.name
    return names


def _named(
    inst: BlockInstance, order: list[str] | None, context: str
) -> dict[str, str]:
    """Return the instance's overrides by parameter name, mapping positional ones through `order`."""
    named: dict[str, str] = {}
    for key, value in inst.params.items():
        if not key.startswith("#"):
            named[key] = value
            continue
        position = int(key[1:])
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

    A record from slang lists every parameter and localparam, and an override it lacks fails. A record from the native frontend has no localparams, so an override it lacks is a warning. A block with no record (hardened by an older rtl_buddy) is checked against its synthesis's `params:` only, with a warning to re-harden. Warnings are reported once per block and parameter, with an instance count.
    """
    by_name = {b.ref.name: b for b in blocks}
    records = {name: block_param_record(b) for name, b in by_name.items()}
    warnings: list[str] = []
    unrecorded: set[str] = set()
    unchecked: dict[tuple[str, str], list[BlockInstance]] = {}
    named_overrides: dict[int, dict[str, str]] = {}
    for inst in instances:
        context = (
            f"{where}: instance {inst.instance!r} (line {inst.line}) of block "
            f"{inst.module!r}"
        )
        record = records.get(inst.module)
        named = _named(inst, record.order if record else None, context)
        named_overrides[id(inst)] = named
        if record is None:
            if inst.module not in unrecorded:
                unrecorded.add(inst.module)
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
                unchecked.setdefault((inst.module, key), []).append(inst)
                continue
            if not literals_equal(value, record.parameters[key]):
                mismatched.append(key)
        if mismatched:
            got = ", ".join(f"{k}={named[k]}" for k in mismatched)
            want = ", ".join(
                f"{k}={_shown(record.parameters[k], signed=_is_signed(named[k]))}"
                for k in mismatched
            )
            raise BlockParamError(
                f"{context} sets {got}, but the block was hardened with {want} — "
                "make the instance match, or re-harden the block with the "
                "parameters the parent needs"
            )
    for (module, key), insts in unchecked.items():
        record = records[module]
        warnings.append(
            f"{where}: {key} on block {module!r} is not checked on "
            f"{_count(insts)}: the block's record ({record.frontend} frontend) has "
            "no localparams — harden it with frontend: slang for a complete record"
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


def _is_signed(literal: str) -> bool:
    """Whether a netlist override is a signed integer literal: `-1`, `32'sd5`."""
    lit = _literal(literal)
    return isinstance(lit, tuple) and lit[2]


def _shown(literal: str, *, signed: bool = False) -> str:
    """Return a recorded literal as a reader would write it: `32'd16` rather than 32 bits.

    A record carries no signedness, so a negative value reads as its unsigned bits. When the override it is shown against is signed, a value with its top bit set is shown signed, `-32'sd1` rather than `32'd4294967295`.
    """
    value = parse_value(literal)
    if isinstance(value, tuple) and value[1]:
        number, width = value
        if signed and number >= 1 << (width - 1):
            number -= 1 << width
        if number < 0:
            return f"-{width}'sd{-number}"
        return f"{width}'d{number}"
    return literal


def block_param_record(block) -> ParamRecord | None:
    """Return the parameter record published with a resolved block's abstract, or None."""
    return read_param_record(
        os.path.join(block.abstract_dir, f"{block.ref.name}{PARAM_RECORD_SUFFIX}")
    )


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
    """Check every `blocks:` instance in netlist `text` and rewrite them for OpenROAD.

    Returns the rewritten text (unchanged when no instance needs it) and the instances that changed. Raises BlockParamError when a check fails; warnings are logged as `blocks.check_warning`.
    """
    modules = {b.ref.name for b in blocks}
    if not modules:
        return text, []
    index, sites = _scan(text, modules, where)
    instances = _instances(index, sites, where)
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
    cleaned, changed = _rewrite(text, sites)
    _verify(text, cleaned, changed, modules)
    return cleaned, [s.instance for s, _, _ in changed if s.instance is not None]


def clean_netlist(
    source: str, destination: str, blocks, *, where: str | None = None
) -> tuple[str, list[BlockInstance]]:
    """Return the netlist path OpenROAD should read, and the instances that were rewritten.

    Every instance of a block is checked. When one needs rewriting, a rewritten copy is written to `destination`, which is returned; `destination` may be `source` itself. Otherwise `source` is returned and any stale `destination` is removed. `where` names the netlist in an error, by default `source`. Raises BlockParamError on a failed check and OSError when the netlist cannot be read or written.
    """
    in_place = os.path.abspath(destination) == os.path.abspath(source)
    if not blocks:
        if not in_place:
            _unlink(destination)
        return source, []
    with open(source) as f:
        text = f.read()
    cleaned, instances = clean_text(text, blocks, where or source)
    if cleaned == text:
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
