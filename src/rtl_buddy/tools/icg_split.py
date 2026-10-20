"""Split each clock-gating cell that drives more than a limit of flops into copies of near-equal share.

A Yosys script runs this file between ``write_json`` and ``read_json`` after ``clockgate`` (``clock-gating-max-flops``)::

    python -I icg_split.py <in.json> <out.json> <max_flops>

An ICG is a cell ``clockgate`` named (``$auto$clockgate.cc:...``). One whose gated clock drives n > max cell input pins becomes ceil(n/max) cells: the original and copies of it with the same type, parameters, attributes and input connections, each driving its own net. Loads are dealt in order of (cell name, port, bit), the first n mod k cells taking one more than the rest, so the result is deterministic. Each split prints one line to stdout, which Yosys logs.

The module is stdlib-only, so it runs under ``-I`` without the package on the path.
"""

from __future__ import annotations

import copy
import json
import math
import sys

ICG_PREFIX = "$auto$clockgate.cc:"


def _output_port(cell: dict) -> str | None:
    """The cell's one single-bit output port, or None."""
    outputs = [
        port
        for port, direction in (cell.get("port_directions") or {}).items()
        if direction == "output"
    ]
    if len(outputs) != 1 or len(cell["connections"].get(outputs[0], ())) != 1:
        return None
    return outputs[0]


def _shares(count: int, max_flops: int) -> list[int]:
    """Near-equal shares of ``count`` loads, none above ``max_flops``, larger first."""
    groups = math.ceil(count / max_flops)
    base, extra = divmod(count, groups)
    return [base + 1] * extra + [base] * (groups - extra)


def split_module(module: dict, max_flops: int) -> list[tuple[str, int, int]]:
    """Split the module's ICGs in place; return ``(icg, loads, cells)`` for each one split."""
    cells = module.get("cells") or {}
    icgs = {
        name: port
        for name in sorted(cells)
        if name.startswith(ICG_PREFIX) and (port := _output_port(cells[name]))
    }
    if not icgs:
        return []
    gclk = {cells[name]["connections"][port][0]: name for name, port in icgs.items()}
    loads: dict[str, list[tuple[str, str, int]]] = {name: [] for name in icgs}
    used = 1
    for name, cell in cells.items():
        directions = cell.get("port_directions") or {}
        for port, bits in cell["connections"].items():
            for index, bit in enumerate(bits):
                if isinstance(bit, int):
                    used = max(used, bit)
                    if bit in gclk and directions.get(port) != "output":
                        loads[gclk[bit]].append((name, port, index))
    for net in (module.get("netnames") or {}).values():
        used = max([used, *(b for b in net["bits"] if isinstance(b, int))])
    for port in (module.get("ports") or {}).values():
        used = max([used, *(b for b in port["bits"] if isinstance(b, int))])

    netnames = module.setdefault("netnames", {})
    done = []
    for icg, out_port in icgs.items():
        sinks = sorted(loads[icg])
        if len(sinks) <= max_flops:
            continue
        shares = _shares(len(sinks), max_flops)
        start = shares[0]
        for copy_index, share in enumerate(shares[1:], start=1):
            used += 1
            name = f"{icg}$split{copy_index}"
            twin = copy.deepcopy(cells[icg])
            twin["connections"][out_port] = [used]
            cells[name] = twin
            netnames[f"{name}$gclk"] = {
                "hide_name": 1,
                "bits": [used],
                "attributes": {},
            }
            for cell, port, index in sinks[start : start + share]:
                cells[cell]["connections"][port][index] = used
            start += share
        done.append((icg, len(sinks), len(shares)))
    return done


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write("usage: icg_split.py <in.json> <out.json> <max_flops>\n")
        return 2
    source, target, max_flops = argv[1], argv[2], int(argv[3])
    if max_flops < 1:
        sys.stderr.write(f"icg_split: max_flops must be positive, got {max_flops}\n")
        return 2
    with open(source) as f:
        design = json.load(f)
    for module_name in sorted(design.get("modules") or {}):
        module = design["modules"][module_name]
        for icg, count, parts in split_module(module, max_flops):
            print(f"icg_split: {module_name} {icg}: {count} loads over {parts} cells")
    with open(target, "w") as f:
        json.dump(design, f)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv))
