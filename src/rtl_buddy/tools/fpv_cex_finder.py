"""Resolve SymbiYosys counterexample VCD paths for ``rb wave-fpv``.

Sby writes a CEX VCD at ``<workdir>/engine_<N>/trace.vcd`` whenever the
proof disproves a property. Multiple engines can each produce a trace —
we return the first one found (sorted by engine name) since they all
witness the same property failure.
"""

from __future__ import annotations

from pathlib import Path


def find_cex_vcd(suite_dir: str, verif_name: str) -> str | None:
    """Return the CEX VCD path for an FPV verification, or ``None``.

    Looks for ``<suite_dir>/artefacts/<verif_name>/sby_workdir/engine_<N>/trace.vcd``.
    Returns the first match in sorted engine order. ``None`` when the
    workdir is absent (verification has not run) or no engine produced
    a trace (the proof passed, or sby died before any engine started).
    """
    workdir = Path(suite_dir) / "artefacts" / verif_name / "sby_workdir"
    if not workdir.is_dir():
        return None
    for entry in sorted(workdir.iterdir()):
        if not entry.name.startswith("engine_") or not entry.is_dir():
            continue
        trace = entry / "trace.vcd"
        if trace.is_file():
            return str(trace)
    return None
