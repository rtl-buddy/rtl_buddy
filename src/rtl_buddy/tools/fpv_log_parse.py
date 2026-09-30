"""Per-engine status from SymbiYosys ``logfile.txt``, for the ``rb fpv`` results table.

Parsed lines look like:

    SBY ... summary: engine_0 (smtbmc yices) returned pass
    SBY ... summary: engine_0 did not produce any traces
    SBY ... summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:00 (0)
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
    """One engine's parsed log lines; ``verdict`` is None until its ``returned`` line appears."""

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
    """Return engine dicts sorted by index; empty when the log has no engine summary lines."""
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
    """Return the elapsed seconds from the summary line, or ``None`` when it is missing."""
    m = _ELAPSED_RE.search(log_text)
    return int(m["secs"]) if m else None


def read_workdir_log(workdir: str) -> str | None:
    """Return ``<workdir>/logfile.txt`` text, or ``None`` when absent."""
    path = Path(workdir) / "logfile.txt"
    if not path.is_file():
        return None
    return path.read_text()


def summarize_engines(per_engine: list[dict]) -> str:
    """Render a per-engine list as one line, e.g. ``1/2 pass (smtbmc yices won)``."""
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
        spec = winners[0].get("spec") or "?"
        return f"{passed}/{total} pass ({spec} won)"
    return f"0/{total} pass"
