"""Supply-port order in a routed run's powered netlist and a hardened block's timing model.

OpenROAD writes a top's ports in the order of its timing graph's top cell. A session that ran `pdngen` appends the supply ports after every other port; a session that read the database puts them among the scalar ports, before the buses. rb moves them last, sorted by name, in `<top>.routed.pwr.v` and in the abstract's `<top>.lib`, so both are the same whether the run was one session, staged, resumed or dispatched (#889, #890). The supply ports are the ports the powered netlist declares and the logical `<top>.routed.v` does not.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .artifact_paths import atomic_tmp_name

# OpenSTA's VerilogWriter: `module <top> (<port>,\n    <port>);`.
_PORT_SEPARATOR = ",\n    "
_DECLARATION = re.compile(r"^ (?:input|output|inout)\b")
# A Verilog identifier, escaped (`\a[0] `) or simple, in any port-list layout.
_PORT_TOKEN = re.compile(r"\\\S+|[^\s,]+")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_GROUP_HEAD = re.compile(r'(\w+)\s*\(\s*"?([^")]*?)"?\s*\)\s*$')


def _header(text: str, module: str) -> tuple[int, int, list[str]] | None:
    """Return the span of `module`'s port list in `text` and its port names, or None without one."""
    match = re.search(rf"^module {re.escape(module)} \(", text, re.M)
    if match is None:
        return None
    end = text.find(");", match.end())
    if end < 0:
        return None
    body = text[match.end() : end]
    return match.end(), end, body.split(_PORT_SEPARATOR) if body else []


def netlist_ports(text: str, module: str) -> list[str] | None:
    """Return the port names of `module` in a netlist, in order, or None without its port list.

    Any port-list layout is read, not only the one OpenROAD writes.
    """
    header = _header(text, module)
    if header is None:
        return None
    return _PORT_TOKEN.findall(text[header[0] : header[1]])


def _last(items: list, is_supply, key) -> list:
    """`items` with the supply ones moved last, sorted by `key`."""
    return [i for i in items if not is_supply(i)] + sorted(
        (i for i in items if is_supply(i)), key=key
    )


def order_netlist(text: str, module: str, supplies: set[str]) -> str:
    """Return `text` with `supplies` last, by name, in `module`'s port list and port declarations."""
    header = _header(text, module)
    if header is None or not supplies:
        return text
    start, end, ports = header
    if [p.strip() for p in ports] != netlist_ports(text, module):
        return text
    text = (
        text[:start]
        + _PORT_SEPARATOR.join(_last(ports, lambda p: p.strip() in supplies, str))
        + text[end:]
    )
    lines = text[start:].split("\n")
    first = next((i for i, ln in enumerate(lines) if _DECLARATION.match(ln)), None)
    if first is None:
        return text
    last = first
    while last < len(lines) and _DECLARATION.match(lines[last]):
        last += 1

    def _name(line: str) -> str:
        return line.rstrip().rstrip(";").split()[-1]

    lines[first:last] = _last(
        lines[first:last], lambda ln: _name(ln) in supplies, _name
    )
    return text[:start] + "\n".join(lines)


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
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    """Replace `path` with `text` through a temporary file, so a failed write leaves the original."""
    tmp = os.path.join(os.path.dirname(path), atomic_tmp_name(os.path.basename(path)))
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def canonicalise(
    logical: str, powered: str, module: str, liberty: str | None = None
) -> SupplyOrder:
    """Move the supply ports last, by name, in the powered netlist and, when given, the timing model.

    A file is rewritten only when its order changes, and then atomically. Without the logical netlist's or the powered netlist's port list nothing is rewritten. Each file left out of order, or not checked, is listed in `unverified`. A read, write or decoding error raises `OSError` or `UnicodeDecodeError`.
    """
    result = SupplyOrder()
    texts = {}
    for path in (logical, powered):
        if not os.path.isfile(path):
            result.unverified.append((path, "missing; supply order unchanged"))
            return result
        texts[path] = _read(path)
        if netlist_ports(texts[path], module) is None:
            result.unverified.append(
                (path, f"no port list of module {module}; supply order unchanged")
            )
            return result
    signals = set(netlist_ports(texts[logical], module))
    supplies = {p for p in netlist_ports(texts[powered], module) if p not in signals}
    result.supplies = sorted(supplies)
    if not supplies:
        return result
    ordered = order_netlist(texts[powered], module, supplies)
    if ordered != texts[powered]:
        _write(powered, ordered)
    if not _supplies_last(netlist_ports(ordered, module), supplies):
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
