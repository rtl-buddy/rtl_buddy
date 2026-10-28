"""Supply-port order in a routed run's powered netlist and a hardened block's timing model.

OpenROAD writes a top's ports in the order of its timing graph's top cell. A session that ran `pdngen` appends the supply ports after every other port; a session that read the database puts them among the scalar ports, before the buses. rb moves them last, sorted by name, in `<top>.routed.pwr.v` and in the abstract's `<top>.lib`, so both are the same whether the run was one session, staged, resumed or dispatched (#889, #890). The supply ports are the ports the powered netlist declares and the logical `<top>.routed.v` does not.

The netlists are read only up to the top module's port declarations, and the powered one is rewritten as a stream, so memory does not grow with the netlist.
"""

import io
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .artifact_paths import atomic_tmp_name

# OpenSTA's VerilogWriter: `module <top> (<port>,\n    <port>);`.
_PORT_SEPARATOR = ",\n    "
_DECLARATION = re.compile(r"^ (?:input|output|inout)\b")
# A Verilog identifier, escaped (`\a[0] `) or simple, in any port-list layout.
_PORT_TOKEN = re.compile(r"\\\S+|[^\s,]+")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_GROUP_HEAD = re.compile(r'(\w+)\s*\(\s*"?([^")]*?)"?\s*\)\s*$')
# What a rewrite copies at a time.
_CHUNK = 1 << 20


@dataclass
class _Header:
    """Where `module`'s port list and its port declarations sit in a netlist, by byte offset: the port list between `start` and its `);` at `end`, and the run of declaration lines, newlines included, between `decl_start` and `decl_end` (empty without one)."""

    ports: str
    start: int
    end: int
    declarations: bytes = b""
    decl_start: int = 0
    decl_end: int = 0


def _decode(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        e.reason = f"{e.reason} in {path}"
        raise


def _scan(
    f: BinaryIO, module: str, path: str = "", declarations: bool = True
) -> _Header | None:
    """Read `f` up to `module`'s port list, then, with `declarations`, up to the end of the first run of port declaration lines after it; None without the port list.

    A name that is not a simple identifier is written escaped, `module \\my-model (`. The lines before the port list are matched undecoded; a decoding error in a line after them names `path`.
    """
    name = re.escape(module.encode())
    opening = re.compile(rb"module (?:" + name + rb"|\\" + name + rb") \(")
    offset = 0
    header = None
    parts: list[bytes] = []
    lines: list[bytes] = []
    for line in iter(f.readline, b""):
        here, offset = offset, offset + len(line)
        if header is None:
            match = opening.match(line)
            if match is None:
                continue
            header = _Header("", here + match.end(), -1)
            line = line[match.end() :]
            here = header.start
        if header.end < 0:
            close = line.find(b");")
            if close < 0:
                parts.append(line)
                continue
            parts.append(line[:close])
            header.end = here + close
            header.ports = _decode(b"".join(parts), path)
            if not declarations:
                return header
            continue
        if _DECLARATION.match(_decode(line, path)):
            if not lines:
                header.decl_start = here
            lines.append(line)
        elif lines:
            break
    if header is None or header.end < 0:
        return None
    if lines:
        header.declarations = b"".join(lines)
        header.decl_end = header.decl_start + len(header.declarations)
    return header


def _ports(header: _Header | None) -> list[str] | None:
    return None if header is None else _PORT_TOKEN.findall(header.ports)


def netlist_ports(text: str, module: str) -> list[str] | None:
    """Return the port names of `module` in a netlist, in order, or None without its port list.

    Any port-list layout is read, not only the one OpenROAD writes.
    """
    return _ports(_scan(io.BytesIO(text.encode()), module, declarations=False))


def _file_ports(path: str, module: str) -> list[str] | None:
    """`netlist_ports` of the netlist at `path`, read only up to the port list."""
    with open(path, "rb") as f:
        return _ports(_scan(f, module, path, declarations=False))


def _last(items: list, is_supply, key) -> list:
    """`items` with the supply ones moved last, sorted by `key`."""
    return [i for i in items if not is_supply(i)] + sorted(
        (i for i in items if is_supply(i)), key=key
    )


def _edits(header: _Header | None, supplies: set[str], path: str = "") -> list:
    """The `(start, end, bytes)` replacements that put `supplies` last, by name, in the port list and the port declarations `header` found; empty when the order is already so, or the port list is not in OpenSTA's layout."""
    if header is None or not supplies:
        return []
    ports = header.ports.split(_PORT_SEPARATOR) if header.ports else []
    if [p.strip() for p in ports] != _ports(header):
        return []
    ordered = _PORT_SEPARATOR.join(_last(ports, lambda p: p.strip() in supplies, str))
    edits = [(header.start, header.end, header.ports, ordered)]
    if header.declarations:
        text = _decode(header.declarations, path)
        lines = text.removesuffix("\n").split("\n")

        def _name(line: str) -> str:
            return line.rstrip().rstrip(";").split()[-1]

        lines = _last(lines, lambda ln: _name(ln) in supplies, _name)
        tail = "\n" if text.endswith("\n") else ""
        edits.append(
            (header.decl_start, header.decl_end, text, "\n".join(lines) + tail)
        )
    if all(old == new for _start, _end, old, new in edits):
        return []
    return [(start, end, new.encode()) for start, end, _old, new in edits]


def _copy(src: BinaryIO, dst: BinaryIO, size: int) -> None:
    """Copy `size` bytes from `src` to `dst`, `_CHUNK` at a time."""
    while size > 0:
        chunk = src.read(min(size, _CHUNK))
        if not chunk:
            return
        dst.write(chunk)
        size -= len(chunk)


def _apply(src: BinaryIO, dst: BinaryIO, edits: list) -> None:
    """Copy `src` from its start to `dst` with `edits` made."""
    src.seek(0)
    at = 0
    for start, end, text in edits:
        _copy(src, dst, start - at)
        dst.write(text)
        src.seek(end)
        at = end
    shutil.copyfileobj(src, dst, _CHUNK)


def order_netlist(text: str, module: str, supplies: set[str]) -> str:
    """Return `text` with `supplies` last, by name, in `module`'s port list and port declarations."""
    src = io.BytesIO(text.encode())
    edits = _edits(_scan(src, module), supplies)
    if not edits:
        return text
    dst = io.BytesIO()
    _apply(src, dst, edits)
    return dst.getvalue().decode()


def _order_netlist_file(path: str, module: str, supplies: set[str]) -> None:
    """Put `supplies` last in the netlist at `path` (`order_netlist`), rewriting it atomically only when the order changes."""
    with open(path, "rb") as src:
        edits = _edits(_scan(src, module, path), supplies, path)
        if edits:
            _replace(path, lambda dst: _apply(src, dst, edits))


def _braces(line: str) -> int:
    """The net brace depth change of a Liberty line, ignoring quoted text."""
    depth = 0
    quoted = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "{":
            depth += 1
        elif not quoted and ch == "}":
            depth -= 1
    return depth


def order_liberty(text: str, cell: str, supplies: set[str]) -> str:
    """Return `text` with the `pin` groups of `supplies` last, by name, in `cell`'s group."""
    if not supplies:
        return text
    lines = text.split("\n")
    opening = re.compile(rf'^\s*cell\s*\(\s*"?{re.escape(cell)}"?\s*\)\s*{{')
    start = next((i for i, ln in enumerate(lines) if opening.match(ln)), None)
    if start is None:
        return text
    pin = re.compile(r'^\s*pin\s*\(\s*"?([^")]+?)"?\s*\)\s*{')
    moved: list[tuple[str, list[str]]] = []
    kept: list[str] = []
    depth = _braces(lines[start])
    i = start + 1
    while i < len(lines) and depth > 0:
        match = pin.match(lines[i]) if depth == 1 else None
        if match and match.group(1) in supplies:
            group_depth = 0
            group: list[str] = []
            while i < len(lines):
                group.append(lines[i])
                group_depth += _braces(lines[i])
                i += 1
                if group_depth <= 0:
                    break
            moved.append((match.group(1), group))
            continue
        depth += _braces(lines[i])
        if depth <= 0:
            break
        kept.append(lines[i])
        i += 1
    if not moved or i >= len(lines):
        return text
    body = kept + [ln for _, group in sorted(moved) for ln in group]
    return "\n".join(lines[: start + 1] + body + lines[i:])


def liberty_pins(text: str, cell: str) -> list[str] | None:
    """Return the names of the `pin`, `bus` and `bundle` groups directly in `cell`'s group, in order, or None without the cell.

    Every brace outside a comment or quoted text counts, so a brace on the next line or a group closed on a shared line is read.
    """
    text = _COMMENT.sub(" ", text)
    opening = re.compile(rf'^\s*cell\s*\(\s*"?{re.escape(cell)}"?\s*\)\s*{{', re.M)
    match = opening.search(text)
    if match is None:
        return None
    names: list[str] = []
    depth = 1
    quoted = False
    head_start = match.end()
    for token in re.compile(r'[{};"]').finditer(text, match.end()):
        ch, i = token.group(), token.start()
        if ch == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if ch == "{":
            if depth == 1:
                group = _GROUP_HEAD.search(text[head_start:i])
                if group and group.group(1) in ("pin", "bus", "bundle"):
                    names.append(group.group(2))
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return names
        head_start = i + 1
    return None


def _supplies_last(names: list[str] | None, supplies: set[str]) -> bool:
    """Whether `names` ends with the `supplies` it holds, sorted by name."""
    if names is None:
        return False
    present = [n for n in names if n in supplies]
    return names[len(names) - len(present) :] == sorted(present)


@dataclass
class SupplyOrder:
    """What `canonicalise` did: the supply ports it found, and each file it could not order or verify with the reason."""

    supplies: list[str] = field(default_factory=list)
    unverified: list[tuple[str, str]] = field(default_factory=list)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError as e:
        e.reason = f"{e.reason} in {path}"
        raise


def _replace(path: str, write) -> None:
    """Replace `path` with what `write(file)` writes to a binary temporary file, so a failed write leaves the original."""
    tmp = os.path.join(os.path.dirname(path), atomic_tmp_name(os.path.basename(path)))
    try:
        with open(tmp, "wb") as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write(path: str, text: str) -> None:
    _replace(path, lambda f: f.write(text.encode("utf-8")))


def canonicalise(
    logical: str, powered: str, module: str, liberty: str | None = None
) -> SupplyOrder:
    """Move the supply ports last, by name, in the powered netlist and, when given, the timing model.

    A file is rewritten only when its order changes, and then atomically; the netlists are read only up to the top module's port declarations, and the powered one is rewritten as a stream (`_order_netlist_file`). Without the logical netlist's or the powered netlist's port list nothing is rewritten. Each file left out of order, or not checked, is listed in `unverified`. A read, write or decoding error raises `OSError` or `UnicodeDecodeError`.
    """
    result = SupplyOrder()
    ports = {}
    for path in (logical, powered):
        if not os.path.isfile(path):
            result.unverified.append((path, "missing; supply order unchanged"))
            return result
        ports[path] = _file_ports(path, module)
        if ports[path] is None:
            result.unverified.append(
                (path, f"no port list of module {module}; supply order unchanged")
            )
            return result
    signals = set(ports[logical])
    supplies = {p for p in ports[powered] if p not in signals}
    result.supplies = sorted(supplies)
    if not supplies:
        return result
    _order_netlist_file(powered, module, supplies)
    if not _supplies_last(_file_ports(powered, module), supplies):
        result.unverified.append(
            (powered, f"supply ports {', '.join(result.supplies)} are not last")
        )
    if liberty is not None and os.path.isfile(liberty):
        model = _read(liberty)
        ordered = order_liberty(model, module, supplies)
        if ordered != model:
            _write(liberty, ordered)
        pins = liberty_pins(ordered, module)
        if pins is None:
            result.unverified.append((liberty, f"no cell group {module}"))
        elif not _supplies_last(pins, supplies):
            result.unverified.append(
                (liberty, f"supply pins {', '.join(result.supplies)} are not last")
            )
    return result
