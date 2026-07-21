# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Shared waveform-trace discovery for `rb wave` and `rb axi-profile`.

Both commands read the per-test dump produced by a debug sim, which lands
under ``artefacts/<test>/``. The filename depends on which builder ran:
Verilator dumps ``dump.fst``, Icarus (and other plain-VCD dumpers)
``dump.vcd``, and VCS ``vcdplus.vpd``. Resolution picks the newest existing
candidate so the consumer follows whichever builder ran last.
"""

import os

# Named in errors in this order; the actual pick is by newest mtime so the
# consumer follows the builder that ran most recently.
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
