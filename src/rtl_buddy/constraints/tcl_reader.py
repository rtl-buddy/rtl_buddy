"""Read SDC/XDC text as a list of Tcl commands.

Every constraint consumer calls :func:`read_commands` instead of matching regexes per line, so continuations, braced values, nested collections and ``#`` inside braces are read correctly. Two backends answer, in this order:

``tcl``
    A real Tcl safe interpreter that evaluates the file, so ``$var``, ``[expr ...]`` and ``;`` come out right. It runs in a short-lived worker process (:mod:`rtl_buddy.constraints.tcl_worker`), never in this one; this process must not import ``_tkinter``.

``tokenizer``
    Fallback when the worker cannot start (no ``_tkinter``, no usable Tcl library): the vendored word tokenizer (:mod:`rtl_buddy.constraints.tcl_tokenizer`). It splits words but evaluates nothing, so ``$p`` and ``[expr ...]`` stay literal and the file gets one ``constraints.tokenizer_skipped`` warning.

``RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl`` forces a backend. Both return the same command names, line numbers and :func:`extract_names` results.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .tcl_tokenizer import _extract_names, _tokenize
from .tcl_worker import TKINTER_HINTS

logger = logging.getLogger(__name__)

__all__ = [
    "TclCommand",
    "UnreadCommand",
    "ReadResult",
    "read_commands",
    "read_constraints",
    "backend_name",
    "backend_description",
    "extract_names",
    "tcl_available",
    "TKINTER_HINTS",
    "BACKEND_ENV",
    "TCL_BACKEND",
    "TOKENIZER_BACKEND",
]

TCL_BACKEND = "tcl"
TOKENIZER_BACKEND = "tokenizer"

#: ``tokenizer`` forces the fallback even where an interp is available; ``tcl`` is fatal without one.
BACKEND_ENV = "RTL_BUDDY_CONSTRAINT_READER"

#: Tcl the tokenizer does not evaluate; seeing any of these triggers one warning.
OUT_OF_SCOPE_FEATURES = ("$var", "expr", "proc", "command substitution", "source")

#: Scripting commands that make the tokenizer's words unreliable. Ordinary XDC commands (``set_property``, ...) are excluded, or every XDC would warn.
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
    #: Words after the command name: evaluated under ``tcl``, literal under ``tokenizer``. A brace group or bracket span is one word.
    words: list[str] = field(default_factory=list)
    #: 1-based line the command starts on, or ``None`` when unknown.
    line: int | None = None
    #: The command as a single line, or ``None`` when unknown.
    raw: str | None = None
    #: The file the command was read from when ``read_commands`` followed ``source`` (``script_path``); ``line`` counts in it. ``None`` otherwise.
    file: str | None = None


@dataclass(frozen=True)
class UnreadCommand:
    """A command of interest the reader found but did not evaluate, so it is absent from the result, or present only for what ran before an error.

    Under ``tokenizer``: inside another command's ``{...}`` or ``[...]`` word (a ``foreach``, ``if`` or ``proc`` body, a command substitution). Under ``tcl``: inside a top-level statement the lenient re-read skipped after a Tcl error (``error``).
    """

    name: str
    #: The command that holds it, such as ``foreach``; under ``tcl``, the skipped statement's command, which may be ``name`` itself.
    enclosing: str
    #: 1-based line of ``name`` in ``file`` (or the read text), or ``None`` when unknown.
    line: int | None = None
    file: str | None = None
    #: The Tcl error that made the ``tcl`` reader skip the statement; ``None`` under ``tokenizer``.
    error: str | None = None


@dataclass
class ReadResult:
    """What :func:`read_constraints` read: the commands, the backend that answered, and the commands of interest it found but could not read."""

    commands: list[TclCommand]
    backend: str
    #: Under ``tcl``, empty unless the lenient re-read skipped a statement.
    unread: list[UnreadCommand] = field(default_factory=list)


#: Wall-clock budget for evaluating one constraint file.
TCL_TIME_LIMIT_SECONDS = 5
#: Command-count budget; the recorder itself spends a few commands per constraint.
TCL_COMMAND_LIMIT = 5_000_000
#: Extra seconds beyond the time limit before the worker process is killed.
TCL_WORKER_GRACE_SECONDS = 10

#: Run as ``python -m`` so the worker resolves like the package, whatever the install layout.
WORKER_MODULE = "rtl_buddy.constraints.tcl_worker"


def _worker_command() -> list[str]:
    """Argv for one worker run (a seam the tests replace)."""
    return [sys.executable, "-m", WORKER_MODULE]


def _worker_timeout() -> float:
    return max(0.1, TCL_TIME_LIMIT_SECONDS + TCL_WORKER_GRACE_SECONDS)


def _worker_env() -> dict[str, str]:
    """This process's environment, with the package root on ``PYTHONPATH`` so the worker can import itself."""
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


def _parse_response(stdout: str) -> dict | None:
    """The worker's JSON answer (the last JSON object on stdout), or ``None``."""
    for candidate in (stdout, *reversed(stdout.strip().splitlines())):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    return None


def _run_worker(
    text: str,
    *,
    interest: frozenset[str] | None,
    script_path: str | None = None,
    query_names: bool = False,
) -> dict:
    """Evaluate ``text`` in a worker process; never raises.

    A timeout, non-zero exit or malformed output comes back as an ``{"ok": false}`` response, like a Tcl error.
    """
    request = {
        "text": text,
        "interest": None if interest is None else sorted(interest),
        "command_limit": TCL_COMMAND_LIMIT,
        "time_limit_seconds": TCL_TIME_LIMIT_SECONDS,
    }
    # Only sent when set, so a plain request stays byte-identical.
    if script_path is not None:
        request["script_path"] = script_path
    if query_names:
        request["query_names"] = True
    timeout = _worker_timeout()
    try:
        # json.dumps escapes non-ASCII, so the pipe is ASCII in any locale.
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


@dataclass(frozen=True)
class _Probe:
    """Whether a worker can run here, and which Tcl it found."""

    #: ``None`` when the worker answered; why it did not otherwise.
    error: str | None
    patchlevel: str | None = None


#: Probed once per process. Tests replace this object.
_PROBE: _Probe | None = None
_PROBE_LOCK = threading.Lock()
#: ``constraints.tcl_unavailable`` is logged once per process.
_UNAVAILABLE_LOGGED = False


def _probe() -> _Probe:
    """Ask a worker whether it can evaluate anything at all (cached)."""
    global _PROBE
    if _PROBE is None:
        with _PROBE_LOCK:
            if _PROBE is None:
                # An empty file cannot fail, so a non-ok answer means no interp.
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
    """Which backend :func:`read_commands` tries first.

    ``RTL_BUDDY_CONSTRAINT_READER`` wins when set; ``tcl`` with no runnable worker is fatal.
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


def extract_names(word: str) -> list[str]:
    """Names inside one word, with ``get_*`` heads, ``/pin`` remainders and a trailing ``/*`` removed, e.g.::

    [get_pins [get_cells u_a]/C]  ->  ["u_a"]
    """
    if not word:
        return []
    names, _saw_filter = _extract_names(word)
    out: list[str] = []
    for tok in names:
        if tok in {"get_clocks", "get_cells", "get_ports", "get_pins", "get_nets"}:
            continue
        if tok.startswith("/"):
            continue
        if tok.endswith("/*"):
            tok = tok.rsplit("/", 1)[0]
        if tok:
            out.append(tok)
    return out


def _log_worker_warnings(response: dict, source: str | None) -> None:
    skipped: dict[str | None, int] = {}
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
        elif warning.get("kind") == "source_unreadable":
            log_event(
                logger,
                logging.WARNING,
                "constraints.source_unreadable",
                source=warning.get("file") or source,
                line=warning.get("line"),
                included=warning.get("included") or "",
                error=warning.get("error") or "",
            )
        elif warning.get("kind") == "check_skipped":
            # The stubs cannot answer a design query, so the file's own check is assumed to pass.
            file = warning.get("file") or source
            skipped[file] = skipped.get(file, 0) + 1
            log_event(
                logger,
                logging.DEBUG,
                "constraints.tcl_check_skipped",
                source=file,
                line=warning.get("line"),
                message=warning.get("message") or "",
            )
    for file, count in skipped.items():
        log_event(
            logger,
            logging.WARNING,
            "constraints.tcl_checks_skipped",
            source=file,
            count=count,
        )


def _read_with_tcl(
    text: str,
    *,
    interest: frozenset[str] | None,
    source: str | None,
    script_path: str | None = None,
    lenient_queries: bool = False,
) -> tuple[list[TclCommand], list[UnreadCommand]] | None:
    """Evaluate ``text`` in the worker's recording safe interp.

    Returns the commands in ``interest`` and the commands in ``interest`` inside statements the lenient re-read skipped, or ``None`` when the interp did not read the file (syntax error, unset variable, resource limit, no answer). With ``lenient_queries``, a failed read is retried once with object queries answering their name arguments as a list (see :func:`read_commands`).
    """
    response = _run_worker(text, interest=interest, script_path=script_path)
    failure = response
    warned = response

    if not response.get("ok") and lenient_queries:
        retry = _run_worker(
            text, interest=interest, script_path=script_path, query_names=True
        )
        # The retry's error is the one that stopped the read; the first one may be the very query form the retry answers.
        failure = warned = retry
        if retry.get("ok"):
            line = response.get("line")
            log_event(
                logger,
                logging.DEBUG,
                "constraints.tcl_query_names",
                source=response.get("file") or source,
                line=line if isinstance(line, int) else None,
                message=str(response.get("error") or ""),
            )
            response = retry

    # Warnings arrive even when evaluation failed; a retry's include the checks it skipped.
    _log_worker_warnings(warned, source)

    if not response.get("ok"):
        line = failure.get("line")
        log_event(
            logger,
            logging.WARNING,
            "constraints.tcl_error",
            source=failure.get("file") or source,
            line=line if isinstance(line, int) else None,
            message=str(failure.get("error") or "the Tcl reader worker failed"),
        )
        return None

    unread: list[UnreadCommand] = []
    for warning in response.get("warnings") or ():
        if not isinstance(warning, dict) or warning.get("kind") != "statement_skipped":
            continue
        file = warning.get("file") or source
        line = warning.get("line") if isinstance(warning.get("line"), int) else 1
        message = str(warning.get("message") or "")
        log_event(
            logger,
            logging.WARNING,
            "constraints.tcl_statement_skipped",
            source=file,
            line=line,
            message=message,
        )
        if interest is not None:
            unread.extend(
                _statement_unread(
                    str(warning.get("text") or ""), line, interest, file, message
                )
            )

    commands: list[TclCommand] = []
    for raw in response.get("commands") or ():
        name = str(raw["name"])
        words = [str(w) for w in raw.get("words") or ()]
        line = raw.get("line")
        file = raw.get("file")
        commands.append(
            TclCommand(
                name=name,
                words=words,
                line=line if isinstance(line, int) else None,
                # One line, like the tokenizer's reconstruction.
                raw=" ".join(" ".join([name, *words]).split()),
                file=str(file) if file else None,
            )
        )
    return commands, unread


def _logical_spans(text: str) -> list[tuple[int, str]]:
    """Return ``(line, span_text)`` for each logical command in ``text``.

    Mirrors the command-boundary rules of the vendored ``_tokenize``; keep them in step with it.
    """
    spans: list[tuple[int, str]] = []
    i = 0
    n = len(text)
    line = 1
    start: int | None = None
    start_line = 1
    # Mirrors the tokenizer's bare-word state, which decides whether `#`, `{`, `[` and `"` are special.
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


#: Deepest literal ``source`` nesting the tokenizer follows.
_MAX_TOKENIZER_SOURCE_DEPTH = 16


def _tokenizer_source_target(words: list[str], script_path: str) -> str | None:
    """The file a literal ``source <path>`` names, relative to ``script_path``'s directory then the working directory; ``None`` for a computed path or a missing file."""
    if len(words) < 2:
        return None
    target = words[-1]
    if target[:1] == "{" and target[-1:] == "}":
        target = target[1:-1]
    if not target or any(ch in target for ch in "$[") or target.startswith("-"):
        return None
    candidates = (
        [target]
        if os.path.isabs(target)
        else [os.path.join(os.path.dirname(script_path), target), target]
    )
    found = next((c for c in candidates if os.path.isfile(c)), None)
    return os.path.abspath(found) if found else None


#: Commands whose braced words include Tcl scripts (bodies, conditions, scripts to evaluate); the braced words of any other command are data.
_SCRIPT_BODY_COMMANDS = frozenset(
    {
        "apply",
        "catch",
        "dict",
        "else",
        "elseif",
        "eval",
        "for",
        "foreach",
        "if",
        "lmap",
        "namespace",
        "proc",
        "switch",
        "time",
        "try",
        "uplevel",
        "while",
    }
)


def _split_commands(words: list[str]) -> list[list[str]]:
    """Split one tokenized span into its ``;``-separated commands."""
    commands: list[list[str]] = [[]]
    for word in words:
        if word == ";":
            commands.append([])
        elif word.endswith(";") and not word.endswith("\\;"):
            commands[-1].append(word[:-1])
            commands.append([])
        else:
            commands[-1].append(word)
    return [command for command in commands if command]


def _nested_unread(
    name: str,
    words: list[str],
    span: str,
    line_no: int,
    interest: frozenset[str],
    enclosing: str,
    file: str | None,
) -> list[UnreadCommand]:
    """Commands in ``interest`` inside the ``[...]`` words of command ``name``, and inside its ``{...}`` words when it takes scripts (`_SCRIPT_BODY_COMMANDS`), at any depth; found, not evaluated."""
    found: list[UnreadCommand] = []
    for word in words:
        if len(word) < 2:
            continue
        pair = (word[0], word[-1])
        if pair != ("[", "]") and not (
            pair == ("{", "}") and name in _SCRIPT_BODY_COMMANDS
        ):
            continue
        inner = word[1:-1]
        offset = span.find(word)
        base = line_no + (span[: offset + 1].count("\n") if offset >= 0 else 0)
        for inner_line, inner_span in _logical_spans(inner):
            at = base + inner_line - 1
            tokens = [w for group in _tokenize(inner_span) for w in group]
            for command in _split_commands(tokens):
                if command[0] in interest:
                    found.append(UnreadCommand(command[0], enclosing, at, file))
                found.extend(
                    _nested_unread(
                        command[0],
                        command[1:],
                        inner_span,
                        at,
                        interest,
                        enclosing,
                        file,
                    )
                )
    return found


def _statement_unread(
    text: str,
    line: int,
    interest: frozenset[str],
    file: str | None,
    error: str,
) -> list[UnreadCommand]:
    """Commands in ``interest`` in one statement the ``tcl`` reader skipped at ``error``, at any depth; ``line`` is where the statement starts."""
    found: list[UnreadCommand] = []
    for inner_line, span in _logical_spans(text):
        at = line + inner_line - 1
        tokens = [w for group in _tokenize(span) for w in group]
        for command in _split_commands(tokens):
            name = command[0]
            if name in interest:
                found.append(UnreadCommand(name, name, at, file, error))
            found.extend(
                replace(unread, error=error)
                for unread in _nested_unread(
                    name, command[1:], span, at, interest, name, file
                )
            )
    return found


def _read_with_tokenizer(
    text: str,
    *,
    interest: frozenset[str] | None,
    source: str | None,
    script_path: str | None = None,
    unread: list[UnreadCommand] | None = None,
    _depth: int = 0,
) -> list[TclCommand]:
    """Split ``text`` into words without evaluating it.

    Logs at most one ``constraints.tokenizer_skipped`` warning per call, at the first ``$`` reference or scripting command; the whole file is still read. With ``script_path``, a ``source`` of a literal path is read in place. With ``unread``, each command in ``interest`` found inside another command's braced or bracketed word is appended to it (:class:`UnreadCommand`).
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

        if interest is None or name in interest:
            commands.append(
                TclCommand(
                    name=name,
                    words=words[1:],
                    line=line_no,
                    raw=_reconstruct(span),
                    file=script_path,
                )
            )
        elif unread is not None:
            unread.extend(
                _nested_unread(
                    name,
                    words[1:],
                    span,
                    line_no,
                    interest,
                    name,
                    script_path or source,
                )
            )

        if (
            name == "source"
            and script_path is not None
            and _depth < _MAX_TOKENIZER_SOURCE_DEPTH
        ):
            included = _tokenizer_source_target(words, script_path)
            if included is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "constraints.source_unreadable",
                    source=script_path,
                    line=line_no,
                    included=words[-1] if len(words) > 1 else "",
                    error="the tokenizer follows only a literal, existing path",
                )
                continue
            try:
                body = Path(included).read_text(errors="replace")
            except OSError:
                continue
            commands.extend(
                _read_with_tokenizer(
                    body.replace("\r\n", "\n").replace("\r", "\n"),
                    interest=interest,
                    source=included,
                    script_path=included,
                    unread=unread,
                    _depth=_depth + 1,
                )
            )

    return commands


def read_commands(
    text: str,
    *,
    interest: frozenset[str] | None,
    source: str | None = None,
    script_path: str | None = None,
    lenient_queries: bool = False,
) -> tuple[list[TclCommand], str]:
    """Return ``(commands, backend)`` for the commands in ``interest``, or every command when it is ``None``.

    `backend` is the one that answered: ``"tokenizer"`` when ``tcl`` is not selected, not available, or refused the file. Line endings are normalised first so CRLF continuations work.

    ``script_path`` names the file ``text`` was read from. With it, ``source`` is followed: relative to the sourcing file's directory, then the working directory, with ``[info script]`` and path-only ``file`` subcommands (``dirname``, ``join``, ``normalize`` ...) answering as Tcl would; each command's ``file`` says where it came from. As in Tcl, the sourced file runs in the caller's scope, so it sees the caller's variables, arrays and procs. The tokenizer follows only a literal path. Without it, ``source`` is reported as unsupported.

    ``lenient_queries`` retries a file the interp refused with object queries (``get_ports`` ...) and name accessors (``get_full_name`` ...) answering their name arguments as a Tcl list rather than their source form, so a proc or loop that iterates a query's result is evaluated, and with ``get_property``/``get_attribute`` of the ``period`` of an object ``get_clocks`` returned answering the ``-period`` of the clock the file created by that name. The retry skips each error the file raises with ``error``, ``throw`` or ``return -code error``, logged at DEBUG as ``constraints.tcl_check_skipped``, and carries on after it: a check such as ``[get_property $p direction] ne "input"`` needs the design, which the reader does not have. A top-level command (of the file or a file it sources, split at newlines and ``;`` as Tcl splits them) that still raises a Tcl error is skipped, warned as ``constraints.tcl_statement_skipped`` with its file and line, and the file carries on after it; the commands it had already run are kept. A ``source`` loop, a command that never closes, or a resource limit still fails the read. Words then hold the names, not ``[get_ports ...]``; use it only where the query form does not matter.
    """
    result = read_constraints(
        text,
        interest=interest,
        source=source,
        script_path=script_path,
        lenient_queries=lenient_queries,
    )
    return result.commands, result.backend


def read_constraints(
    text: str,
    *,
    interest: frozenset[str] | None,
    source: str | None = None,
    script_path: str | None = None,
    lenient_queries: bool = False,
) -> ReadResult:
    """:func:`read_commands`, plus the commands in ``interest`` the reader found but could not read (``unread``): under ``tokenizer``, inside a body or command substitution; under ``tcl``, inside a statement the lenient re-read skipped. For a caller that must say why a command is missing. ``unread`` is collected only when ``interest`` is set."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if script_path is not None:
        script_path = os.path.abspath(script_path)
    if select_backend() == TCL_BACKEND:
        try:
            read = _read_with_tcl(
                text,
                interest=interest,
                source=source,
                script_path=script_path,
                lenient_queries=lenient_queries,
            )
        except Exception as exc:
            # Fall back to the tokenizer instead of returning no constraints.
            log_event(
                logger,
                logging.WARNING,
                "constraints.tcl_error",
                source=source,
                line=None,
                message=f"{type(exc).__name__}: {exc}",
            )
            read = None
        if read is not None:
            return ReadResult(read[0], TCL_BACKEND, read[1])
    unread: list[UnreadCommand] = []
    commands = _read_with_tokenizer(
        text,
        interest=interest,
        source=source,
        script_path=script_path,
        unread=unread if interest is not None else None,
    )
    return ReadResult(commands, TOKENIZER_BACKEND, unread)
