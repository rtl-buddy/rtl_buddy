"""Lexical helpers for SystemVerilog source text used by the release flow.

The release flow never elaborates the design: Verible's obfuscator is lexical, so
every check that guards it is lexical too. :func:`tokens` splits source text into
comments, string literals, identifiers and everything else; the other helpers are
built on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Literal

TokenKind = Literal["comment", "string", "ident", "other"]

_IDENT_START = re.compile(r"[A-Za-z_]")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# An escaped identifier runs from the backslash to the next whitespace.
_ESCAPED_IDENT = re.compile(r"\\\S+")


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    line: int
    #: True for an identifier spelled after a backtick: a macro use or a directive.
    directive: bool = False


def tokens(text: str) -> Iterator[Token]:
    """Yield the tokens of ``text`` in order; concatenating their ``text`` gives ``text`` back."""
    i = 0
    n = len(text)
    line = 1
    other_start = 0

    def flush_other(end: int):
        nonlocal other_start
        if end > other_start:
            chunk = text[other_start:end]
            yield Token("other", chunk, line - chunk.count("\n"))
        other_start = end

    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            yield from flush_other(i)
            if text[i + 1] == "/":
                end = text.find("\n", i)
                end = n if end < 0 else end
            else:
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
            chunk = text[i:end]
            yield Token("comment", chunk, line)
            line += chunk.count("\n")
            i = end
            other_start = i
            continue
        if c == '"' and not (i > 0 and text[i - 1] == "`"):
            # A backtick-quote is a macro-body quote, not a string literal.
            yield from flush_other(i)
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n":
                    # An unterminated literal stops at the line end.
                    break
                j += 1
            end = min(j + 1, n)
            chunk = text[i:end]
            yield Token("string", chunk, line)
            line += chunk.count("\n")
            i = end
            other_start = i
            continue
        if c == "\\":
            m = _ESCAPED_IDENT.match(text, i)
            if m:
                yield from flush_other(i)
                yield Token("ident", m.group(0), line)
                i = m.end()
                other_start = i
                continue
        if _IDENT_START.match(c) and (i == 0 or not _is_ident_char(text[i - 1])):
            m = _IDENT.match(text, i)
            assert m is not None
            directive = i > 0 and text[i - 1] == "`"
            yield from flush_other(i)
            yield Token("ident", m.group(0), line, directive)
            i = m.end()
            other_start = i
            continue
        if c == "\n":
            line += 1
        i += 1
    yield from flush_other(n)


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c in "_$'"


def identifiers(text: str) -> set[str]:
    """Return every plain identifier in ``text``, outside comments and string literals.

    Escaped identifiers are left out: Verible's obfuscator does not rename them.
    """
    return {
        t.text
        for t in tokens(text)
        if t.kind == "ident" and not t.text.startswith("\\")
    }


def strip_comments(text: str, keep: re.Pattern[str] | None = None) -> str:
    """Remove comments from ``text`` and keep its line numbering.

    A comment matching ``keep`` (tool directives such as ``// synopsys translate_off``)
    is kept whole. A removed block comment leaves its newlines behind, and trailing
    whitespace left on a line by a removed comment is trimmed.
    """
    out: list[str] = []
    for t in tokens(text):
        if t.kind == "comment" and not (keep is not None and keep.search(t.text)):
            out.append("\n" * t.text.count("\n"))
        else:
            out.append(t.text)
    return "\n".join(line.rstrip() for line in "".join(out).split("\n"))


#: A double backtick with a name character on both sides joins two pieces into one identifier. ```O``.f`` only delimits an argument and renames consistently.
_PASTE_JOIN = re.compile(r"\w``\w")


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    detail: str


def lexical_hazards(text: str) -> list[Finding]:
    """Constructs Verible's lexical obfuscator renames inconsistently.

    - ``token-paste``: a macro body joining two name pieces with a double
      backtick (``a``_nxt``). The pieces are renamed separately, so the pasted
      name no longer matches the declaration it was meant to spell. A double
      backtick that only delimits an argument (```O``.field``) is harmless.
    - ``macro-string``: a backtick-quote in a macro body. The quoted argument is
      renamed, but the resulting string is compared or printed as text.
    """
    masked = "".join(
        t.text if t.kind in ("ident", "other") else re.sub(r"[^\n]", " ", t.text)
        for t in tokens(text)
    )
    found: list[Finding] = []
    for lineno, line_text in enumerate(masked.split("\n"), 1):
        if _PASTE_JOIN.search(line_text):
            found.append(Finding(lineno, "token-paste", line_text.strip()))
        elif '`"' in line_text:
            found.append(Finding(lineno, "macro-string", line_text.strip()))
    return found


_DEFINE_HEAD = re.compile(r"`define\s+\w+\(([^)]*)\)")
_PASTE_RUN = re.compile(r"(?:\w+|``)+")


def paste_patterns(text: str) -> tuple[list[re.Pattern[str]], set[str]]:
    """The names token pasting in ``text`` can form, for preserving them.

    Each pasted run (``reg``N``_ctl``) becomes a pattern in which a piece
    naming a parameter of the enclosing macro matches any word characters and
    every other piece is literal (``reg(\\w*)_ctl``). Returns the patterns and
    the literal pieces, both of which must keep their spelling for the pasted
    names to keep matching their declarations. A parameter piece is a capture
    group, so the caller can also keep an identifier passed as that argument.
    """
    masked = "".join(
        t.text if t.kind in ("ident", "other") else re.sub(r"[^\n]", " ", t.text)
        for t in tokens(text)
    )
    patterns: list[re.Pattern[str]] = []
    literals: set[str] = set()
    params: set[str] = set()
    continuing = False
    for line in masked.split("\n"):
        head = _DEFINE_HEAD.search(line)
        if head:
            params = {
                p.split("=")[0].strip() for p in head.group(1).split(",") if p.strip()
            }
        elif not continuing:
            params = set()
        continuing = line.rstrip().endswith("\\")
        for run in _PASTE_RUN.findall(line):
            if not _PASTE_JOIN.search(run):
                continue
            parts = []
            for piece in run.split("``"):
                if piece in params:
                    parts.append(r"(\w*)")
                elif piece:
                    literals.add(piece)
                    parts.append(re.escape(piece))
            patterns.append(re.compile("".join(parts)))
    return patterns, literals


_DECL_KEYWORDS = {
    "module",
    "macromodule",
    "package",
    "interface",
    "program",
    "class",
    "checker",
    "primitive",
}


def declared_units(text: str) -> set[str]:
    """Return names declared by ``module`` / ``package`` / ``interface`` / ``program`` / ``class`` / ``checker`` / ``primitive``.

    Lexical: the identifier after the keyword, skipping lifetime qualifiers and,
    for ``interface class`` / ``virtual class``, the second keyword.
    """
    names: set[str] = set()
    idents = [t for t in tokens(text) if t.kind == "ident"]
    skip = {"automatic", "static", "class", "virtual"}
    for idx, t in enumerate(idents):
        if t.directive or t.text not in _DECL_KEYWORDS:
            continue
        j = idx + 1
        while j < len(idents) and idents[j].text in skip:
            j += 1
        if j < len(idents) and not idents[j].directive:
            names.add(idents[j].text)
    return names


_INCLUDE = re.compile(r'`include\s+"([^"]+)"')


def include_targets(text: str) -> list[str]:
    """Return the file names of the ``\\`include "..."`` directives in ``text``, outside comments."""
    code = "".join(t.text for t in tokens(text) if t.kind != "comment")
    return _INCLUDE.findall(code)


def header_names(text: str) -> set[str]:
    """Module names, the identifiers of each module header, and ``parameter`` names.

    A lexical stand-in for the obfuscator's ``--preserve_interface`` on sources
    it cannot parse, such as vendor models. A header runs from ``module`` to the
    first ``;`` outside parentheses, so it covers ANSI and non-ANSI port lists.
    """
    names: set[str] = set()
    toks = [t for t in tokens(text) if t.kind in ("ident", "other")]
    in_header = False
    depth = 0
    after_param = False
    for i, t in enumerate(toks):
        if t.kind == "ident":
            if t.directive:
                continue
            if t.text in ("module", "macromodule"):
                in_header, depth = True, 0
                continue
            if in_header or after_param:
                names.add(t.text)
                after_param = False
            if t.text == "parameter":
                after_param = True
            continue
        for c in t.text:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == ";" and depth == 0:
                in_header = False
                after_param = False
    return names
