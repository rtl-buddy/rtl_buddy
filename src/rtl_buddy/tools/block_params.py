"""Parameter overrides on instances of `blocks:` modules in a mapped netlist.

A parent instances a parameterised hardened block through a `(* blackbox *)` stub that declares the block's parameters. Yosys writes every parameter of such an instance into the netlist as a `#(...)` override, localparams included, and OpenROAD's structural `read_verilog` rejects the syntax (`STA-0171`). A hardened block has exactly one parameterisation, the one its abstract was built from, so the overrides carry no information OpenROAD needs once they are checked.

`clean_netlist` strips the overrides from the instances of the named modules after checking them:

- Every instance of a block carries the same values.
- A parameter that the block's synthesis sets in `params:` has that value.

Other parameters, the stub's defaults and localparams such as a width derived with `$clog2`, are not checked: a hardened block's netlist records none of its elaborated values, so there is nothing to compare them with. They follow from the checked ones when the stub matches the block's RTL.

The netlist is tokenised, not matched with a regular expression, so escaped identifiers, sized and signed literals, strings, attributes, comments and line breaks inside an override list are handled.
"""

import os
import re
from dataclasses import dataclass, field

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


def blackbox_modules(text: str) -> list[BlackboxModule]:
    """Return the modules in Verilog `text` that a `(* blackbox *)` attribute marks.

    The header runs from `module` to the first `;` outside parentheses, so a parameter port list such as `#(parameter int W = 8, localparam int AW = $clog2(W))` is handled.
    """
    toks = _tokens(text, attributes=True)
    found = []
    i = 0
    while i < len(toks) - 2:
        attr = toks[i]
        if not (
            attr.kind == "attr"
            and _BLACKBOX_ATTR_RE.match(attr.text)
            and toks[i + 1].text in ("module", "macromodule")
            and toks[i + 2].kind in ("id", "escid")
        ):
            i += 1
            continue
        depth = 0
        j = i + 3
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
                name=toks[i + 2].name,
                start=attr.start,
                header_start=toks[i + 1].start,
                header_end=toks[j].end,
                end=toks[k].end,
            )
        )
        i = k + 1
    return found


def defined_modules(text: str) -> set[str]:
    """Return the names of the modules Verilog `text` declares.

    Comments, strings and attributes cannot fake a declaration.
    """
    toks = _tokens(text)
    names = set()
    for i, tok in enumerate(toks[:-1]):
        if tok.kind == "id" and tok.text in ("module", "macromodule"):
            nxt = toks[i + 1]
            if nxt.kind in ("id", "escid"):
                names.add(nxt.name)
    return names


@dataclass
class BlockInstance:
    """One instance of a block module that carries a parameter override list."""

    module: str
    instance: str
    line: int
    #: Parameter name to its value as written, in netlist order.
    params: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.instance} ({shown})"


class BlockParamError(Exception):
    """Raised when a block instance's overrides cannot be stripped safely; the message says why."""


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
    """
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
        return text, found
    pieces = []
    last = 0
    for start, end in cuts:
        pieces.append(text[last:start])
        # Keep the module name and instance name apart, which an escaped name needs.
        if end < len(text) and not text[end].isspace():
            pieces.append(" ")
        last = end
    pieces.append(text[last:])
    return "".join(pieces), found


_BASED_RE = re.compile(
    r"^(?P<width>\d+)?'(?P<signed>[sS])?(?P<base>[bBoOdDhH])(?P<digits>[0-9a-fA-FxXzZ?]+)$"
)
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}


def parse_value(text: str):
    """Return a Verilog parameter value as an int, float or str, or None when it is not a literal.

    A sized literal is returned as `(value, width)`; a negative or signed one is sign-extended. Literals with x or z bits are returned as their normalised text, so they compare only with the same text.
    """
    s = "".join(str(text).split()).replace("_", "")
    negative = False
    if s.startswith(("-", "+")):
        negative = s[0] == "-"
        s = s[1:]
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1] if not negative else None
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


def check_instances(
    instances: list[BlockInstance], expected: dict[str, tuple[str, dict]], where: str
) -> None:
    """Raise BlockParamError unless the overrides agree with each other and with each block's `params:`.

    `expected` maps a block module to `(description of its synthesis, its params)`.
    """
    by_module: dict[str, list[BlockInstance]] = {}
    for inst in instances:
        by_module.setdefault(inst.module, []).append(inst)
    for module, insts in by_module.items():
        first = insts[0]
        for other in insts[1:]:
            keys = set(first.params) | set(other.params)
            differ = sorted(
                k
                for k in keys
                if parse_value(first.params.get(k, ""))
                != parse_value(other.params.get(k, ""))
            )
            if differ:
                raise BlockParamError(
                    f"{where}: instances of block {module!r} have different "
                    f"parameters ({', '.join(differ)}): {first.describe()} vs "
                    f"{other.describe()} — a hardened block has one "
                    "parameterisation; harden one block per parameter set"
                )
        synth, params = expected.get(module, ("", {}))
        for inst in insts:
            for key, configured in (params or {}).items():
                if key in inst.params and not values_equal(
                    inst.params[key], configured
                ):
                    raise BlockParamError(
                        f"{where}: instance {inst.instance!r} (line {inst.line}) of "
                        f"block {module!r} "
                        f"sets {key}={inst.params[key]}, but the block was "
                        f"synthesised with {key}={configured!r} ({synth}) — make "
                        "the instance match, or re-harden the block with the "
                        "parameters the parent needs"
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


def clean_text(text: str, blocks, where: str) -> tuple[str, list[BlockInstance]]:
    """Check and strip the overrides on `blocks:` instances in netlist `text`.

    Returns the text unchanged when no instance carries an override. Raises BlockParamError on a mismatch.
    """
    modules = {b.ref.name for b in blocks}
    if not modules:
        return text, []
    cleaned, instances = strip_overrides(text, modules)
    if instances:
        check_instances(instances, block_expectations(blocks), where)
    return cleaned, instances


def clean_netlist(
    source: str, destination: str, blocks, *, where: str | None = None
) -> tuple[str, list[BlockInstance]]:
    """Return the netlist path OpenROAD should read, and the instances whose overrides were stripped.

    When an instance of a block carries overrides, they are checked and a stripped copy is written to `destination`, which is returned; `destination` may be `source` itself. Otherwise `source` is returned and any stale `destination` is removed. `where` names the netlist in an error, by default `source`. Raises BlockParamError on a mismatch and OSError when the netlist cannot be read or written.
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
    staging = destination + ".tmp"
    with open(staging, "w") as f:
        f.write(cleaned)
    os.replace(staging, destination)
    return destination, instances


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
