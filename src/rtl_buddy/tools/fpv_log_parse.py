"""Per-engine status extraction from SymbiYosys ``logfile.txt``.

Sby's ``status`` file gives the overall verdict; the per-engine
breakdown lives only in the prose ``logfile.txt``. The relevant lines
share a stable ``summary:`` prefix:

    SBY ... summary: engine_0 (smtbmc yices) returned pass
    SBY ... summary: engine_0 did not produce any traces
    SBY ... summary: engine_1 (smtbmc z3) returned pass
    SBY ... summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:00 (0)

We parse those into a per-engine list so the ``rb fpv`` results table
can show which engines ran and which won.

Per-property granularity is not extractable — sby has no
structured per-assertion output today (see #133).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_ENGINE_VERDICT_RE = re.compile(
    r"summary:\s+engine_(?P<idx>\d+)\s+\((?P<spec>[^)]*)\)\s+returned\s+(?P<verdict>\S+)"
)
_ENGINE_TRACE_RE = re.compile(
    r"summary:\s+engine_(?P<idx>\d+)\s+"
    r"(?P<msg>did not produce any traces|produced (?P<count>\d+) traces?)"
)
_ELAPSED_RE = re.compile(
    r"summary:\s+Elapsed clock time \[H:MM:SS \(secs\)\]:\s+\S+\s+\((?P<secs>\d+)\)"
)


@dataclass
class EnginePartial:
    """One engine's parsed view of the logfile. Verdict is None until
    the corresponding ``returned X`` line is seen."""

    idx: int
    spec: str | None = None
    verdict: str | None = None
    trace_count: int | None = None  # None = unknown, 0 = explicit "did not produce"

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "spec": self.spec,
            "verdict": self.verdict,
            "trace_count": self.trace_count,
        }


def parse_engine_summary(log_text: str) -> list[dict]:
    """Return a list of engine dicts sorted by index, parsed from
    ``logfile.txt`` text. Empty list when no engine summary lines are
    present (failed setup, sby exited before any engine ran).
    """
    engines: dict[int, EnginePartial] = {}
    for line in log_text.splitlines():
        m = _ENGINE_VERDICT_RE.search(line)
        if m:
            idx = int(m["idx"])
            entry = engines.setdefault(idx, EnginePartial(idx=idx))
            entry.spec = m["spec"].strip()
            entry.verdict = m["verdict"]
            continue
        m = _ENGINE_TRACE_RE.search(line)
        if m:
            idx = int(m["idx"])
            entry = engines.setdefault(idx, EnginePartial(idx=idx))
            if m["msg"].startswith("did not"):
                entry.trace_count = 0
            elif m["count"]:
                entry.trace_count = int(m["count"])
    return [engines[i].to_dict() for i in sorted(engines)]


def parse_elapsed_seconds(log_text: str) -> int | None:
    """Return the elapsed clock time in seconds from the logfile, or
    ``None`` when the summary line is missing."""
    m = _ELAPSED_RE.search(log_text)
    return int(m["secs"]) if m else None


def read_workdir_log(workdir: str) -> str | None:
    """Convenience: read ``<workdir>/logfile.txt`` if present."""
    path = Path(workdir) / "logfile.txt"
    if not path.is_file():
        return None
    return path.read_text()


def summarize_engines(per_engine: list[dict]) -> str:
    """Compact one-line render of a per-engine list for the results
    table. Examples:

        []                             -> "no engine data"
        [pass]                         -> "1/1 pass (smtbmc yices)"
        [pass, pass]                   -> "2/2 pass"
        [pass, fail]                   -> "1/2 pass (smtbmc yices won)"
        [fail, fail]                   -> "0/2 pass"
    """
    if not per_engine:
        return "no engine data"
    total = len(per_engine)
    winners = [e for e in per_engine if e.get("verdict") == "pass"]
    passed = len(winners)
    if total == 1:
        e = per_engine[0]
        spec = e.get("spec") or "engine"
        return f"1/1 {e.get('verdict') or '?'} ({spec})"
    if passed == total:
        return f"{passed}/{total} pass"
    if passed > 0:
        # When only some engines pass, name one of the winners so the
        # user knows which spec to keep.
        spec = winners[0].get("spec") or "?"
        return f"{passed}/{total} pass ({spec} won)"
    return f"0/{total} pass"
