"""Locate SymbiYosys counterexample VCDs for ``rb wave-fpv``."""

from __future__ import annotations

from pathlib import Path


def find_cex_vcd(suite_dir: str, verif_name: str) -> str | None:
    """Return the first ``engine_<N>/trace.vcd`` under the verification's sby workdir, or ``None``.

    Engines are searched in sorted name order. ``None`` means the workdir is
    absent or no engine wrote a trace.
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
