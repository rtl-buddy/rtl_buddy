"""Evaluate one SDC/XDC file in a Tcl safe interp — in a worker process (#641).

``python -m rtl_buddy.constraints.tcl_worker`` reads one JSON request on
stdin and writes one JSON response on stdout, then exits::

    {"text": "...", "interest": ["create_clock", ...],
     "command_limit": 5000000, "time_limit_seconds": 5}

    {"ok": true, "patchlevel": "9.0.3",
     "commands": [{"name": "create_clock", "words": [...], "line": 1}],
     "warnings": [{"kind": "include_unsupported", "line": 1,
                   "included": "shared/clocks.sdc"}]}

    {"ok": false, "kind": "tcl_error"|"resource_limit"|"unavailable",
     "error": "...", "line": 12, "warnings": [...]}

**Why this is a separate process.** ``tkinter`` — the only Tcl the
stdlib ships — starts Tcl's ``NotifierThreadProc`` the moment ``_tkinter``
loads, and that thread never goes away. On macOS a later
``subprocess.Popen`` (fork + exec with ``close_fds=True``) from the same
process can wedge in the forked child inside
``child_exec -> _close_open_fds_maybe_unsafe -> close()``, in
uninterruptible kernel state, forever; the parent then blocks reading the
exec errpipe. Reproduced on 2026-09-22 (1 of 6 full pytest runs, Tcl
9.0.3 on the uv-managed 3.12): a sample of the orphaned child showed its
main thread in ``child_exec``/``close`` and a second thread in
``NotifierThreadProc``/``__select`` after sitting for 9+ hours. In
production that is ``rb synth`` / ``rb pnr`` hanging on an EDA tool launch
after reading an SDC.

So: the rtl_buddy process must never import ``_tkinter``. It reads
constraints by running this module, and ``tests/test_tcl_reader.py`` pins
``"_tkinter" not in sys.modules`` after a tcl-backend read. Do not move
the interp back in process.

Cost of the split, measured on an M-series mac (Python 3.12, Tcl 9.0.3):
50-90 ms per ``read_commands`` call — process start, ``import tkinter``
and ``tkinter.Tcl()`` — against a handful of constraint files per run.
That is well under the noise of the tools the constraints are read for.

This module is deliberately stdlib-only and imports ``tkinter`` lazily
inside :func:`main`, so importing it costs nothing and a Python without
``_tkinter`` answers ``{"ok": false, "kind": "unavailable"}`` instead of
failing to start.
"""

from __future__ import annotations

import json
import sys

__all__ = ["TKINTER_HINTS", "main"]

#: What to do about a Python without ``_tkinter``. Named in the reader's
#: one-time ``constraints.tcl_unavailable`` warning, because "install
#: tkinter" is not actionable on its own. Lives here rather than in
#: :mod:`rtl_buddy.constraints.tcl_reader` so the worker stays stdlib-only.
TKINTER_HINTS = (
    "uv-managed Python bundles Tcl/Tk (`uv python install --managed-python`)",
    "Homebrew: `brew install python-tk@<X.Y>` for the running interpreter",
    "RHEL/Rocky/Alma/Fedora: `dnf install python3-tkinter`",
)

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

#: The one child interp this process ever creates. A fresh process per
#: file is what keeps `set` state, procs and namespaces from one
#: constraint file out of the next one.
_CHILD = "rb_sdc"


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


def _evaluate(
    text: str,
    *,
    interest: frozenset[str],
    command_limit: int,
    time_limit_seconds: float,
) -> dict:
    """Run ``text`` in a recording safe interp and return the response dict.

    ``tkinter`` is imported here, never at module import: a caller that
    only wants :data:`TKINTER_HINTS` must not pay the fork hazard this
    module exists to contain.
    """
    import time
    import tkinter

    # Keep the Tk object alive alongside the interpreter handle it owns;
    # dropping it would finalize the interpreter under us. Never `Tk()` —
    # `Tcl()` needs no display.
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
        if name in interest:
            commands.append({"name": name, "words": rest, "line": line_no})
        # Collections collapse to their own source form, so a nested
        # `[get_pins [get_cells u_a]/C]` reads the same as it does under
        # the tokenizer. Everything else is opaque by the same rule.
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
        # Catch in the *parent*: a limit error cannot be caught inside the
        # interp it fired on, and the parent's options dict carries the
        # child's `-errorline`.
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
                # A limit is a property of the run, not of the file; the
                # reader logs both the same way but the kind lets a caller
                # tell "Tcl refused this text" from "this text ran away".
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
    """Read one request on stdin, write one response on stdout.

    Both travel as JSON with the default ``ensure_ascii``, so the pipes
    carry pure ASCII whatever the locale and whatever is in the constraint
    file.
    """
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        response = {"ok": False, "kind": "tcl_error", "error": f"bad request: {exc}"}
    else:
        try:
            response = _evaluate(
                str(request.get("text", "")),
                interest=frozenset(request.get("interest") or ()),
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
            # A Tk build that will not start (no usable init.tcl, a
            # mismatched library) reaches the reader as a failed read like
            # any other; the availability probe reads it as "no interp
            # here" because the probe sends an empty file, which nothing
            # else can fail on.
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
