"""Rewrite SDC constraints so they stay valid against obfuscated RTL.

An SDC file is a Tcl script: object names can be built at run time
(``get_ports ${prefix}_clk_i`` inside a ``foreach``). The file is evaluated by
the constraint reader's Tcl backend (:mod:`rtl_buddy.constraints.tcl_reader`),
which records every command with its words already substituted. The recorded
commands are written back out, expanded, with each design-object name
translated through the release name map.

- ``get_ports`` patterns must each match a port of the constraint's scope
  module. Its interface is preserved, so the names need no translation; a
  pattern matching nothing is a stale constraint.
- ``get_cells`` / ``get_pins`` / ``get_nets`` paths are translated segment by
  segment. A wildcard segment that would have to match renamed names cannot be
  translated faithfully and is an error, as is ``-filter``.
- Every other query (``get_clocks``, ``all_inputs``...) passes through.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..constraints.tcl_reader import TCL_BACKEND, read_commands
from ..constraints.tcl_tokenizer import _tokenize
from ..errors import FatalRtlBuddyError
from .namemap import NameMap

_QUERIES = frozenset(
    {
        "get_ports",
        "get_pins",
        "get_cells",
        "get_nets",
        "get_clocks",
        "get_lib_cells",
        "get_lib_pins",
        "get_libs",
        "get_designs",
        "all_inputs",
        "all_outputs",
        "all_clocks",
        "all_registers",
        "all_fanin",
        "all_fanout",
    }
)
_RENAMED = {"get_pins", "get_cells", "get_nets"}
_VALUE_OPTIONS = {"-of_objects", "-filter", "-hierarchical_separator"}
_SPAN = re.compile(r"\[(" + "|".join(sorted(_QUERIES)) + r")\b")


@dataclass
class _Ctx:
    name_map: NameMap
    scope: str
    scope_ports: set[str]
    errors: list[str] = field(default_factory=list)


def _wild(s: str) -> bool:
    return any(c in s for c in "*?")


def _unbrace(s: str) -> str:
    return s[1:-1] if s.startswith("{") and s.endswith("}") else s


def _segment(seg: str, ctx: _Ctx) -> str:
    renamed = ctx.name_map.renamed()
    if _wild(seg):
        rx = re.compile(fnmatch.translate(seg))
        hits = sorted(k for k in renamed if rx.fullmatch(k))
        if hits:
            ctx.errors.append(
                f"wildcard '{seg}' would have to match obfuscated names "
                f"({', '.join(hits[:5])}); list the objects explicitly"
            )
        return seg
    m = re.fullmatch(r"(.*?)((?:_reg)?(?:\[[^\]]*\])*)", seg)
    base, suffix = (m.group(1), m.group(2)) if m else (seg, "")
    return renamed[base] + suffix if base in renamed else seg


def _span(text: str, ctx: _Ctx) -> str:
    """Rewrite one ``[query ...]`` span."""
    words = _tokenize(text[1:-1])[0]
    cmd, args = words[0], words[1:]
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and not _wild(a):
            out.append(a)
            if a == "-filter":
                ctx.errors.append(
                    f"{cmd} -filter cannot be translated for obfuscated names; use explicit names"
                )
            if a in _VALUE_OPTIONS and i + 1 < len(args):
                out.append(_element(args[i + 1], ctx))
                i += 1
            i += 1
            continue
        if a.startswith("["):
            out.append(_element(a, ctx))
            i += 1
            continue
        patterns = _unbrace(a).split()
        if cmd == "get_ports":
            for p in patterns:
                rx = re.compile(fnmatch.translate(re.sub(r"\[[^\]]*\]$", "", p)))
                if not any(rx.fullmatch(port) for port in ctx.scope_ports):
                    ctx.errors.append(f"get_ports '{p}' matches no port of {ctx.scope}")
        elif cmd in _RENAMED:
            patterns = [
                "/".join(_segment(s, ctx) for s in p.split("/")) for p in patterns
            ]
        out.append("{" + " ".join(patterns) + "}")
        i += 1
    return "[" + " ".join([cmd, *out]) + "]"


def _span_end(text: str, start: int) -> int:
    """Index just past the ``]`` closing the span that opens at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise FatalRtlBuddyError(f"unbalanced brackets in constraint word {text!r}")


def _sub_spans(text: str, ctx: _Ctx) -> str:
    out: list[str] = []
    pos = 0
    for m in _SPAN.finditer(text):
        if m.start() < pos:
            continue
        end = _span_end(text, m.start())
        out.append(text[pos : m.start()])
        out.append(_span(text[m.start() : end], ctx))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _element(t: str, ctx: _Ctx) -> str:
    """One Tcl word: a query span, a literal, or text with queries concatenated in."""
    body = _unbrace(t)
    if not _SPAN.search(body):
        return t
    if _SPAN.match(body) and _span_end(body, 0) == len(body):
        return _span(body, ctx)
    if any(c in body for c in '"$\\'):
        ctx.errors.append(f"cannot re-quote the constraint word {t!r}")
        return t
    # Double quotes keep `[...]` evaluated and the surrounding text concatenated.
    return '"' + _sub_spans(body, ctx) + '"'


def _word(word: str, ctx: _Ctx) -> str:
    if not _SPAN.search(word):
        return word
    toks = [w for part in _tokenize(word) for w in part]
    if len(toks) == 1:
        return _element(toks[0], ctx)
    return "[list " + " ".join(_element(t, ctx) for t in toks) + "]"


def rewrite(
    sdc: Path, name_map: NameMap, scope: str, scope_ports: set[str]
) -> tuple[str, list[str]]:
    """Return the rewritten text of ``sdc`` and the problems found (the caller fails on any)."""
    if not sdc.is_file():
        raise FatalRtlBuddyError(f"constraint file not found: {sdc}")
    commands, backend = read_commands(sdc.read_text(), interest=None, source=str(sdc))
    if backend != TCL_BACKEND:
        raise FatalRtlBuddyError(
            f"{sdc.name}: rewriting constraints for a release needs the Tcl "
            "constraint reader (a Python with tkinter), not the tokenizer fallback: "
            "variables and loops must be evaluated to translate names"
        )
    ctx = _Ctx(name_map, scope, scope_ports)
    lines = [
        f"# Generated by rtl-buddy release from {sdc.name}: evaluated, expanded and",
        "# translated for the released RTL.",
    ]
    for c in commands:
        if c.name in _QUERIES:
            continue
        if c.name == "source":
            ctx.errors.append(
                f"line {c.line}: `source` is not supported; inline the sourced file"
            )
            continue
        lines.append(" ".join([c.name, *(_word(w, ctx) for w in c.words)]))
    return "\n".join(lines) + "\n", ctx.errors


_INTERNAL_QUERY = re.compile(r"\bget_(cells|pins|nets)\b")
_PORT_QUERY = re.compile(r"\bget_ports\s+(?:-\w+\s+)*(\{[^}]*\}|[^\s\]]+)")


def check_verbatim(sdc: Path, scope: str, scope_ports: set[str]) -> list[str]:
    """Problems that stop ``sdc`` shipping unchanged against a preserved ``scope``.

    For constraint scripts that query the design while they run (``get_property``,
    loops over ``all_inputs``) and so cannot be evaluated without it. Such a file
    may only name the scope's ports, whose spelling the release keeps: any
    ``get_cells`` / ``get_pins`` / ``get_nets`` is a problem, and every literal
    ``get_ports`` pattern must match a scope port. Patterns built from variables
    cannot be checked here.
    """
    if not sdc.is_file():
        raise FatalRtlBuddyError(f"constraint file not found: {sdc}")
    problems: list[str] = []
    for lineno, raw in enumerate(sdc.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0] if raw.lstrip().startswith("#") else raw
        if _INTERNAL_QUERY.search(line):
            problems.append(
                f"line {lineno}: names internal objects; use mode `rewrite`: {raw.strip()}"
            )
        for m in _PORT_QUERY.finditer(line):
            arg = m.group(1)
            if any(c in arg for c in "$["):
                continue
            for p in _unbrace(arg).split():
                rx = re.compile(fnmatch.translate(re.sub(r"\[[^\]]*\]$", "", p)))
                if not any(rx.fullmatch(port) for port in scope_ports):
                    problems.append(
                        f"line {lineno}: get_ports '{p}' matches no port of {scope}"
                    )
    return problems
