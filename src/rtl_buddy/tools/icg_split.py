"""Split each clock-gating cell that drives more than a limit of flops into copies of near-equal share.

A Yosys script runs this file between ``write_rtlil`` and ``read_rtlil`` after ``clockgate`` (``clock-gating-max-flops``)::

    python -I icg_split.py <in.il> <out.il> <max_flops>

An ICG is a cell ``clockgate`` named (``$auto$clockgate.cc:...``). One whose gated clock drives n > max cell input pins becomes ceil(n/max) cells: the original and copies of it with the same type, parameters, attributes and input connections, each driving its own net. Loads are dealt in order of (cell name, port, bit), the first n mod k cells taking one more than the rest, so the result is deterministic. Each split prints one line to stdout, which Yosys logs; an ICG left over-full prints a line starting with :data:`WARNING_PREFIX`. The input file is removed once read.

Only the lines of a split change: new wire and cell statements, and the connections of the loads that move. Every other line is written back as read, so a design with nothing to split comes back byte-identical.

The module is stdlib-only, so it runs under ``-I`` without the package on the path.
"""

from __future__ import annotations

import math
import os
import re
import sys

ICG_PREFIX = "$auto$clockgate.cc:"
#: Start of the line printed for an ICG that drives more than the limit but is not split.
WARNING_PREFIX = "icg_split: warning: "

_OPENS = frozenset({"module", "cell", "process", "switch"})
_INDEX_RE = re.compile(r"\[(\d+)(?::(\d+))?\]$")
_CONST_RE = re.compile(r"(\d+)'([01xzm-]*)$")
_INT_RE = re.compile(r"-?\d+$")


def _parse_sig(tokens: list[str], pos: int, widths: dict[str, int]):
    """Parse one RTLIL sigspec from ``tokens[pos:]``; return its bits, LSB first, and the next position.

    A wire bit is ``(wire, index)``; a constant bit is its character.
    """
    tok = tokens[pos]
    pos += 1
    if tok == "{":
        parts = []
        while tokens[pos] != "}":
            part, pos = _parse_sig(tokens, pos, widths)
            parts.append(part)
        pos += 1
        bits = [bit for part in reversed(parts) for bit in part]
    elif tok[0] in "\\$":
        bits = [(tok, i) for i in range(widths[tok])]
    elif m := _CONST_RE.match(tok):
        bits = list(reversed(m.group(2)))
    elif _INT_RE.match(tok):
        value = int(tok) & 0xFFFFFFFF
        bits = ["1" if value >> i & 1 else "0" for i in range(32)]
    else:
        raise ValueError(f"unexpected sigspec token {tok!r}")
    while pos < len(tokens) and (m := _INDEX_RE.match(tokens[pos])):
        hi = int(m.group(1))
        lo = int(m.group(2)) if m.group(2) is not None else hi
        bits = bits[lo : hi + 1]
        pos += 1
    return bits, pos


def _format_bit(bit) -> str:
    return f"{bit[0]} [{bit[1]}]" if isinstance(bit, tuple) else f"1'{bit}"


def _format_sig(bits: list, widths: dict[str, int]) -> str:
    if len(bits) == 1:
        bit = bits[0]
        if isinstance(bit, tuple) and bit[1] == 0 and widths.get(bit[0]) == 1:
            return bit[0]
        return _format_bit(bit)
    return "{ " + " ".join(_format_bit(b) for b in reversed(bits)) + " }"


class _Module:
    """The statements of one RTLIL module that a split reads or rewrites, by line number."""

    def __init__(self, name: str, start: int):
        self.name = name
        self.start = start
        self.widths: dict[str, int] = {}
        self.outputs: dict[str, int] = {}
        # name -> [type, header line, end line, first attribute line, {port: (line, sig text)}]
        self.cells: dict[str, list] = {}
        self.connects: list[str] = []
        self.has_process = False


def _scan(lines: list[str]) -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    stack: list[str] = []
    module: _Module | None = None
    cell: list | None = None
    attr_start: int | None = None
    for no, line in enumerate(lines):
        words = line.split()
        if not words:
            continue
        head = words[0]
        if head == "end":
            closed = stack.pop() if stack else None
            if closed == "cell" and cell is not None:
                cell[2] = no
                cell = None
            elif closed == "module":
                module = None
            continue
        if head == "attribute":
            if attr_start is None:
                attr_start = no
            continue
        first_attr, attr_start = attr_start, None
        if head in _OPENS:
            stack.append(head)
        if head == "module":
            module = modules[words[1]] = _Module(words[1], no)
        elif module is None or len(stack) > 2 or (stack and stack[-1] == "switch"):
            continue
        elif head == "process":
            module.has_process = True
        elif head == "wire" and len(stack) == 1:
            width = int(words[words.index("width") + 1]) if "width" in words else 1
            module.widths[words[-1]] = width
            if "output" in words:
                module.outputs[words[-1]] = width
        elif head == "cell":
            cell = module.cells[words[2]] = [
                words[1],
                no,
                None,
                no if first_attr is None else first_attr,
                {},
            ]
        elif head == "connect" and len(stack) == 2 and cell is not None:
            cell[4][words[1]] = (no, " ".join(words[2:]))
        elif head == "connect" and len(stack) == 1:
            module.connects.append(" ".join(words[1:]))
    return modules


def _shares(count: int, max_flops: int) -> list[int]:
    """Near-equal shares of ``count`` loads, none above ``max_flops``, larger first."""
    groups = math.ceil(count / max_flops)
    base, extra = divmod(count, groups)
    return [base + 1] * extra + [base] * (groups - extra)


def _display(name: str) -> str:
    return name[1:] if name.startswith("\\") else name


def split_module(
    lines: list[str], module: _Module, types: dict[str, _Module], max_flops: int
) -> tuple[list[tuple[str, int, int]], list[str], dict[int, list[str]]]:
    """Plan the splits of one module's ICGs.

    Returns ``(icg, loads, cells)`` for each ICG split, a warning for each over-full ICG left alone,
    and the edits: line number to the lines that replace it.
    """
    icgs = sorted(name for name in module.cells if name.startswith(ICG_PREFIX))
    if not icgs:
        return [], [], {}
    mod = _display(module.name)
    if module.has_process:
        return [], [f"{mod}: has processes, its ICGs are not split"], {}
    widths = module.widths

    parent: dict = {}

    def find(bit):
        while (up := parent.get(bit, bit)) != bit:
            parent[bit] = parent.get(up, up)
            bit = up
        return bit

    for text in module.connects:
        tokens = text.split()
        lhs, pos = _parse_sig(tokens, 0, widths)
        rhs, _ = _parse_sig(tokens, pos, widths)
        for a, b in zip(lhs, rhs):
            if isinstance(a, tuple) and isinstance(b, tuple):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

    sigs: dict[tuple[str, str], list] = {}
    loads: dict = {}
    for name, (ctype, _h, _e, _a, ports) in module.cells.items():
        outputs = types[ctype].outputs if ctype in types else {}
        for port, (_line, text) in ports.items():
            bits, _ = _parse_sig(text.split(), 0, widths)
            sigs[(name, port)] = bits
            if port in outputs:
                continue
            for index, bit in enumerate(bits):
                if isinstance(bit, tuple):
                    loads.setdefault(find(bit), []).append((name, port, index))

    done: list[tuple[str, int, int]] = []
    warnings: list[str] = []
    edits: dict[int, list[str]] = {}
    new_wires: list[str] = []
    moved: dict[tuple[str, str], dict[int, tuple[str, int]]] = {}
    for icg in icgs:
        ctype, header, end, first, ports = module.cells[icg]
        if ctype not in types:
            warnings.append(
                f"{mod} {icg}: cell type {_display(ctype)} has no ports in the design, not split"
            )
            continue
        outputs = [p for p in types[ctype].outputs if p in ports]
        out_bits = [bit for port in outputs for bit in sigs[(icg, port)]]
        fanout = max(
            (len(loads.get(find(b), ())) for b in out_bits if isinstance(b, tuple)),
            default=0,
        )
        if (
            len(outputs) != 1
            or len(out_bits) != 1
            or not isinstance(out_bits[0], tuple)
        ):
            if fanout > max_flops:
                warnings.append(
                    f"{mod} {icg}: {_display(ctype)} has {len(out_bits)} output bits, "
                    f"so its {fanout} loads are not split"
                )
            continue
        out_port = outputs[0]
        sinks = sorted(loads.get(find(out_bits[0]), ()))
        if len(sinks) <= max_flops:
            continue
        shares = _shares(len(sinks), max_flops)
        start = shares[0]
        out_line = ports[out_port][0]
        indent = lines[out_line][: len(lines[out_line]) - len(lines[out_line].lstrip())]
        copies: list[str] = []
        for copy_index, share in enumerate(shares[1:], start=1):
            name = f"{icg}$split{copy_index}"
            wire = f"{name}$gclk"
            if name in module.cells or wire in widths:
                raise ValueError(f"{mod}: {name} or {wire} already exists")
            widths[wire] = 1
            new_wires.append(wire)
            for no in range(first, end + 1):
                if no == header:
                    copies.append(lines[no].replace(f" {icg}", f" {name}", 1))
                elif no == out_line:
                    copies.append(f"{indent}connect {out_port} {wire}\n")
                else:
                    copies.append(lines[no])
            for cell, port, index in sinks[start : start + share]:
                moved.setdefault((cell, port), {})[index] = (wire, 0)
            start += share
        edits[end] = [lines[end], *copies]
        done.append((icg, len(sinks), len(shares)))

    for (cell, port), changes in moved.items():
        bits = [changes.get(i, bit) for i, bit in enumerate(sigs[(cell, port)])]
        no = module.cells[cell][4][port][0]
        line = lines[no]
        indent = line[: len(line) - len(line.lstrip())]
        replaced = f"{indent}connect {port} {_format_sig(bits, widths)}\n"
        edits[no] = [replaced]
    if new_wires:
        added = [f"  wire {wire}\n" for wire in new_wires]
        edits[module.start] = [lines[module.start], *added]
    return done, warnings, edits


def split_design(text: str, max_flops: int) -> tuple[str, list[str]]:
    """Split the design's over-full ICGs; return the new RTLIL text and the lines to print."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
        trailing = False
    else:
        trailing = True
    modules = _scan(lines)
    report: list[str] = []
    edits: dict[int, list[str]] = {}
    for name in sorted(modules):
        done, warnings, module_edits = split_module(
            lines, modules[name], modules, max_flops
        )
        edits.update(module_edits)
        for icg, count, parts in done:
            report.append(
                f"icg_split: {_display(name)} {icg}: {count} loads over {parts} cells"
            )
        report.extend(WARNING_PREFIX + w for w in warnings)
    if not edits:
        return text, report
    out = []
    for no, line in enumerate(lines):
        out.extend(edits.get(no, [line]))
    result = "".join(out)
    return (result if trailing else result[:-1]), report


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write("usage: icg_split.py <in.il> <out.il> <max_flops>\n")
        return 2
    source, target, max_flops = argv[1], argv[2], int(argv[3])
    if max_flops < 1:
        sys.stderr.write(f"icg_split: max_flops must be positive, got {max_flops}\n")
        return 2
    with open(source, encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    os.remove(source)
    result, report = split_design(text, max_flops)
    for line in report:
        print(line)
    with open(target, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv))
