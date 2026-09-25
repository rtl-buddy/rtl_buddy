"""Tcl-aware SDC/XDC word tokenizer — VENDORED COPY, DO NOT EDIT HERE.

Source repo:   rtl-buddy/rtl-buddy-cdc
Source file:   src/rtl_buddy_cdc/sdc.py
Pinned commit: 53b5f34161debccda45c44a33c9b663ee5410b1a  (main, 2026-09-22)

``_tokenize`` and ``_extract_names`` below are a **verbatim copy** of the
functions of the same name in that file at that commit. Do not edit them
here: fix the upstream file, re-copy, and bump the hash in this header
and in ``scripts/check-vendored-tokenizer.sh`` (a non-blocking CI job
diffs the two function bodies against upstream and fails on drift).

Stdlib-only on purpose — the copy must stay droppable into either repo.

Why a copy and not a dependency: ``rtl_buddy`` does not depend on
``rtl_buddy_cdc`` (the CDC engine is an optional external tool, invoked
as a subprocess), and the tokenizer is ~120 lines of pure text handling.
rtl-buddy/rtl-buddy-cdc#298 splits it out upstream into its own
``tcl_tokenizer.py``, after which this copy re-points at that file.
"""

from __future__ import annotations

__all__ = ["_tokenize", "_extract_names"]


def _tokenize(text: str) -> list[list[str]]:
    """Tokenize SDC source into a list of word lists, one per command line.

    Handles the Tcl-flavoured syntax that SDC actually uses:

    - Whitespace splits words at the top level.
    - ``{...}`` braces — single word, nested braces respected, content
      kept literal (we don't substitute inside).
    - ``[...]`` brackets — single word, nested brackets respected,
      content kept literal (we treat the bracket span as opaque; the
      handler peels ``get_ports`` / ``get_pins`` / ``get_clocks``).
    - ``"..."`` double-quotes — single word; the quotes are stripped
      and ``\\<c>`` escapes the next character inside.
    - ``\\<newline>`` line continuation collapses to a single space.
    - ``#`` at a word boundary starts a comment to end-of-line; the
      partial command (if any) is flushed, matching the existing
      "comments break continuation" behaviour.
    - ``\\n`` ends a logical command.

    Out-of-scope on purpose (see issue #144's "Rejected alternative"
    section): variable expansion (``$x``), ``expr``, ``proc``, command
    substitution evaluation, ``source`` includes. If any of these
    become real requirements, switch to ``tkinter.Tcl()`` rather than
    grow them onto this tokenizer.
    """
    lines: list[list[str]] = []
    current: list[str] = []
    word: list[str] = []
    i = 0
    n = len(text)

    def flush_word() -> None:
        if word:
            current.append("".join(word))
            word.clear()

    def flush_line() -> None:
        flush_word()
        if current:
            lines.append(current.copy())
            current.clear()

    while i < n:
        c = text[i]

        # Line continuation: backslash-newline collapses to whitespace.
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            flush_word()
            i += 2
            continue

        # Newline ends the logical command.
        if c == "\n":
            flush_line()
            i += 1
            continue

        # Top-level whitespace splits words.
        if c in " \t\r":
            flush_word()
            i += 1
            continue

        # Comment at word boundary: skip to end-of-line. A comment that
        # appears between continued lines breaks the continuation,
        # matching the previous line-based behaviour.
        if c == "#" and not word:
            flush_line()
            while i < n and text[i] != "\n":
                i += 1
            continue

        # Brace word — nested braces respected, content kept literal.
        if c == "{" and not word:
            depth = 1
            start = i
            i += 1
            while i < n and depth > 0:
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            current.append(text[start:i])
            continue

        # Bracket word — nested brackets respected, content kept literal.
        if c == "[" and not word:
            depth = 1
            start = i
            i += 1
            while i < n and depth > 0:
                ch = text[i]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                i += 1
            current.append(text[start:i])
            continue

        # Double-quoted word — strip quotes; backslash escapes the next char.
        if c == '"' and not word:
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            current.append("".join(buf))
            if i < n:
                i += 1  # closing quote
            continue

        word.append(c)
        i += 1

    flush_line()
    return lines


def _extract_names(word: str) -> tuple[list[str], bool]:
    """Peel a single tokenized word into a list of identifier names.

    Accepts the three shapes a port/pin/clock argument can take:

    - ``{ck0 ck1}`` — brace collection, names are whitespace-separated.
    - ``[get_ports ck0 ck1]`` — bracket form, optional ``get_*`` head
      stripped, ``-filter`` clauses dropped (returns ``saw_filter=True``
      so the caller can surface a partial-parse warning).
    - ``ck0`` — bare identifier.

    Returns ``(names, saw_filter)``.
    """
    saw_filter = "-filter" in word
    cleaned = word
    for chunk in ("[", "]", "{", "}"):
        cleaned = cleaned.replace(chunk, " ")
    parts = cleaned.split()
    if parts and parts[0] in {"get_ports", "get_pins", "get_clocks"}:
        parts = parts[1:]
    if saw_filter:
        # Filter expressions can contain anything; drop everything
        # from -filter onwards. Names that appeared before -filter are
        # still valid, but in practice nothing precedes it and the
        # list ends up empty.
        try:
            cut = parts.index("-filter")
            parts = parts[:cut]
        except ValueError:
            pass
    cleaned_names: list[str] = []
    for tok in parts:
        if tok == "-include_generated_clocks":
            continue
        if tok.startswith("-"):
            # Conservative: skip vendor flags inside a get_* expression.
            continue
        cleaned_names.append(tok)
    return cleaned_names, saw_filter
