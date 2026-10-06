"""Evaluate one SDC/XDC file in a Tcl safe interp, in a worker process.

``python -m rtl_buddy.constraints.tcl_worker`` reads one JSON request on stdin, writes one JSON response on stdout and exits::

    {"text": "...", "interest": ["create_clock", ...] | null (every command),
     "command_limit": 5000000, "time_limit_seconds": 5}

    {"ok": true, "patchlevel": "9.0.3",
     "commands": [{"name": "create_clock", "words": [...], "line": 1}],
     "warnings": [{"kind": "include_unsupported", "line": 1,
                   "included": "shared/clocks.sdc"}]}

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

#: Name of the single child interp; a fresh process per file keeps state from leaking between files.
_CHILD = "rb_sdc"


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
) -> dict:
    """Run ``text`` in a recording safe interp and return the response dict.

    ``tkinter`` is imported here, not at module import.
    """
    import time
    import tkinter

    # Keep `root` alive: dropping it finalizes the interpreter. `Tcl()`, not `Tk()`, needs no display.
    root = tkinter.Tcl()
    app = root.tk

    patchlevel = str(app.call("info", "patchlevel"))
    commands: list[dict] = []
    warnings: list[dict] = []
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
            warnings.append(
                {
                    "kind": "include_unsupported",
                    "line": line_no,
                    "included": rest[-1] if rest else "",
                }
            )
        if interest is None or name in interest:
            commands.append({"name": name, "words": rest, "line": line_no})
        # Return the command's source form so nested `[get_pins [get_cells u_a]/C]` matches the tokenizer.
        return "[" + " ".join([name, *rest]) + "]"

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
