"""Supply-port order in a routed run's powered netlist and a hardened block's timing model.

OpenROAD writes a top's ports in the order of its timing graph's top cell. A session that ran `pdngen` appends the supply ports after every other port; a session that read the database puts them among the scalar ports, before the buses. rb moves them last, sorted by name, in `<top>.routed.pwr.v` and in the abstract's `<top>.lib`, so both are the same whether the run was one session, staged, resumed or dispatched (#889, #890). The supply ports are the ports the powered netlist declares and the logical `<top>.routed.v` does not.
"""

import re
from pathlib import Path

# OpenSTA's VerilogWriter: `module <top> (<port>,\n    <port>);`.
_PORT_SEPARATOR = ",\n    "
_DECLARATION = re.compile(r"^ (?:input|output|inout)\b")


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


def netlist_ports(text: str, module: str) -> list[str]:
    """Return the port names of `module` in a netlist OpenROAD wrote, in order."""
    header = _header(text, module)
    return header[2] if header else []


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
    text = (
        text[:start]
        + _PORT_SEPARATOR.join(_last(ports, supplies.__contains__, str))
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


def canonicalise(
    logical: str, powered: str, module: str, liberty: str | None = None
) -> list[str]:
    """Move the supply ports last, by name, in the powered netlist and, when given, the timing model; return them.

    The files are rewritten in place only when their order changes. A missing netlist leaves everything as it is.
    """
    try:
        logical_text = Path(logical).read_text()
        powered_text = Path(powered).read_text()
    except OSError:
        return []
    signals = set(netlist_ports(logical_text, module))
    supplies = {p for p in netlist_ports(powered_text, module) if p not in signals}
    if not supplies:
        return []
    ordered = order_netlist(powered_text, module, supplies)
    if ordered != powered_text:
        Path(powered).write_text(ordered)
    if liberty is not None and Path(liberty).is_file():
        model = Path(liberty).read_text()
        ordered = order_liberty(model, module, supplies)
        if ordered != model:
            Path(liberty).write_text(ordered)
    return sorted(supplies)
