# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Shared helpers for executing hook scripts (`sweep` / `preproc`).

Hook scripts are exec()'d rather than imported as real modules. Contract:
the exec namespace always carries `__name__` set to `HOOK_MODULE_NAME`,
never `"__main__"`, so a hook body guarded by `if __name__ == "__main__":`
is deterministically skipped. Hook logic belongs at module top level (see
docs/concepts/plugins.md).

`exec_hook_script()` additionally registers a real module object under
`HOOK_MODULE_NAME` in `sys.modules` for the duration of the exec. Classes
defined by the hook get `__module__ = HOOK_MODULE_NAME`, and stdlib
machinery resolves that back through `sys.modules` — CPython 3.11's
`dataclasses._is_type` does `sys.modules.get(cls.__module__).__dict__`
unguarded, so a `@dataclass` in a hook using string annotations (any hook
with `from __future__ import annotations`) crashes with
`'NoneType' object has no attribute '__dict__'` if the sentinel is not
registered. Hooks execute sequentially in-process, so a single shared
registration slot is safe; the previous binding (if any) is restored on
exit, success or raise.

It also captures the hook's `sys.stdout` (issue #371): hooks run in-process,
so a `print()` in a hook would otherwise land on `rtl_buddy`'s own stdout —
under `--machine` the stream reserved for the JSON envelope, which the extra
text makes unparseable. Captured lines are re-emitted as `hook.stdout` log
events, which reach stderr and `rtl_buddy.log` but never stdout.
"""

import contextlib
import io
import logging
import os
import sys
import types

from .logging_utils import log_console_event

logger = logging.getLogger(__name__)

HOOK_MODULE_NAME = "__rtl_buddy_hook__"


class _HookStdout(io.TextIOBase):
    """Stands in for ``sys.stdout`` while a hook script runs (issue #371).

    Hooks are exec()'d in-process, so a plain `print()` in a hook lands on
    `rtl_buddy`'s own stdout — the stream `--machine` reserves for the single
    JSON envelope, which the leading hook text then makes unparseable. Every
    complete line written here is re-emitted as a structured ``hook.stdout``
    event instead: it reaches the console on stderr (both modes, regardless
    of verbosity — hook progress is a liveness signal) and `rtl_buddy.log`,
    and never stdout.

    Line-buffered rather than accumulated so a long-running hook still
    reports as it goes; the trailing partial line is flushed by the caller.
    """

    def __init__(self, script_path, stage=None):
        self._script_path = script_path
        self._stage = stage
        self._buffer = ""

    def writable(self):
        return True

    def isatty(self):
        # A hook asking "am I on a terminal?" must not be told yes: its
        # output is going to the log, not to a screen it can address.
        return False

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError(f"string argument expected, got {type(text).__name__}")
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self):
        if self._buffer:
            line, self._buffer = self._buffer, ""
            self._emit(line)

    def _emit(self, line):
        # Blank lines carry no information once the text is re-framed with a
        # per-line prefix, and a hook that prints a banner would otherwise
        # emit empty log records.
        if not line.strip():
            return
        log_console_event(
            logger,
            logging.INFO,
            "hook.stdout",
            script=self._script_path,
            stage=self._stage,
            line=line.rstrip(),
        )


def build_hook_namespace(script_path, **variables):
    """Build the exec() namespace for a hook script.

    Returns `variables` plus `__file__` (absolute path to `script_path`)
    and `__name__` set to the `HOOK_MODULE_NAME` sentinel.
    """
    return {
        **variables,
        "__file__": os.path.abspath(script_path),
        "__name__": HOOK_MODULE_NAME,
    }


def exec_hook_script(script_path, code, *, stage=None, **variables):
    """Exec a hook script under the documented namespace contract and return
    the resulting namespace dict.

    The namespace is a real module's `__dict__`, and the module is registered
    in `sys.modules[HOOK_MODULE_NAME]` while the script runs (see module
    docstring for why). Exceptions from the script propagate to the caller
    unchanged; the previous `sys.modules` binding is always restored.

    `sys.stdout` is replaced by :class:`_HookStdout` for the duration of the
    exec, so anything the hook prints becomes a `hook.stdout` log event
    instead of raw text on `rtl_buddy`'s stdout (issue #371). `stage` is a
    reserved keyword naming the hook stage (`"preproc"` / `"sweep"`) for
    those events; it is not injected into the hook namespace. Hook `stderr`
    is left alone: it is not the envelope stream, so it is already safe.
    """
    mod = types.ModuleType(HOOK_MODULE_NAME)
    mod.__dict__.update(build_hook_namespace(script_path, **variables))
    sentinel = object()
    prev = sys.modules.get(HOOK_MODULE_NAME, sentinel)
    sys.modules[HOOK_MODULE_NAME] = mod
    hook_stdout = _HookStdout(script_path, stage=stage)
    try:
        # A hook that rebinds sys.stdout itself is out of scope but must not
        # break anything: redirect_stdout restores the outer stream on exit
        # either way, and the trailing partial line is flushed off our own
        # object rather than off whatever sys.stdout ended up being.
        with contextlib.redirect_stdout(hook_stdout):
            exec(code, mod.__dict__)
    finally:
        hook_stdout.flush()
        if prev is sentinel:
            sys.modules.pop(HOOK_MODULE_NAME, None)
        else:
            sys.modules[HOOK_MODULE_NAME] = prev
    return mod.__dict__
