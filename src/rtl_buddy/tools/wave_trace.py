# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Waveform-trace discovery shared by `rb wave` and `rb axi-profile`.

Finds the per-test dump under ``artefacts/<test>/`` (``dump.fst``, ``dump.vcd`` or ``vcdplus.vpd``, depending on the builder) and picks the newest.
"""

import os

TRACE_CANDIDATES = ("dump.fst", "dump.vcd", "vcdplus.vpd")


def existing_traces(trace_dir: str) -> list[str]:
    """Return the existing trace files under ``trace_dir`` (any order)."""
    return [
        os.path.join(trace_dir, name)
        for name in TRACE_CANDIDATES
        if os.path.isfile(os.path.join(trace_dir, name))
    ]


def newest_trace(trace_dir: str) -> str | None:
    """Return the newest existing trace under ``trace_dir``, or None."""
    candidates = existing_traces(trace_dir)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)
