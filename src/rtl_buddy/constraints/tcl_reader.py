"""Read SDC/XDC text as a list of Tcl commands (rtl-buddy/rtl_buddy#642, #641).

Every constraint consumer in this repo goes through :func:`read_commands`
rather than matching a regex per physical line. The regexes that used to
live in ``synth_yosys`` and ``cdc_xdc_audit`` mis-read anything that is
legal SDC but not one-line-plain: a ``\\``-continued command, a braced
value (``-period {10.0}``), a nested collection
(``[get_pins [get_cells u_a]/C]``), or a ``#`` inside braces.

Two backends answer that call, in this order (#641):

``tcl``
    A real Tcl interpreter — an ``interp create -safe`` child whose
    ``unknown`` handler is aliased back into Python. Every command the
    file runs is therefore *evaluated*: ``$var``, ``[expr …]``, ``\\``
    continuations, braces, nested brackets and ``;`` separators all come
    out right, because Tcl itself did the parsing. Nothing dangerous is
    reachable — a safe interp has no ``exec`` / ``open`` / ``file`` /
    ``socket`` / ``source`` / ``cd`` / ``glob``, so those land in the
    recorder like any other unknown command, and ``interp limit`` bounds
    a file that tries to loop forever.

    The interp runs in a **short-lived worker process**
    (:mod:`rtl_buddy.constraints.tcl_worker`, one per
    :func:`read_commands` call, 50-90 ms), never in this one. Loading
    ``_tkinter`` starts a Tcl notifier thread that never exits, and on
    macOS a later ``subprocess.Popen`` from such a process can wedge its
    forked child in ``close()`` forever — an ``rb synth`` that hangs
    launching yosys after reading an SDC. The worker module's docstring
    has the evidence; this process must never import ``_tkinter``, and a
    test pins that.

``tokenizer``
    The fallback for a Python whose worker cannot start (no ``_tkinter``
    in the interpreter running ``rb``, no usable Tcl library): the vendored
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

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .tcl_tokenizer import _extract_names, _tokenize
from .tcl_worker import TKINTER_HINTS

logger = logging.getLogger(__name__)

__all__ = [
    "TclCommand",
    "read_commands",
    "backend_name",
    "backend_description",
    "extract_names",
    "tcl_available",
    "TKINTER_HINTS",
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

#: ``TKINTER_HINTS`` (the "install tkinter like this" lines named in the
#: one-time ``constraints.tcl_unavailable`` warning) is imported from
#: :mod:`~rtl_buddy.constraints.tcl_worker`, which owns it — the worker is
#: the module that actually needs ``_tkinter`` — and re-exported here.

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
# the worker process


#: Wall-clock budget for evaluating one constraint file. A safe interp
#: happily runs `while 1 {}` forever; `interp limit` is what turns that
#: into an error instead of a hang.
TCL_TIME_LIMIT_SECONDS = 5
#: Second guard, for a file that spins fast rather than long. Generous:
#: the recorder's own `unknown` body costs a handful of commands per
#: constraint, so a 20k-line SDC already spends a few hundred thousand.
TCL_COMMAND_LIMIT = 5_000_000
#: How much longer than the interp's own time limit the worker process is
#: given before it is killed. It only has to cover process start plus
#: `tkinter.Tcl()` (50-90 ms measured); anything past it means the interp
#: limit did not fire, which is a hang, which is what this whole design is
#: about not having in the rtl_buddy process.
TCL_WORKER_GRACE_SECONDS = 10

#: Run as ``python -m`` so the worker resolves the same way the package
#: does, whatever the install layout.
WORKER_MODULE = "rtl_buddy.constraints.tcl_worker"


def _worker_command() -> list[str]:
    """Argv for one worker run (a seam the tests replace)."""
    return [sys.executable, "-m", WORKER_MODULE]


def _worker_timeout() -> float:
    return max(0.1, TCL_TIME_LIMIT_SECONDS + TCL_WORKER_GRACE_SECONDS)


def _worker_env() -> dict[str, str]:
    """This process's environment, with our own package root importable.

    ``sys.executable -m rtl_buddy...`` normally finds the package on its
    own, but a run from a checkout or an odd ``PYTHONPATH`` need not, and
    a worker that cannot import itself would read as "no Tcl here".
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


def _parse_response(stdout: str) -> dict | None:
    """The worker's JSON answer, or ``None`` when there isn't one.

    Tolerates a line of noise before the answer (a Tk build that prints a
    warning on startup): the response is the last JSON object on stdout.
    """
    for candidate in (stdout, *reversed(stdout.strip().splitlines())):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    return None


def _run_worker(text: str, *, interest: frozenset[str]) -> dict:
    """Evaluate ``text`` in a worker process; never raises, always answers.

    Every way the worker can fail to answer — killed on timeout, exited
    non-zero, wrote something that is not the protocol — comes back as the
    same ``{"ok": false}`` shape a Tcl error does, because the caller does
    the same thing with all of them: warn once about this file and read it
    with the tokenizer instead.
    """
    request = {
        "text": text,
        "interest": sorted(interest),
        "command_limit": TCL_COMMAND_LIMIT,
        "time_limit_seconds": TCL_TIME_LIMIT_SECONDS,
    }
    timeout = _worker_timeout()
    try:
        # json.dumps escapes non-ASCII, so the pipes stay ASCII whatever
        # the locale the child inherits says its stdin is encoded in.
        proc = subprocess.run(
            _worker_command(),
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_worker_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "kind": "resource_limit",
            "error": (
                f"the Tcl reader worker did not answer within {timeout:g}s "
                "and was killed"
            ),
        }
    except OSError as exc:
        return {
            "ok": False,
            "kind": "unavailable",
            "error": f"cannot run the Tcl reader worker: {type(exc).__name__}: {exc}",
        }
    response = _parse_response(proc.stdout)
    if response is None:
        noise = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {
            "ok": False,
            "kind": "tcl_error",
            "error": (
                f"the Tcl reader worker exited {proc.returncode} without a "
                f"usable answer ({noise[-1] if noise else 'no output'})"
            ),
        }
    return response


# ---------------------------------------------------------------------------
# backend selection


@dataclass(frozen=True)
class _Probe:
    """Whether a worker can run here, and which Tcl it found."""

    #: ``None`` when the worker answered; why it did not otherwise.
    error: str | None
    patchlevel: str | None = None


#: Probed once per process — running a worker is the only honest test, and
#: the answer cannot change under us. Tests replace this object.
_PROBE: _Probe | None = None
_PROBE_LOCK = threading.Lock()
#: ``constraints.tcl_unavailable`` is a property of the interpreter, not
#: of the file being read, so it is logged once per process.
_UNAVAILABLE_LOGGED = False


def _probe() -> _Probe:
    """Ask a worker whether it can evaluate anything at all (cached)."""
    global _PROBE
    if _PROBE is None:
        with _PROBE_LOCK:
            if _PROBE is None:
                # An empty file: nothing in it can fail, so a non-ok answer
                # means the interp never came up.
                response = _run_worker("", interest=frozenset())
                if response.get("ok"):
                    patchlevel = response.get("patchlevel")
                    _PROBE = _Probe(None, str(patchlevel) if patchlevel else None)
                else:
                    _PROBE = _Probe(
                        str(response.get("error") or "the Tcl reader worker failed")
                    )
    return _PROBE


def tcl_available() -> bool:
    """True when a Tcl reader worker runs here, so the ``tcl`` backend can."""
    return _probe().error is None


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

    ``RTL_BUDDY_CONSTRAINT_READER`` wins when set. Asking for ``tcl``
    where no worker can run is fatal rather than a silent downgrade: the
    override exists to pin a backend, so quietly answering with the other
    one would defeat it.
    """
    override = _env_override()
    error = _probe().error
    if override == TCL_BACKEND:
        if error is not None:
            raise FatalRtlBuddyError(
                f"{BACKEND_ENV}={TCL_BACKEND} but no Tcl interpreter is "
                f"reachable from this Python ({error}). " + "; ".join(TKINTER_HINTS)
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
    """``info patchlevel`` of the worker's Tcl library, or ``None`` without one."""
    return _probe().patchlevel


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
# the tcl backend: a recording safe interp, in a worker process


def _read_with_tcl(
    text: str,
    *,
    interest: frozenset[str],
    source: str | None,
) -> list[TclCommand] | None:
    """Evaluate ``text`` in a recording safe interp, out of process.

    Returns the commands in ``interest``, or ``None`` when the interp did
    not read the file (syntax error, unset variable, resource limit,
    worker that never answered) — the caller then falls back to the
    tokenizer, which reads text that Tcl refuses to run.

    The interp lives in :mod:`rtl_buddy.constraints.tcl_worker` rather
    than here on purpose: see that module's docstring for the macOS
    fork+exec hazard that ``_tkinter``'s notifier thread creates in
    whatever process loads it.
    """
    response = _run_worker(text, interest=interest)

    # Warnings come back even from a failed evaluation: a `source` line
    # read before the error is still a `source` line the caller needs to
    # hear about, which is what the in-process recorder did.
    for warning in response.get("warnings") or ():
        if not isinstance(warning, dict):
            continue
        if warning.get("kind") == "include_unsupported":
            log_event(
                logger,
                logging.WARNING,
                "constraints.include_unsupported",
                source=source,
                line=warning.get("line"),
                included=warning.get("included") or "",
            )

    if not response.get("ok"):
        line = response.get("line")
        log_event(
            logger,
            logging.WARNING,
            "constraints.tcl_error",
            source=source,
            line=line if isinstance(line, int) else None,
            message=str(response.get("error") or "the Tcl reader worker failed"),
        )
        return None

    commands: list[TclCommand] = []
    for raw in response.get("commands") or ():
        name = str(raw["name"])
        words = [str(w) for w in raw.get("words") or ()]
        line = raw.get("line")
        commands.append(
            TclCommand(
                name=name,
                words=words,
                line=line if isinstance(line, int) else None,
                # one line, like the tokenizer's reconstruction: a brace
                # group written across lines reads back folded.
                raw=" ".join(" ".join([name, *words]).split()),
            )
        )
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

    Line endings are normalised first: a Windows-authored file ends a
    ``\\``-continued line with ``\\\r\n``, which neither backend
    treats as a continuation (Tcl sees a backslash-escaped ``\r`` and
    then a newline), so the continued command silently lost its
    arguments.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
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
