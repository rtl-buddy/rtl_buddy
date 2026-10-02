"""Liberty reading helpers: gzip-aware text access and `time_unit` detection.

OpenSTA reports times, and reads SDC values, in the `time_unit` of the first Liberty it loads. ABC's `-D` and `stime -p` work in picoseconds. The flows scale through :func:`liberty_time_unit_ps`.
"""

import gzip
import re

#: Liberty's default `time_unit`, in picoseconds, which OpenSTA also assumes when a library sets none.
DEFAULT_PS_PER_UNIT = 1000.0

_GZIP_MAGIC = b"\x1f\x8b"
_CODE_RE = re.compile(r'\s+|/\*|"|[{}:;]|[^\s{}:;"/]+|/')
_VALUE_RE = re.compile(r"^\s*([0-9.]+(?:[eE][-+]?\d+)?)\s*([A-Za-z]+)\s*$")
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


def _tokens(lines):
    """Yield `(is_string, text)` Liberty tokens, skipping whitespace and `/* */` comments across lines."""
    mode = None
    buf: list[str] = []
    for line in lines:
        pos, n = 0, len(line)
        while pos < n:
            if mode == "comment":
                end = line.find("*/", pos)
                if end < 0:
                    break
                pos, mode = end + 2, None
            elif mode == "string":
                end = line.find('"', pos)
                if end < 0:
                    buf.append(line[pos:])
                    break
                buf.append(line[pos:end])
                yield True, "".join(buf)
                buf, pos, mode = [], end + 1, None
            else:
                m = _CODE_RE.match(line, pos)
                tok, pos = m.group(), m.end()
                if tok == "/*":
                    mode = "comment"
                elif tok == '"':
                    mode = "string"
                elif not tok.isspace():
                    yield False, tok


def time_unit_ps(path: str) -> float | None:
    """Return the library group's `time_unit` in picoseconds, or None if the file cannot be read.

    Only an attribute directly inside the `library` group counts; one nested in a cell or other group is ignored. A library with none has the Liberty default.
    """
    try:
        with open_liberty(path) as f:
            depth, seen = 0, 0
            for is_string, tok in _tokens(f):
                if not is_string and tok in "{}":
                    depth += 1 if tok == "{" else -1
                    seen = 0
                elif seen == 2:
                    m = _VALUE_RE.match(tok)
                    if m is None:
                        raise LibertyTimeUnitError(
                            f"{path}: time_unit {tok} is not a time", {path: tok}
                        )
                    value, suffix = m.groups()
                    scale = _PS_PER_SUFFIX.get(suffix.lower())
                    if scale is None:
                        raise LibertyTimeUnitError(
                            f"{path}: time_unit {value}{suffix} is not a time",
                            {path: f"{value}{suffix}"},
                        )
                    return round(float(value) * scale, 9)
                elif seen == 1:
                    seen = 2 if (not is_string and tok == ":") else 0
                elif depth == 1 and not is_string and tok == "time_unit":
                    seen = 1
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
