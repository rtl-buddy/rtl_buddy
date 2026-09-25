"""Read SDC/XDC text as a list of Tcl commands (rtl-buddy/rtl_buddy#642).

Every constraint consumer in this repo goes through :func:`read_commands`
rather than matching a regex per physical line. The regexes that used to
live in ``synth_yosys`` and ``cdc_xdc_audit`` mis-read anything that is
legal SDC but not one-line-plain: a ``\\``-continued command, a braced
value (``-period {10.0}``), a nested collection
(``[get_pins [get_cells u_a]/C]``), or a ``#`` inside braces.

The word splitting itself is done by the vendored tokenizer
(:mod:`rtl_buddy.constraints.tcl_tokenizer`, a verbatim copy of
rtl-buddy-cdc's). This module adds only what the vendored functions do
not carry: logical-command boundaries with their source line number and
the reconstructed command text, plus a single "this file uses Tcl we do
not evaluate" warning per file.

:func:`read_commands` returns the backend name alongside the commands so
rtl-buddy/rtl_buddy#641 can put a real ``tkinter.Tcl()`` safe interp
behind the same call and have callers report which one answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..logging_utils import log_event
from .tcl_tokenizer import _extract_names, _tokenize

logger = logging.getLogger(__name__)

__all__ = [
    "TclCommand",
    "read_commands",
    "backend_name",
    "extract_names",
    "TOKENIZER_BACKEND",
]

#: The only backend today. #641 adds ``"tcl"`` (a ``tkinter.Tcl()`` safe
#: interp) and picks between them inside :func:`read_commands`.
TOKENIZER_BACKEND = "tokenizer"

#: Tcl the tokenizer deliberately does not evaluate. Seeing any of these
#: means the file's real meaning may differ from what we read, so the
#: caller gets one warning naming them (see issue #144's "Rejected
#: alternative" section upstream: the fix is an interp, not more regex).
OUT_OF_SCOPE_FEATURES = ("$var", "expr", "proc", "command substitution", "source")

#: Command names that only make sense under an interpreter. A constraint
#: file that uses them is scripting, not declaring, and the words we hand
#: back for the surrounding commands may be wrong. Commands we simply do
#: not care about (``set_property``, ``create_pblock``, ...) are NOT in
#: here — they are out of scope by design, not unreadable, and warning on
#: them would fire on every real XDC.
_SCRIPTING_COMMANDS = frozenset(
    {
        "set",
        "expr",
        "proc",
        "source",
        "eval",
        "subst",
        "if",
        "else",
        "elseif",
        "foreach",
        "for",
        "while",
        "switch",
        "uplevel",
        "upvar",
        "namespace",
        "rename",
        "unset",
    }
)


@dataclass
class TclCommand:
    """One logical Tcl command read out of a constraint file."""

    name: str
    #: Literal words after the command name. Braces/quotes are stripped by
    #: the tokenizer's own rules; bracket spans are kept whole and opaque.
    words: list[str] = field(default_factory=list)
    #: 1-based line the command starts on, or ``None`` when unknown.
    line: int | None = None
    #: The logical command text with continuations folded out, or ``None``.
    raw: str | None = None


def backend_name() -> str:
    """Name of the backend :func:`read_commands` uses (for ``rb tool-check``)."""
    return TOKENIZER_BACKEND


def extract_names(word: str) -> list[str]:
    """Names inside one tokenized word, with collection heads peeled off.

    Thin wrapper over the vendored ``_extract_names`` that additionally
    drops nested ``get_*`` heads (upstream only peels a *leading*
    ``get_ports`` / ``get_pins`` / ``get_clocks``, so ``get_cells`` and
    anything nested survives) and the ``/pin`` remainder a nested
    collection leaves behind, e.g.::

        [get_pins [get_cells u_a]/C]  ->  ["u_a"]

    A trailing ``/*`` cell wildcard is trimmed to the instance token, as
    the XDC ``get_cells`` convention intends.
    """
    if not word:
        return []
    names, _saw_filter = _extract_names(word)
    out: list[str] = []
    for tok in names:
        if tok in {"get_clocks", "get_cells", "get_ports", "get_pins", "get_nets"}:
            # A nested collection head, e.g. `[get_pins [get_cells u]/C]`.
            continue
        if tok.startswith("/"):
            # The `/C` left over once a nested `[get_cells u]` is peeled.
            continue
        if tok.endswith("/*"):
            tok = tok.rsplit("/", 1)[0]
        if tok:
            out.append(tok)
    return out


def _logical_spans(text: str) -> list[tuple[int, str]]:
    """Return ``(line, span_text)`` for each logical command in ``text``.

    Mirrors only the *boundary* rules of the vendored ``_tokenize``
    (continuation, newline, word-boundary ``#``, and the brace / bracket /
    quote spans that swallow both) — the word splitting itself is left to
    the vendored function, which each span is handed to in turn. Keeping
    the two in lockstep is what lets the vendored file stay verbatim.
    """
    spans: list[tuple[int, str]] = []
    i = 0
    n = len(text)
    line = 1
    start: int | None = None
    start_line = 1
    # Mirrors the tokenizer's "is a bare word in progress" state, which is
    # what decides whether `#`, `{`, `[` and `"` are special or literal.
    in_word = False

    def flush(end: int) -> None:
        nonlocal start
        if start is not None and text[start:end].strip():
            spans.append((start_line, text[start:end]))
        start = None

    while i < n:
        c = text[i]

        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            in_word = False
            i += 2
            line += 1
            continue

        if c == "\n":
            flush(i)
            in_word = False
            i += 1
            line += 1
            continue

        if c in " \t\r":
            in_word = False
            i += 1
            continue

        if c == "#" and not in_word:
            flush(i)
            while i < n and text[i] != "\n":
                i += 1
            continue

        if start is None:
            start = i
            start_line = line

        if c in "{[" and not in_word:
            closer = "}" if c == "{" else "]"
            depth = 1
            i += 1
            while i < n and depth > 0:
                ch = text[i]
                if ch == c:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                elif ch == "\n":
                    line += 1
                i += 1
            continue

        if c == '"' and not in_word:
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    if text[i + 1] == "\n":
                        line += 1
                    i += 2
                else:
                    if text[i] == "\n":
                        line += 1
                    i += 1
            if i < n:
                i += 1
            continue

        in_word = True
        i += 1

    flush(n)
    return spans


def _reconstruct(span: str) -> str:
    """The logical command as one line: continuations folded, runs squeezed."""
    return " ".join(span.replace("\\\n", " ").split())


def read_commands(
    text: str,
    *,
    interest: frozenset[str],
    source: str | None = None,
) -> tuple[list[TclCommand], str]:
    """Return ``(commands, backend)`` for the commands in ``interest``.

    ``backend`` is ``"tokenizer"`` for now; rtl-buddy/rtl_buddy#641 adds a
    ``"tcl"`` interp behind this same signature.

    At most one ``constraints.tokenizer_skipped`` warning is logged per
    call, the first time the file shows Tcl this backend does not evaluate
    (a ``$`` variable reference, or a scripting command such as ``set`` /
    ``expr`` / ``proc`` / ``source``). The whole file is still read — the
    warning says the reading may be incomplete, it does not stop it.
    """
    commands: list[TclCommand] = []
    warned = False

    for line_no, span in _logical_spans(text):
        tokenized = _tokenize(span)
        if not tokenized:
            continue
        words = [w for group in tokenized for w in group]
        if not words:
            continue
        name = words[0]

        if not warned:
            trigger = None
            if name in _SCRIPTING_COMMANDS:
                trigger = name
            else:
                trigger = next((w for w in words if "$" in w), None)
            if trigger is not None:
                warned = True
                log_event(
                    logger,
                    logging.WARNING,
                    "constraints.tokenizer_skipped",
                    source=source,
                    line=line_no,
                    command=name,
                    trigger=trigger,
                    features=list(OUT_OF_SCOPE_FEATURES),
                )

        if name not in interest:
            continue
        commands.append(
            TclCommand(
                name=name,
                words=words[1:],
                line=line_no,
                raw=_reconstruct(span),
            )
        )

    return commands, TOKENIZER_BACKEND
