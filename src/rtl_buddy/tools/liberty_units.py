"""Liberty reading helpers: gzip-aware text access and `time_unit` detection.

OpenSTA reports times, and reads SDC values, in the `time_unit` of the first Liberty it loads. ABC's `-D` and `stime -p` work in picoseconds. The flows scale through :func:`liberty_time_unit_ps`.
"""

import gzip
import re

#: Liberty's default `time_unit`, in picoseconds, which OpenSTA also assumes when a library sets none.
DEFAULT_PS_PER_UNIT = 1000.0

_GZIP_MAGIC = b"\x1f\x8b"
_TIME_UNIT_RE = re.compile(
    r'^\s*time_unit\s*:\s*"?\s*([0-9.]+(?:[eE][-+]?\d+)?)\s*([A-Za-z]+)\s*"?\s*;?'
)
_CELL_RE = re.compile(r"^\s*cell\s*(?:\(|$)")
_PS_PER_SUFFIX = {"fs": 1e-3, "ps": 1.0, "ns": 1e3, "us": 1e6, "ms": 1e9, "s": 1e12}


class LibertyTimeUnitError(ValueError):
    """The Liberty files of one run disagree on `time_unit`, or one sets a value that is not a time.

    `units` maps each readable path to its `time_unit` as written for the message.
    """

    def __init__(self, message: str, units: dict[str, str]):
        super().__init__(message)
        self.units = units


def open_liberty(path: str):
    """Open a Liberty file for text reading, decompressing it when it starts with the gzip magic number."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == _GZIP_MAGIC:
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def format_time_unit(ps: float) -> str:
    """Return a picosecond scale as Liberty writes it, e.g. `1ns` or `100ps`."""
    if ps >= 1000.0 and ps % 1000.0 == 0:
        return f"{ps / 1000.0:g}ns"
    return f"{ps:g}ps"


def time_unit_ps(path: str) -> float | None:
    """Return the Liberty header's `time_unit` in picoseconds, or None if the file cannot be read.

    Only the header is scanned: a file with no `time_unit` before its first `cell` has the Liberty default.
    """
    try:
        with open_liberty(path) as f:
            for line in f:
                m = _TIME_UNIT_RE.match(line)
                if m:
                    value, suffix = m.groups()
                    scale = _PS_PER_SUFFIX.get(suffix.lower())
                    if scale is None:
                        raise LibertyTimeUnitError(
                            f"{path}: time_unit {value}{suffix} is not a time",
                            {path: f"{value}{suffix}"},
                        )
                    return round(float(value) * scale, 9)
                if _CELL_RE.match(line):
                    break
    except (OSError, EOFError):
        return None
    return DEFAULT_PS_PER_UNIT


def liberty_time_unit_ps(paths) -> float:
    """Return the `time_unit`, in picoseconds, that every readable Liberty in `paths` shares.

    Unreadable files are skipped; the tool reports them. No readable file gives the Liberty default. Raises :class:`LibertyTimeUnitError` when the files disagree.
    """
    units: dict[str, float] = {}
    for path in dict.fromkeys(p for p in paths if p):
        unit = time_unit_ps(path)
        if unit is not None:
            units[path] = unit
    distinct = set(units.values())
    if len(distinct) > 1:
        written = {p: format_time_unit(u) for p, u in units.items()}
        raise LibertyTimeUnitError(
            "Liberty files disagree on time_unit: "
            + ", ".join(f"{p} ({u})" for p, u in written.items()),
            written,
        )
    return distinct.pop() if distinct else DEFAULT_PS_PER_UNIT
