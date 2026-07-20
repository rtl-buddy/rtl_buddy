# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Shared helper for building the exec() namespace used by hook scripts.

`sweep` and `preproc` hook scripts are exec()'d into a hand-built namespace
rather than imported as real modules, so plain module dicts have no
`__name__` key by default. Contract: `build_hook_namespace()` always sets
`__name__` to `HOOK_MODULE_NAME`, never `"__main__"`, so a hook body guarded
by `if __name__ == "__main__":` is deterministically skipped. Hook logic
belongs at module top level (see docs/concepts/plugins.md).
"""

import os

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
