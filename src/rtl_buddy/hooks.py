# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Helpers for executing `sweep` and `preproc` hook scripts.

Hooks are exec()'d with `__name__` set to `HOOK_MODULE_NAME`, never `"__main__"`, and
their stdout is captured as `hook.stdout` log events. See docs/concepts/plugins.md.
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
    """Stands in for ``sys.stdout`` while a hook runs.

    Each complete line becomes a ``hook.stdout`` log event. Output goes to stderr and
    `rtl_buddy.log`, never to stdout, which `--machine` reserves for the JSON envelope.
    It has no ``fileno()`` or ``.buffer``, so writes that bypass Python-level printing
    raise instead of corrupting the envelope. Output from child processes bypasses the
    capture.
    """

    def __init__(self, script_path, stage=None):
        self._script_path = script_path
        self._stage = stage
        self._buffer = ""

    def writable(self):
        return True

    def isatty(self):
        # A hook must not be told it is on a terminal; its output goes to the log.
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
    """Return the exec namespace: `variables`, `__file__` (absolute `script_path`) and
    `__name__` set to `HOOK_MODULE_NAME`.
    """
    return {
        **variables,
        "__file__": os.path.abspath(script_path),
        "__name__": HOOK_MODULE_NAME,
    }


def exec_hook_script(script_path, code, *, stage=None, **variables):
    """Exec a hook script and return its namespace dict.

    The namespace is a real module's `__dict__`, registered as
    `sys.modules[HOOK_MODULE_NAME]` during the exec so `@dataclass` with string
    annotations works; the previous binding is restored afterwards. Exceptions propagate
    unchanged. `stage` (`"preproc"` or `"sweep"`) labels the `hook.stdout` events and is
    not injected into the namespace. Hook stderr is not captured.
    """
    mod = types.ModuleType(HOOK_MODULE_NAME)
    mod.__dict__.update(build_hook_namespace(script_path, **variables))
    sentinel = object()
    prev = sys.modules.get(HOOK_MODULE_NAME, sentinel)
    sys.modules[HOOK_MODULE_NAME] = mod
    hook_stdout = _HookStdout(script_path, stage=stage)
    try:
        # The trailing partial line is flushed from our own object, even if the hook
        # rebinds sys.stdout.
        with contextlib.redirect_stdout(hook_stdout):
            exec(code, mod.__dict__)
    finally:
        hook_stdout.flush()
        if prev is sentinel:
            sys.modules.pop(HOOK_MODULE_NAME, None)
        else:
            sys.modules[HOOK_MODULE_NAME] = prev
    return mod.__dict__
