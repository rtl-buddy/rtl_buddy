"""Evaluate one SDC/XDC file in a Tcl safe interp, in a worker process.

``python -m rtl_buddy.constraints.tcl_worker`` reads one JSON request on stdin, writes one JSON response on stdout and exits::

    {"text": "...", "interest": ["create_clock", ...] | null (every command),
     "command_limit": 5000000, "time_limit_seconds": 5,
     "script_path": "/abs/top.sdc" | null, "query_names": false}

    {"ok": true, "patchlevel": "9.0.3",
     "commands": [{"name": "create_clock", "words": [...], "line": 1}],
     "warnings": [{"kind": "include_unsupported", "line": 1,
                   "included": "shared/clocks.sdc"}]}

With ``script_path``, ``[info script]`` returns it, the child gets a path-only ``file`` command, and ``source`` reads and evaluates the named file (relative to the sourcing file's directory, then the working directory) in the same interp: commands carry ``"file"``, and an unreadable include is a ``source_unreadable`` warning. With ``query_names``, the object queries (``get_ports`` ...) and the name accessors (``get_full_name`` ...) return their name arguments as a Tcl list instead of their source form, so a loop over a query's result runs, and ``error`` returns instead of raising, so a check the stubs cannot answer (``[get_property $p direction] ne "input"``) does not stop the read; each skipped ``error`` is a ``check_skipped`` warning.

    {"ok": false, "kind": "tcl_error"|"resource_limit"|"unavailable",
     "error": "...", "line": 12, "warnings": [...]}

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

#: The child's `source` under ``script_path``: the parent reads and evaluates the file in this interp. It raises with `return -code error`, which the lenient `error` (`_ERROR_PROC`) cannot skip.
_SOURCE_PROC = r"""
proc source args {
    if {[catch {dict get [info frame -1] line} __rb_line]} {
        set __rb_line {}
    }
    set __rb_result [__rb_source $__rb_line {*}$args]
    set __rb_error [__rb_source_error]
    if {$__rb_error ne ""} {
        return -code error $__rb_error
    }
    return $__rb_result
}
"""

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
        if query_names and (name in _QUERY_COMMANDS or name in _NAME_COMMANDS):
            return _tcl_list(_query_names(words[1:]))
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

    # A Python exception in a callback reaches Tcl without its message, so `source` reports
    # failures here and the child's `source` proc raises them (see `_SOURCE_PROC`).
    source_errors: list[str] = []

    def source_error() -> str:
        return source_errors.pop() if source_errors else ""

    def source_command(line: str, *words: str) -> str:
        try:
            return _source(line, *words)
        except Exception as exc:
            source_errors.append(str(exc) or type(exc).__name__)
            return ""

    def _source(line: str, *words: str) -> str:
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
            return ""
        path = os.path.abspath(path)
        scripts.append(path)
        app.call(_CHILD, "eval", ["info", "script", path])
        try:
            return str(app.call(_CHILD, "eval", body))
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
        if query_names:
            app.createcommand("__rb_check_impl", check_skipped)
            app.call("interp", "alias", _CHILD, "__rb_check", "", "__rb_check_impl")
            app.call(_CHILD, "eval", _ERROR_PROC)
        if scripts:
            for alias, impl in (
                ("__rb_file", file_command),
                ("__rb_source", source_command),
                ("__rb_source_error", source_error),
            ):
                app.createcommand(alias + "_impl", impl)
                app.call("interp", "alias", _CHILD, alias, "", alias + "_impl")
            app.call(_CHILD, "eval", _FILE_PROC)
            app.call(_CHILD, "eval", _SOURCE_PROC)
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
        app.setvar("__rb_script", text)
        # Catch in the parent: a limit error cannot be caught inside the interp it fired on.
        failed = int(
            app.eval(
                f"catch {{interp eval {_CHILD} $::__rb_script}} ::__rb_msg ::__rb_opts"
            )
        )
        if failed:
            message = str(app.getvar("__rb_msg"))
            raw_line = app.eval(
                "if {[dict exists $::__rb_opts -errorline]} "
                "{dict get $::__rb_opts -errorline} else {return {}}"
            )
            return {
                "ok": False,
                "kind": (
                    "resource_limit" if "limit exceeded" in message else "tcl_error"
                ),
                "error": message,
                "line": int(raw_line) if str(raw_line).isdigit() else None,
                "warnings": warnings,
            }
    finally:
        for cleanup in (
            lambda: app.call("interp", "delete", _CHILD),
            lambda: app.deletecommand("__rb_record_impl"),
            lambda: app.deletecommand("__rb_check_impl"),
            lambda: app.deletecommand("__rb_file_impl"),
            lambda: app.deletecommand("__rb_source_impl"),
            lambda: app.deletecommand("__rb_source_error_impl"),
            lambda: app.call("unset", "-nocomplain", "::__rb_script"),
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
