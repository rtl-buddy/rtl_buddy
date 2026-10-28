"""Evaluate one SDC/XDC file in a Tcl safe interp, in a worker process.

``python -m rtl_buddy.constraints.tcl_worker`` reads one JSON request on stdin, writes one JSON response on stdout and exits::

    {"text": "...", "interest": ["create_clock", ...] | null (every command),
     "command_limit": 5000000, "time_limit_seconds": 5,
     "script_path": "/abs/top.sdc" | null, "query_names": false}

    {"ok": true, "patchlevel": "9.0.3",
     "commands": [{"name": "create_clock", "words": [...], "line": 1}],
     "warnings": [{"kind": "include_unsupported", "line": 1,
                   "included": "shared/clocks.sdc"}]}

With ``script_path``, ``[info script]`` returns it, the child gets a path-only ``file`` command, and ``source`` reads the named file (relative to the sourcing file's directory, then the working directory) and evaluates it as Tcl's ``source`` does: in the same interp, in the caller's frame (the global frame for a top-level ``source``), with ``[info script]`` naming it while it runs. Commands carry ``"file"``, and an unreadable include is a ``source_unreadable`` warning.

With ``query_names``, the object queries (``get_ports`` ...) and the name accessors (``get_full_name`` ...) return their name arguments as a Tcl list instead of their source form, so a loop over a query's result runs; ``get_property``/``get_attribute`` of the ``period`` of a name ``get_clocks`` returned last returns the ``-period`` of the clock the file created by that name. ``error``, ``throw`` and ``return -code error`` return instead of raising, so a check the stubs cannot answer (``[get_property $p direction] ne "input"``) does not stop the read; each skipped ``error`` is a ``check_skipped`` warning. A top-level statement of a file (the read text or a sourced file) that still fails is skipped and the file carries on after it; each is a ``statement_skipped`` warning with its ``line``, ``message`` and ``text``. A ``source`` loop, a statement that never closes, or a resource limit still fails the read.

    {"ok": false, "kind": "tcl_error"|"resource_limit"|"unavailable",
     "error": "...", "line": 12, "file": "/abs/inc.sdc", "warnings": [...]}

A failure's ``line`` is where the statement that raised it starts; ``file`` is present when that statement is in a sourced file.

The rtl_buddy process must never import ``_tkinter``: it starts a Tcl notifier thread that can hang a later fork+exec on macOS. Do not move the interp in process; ``tests/test_tcl_reader.py`` checks ``"_tkinter" not in sys.modules``.

The module is stdlib-only and imports ``tkinter`` inside :func:`main`, so a Python without ``_tkinter`` answers ``{"ok": false, "kind": "unavailable"}``.
"""

from __future__ import annotations

import json
import sys

__all__ = ["TKINTER_HINTS", "main"]

#: Remedies for a Python without ``_tkinter``, shown in the reader's ``constraints.tcl_unavailable`` warning.
TKINTER_HINTS = (
    "uv-managed Python bundles Tcl/Tk (`uv python install --managed-python`)",
    "Homebrew: `brew install python-tk@<X.Y>` for the running interpreter",
    "RHEL/Rocky/Alma/Fedora: `dnf install python3-tkinter`",
)

#: Safe-interp commands that can block or write; hidden so they reach the recorder.
_HIDE_COMMANDS = ("after", "vwait", "update", "puts", "exit")

#: The recorder. `unknown` fires for every command a safe interp lacks (all SDC/XDC commands, `source`, `open`, ...). `info frame -1` is the caller's frame, so `line` is where the command starts.
_UNKNOWN_PROC = r"""
proc unknown args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    return [__rb_record $__rb_line {*}$args]
}
"""

#: The child's `file` under ``script_path``: path subcommands answer, the rest are recorded like `unknown`.
_FILE_PROC = r"""
proc file args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    return [__rb_file $__rb_line {*}$args]
}
"""

#: The child's `source` under ``script_path``: the parent reads the file and evaluates it in this interp, in this proc's caller's frame. What ends the file early (a `return -level 2`, a `break`, a failure) is re-raised here with its return options, so it reaches the caller as it does from Tcl's `source`. A failure is raised with errorcode ``RB_SOURCE <file> <line>``, where the failing statement is, and is never skipped. ``RETURN`` is the interp's own `return`, renamed under ``query_names`` (`_RETURN_PROC`), so the lenient checks cannot skip a failed `source`.
_SOURCE_PROC = r"""
proc source args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    set __rb_result [__rb_source $__rb_line {*}$args]
    set __rb_outcome [__rb_source_outcome]
    if {$__rb_outcome ne ""} {
        RETURN -options [lindex $__rb_outcome 0] [lindex $__rb_outcome 1]
    }
    RETURN $__rb_result
}
"""

#: Errorcode head of a failure raised out of `source` (`_SOURCE_PROC`).
_SOURCE_ERRORCODE = "RB_SOURCE"

#: The child's `error` under ``query_names``: the check is recorded and the script carries on.
_ERROR_PROC = r"""
proc error args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    __rb_check $__rb_line {*}$args
    return
}
"""

#: The child's `throw` under ``query_names``: recorded and skipped like `error`.
_THROW_PROC = r"""
proc throw {type message} {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    __rb_check $__rb_line $message
    return
}
"""

#: The interp's `return`, renamed under ``query_names`` so `_RETURN_PROC` can stand in for it.
_REAL_RETURN = "__rb_return"

#: The child's `return` under ``query_names``: `return -code error` (or ``-code 1``, or ``-options`` with either) is recorded and skipped like `error`, and the script carries on after it; any other `return` runs the real one in the caller's frame via `tailcall`.
_RETURN_PROC = r"""
proc return args {
    set code ok
    set n [llength $args]
    for {set i 0} {$i + 1 < $n} {incr i 2} {
        set key [lindex $args $i]
        set value [lindex $args [expr {$i + 1}]]
        if {$key eq "-code"} {
            set code $value
        } elseif {$key eq "-options" && [catch {dict get $value -code} c] == 0} {
            set code $c
        }
    }
    if {$code in {error 1}} {
        if {[catch {dict get [info frame -1] line} line]} {
            set line {}
        }
        __rb_check $line [expr {$n % 2 ? [lindex $args end] : ""}]
        __rb_return
    }
    tailcall __rb_return {*}$args
}
"""

#: Name of the single child interp; a fresh process per file keeps state from leaking between files.
_CHILD = "rb_sdc"

#: Object queries that answer with their name arguments under ``query_names``.
_QUERY_COMMANDS = frozenset(
    {
        "get_ports",
        "get_pins",
        "get_cells",
        "get_nets",
        "get_clocks",
        "get_lib_cells",
        "all_inputs",
        "all_outputs",
        "all_clocks",
        "all_registers",
    }
)
#: Accessors that answer an object's name; under ``query_names`` an object is its name.
_NAME_COMMANDS = frozenset({"get_full_name", "get_name", "get_object_name"})
#: Query flags that take a value, which is not a name.
_QUERY_VALUE_FLAGS = frozenset({"-filter", "-of_objects", "-hsc", "-regexp_filter"})
#: Property accessors that answer a created clock's ``period`` under ``query_names``.
_PROPERTY_COMMANDS = frozenset({"get_property", "get_attribute"})
#: The query whose result names clocks; a property is answered only for a name it returned last.
_CLOCK_QUERY = "get_clocks"
#: ``create_clock`` flags that take a value.
_CLOCK_VALUE_FLAGS = frozenset({"-name", "-period", "-waveform", "-comment"})
#: Deepest ``source`` nesting followed before the read fails (a file sourcing itself).
_MAX_SOURCE_DEPTH = 16
#: ``file`` subcommands the child gets under ``script_path``: path arithmetic and read-only tests.
_PATH_SUBCOMMANDS = frozenset(
    {
        "dirname",
        "tail",
        "join",
        "normalize",
        "rootname",
        "extension",
        "split",
        "nativename",
        "pathtype",
        "exists",
        "isfile",
        "isdirectory",
    }
)


def _tcl_list(items: list[str]) -> str:
    """Format ``items`` as a Tcl list, bracing any element a list parse would split or substitute."""
    out = []
    for item in items:
        if item and not any(ch.isspace() or ch in '{}"\\;$' for ch in item):
            out.append(item)
        else:
            out.append("{" + item + "}")
    return " ".join(out)


def _query_names(words: tuple[str, ...]) -> list[str]:
    """The name arguments of one object query, split as Tcl lists; flags and their values dropped."""
    names: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in _QUERY_VALUE_FLAGS
            continue
        names.extend(word.split())
    return names


def _clock_definition(words: tuple[str, ...]) -> tuple[str, str] | None:
    """``(name, period)`` of one ``create_clock``: ``-name``, else its first source, as SDC names the clock."""
    name = period = None
    sources: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _CLOCK_VALUE_FLAGS and i + 1 < len(words):
            if word == "-name":
                name = words[i + 1]
            elif word == "-period":
                period = words[i + 1]
            i += 2
            continue
        if not word.startswith("-"):
            sources.extend(word.split())
        i += 1
    name = name or (sources[0] if sources else None)
    return (name, period) if name and period is not None else None


def _clock_period(
    words: tuple[str, ...], periods: dict[str, str], queried_by: dict[str, str]
) -> str | None:
    """The period a ``get_property <clock> period`` (either argument order) asks for, when the file created that clock and the object is a name ``get_clocks`` returned last (not ``get_ports`` of a port with the clock's name)."""
    args: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word == "-object_type":
            skip = True
        elif not word.startswith("-"):
            args.append(word)
    if len(args) != 2:
        return None
    for obj, prop in (args, args[::-1]):
        if (
            prop.lower() == "period"
            and obj in periods
            and queried_by.get(obj) == _CLOCK_QUERY
        ):
            return periods[obj]
    return None


def _path_command(base_dir: str, sub: str, args: tuple[str, ...]) -> str:
    """Answer one path-only ``file`` subcommand, as Tcl's ``file`` would."""
    import os

    if sub == "dirname":
        d = os.path.dirname(args[0].rstrip("/") or "/")
        return d or "."
    if sub == "tail":
        return os.path.basename(args[0].rstrip("/"))
    if sub == "join":
        return os.path.join(*args) if args else ""
    if sub == "normalize":
        return os.path.normpath(os.path.abspath(args[0]))
    if sub == "rootname":
        return os.path.splitext(args[0])[0]
    if sub == "extension":
        return os.path.splitext(args[0])[1]
    if sub == "split":
        parts = [p for p in args[0].split("/") if p]
        return _tcl_list((["/"] if args[0].startswith("/") else []) + parts)
    if sub == "nativename":
        return args[0]
    if sub == "pathtype":
        return "absolute" if os.path.isabs(args[0]) else "relative"
    if sub == "exists":
        return "1" if os.path.exists(os.path.join(base_dir, args[0])) else "0"
    if sub == "isfile":
        return "1" if os.path.isfile(os.path.join(base_dir, args[0])) else "0"
    if sub == "isdirectory":
        return "1" if os.path.isdir(os.path.join(base_dir, args[0])) else "0"
    raise ValueError(sub)


class _ScriptError(Exception):
    """A Tcl error that fails the read, with where the statement that raised it starts."""

    def __init__(self, message: str, file: str | None, line: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line


def _quote_word(word: str) -> str:
    """Brace-quote a word containing whitespace so it stays one word, matching the tokenizer backend.

    Words already wrapped in ``[...]`` or ``{...}`` are left alone.
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


def _evaluate(
    text: str,
    *,
    interest: frozenset[str] | None,
    command_limit: int,
    time_limit_seconds: float,
    script_path: str | None = None,
    query_names: bool = False,
) -> dict:
    """Run ``text`` in a recording safe interp and return the response dict.

    ``tkinter`` is imported here, not at module import.
    """
    import os
    import time
    import tkinter

    # Keep `root` alive: dropping it finalizes the interpreter. `Tcl()`, not `Tk()`, needs no display.
    root = tkinter.Tcl()
    app = root.tk

    patchlevel = str(app.call("info", "patchlevel"))
    commands: list[dict] = []
    warnings: list[dict] = []
    include_warned = False
    # The files being evaluated, innermost last; only under `script_path`.
    scripts: list[str] = (
        [os.path.abspath(script_path)] if script_path is not None else []
    )

    # Under `query_names`, the `-period` of each clock the file created, by clock name, and
    # the object query that last returned each name.
    clock_periods: dict[str, str] = {}
    queried_by: dict[str, str] = {}

    def _line(line: str) -> int | None:
        try:
            return int(line)
        except (TypeError, ValueError):
            return None

    def _append(name: str, rest: list[str], line_no: int | None) -> None:
        if interest is None or name in interest:
            entry = {"name": name, "words": rest, "line": line_no}
            if scripts:
                entry["file"] = scripts[-1]
            commands.append(entry)

    def record(line: str, *words: str) -> str:
        nonlocal include_warned
        if not words:
            return ""
        name = words[0]
        rest = [_quote_word(w) for w in words[1:]]
        line_no = _line(line)
        if name == "source" and not include_warned:
            include_warned = True
            warnings.append(
                {
                    "kind": "include_unsupported",
                    "line": line_no,
                    "included": rest[-1] if rest else "",
                }
            )
        _append(name, rest, line_no)
        if query_names:
            if name in _QUERY_COMMANDS or name in _NAME_COMMANDS:
                names = _query_names(words[1:])
                if name in _QUERY_COMMANDS:
                    queried_by.update(dict.fromkeys(names, name))
                return _tcl_list(names)
            if name == "create_clock":
                clock = _clock_definition(words[1:])
                if clock is not None:
                    clock_periods[clock[0]] = clock[1]
            elif name in _PROPERTY_COMMANDS:
                period = _clock_period(words[1:], clock_periods, queried_by)
                if period is not None:
                    return period
        # Return the command's source form so nested `[get_pins [get_cells u_a]/C]` matches the tokenizer.
        return "[" + " ".join([name, *rest]) + "]"

    def check_skipped(line: str, *words: str) -> str:
        warning = {
            "kind": "check_skipped",
            "line": _line(line),
            "message": words[0] if words else "",
        }
        if scripts:
            warning["file"] = scripts[-1]
        warnings.append(warning)
        return ""

    def file_command(line: str, *words: str) -> str:
        if len(words) >= 2 and words[0] in _PATH_SUBCOMMANDS:
            try:
                return _path_command(
                    os.path.dirname(scripts[-1]), words[0], tuple(words[1:])
                )
            except (IndexError, ValueError):
                pass
        return record(line, "file", *words)

    def statement_span(lines: list[str], first: int, line: int | None):
        """``(start, end)``, 0-based and end-exclusive, of the top-level statement of ``lines`` that holds 1-based ``line``, splitting from ``first`` as Tcl does; ``None`` when ``line`` is unknown or a statement never closes."""
        if line is None or line <= first:
            return None
        start = first
        while start < len(lines):
            end = start
            text = ""
            while True:
                if end == len(lines):
                    return None
                text += lines[end] + "\n"
                end += 1
                if app.getboolean(app.call("info", "complete", text)):
                    break
            if line <= end:
                return start, end
            start = end
        return None

    def run_script(
        body: str, *, in_caller: bool
    ) -> tuple[tuple[tuple, str] | None, str]:
        """Evaluate one file's text in the child, in the caller's frame when ``in_caller``; under ``query_names``, skip each top-level statement that fails.

        Returns ``(outcome, result)``. ``outcome`` is ``None`` when the file ran to its end or to a plain ``return``; for a sourced file it is otherwise ``(options, message)``, which its ``source`` re-raises, as Tcl's ``source`` passes a ``return -level 2`` or a ``break`` to its caller. A resumed run is padded with the newlines it skips, so ``info frame`` lines stay the file's.
        """
        lines = body.split("\n")
        first = 0
        while True:
            chunk = "\n" * first + "\n".join(lines[first:])
            script = ["catch", chunk, "::__rb_msg", "::__rb_opts"]
            if in_caller:
                script = ["uplevel", "1", script]
            code = int(app.call(_CHILD, "eval", script))
            message, raw_opts = app.splitlist(
                app.call(
                    _CHILD,
                    "eval",
                    "list $::__rb_msg $::__rb_opts [unset ::__rb_msg ::__rb_opts]",
                )
            )[:2]
            items = app.splitlist(raw_opts)
            opts = dict(zip(items[::2], items[1::2]))
            message = str(message)
            returned = (str(opts.get("-code")), str(opts.get("-level")))
            if code == 0 or (code == 2 and returned == ("0", "1")):
                return None, message
            if in_caller and code != 1:
                options = (
                    tuple(str(w) for w in items)
                    if code == 2
                    else ("-code", str(code), "-level", "1")
                )
                return (options, message), ""
            if code == 2:
                if returned != ("1", "1"):
                    return None, message
                code = 1
            elif code == 3:
                message = 'invoked "break" outside of a loop'
            elif code == 4:
                message = 'invoked "continue" outside of a loop'
            line = _line(str(opts.get("-errorline", "")))
            errorcode = [str(w) for w in app.splitlist(opts.get("-errorcode", ""))]
            if errorcode[:1] == [_SOURCE_ERRORCODE]:
                # Raised out of a `source`: where it failed, and never skipped.
                site = errorcode[1:3] + ["", ""]
                raise _ScriptError(message, site[0] or None, _line(site[1]))
            if query_names and code == 1:
                span = statement_span(lines, first, line)
                if span is not None:
                    start, first = span
                    warning = {
                        "kind": "statement_skipped",
                        "line": start + 1,
                        "message": message,
                        "text": "\n".join(lines[start:first]),
                    }
                    if scripts:
                        warning["file"] = scripts[-1]
                    warnings.append(warning)
                    continue
            raise _ScriptError(message, scripts[-1] if scripts else None, line)

    # A Python exception in a callback reaches Tcl without its message, so `source` hands
    # what ended the file early here, as `(options, message)`, and the child's `source`
    # proc re-raises it (see `_SOURCE_PROC`).
    source_outcomes: list[tuple[tuple, str]] = []

    def source_outcome() -> tuple[tuple, str] | str:
        return source_outcomes.pop() if source_outcomes else ""

    def _failure(message: str, file: str, line: str) -> tuple[tuple, str]:
        return ("-code", "1", "-errorcode", (_SOURCE_ERRORCODE, file, line)), message

    def source_command(line: str, *words: str) -> str:
        try:
            outcome, result = _source(line, *words)
        except _ScriptError as exc:
            outcome, result = (
                _failure(
                    exc.message,
                    exc.file or "",
                    "" if exc.line is None else str(exc.line),
                ),
                "",
            )
        except Exception as exc:
            outcome, result = (
                _failure(str(exc) or type(exc).__name__, scripts[-1], line),
                "",
            )
        if outcome is not None:
            source_outcomes.append(outcome)
        return result

    def _source(line: str, *words: str) -> tuple[tuple[tuple, str] | None, str]:
        line_no = _line(line)
        # `source ?-encoding name? fileName`
        target = words[-1] if words else ""
        _append("source", [_quote_word(w) for w in words], line_no)
        if len(scripts) > _MAX_SOURCE_DEPTH:
            raise RuntimeError(
                f"source nesting deeper than {_MAX_SOURCE_DEPTH} at {target}"
            )
        candidates = (
            [target]
            if os.path.isabs(target)
            else [os.path.join(os.path.dirname(scripts[-1]), target), target]
        )
        path = next((c for c in candidates if os.path.isfile(c)), None)
        try:
            if path is None:
                raise OSError("no such file")
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read().replace("\r\n", "\n").replace("\r", "\n")
        except OSError as exc:
            warnings.append(
                {
                    "kind": "source_unreadable",
                    "line": line_no,
                    "included": target,
                    "file": scripts[-1],
                    "error": str(exc),
                }
            )
            return None, ""
        path = os.path.abspath(path)
        scripts.append(path)
        app.call(_CHILD, "eval", ["info", "script", path])
        try:
            return run_script(body, in_caller=True)
        finally:
            scripts.pop()
            app.call(_CHILD, "eval", ["info", "script", scripts[-1]])

    app.call("interp", "create", "-safe", _CHILD)
    app.createcommand("__rb_record_impl", record)
    try:
        for hidden in _HIDE_COMMANDS:
            try:
                app.call("interp", "hide", _CHILD, hidden)
            except Exception:
                pass  # already absent from a safe interp
        app.call("interp", "alias", _CHILD, "__rb_record", "", "__rb_record_impl")
        app.call(_CHILD, "eval", _UNKNOWN_PROC)
        real_return = "return"
        if query_names:
            app.createcommand("__rb_check_impl", check_skipped)
            app.call("interp", "alias", _CHILD, "__rb_check", "", "__rb_check_impl")
            app.call(_CHILD, "eval", _ERROR_PROC)
            app.call(_CHILD, "eval", _THROW_PROC)
            # `tailcall` (Tcl 8.6) keeps a plain `return` exact; without it `return` is left alone.
            if app.call(_CHILD, "eval", "info commands tailcall"):
                app.call(_CHILD, "eval", ["rename", "return", _REAL_RETURN])
                app.call(_CHILD, "eval", _RETURN_PROC)
                real_return = _REAL_RETURN
        if scripts:
            for alias, impl in (
                ("__rb_file", file_command),
                ("__rb_source", source_command),
                ("__rb_source_outcome", source_outcome),
            ):
                app.createcommand(alias + "_impl", impl)
                app.call("interp", "alias", _CHILD, alias, "", alias + "_impl")
            app.call(_CHILD, "eval", _FILE_PROC)
            app.call(_CHILD, "eval", _SOURCE_PROC.replace("RETURN", real_return))
            app.call(_CHILD, "eval", ["info", "script", scripts[0]])
        app.call(
            "interp",
            "limit",
            _CHILD,
            "command",
            "-value",
            int(command_limit),
            "-granularity",
            1,
        )
        app.call(
            "interp",
            "limit",
            _CHILD,
            "time",
            "-seconds",
            int(time.time() + time_limit_seconds),
            "-granularity",
            1,
        )
        top = scripts[0] if scripts else None
        try:
            run_script(text, in_caller=False)
        except _ScriptError as exc:
            failure = {"error": exc.message, "line": exc.line}
            if exc.file and exc.file != top:
                failure["file"] = exc.file
        # A limit error cannot be caught inside the interp it fired on, so it arrives here.
        except tkinter.TclError as exc:
            failure = {"error": str(exc), "line": None}
        else:
            failure = None
        if failure is not None:
            return {
                "ok": False,
                "kind": (
                    "resource_limit"
                    if "limit exceeded" in failure["error"]
                    else "tcl_error"
                ),
                **failure,
                "warnings": warnings,
            }
    finally:
        for cleanup in (
            lambda: app.call("interp", "delete", _CHILD),
            lambda: app.deletecommand("__rb_record_impl"),
            lambda: app.deletecommand("__rb_check_impl"),
            lambda: app.deletecommand("__rb_file_impl"),
            lambda: app.deletecommand("__rb_source_impl"),
            lambda: app.deletecommand("__rb_source_outcome_impl"),
        ):
            try:
                cleanup()
            except Exception:  # pragma: no cover - defensive
                pass

    return {
        "ok": True,
        "patchlevel": patchlevel,
        "commands": commands,
        "warnings": warnings,
    }


def main() -> int:
    """Read one JSON request on stdin and write one ASCII-only JSON response on stdout."""
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        response = {"ok": False, "kind": "tcl_error", "error": f"bad request: {exc}"}
    else:
        try:
            response = _evaluate(
                str(request.get("text", "")),
                interest=(
                    None
                    if request.get("interest", ()) is None
                    else frozenset(request.get("interest") or ())
                ),
                command_limit=int(request.get("command_limit", 5_000_000)),
                time_limit_seconds=float(request.get("time_limit_seconds", 5)),
                script_path=(
                    None
                    if request.get("script_path") is None
                    else str(request["script_path"])
                ),
                query_names=bool(request.get("query_names", False)),
            )
        except ImportError as exc:
            response = {
                "ok": False,
                "kind": "unavailable",
                "error": str(exc),
                "hints": list(TKINTER_HINTS),
            }
        except Exception as exc:  # reported on stdout, never raised
            # A Tk build that cannot start is reported as a failed read; the availability probe reads that as no interp.
            response = {
                "ok": False,
                "kind": "tcl_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
