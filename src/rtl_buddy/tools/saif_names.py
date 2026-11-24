"""Net and instance names in a form OpenSTA's `read_saif` reads, and the `activity.instance-map` rename.

OpenSTA's SAIF lexer reads a name as one token only if it starts with a letter or `_` and goes on
with letters, digits and `_ $ [ ] \\ .`; any other character ends the token and fails the whole
read. A net name has every backslash removed before its pin is looked up, but an instance name is
looked up as written, with only a retry that escapes `[` and `]`. The netlist spells an escaped
Verilog identifier `\\u_a.u0[3] ` as `u_a.u0\\[3\\]`, so that is the spelling written for both:
`.` and `$` as they are, `[` and `]` behind a backslash. `u_a\\.u0` would read, but would name no
instance.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

_FIRST = frozenset(string.ascii_letters + "_")
# Characters the lexer takes inside a name, written as they are.
_PLAIN = frozenset(string.ascii_letters + string.digits + "_$.")
# Characters written behind a backslash: the netlist's own spelling of a bracket in a name.
_ESCAPED = frozenset("[]")
# A name equal to one of these lexes as the keyword, not as a name.
_KEYWORDS = frozenset(
    "SAIFILE SAIFVERSION DIRECTION DESIGN DATE VENDOR PROGRAM_NAME VERSION DIVIDER "
    "TIMESCALE DURATION INSTANCE NET PORT T0 T1 TX TZ TB TC IG".split()
)


def plain_name(name: str) -> str:
    """Return ``name`` with every escape removed: ``\\x`` reads as ``x``.

    A Verilog escaped identifier (``\\u_a.n1``, with or without its terminating space) and a SAIF
    or netlist spelling (``u_a\\.n1``, ``mem\\[3\\]``) both come back as the name itself.
    """
    name = name.strip()
    out = []
    escaped = False
    for ch in name:
        if escaped or ch != "\\":
            out.append(ch)
            escaped = False
        else:
            escaped = True
    return "".join(out)


def saif_name(name: str, *, net: bool) -> str | None:
    """Return ``name`` spelled so `read_saif` reads it and finds the net or instance, or ``None``.

    ``name`` is read with :func:`plain_name`. ``None`` means no spelling reads: a name that does
    not start with a letter or `_`, or that holds a character outside letters, digits and
    ``_ $ . [ ]``. A net named like a SAIF keyword (``T0``, ``NET``) gets a backslash after its
    first character, which the reader removes again; an instance cannot, since its name is looked
    up as written.
    """
    plain = plain_name(name)
    if not plain or plain[0] not in _FIRST:
        return None
    out = []
    for ch in plain:
        if ch in _PLAIN:
            out.append(ch)
        elif ch in _ESCAPED:
            out.append("\\" + ch)
        else:
            return None
    written = "".join(out)
    if written in _KEYWORDS:
        if not net:
            return None
        written = written[0] + "\\" + written[1:]
    return written


class InstanceMapError(ValueError):
    """An `activity.instance-map` file that cannot be read or applied."""


@dataclass
class InstanceMap:
    """`activity.instance-map`: trace instance name -> database instance name, both plain."""

    path: str
    pairs: dict[str, str]
    # sha256 of the file's bytes, for the power run's options digest.
    sha256: str
    # Database names as `read_saif` must read them, by trace name.
    written: dict[str, str] = field(default_factory=dict)


def read_instance_map(path: str) -> InstanceMap:
    """Read an instance-map file: one ``<trace name> <database name>`` pair per line.

    Blank lines and lines starting with ``#`` are skipped. A backslash escapes the next character
    in either name, so ``\\u_a.u0[3]``, ``u_a.u0\\[3\\]`` and ``u_a.u0[3]`` are one name. Raises
    :class:`InstanceMapError` for an unreadable file, a line that is not two names, a trace name
    given two database names, or a database name `read_saif` cannot read.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise InstanceMapError(f"cannot read activity.instance-map {path}: {e}") from e
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InstanceMapError(
            f"activity.instance-map {path} is not UTF-8 text: {e}"
        ) from e
    pairs: dict[str, str] = {}
    written: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 2:
            raise InstanceMapError(
                f"activity.instance-map {path} line {lineno}: expected "
                f"'<trace name> <database name>', got {stripped!r}"
            )
        trace, database = (plain_name(f) for f in fields)
        spelled = saif_name(database, net=False)
        if spelled is None:
            raise InstanceMapError(
                f"activity.instance-map {path} line {lineno}: read_saif cannot "
                f"read the instance name {database!r}"
            )
        if trace in pairs and pairs[trace] != database:
            raise InstanceMapError(
                f"activity.instance-map {path} line {lineno}: {trace!r} is "
                f"mapped to both {pairs[trace]!r} and {database!r}"
            )
        pairs[trace] = database
        written[trace] = spelled
    return InstanceMap(
        path=path,
        pairs=pairs,
        sha256=hashlib.sha256(data).hexdigest(),
        written=written,
    )


# `(INSTANCE name` or `(INSTANCE "design" name`; a name runs to whitespace or a parenthesis, and
# a backslash takes the next character with it.
_INSTANCE_RE = re.compile(
    r'(\(\s*INSTANCE\s+(?:"(?:[^"\\]|\\.)*"\s+)?)((?:\\.|[^\s()\\])+)'
)


class _HashingReader:
    """A binary file that hashes every byte read through it."""

    def __init__(self, fh, digest):
        self._fh = fh
        self._digest = digest

    def read(self, size=-1):
        data = self._fh.read(size)
        self._digest.update(data)
        return data


@dataclass
class MapResult:
    """What :func:`apply_instance_map` did."""

    # sha256 of the source SAIF's bytes as they were read.
    source_sha256: str
    # INSTANCE records renamed.
    renamed: int
    # Map pairs that matched no INSTANCE record.
    unmatched: int


def apply_instance_map(source: str, dest: str, imap: InstanceMap) -> MapResult:
    """Copy the SAIF at ``source`` to ``dest``, renaming each INSTANCE record ``imap`` names.

    A record matches when its name, read with :func:`plain_name`, is a trace name of the map, at
    any depth, and the `INSTANCE` keyword and the name are on one line, as SAIF writers put them.
    Everything else is copied byte for byte. A gzip-compressed source, which `read_saif` also
    reads, is decompressed; ``dest`` is plain text either way. The source file is hashed as it is
    read, so the hash is of the bytes the copy was made from. Raises ``OSError`` if either file
    fails, including a corrupt gzip stream.
    """
    digest = hashlib.sha256()
    used: set[str] = set()
    renamed = 0

    def _rename(m: re.Match) -> str:
        nonlocal renamed
        trace = plain_name(m.group(2))
        spelled = imap.written.get(trace)
        if spelled is None:
            return m.group(0)
        used.add(trace)
        renamed += 1
        return m.group(1) + spelled

    with open(source, "rb") as fh, open(dest, "wb") as out:
        raw_reader = _HashingReader(fh, digest)
        compressed = fh.peek(2)[:2] == b"\x1f\x8b"
        src = gzip.GzipFile(fileobj=raw_reader) if compressed else fh
        for raw in src:
            if not compressed:
                digest.update(raw)
            # latin-1 round-trips every byte; SAIF keywords and names are ASCII.
            line = raw.decode("latin-1")
            if "INSTANCE" in line:
                line = _INSTANCE_RE.sub(_rename, line)
            out.write(line.encode("latin-1"))
        if compressed:
            # Bytes after the gzip stream are part of the file the hash identifies.
            while raw_reader.read(1 << 20):
                pass
    return MapResult(
        source_sha256=digest.hexdigest(),
        renamed=renamed,
        unmatched=len(imap.pairs) - len(used),
    )
