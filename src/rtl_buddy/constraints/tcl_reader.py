"""Read SDC/XDC text as a list of Tcl commands (rtl-buddy/rtl_buddy#642, #641).

Every constraint consumer in this repo goes through :func:`read_commands`
rather than matching a regex per physical line. The regexes that used to
live in ``synth_yosys`` and ``cdc_xdc_audit`` mis-read anything that is
legal SDC but not one-line-plain: a ``\\``-continued command, a braced
value (``-period {10.0}``), a nested collection
(``[get_pins [get_cells u_a]/C]``), or a ``#`` inside braces.

Two backends answer that call, in this order (#641):

``tcl``
    A real Tcl interpreter — ``tkinter.Tcl()`` (no display needed, never
    ``Tk()``) driving an ``interp create -safe`` child whose ``unknown``
    handler is aliased back into Python. Every command the file runs is
    therefore *evaluated*: ``$var``, ``[expr …]``, ``\\`` continuations,
    braces, nested brackets and ``;`` separators all come out right,
    because Tcl itself did the parsing. Nothing dangerous is reachable —
    a safe interp has no ``exec`` / ``open`` / ``file`` / ``socket`` /
    ``source`` / ``cd`` / ``glob``, so those land in the recorder like
    any other unknown command, and ``interp limit`` bounds a file that
    tries to loop forever.

``tokenizer``
    The fallback for a Python built without ``_tkinter``: the vendored
    word tokenizer (:mod:`rtl_buddy.constraints.tcl_tokenizer`, a
    verbatim copy of rtl-buddy-cdc's). It splits words correctly but
    evaluates nothing, so ``$p`` / ``[expr …]`` stay literal and the file
    gets one ``constraints.tokenizer_skipped`` warning saying so.

``RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl`` forces one of them (the
test suite parametrises over both this way; a user can pin the fallback
if an interp ever misreads a file). Both backends are held to the same
observable contract — same command names, same line numbers, and
:func:`extract_names` over their words yields the same names — which
``tests/test_tcl_reader.py`` pins as a cross-backend equivalence test.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .tcl_tokenizer import _extract_names, _tokenize

logger = logging.getLogger(__name__)

__all__ = [
    "TclCommand",
    "read_commands",
    "backend_name",
    "backend_description",
    "extract_names",
    "tkinter_available",
    "BACKEND_ENV",
    "TCL_BACKEND",
    "TOKENIZER_BACKEND",
]

#: Backend names. ``tcl`` is the safe-interp reader (#641), ``tokenizer``
#: the stdlib-only fallback (#642).
TCL_BACKEND = "tcl"
TOKENIZER_BACKEND = "tokenizer"

#: Environment override: ``tokenizer`` forces the fallback even where an
#: interp is available, ``tcl`` refuses to run without one.
BACKEND_ENV = "RTL_BUDDY_CONSTRAINT_READER"

#: What to do about a Python without ``_tkinter``. Named in the one-time
#: ``constraints.tcl_unavailable`` warning, because "install tkinter" is
#: not actionable on its own.
TKINTER_HINTS = (
    "uv-managed Python bundles Tcl/Tk (`uv python install --managed-python`)",
    "Homebrew: `brew install python-tk@<X.Y>` for the running interpreter",
    "RHEL/Rocky/Alma/Fedora: `dnf install python3-tkinter`",
)

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
    #: Words after the command name — evaluated by the ``tcl`` backend,
    #: literal under ``tokenizer``. Either way a brace group or a bracket
    #: span is one word, and :func:`extract_names` peels both the same.
    words: list[str] = field(default_factory=list)
    #: 1-based line the command starts on, or ``None`` when unknown.
    line: int | None = None
    #: The logical command as a single line: continuations folded out
    #: (``tokenizer``) or reconstructed from the evaluated words
    #: (``tcl``). ``None`` when unknown.
    raw: str | None = None


# ---------------------------------------------------------------------------
# backend selection


_UNSET = object()
#: ``None`` once ``_tkinter`` has imported; the ImportError text otherwise.
#: Probed once per process — the answer cannot change under us, and the
#: import is the expensive half.
_TKINTER_ERROR: object | str | None = _UNSET
_TKINTER_LOCK = threading.Lock()
#: ``constraints.tcl_unavailable`` is a property of the interpreter, not
#: of the file being read, so it is logged once per process.
_UNAVAILABLE_LOGGED = False


def _tkinter_error() -> str | None:
    """``None`` when ``_tkinter`` imports, else the ImportError message."""
    global _TKINTER_ERROR
    if _TKINTER_ERROR is _UNSET:
        with _TKINTER_LOCK:
            if _TKINTER_ERROR is _UNSET:
                try:
                    import _tkinter  # noqa: F401
                except ImportError as exc:  # pragma: no cover - env-dependent
                    _TKINTER_ERROR = str(exc)
                else:
                    _TKINTER_ERROR = None
    return _TKINTER_ERROR  # type: ignore[return-value]


def tkinter_available() -> bool:
    """True when this interpreter can run the ``tcl`` backend."""
    return _tkinter_error() is None


def _log_tcl_unavailable(error: str) -> None:
    global _UNAVAILABLE_LOGGED
    if _UNAVAILABLE_LOGGED:
        return
    _UNAVAILABLE_LOGGED = True
    log_event(
        logger,
        logging.WARNING,
        "constraints.tcl_unavailable",
        error=error,
        hints=list(TKINTER_HINTS),
    )


def _env_override() -> str | None:
    raw = os.environ.get(BACKEND_ENV)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value not in (TCL_BACKEND, TOKENIZER_BACKEND):
        raise FatalRtlBuddyError(
            f"{BACKEND_ENV}={raw!r} is not a constraint reader backend; "
            f"expected {TCL_BACKEND!r} or {TOKENIZER_BACKEND!r}"
        )
    return value


def select_backend() -> str:
    """Which backend :func:`read_commands` will try first.

    ``RTL_BUDDY_CONSTRAINT_READER`` wins when set. Asking for ``tcl`` on a
    Python without ``_tkinter`` is fatal rather than a silent downgrade:
    the override exists to pin a backend, so quietly answering with the
    other one would defeat it.
    """
    override = _env_override()
    error = _tkinter_error()
    if override == TCL_BACKEND:
        if error is not None:
            raise FatalRtlBuddyError(
                f"{BACKEND_ENV}={TCL_BACKEND} but this Python cannot import "
                f"_tkinter ({error}). " + "; ".join(TKINTER_HINTS)
            )
        return TCL_BACKEND
    if override == TOKENIZER_BACKEND:
        return TOKENIZER_BACKEND
    if error is None:
        return TCL_BACKEND
    _log_tcl_unavailable(error)
    return TOKENIZER_BACKEND


def backend_name() -> str:
    """Name of the backend :func:`read_commands` uses (for ``rb tool-check``)."""
    return select_backend()


def tcl_patchlevel() -> str | None:
    """``info patchlevel`` of the Tcl library, or ``None`` without one."""
    if not tkinter_available():
        return None
    try:
        return str(_parent_interp().call("info", "patchlevel"))
    except Exception:  # pragma: no cover - defensive
        return None


def backend_description() -> str:
    """Backend name plus the Tcl version behind it, for ``rb tool-check``."""
    name = backend_name()
    if name != TCL_BACKEND:
        return name
    patchlevel = tcl_patchlevel()
    return f"{name} (Tcl {patchlevel})" if patchlevel else name


# ---------------------------------------------------------------------------
# name extraction (shared by both backends)


def extract_names(word: str) -> list[str]:
    """Names inside one word, with collection heads peeled off.

    Thin wrapper over the vendored ``_extract_names`` that additionally
    drops nested ``get_*`` heads (upstream only peels a *leading*
    ``get_ports`` / ``get_pins`` / ``get_clocks``, so ``get_cells`` and
    anything nested survives) and the ``/pin`` remainder a nested
    collection leaves behind, e.g.::

        [get_pins [get_cells u_a]/C]  ->  ["u_a"]

    A trailing ``/*`` cell wildcard is trimmed to the instance token, as
    the XDC ``get_cells`` convention intends.

    The ``tcl`` backend hands this the same shapes on purpose: its
    recorder rebuilds a collection's result as ``[get_cells u_a]`` rather
    than the bare ``u_a``, so a target reads identically whichever
    backend produced it (and ``get_clocks`` stays visible to callers that
    ask "is this a clock or a cell target?").
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


# ---------------------------------------------------------------------------
# the tcl backend: a recording safe interp


#: Wall-clock budget for evaluating one constraint file. A safe interp
#: happily runs `while 1 {}` forever; `interp limit` is what turns that
#: into an error instead of a hang.
TCL_TIME_LIMIT_SECONDS = 5
#: Second guard, for a file that spins fast rather than long. Generous:
#: the recorder's own `unknown` body costs a handful of commands per
#: constraint, so a 20k-line SDC already spends a few hundred thousand.
TCL_COMMAND_LIMIT = 5_000_000

#: Commands a safe interp keeps that can block, write, or error on a
#: channel this process never shares. Hidden so they reach the recorder
#: like every other command a constraint file has no business calling.
_HIDE_COMMANDS = ("after", "vwait", "update", "puts", "exit")

#: The recorder. `unknown` fires for every command a safe interp does not
#: have, which is every SDC/XDC command plus `exec` / `open` / `file` /
#: `source` / `cd` / `glob` (all absent from a safe interp). `info frame
#: -1` is the caller's frame, so `line` is where the command starts.
_UNKNOWN_PROC = r"""
proc unknown args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    return [__rb_record $__rb_line {*}$args]
}
"""

_CHILD_SEQ = itertools.count()
#: One parent interp per thread: a Tcl interpreter belongs to the thread
#: that created it, and rtl_buddy reads constraints from worker threads.
_LOCAL = threading.local()


def _parent_interp():
    """The per-thread ``tkinter.Tcl()`` app object (never ``Tk()``)."""
    app = getattr(_LOCAL, "app", None)
    if app is None:
        import tkinter

        # Keep the Tk object alive alongside the interpreter handle it
        # owns; dropping it would finalize the interpreter under us.
        root = tkinter.Tcl()
        _LOCAL.root = root
        app = _LOCAL.app = root.tk
    return app


def _quote_word(word: str) -> str:
    """Re-brace an evaluated word so it stays one word, as the tokenizer sees it.

    Tcl strips the braces off ``{a b}`` before the command sees it, but
    the tokenizer backend keeps them; brace-quoting anything with
    whitespace is what keeps ``-group {a b}`` a single word on both
    paths. A word this recorder built itself (``[get_ports clk]``) is
    already one word and is left alone.
    """
    if word == "":
        return "{}"
    if not any(ch.isspace() for ch in word):
        return word
    if word.startswith("[") and word.endswith("]"):
        return word
    if word.startswith("{") and word.endswith("}"):
        return word
    return "{" + word + "}"


def _read_with_tcl(
    text: str,
    *,
    interest: frozenset[str],
    source: str | None,
) -> list[TclCommand] | None:
    """Evaluate ``text`` in a recording safe interp.

    Returns the commands in ``interest``, or ``None`` when the interp
    could not read the file (syntax error, unset variable, resource limit)
    — the caller then falls back to the tokenizer, which reads text that
    Tcl refuses to run.
    """
    app = _parent_interp()
    child = f"rb_sdc_{next(_CHILD_SEQ)}"
    record_cmd = f"__rb_record_{child}"
    commands: list[TclCommand] = []
    include_warned = False

    def record(line: str, *words: str) -> str:
        nonlocal include_warned
        if not words:
            return ""
        name = words[0]
        rest = [_quote_word(w) for w in words[1:]]
        line_no: int | None
        try:
            line_no = int(line)
        except (TypeError, ValueError):
            line_no = None
        if name == "source" and not include_warned:
            include_warned = True
            log_event(
                logger,
                logging.WARNING,
                "constraints.include_unsupported",
                source=source,
                line=line_no,
                included=rest[-1] if rest else "",
            )
        if name in interest:
            commands.append(
                TclCommand(
                    name=name,
                    words=rest,
                    line=line_no,
                    # one line, like the tokenizer's reconstruction: a
                    # brace group written across lines reads back folded.
                    raw=" ".join(" ".join([name, *rest]).split()),
                )
            )
        # Collections collapse to their own source form, so a nested
        # `[get_pins [get_cells u_a]/C]` reads the same as it does under
        # the tokenizer. Everything else is opaque by the same rule.
        return "[" + " ".join([name, *rest]) + "]"

    # A fresh child per call: `set` state, procs and namespaces from one
    # constraint file must not be visible to the next.
    app.call("interp", "create", "-safe", child)
    app.createcommand(record_cmd, record)
    try:
        for hidden in _HIDE_COMMANDS:
            try:
                app.call("interp", "hide", child, hidden)
            except Exception:
                pass  # already absent from a safe interp
        app.call("interp", "alias", child, "__rb_record", "", record_cmd)
        app.call(child, "eval", _UNKNOWN_PROC)
        app.call(
            "interp",
            "limit",
            child,
            "command",
            "-value",
            TCL_COMMAND_LIMIT,
            "-granularity",
            1,
        )
        app.call(
            "interp",
            "limit",
            child,
            "time",
            "-seconds",
            int(time.time()) + TCL_TIME_LIMIT_SECONDS,
            "-granularity",
            1,
        )
        app.setvar("__rb_script", text)
        # Catch in the *parent*: a limit error cannot be caught inside the
        # interp it fired on, and the parent's options dict carries the
        # child's `-errorline`.
        failed = int(
            app.eval(
                f"catch {{interp eval {child} $::__rb_script}} ::__rb_msg ::__rb_opts"
            )
        )
        if failed:
            message = str(app.getvar("__rb_msg"))
            line = app.eval(
                "if {[dict exists $::__rb_opts -errorline]} "
                "{dict get $::__rb_opts -errorline} else {return {}}"
            )
            log_event(
                logger,
                logging.WARNING,
                "constraints.tcl_error",
                source=source,
                line=int(line) if str(line).isdigit() else None,
                message=message,
            )
            return None
    finally:
        for cleanup in (
            lambda: app.call("interp", "delete", child),
            lambda: app.deletecommand(record_cmd),
            lambda: app.call("unset", "-nocomplain", "::__rb_script"),
        ):
            try:
                cleanup()
            except Exception:  # pragma: no cover - defensive
                pass
    return commands


# ---------------------------------------------------------------------------
# the tokenizer backend


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


def _read_with_tokenizer(
    text: str,
    *,
    interest: frozenset[str],
    source: str | None,
) -> list[TclCommand]:
    """Split ``text`` into words without evaluating any of it.

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

    return commands


# ---------------------------------------------------------------------------
# the one entry point


def read_commands(
    text: str,
    *,
    interest: frozenset[str],
    source: str | None = None,
) -> tuple[list[TclCommand], str]:
    """Return ``(commands, backend)`` for the commands in ``interest``.

    ``backend`` is the one that actually answered — ``"tcl"`` when the
    safe interp read the file, ``"tokenizer"`` when that backend is not
    selected, not available, or refused the file (a Tcl syntax error, an
    unset variable, a resource limit). Falling back rather than returning
    nothing matters: a constraint file Tcl will not run is still a file
    whose ``create_clock`` lines ABC needs.
    """
    if select_backend() == TCL_BACKEND:
        try:
            commands = _read_with_tcl(text, interest=interest, source=source)
        except Exception as exc:
            # The interp itself misbehaved (a Tk build that will not start,
            # a thread that cannot hold it, ...). That is not a reason to
            # hand the caller no constraints at all.
            log_event(
                logger,
                logging.WARNING,
                "constraints.tcl_error",
                source=source,
                line=None,
                message=f"{type(exc).__name__}: {exc}",
            )
            commands = None
        if commands is not None:
            return commands, TCL_BACKEND
    return (
        _read_with_tokenizer(text, interest=interest, source=source),
        TOKENIZER_BACKEND,
    )
