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
"""

import os
import sys
import types

HOOK_MODULE_NAME = "__rtl_buddy_hook__"


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


def exec_hook_script(script_path, code, **variables):
    """Exec a hook script under the documented namespace contract and return
    the resulting namespace dict.

    The namespace is a real module's `__dict__`, and the module is registered
    in `sys.modules[HOOK_MODULE_NAME]` while the script runs (see module
    docstring for why). Exceptions from the script propagate to the caller
    unchanged; the previous `sys.modules` binding is always restored.
    """
    mod = types.ModuleType(HOOK_MODULE_NAME)
    mod.__dict__.update(build_hook_namespace(script_path, **variables))
    sentinel = object()
    prev = sys.modules.get(HOOK_MODULE_NAME, sentinel)
    sys.modules[HOOK_MODULE_NAME] = mod
    try:
        exec(code, mod.__dict__)
    finally:
        if prev is sentinel:
            sys.modules.pop(HOOK_MODULE_NAME, None)
        else:
            sys.modules[HOOK_MODULE_NAME] = prev
    return mod.__dict__
