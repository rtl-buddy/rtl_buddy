"""Cone-of-influence (COI) coverage for ``rb fpv``.

A separate yosys run over the same design and properties reports the total cell count and
the cells in the union of every assertion's COI (found with `%ci*`, the transitive
input-cone operator). Coverage is COI cells divided by total cells; logic outside the COI
is not verified by any property. The result is design-wide with a per-module rollup, not per property.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..logging_utils import log_event
from ..process_utils import run_managed_process


# `stat` prints one `=== <module> ===` block per module. It appends `(partially selected)`
# to the name under a partial selection, so the name pattern allows spaces.
_STAT_BLOCK_RE = re.compile(
    r"===\s*(?P<module>[^\n=]+?)\s*===\s*\n"
    r"(?P<body>.*?)(?====\s*[^\n=]+\s*===|\Z)",
    re.DOTALL,
)
# Match the standalone "<N> cells" line, not the header or the per-cell-type breakdown.
_CELLS_RE = re.compile(r"^\s*(?P<n>\d+)\s+cells\s*$", re.MULTILINE)
_WIRES_RE = re.compile(r"^\s*(?P<n>\d+)\s+wires\s*$", re.MULTILINE)


# Sentinels logged before each `stat` so its block can be located.
_MARK_TOTAL = "RTL_BUDDY_COI_TOTAL"
_MARK_SELECTED = "RTL_BUDDY_COI_SELECTED"
_MARK_ASSUMES_TOTAL = "RTL_BUDDY_ASSUMES_TOTAL"
_MARK_ASSUMES_IN_COI = "RTL_BUDDY_ASSUMES_IN_COI"

_ALL_MARKERS = (
    _MARK_TOTAL,
    _MARK_SELECTED,
    _MARK_ASSUMES_TOTAL,
    _MARK_ASSUMES_IN_COI,
)


def render_slang_read(
    top: str,
    incdirs: list[str],
    sources: list[str],
    defines: list[str] | None = None,
    params: list[tuple[str, str]] | None = None,
) -> str:
    """Render the ``read_slang`` command shared by the proof (``SbyFpv._render_sby``) and the COI walk.

    Both must parse the same design, so callers vary only ``sources`` (basenames for the
    sby workdir, full paths for COI).

    - ``--single-unit`` compiles the filelist as one unit, so macros and compilation-unit
      ``bind`` reach across files.
    - Include dirs are ``-I`` flags here because ``read_slang`` ignores
      ``verilog_defaults -add -I``.
    - ``--no-synthesis-define -DFORMAL=1`` mirrors ``read -formal``. It comes before the
      model's defines because yosys-slang keeps the first definition of a macro.
    - ``+define+NAME[=VALUE]`` entries become ``-D`` flags verbatim. Yosys splits script
      lines on whitespace and keeps inner quotes, so a value containing whitespace cannot
      be expressed; ``SbyFpv._parse_filelist`` drops those with a warning.
    - Top parameter overrides become ``-G NAME=VALUE``. ``chparam`` fails on this
      frontend because elaboration is eager.

    Source paths go through ``shlex.quote``, which yosys does not honour, so a path with
    whitespace does not work (see ``docs/known-issues.md``).
    """
    inc_args = "".join(f" -I {shlex.quote(inc)}" for inc in incdirs)
    def_args = "".join(f" -D{d}" for d in (defines or []))
    # Not shlex.quote()d: yosys passes inner quotes to slang verbatim, and string
    # parameters need their own (`-G MODE="small"` works, `-G MODE=small` is rejected).
    param_args = "".join(f" -G {name}={value}" for name, value in (params or []))
    src_args = " ".join(shlex.quote(s) for s in sources)
    return (
        f"read_slang --top {top} --single-unit{inc_args} "
        f"--no-synthesis-define -DFORMAL=1{def_args}{param_args} {src_args}"
    )


def render_chparam(top: str, params: list[tuple[str, str]] | None) -> list[str]:
    """Render ``chparam`` lines for the yosys verilog frontend, to go after the reads and before ``prep``."""
    return [f"chparam -set {name} {value} {top}" for name, value in (params or [])]


def build_yosys_script(
    *,
    sources: list[str],
    incdirs: list[str],
    properties: list[str],
    constraints: str | None,
    top: str,
    frontend: str = "verilog",
    plugin_path: str | None = None,
    defines: list[str] | None = None,
    params: list[tuple[str, str]] | None = None,
) -> str:
    """Render the yosys script for the COI analysis.

    Reads sources, constraints, then properties, as the sby script does. ``frontend`` must
    match the one the proof used, since `bind` resolution differs between frontends.
    """
    lines: list[str] = []
    if frontend == "slang":
        if not plugin_path:
            raise ValueError(
                "fpv_coi: frontend='slang' requires a non-empty plugin_path"
            )
        lines.append(f"plugin -i {plugin_path}")
    if frontend != "slang":
        for inc in incdirs:
            lines.append(f"verilog_defaults -add -I {inc}")
        for define in defines or []:
            lines.append(f"verilog_defaults -add -D{define}")
    constraint_files = [constraints] if constraints else []
    all_files = list(sources) + constraint_files + list(properties)
    if frontend == "slang":
        lines.append(render_slang_read(top, incdirs, all_files, defines, params))
    else:
        for src in all_files:
            lines.append(f"read -sv -formal {src}")
        lines.extend(render_chparam(top, params))
    # Flatten: `bind` property modules are submodules, and `stat` counts only the top,
    # so unflattened `$assert` cells would be reported as zero.
    lines.append(f"prep -flatten -top {top}")

    lines.append(f"log === {_MARK_TOTAL} ===")
    lines.append("stat")

    # Newer yosys emits `$check` cells with a FLAVOR parameter, older yosys emits `$assert`
    # and `$assume`; select both. `%ci*` repeats `%ci` to a fixpoint (plain `%ci` is one step).
    lines.append("select -set property_cells t:$assert t:$check r:FLAVOR=assert %i %u")
    lines.append("select -set property_coi @property_cells %ci*")
    lines.append("select @property_coi")
    lines.append(f"log === {_MARK_SELECTED} ===")
    lines.append("stat")

    # Dead-assume analysis: count all assumes, then those constraining logic in the assertion COI.
    lines.append("select -set all_assumes t:$assume t:$check r:FLAVOR=assume %i %u")
    lines.append("select @all_assumes")
    lines.append(f"log === {_MARK_ASSUMES_TOTAL} ===")
    lines.append("stat")
    # An assume is a sink, so it is never in an input cone; walk forward from the COI
    # instead. The walk must skip clock and reset ports (`$check` TRG, FF CLK/ARST/SRST/...),
    # or every assume would count as used. Keep the cell list in step with the FF and memory
    # types `prep` can emit.
    lines.append(
        "select @property_coi %co*"
        ":-$check[TRG]"
        ":-$dff,$dffe,$sdff,$sdffe,$sdffce,$adff,$adffe,$aldff,$aldffe,"
        "$dffsr,$dffsre,$memrd,$memrd_v2,$memwr,$memwr_v2[CLK]"
        ":-$mem,$mem_v2[RD_CLK,WR_CLK]"
        ":-$adff,$adffe,$aldff,$aldffe[ARST]"
        ":-$sdff,$sdffe,$sdffce[SRST]"
        ":-$dffsr,$dffsre[SET,CLR]"
        ":-$aldff,$aldffe[ALOAD]"
        " @all_assumes %i"
    )
    lines.append(f"log === {_MARK_ASSUMES_IN_COI} ===")
    lines.append("stat")
    return "\n".join(lines) + "\n"


def parse_stat_blocks(log_text: str) -> dict[str, dict[str, dict[str, int]]]:
    """Parse the marked `stat` blocks of a yosys log into ``{marker: {module: {"cells": N, "wires": M}}}``.

    A missing marker maps to an empty dict.
    """
    out: dict[str, dict[str, dict[str, int]]] = {m: {} for m in _ALL_MARKERS}

    # Each section runs to the next marker in log order.
    marker_positions: list[tuple[int, str]] = []
    for marker in _ALL_MARKERS:
        idx = log_text.find(f"=== {marker} ===")
        if idx != -1:
            marker_positions.append((idx, marker))
    marker_positions.sort()

    sections: dict[str, str] = {}
    for i, (pos, marker) in enumerate(marker_positions):
        end = marker_positions[i + 1][0] if i + 1 < len(marker_positions) else None
        sections[marker] = log_text[pos:end]

    for marker, section in sections.items():
        for match in _STAT_BLOCK_RE.finditer(section):
            raw_module = match.group("module").strip()
            if raw_module in _ALL_MARKERS:
                continue
            # Drop "(partially selected)" so selected and total stats share a module key.
            module = raw_module.split(" (")[0].strip()
            body = match.group("body")
            cells_m = _CELLS_RE.search(body)
            wires_m = _WIRES_RE.search(body)
            if cells_m is None:
                continue
            out[marker][module] = {
                "cells": int(cells_m.group("n")),
                "wires": int(wires_m.group("n")) if wires_m else 0,
            }
    return out


def compute_coverage(
    blocks: dict[str, dict[str, dict[str, int]]],
) -> dict:
    """Compute aggregate and per-module COI coverage, plus assume counts (total, used, dead)."""
    total_blocks = blocks.get(_MARK_TOTAL, {})
    coi_blocks = blocks.get(_MARK_SELECTED, {})
    assumes_total_blocks = blocks.get(_MARK_ASSUMES_TOTAL, {})
    assumes_in_coi_blocks = blocks.get(_MARK_ASSUMES_IN_COI, {})

    per_module = {}
    total_cells = 0
    coi_cells = 0
    for module, stats in total_blocks.items():
        cells = stats["cells"]
        covered = coi_blocks.get(module, {}).get("cells", 0)
        total_cells += cells
        coi_cells += covered
        per_module[module] = {
            "cells": cells,
            "coi_cells": covered,
            "percent": (covered / cells * 100.0) if cells else 0.0,
        }

    percent = (coi_cells / total_cells * 100.0) if total_cells else 0.0

    assumes_total = sum(s["cells"] for s in assumes_total_blocks.values())
    # `in_assert_coi` counts assumes whose fan-in intersects the COI, not assumes inside it.
    assumes_used = sum(s["cells"] for s in assumes_in_coi_blocks.values())
    assumes_dead = max(assumes_total - assumes_used, 0)

    return {
        "total_cells": total_cells,
        "coi_cells": coi_cells,
        "percent": percent,
        "per_module": per_module,
        "assumes": {
            "total": assumes_total,
            "in_assert_coi": assumes_used,
            "dead": assumes_dead,
        },
    }


def run_coi_analysis(
    *,
    name: str,
    yosys_exe: str,
    sources: list[str],
    incdirs: list[str],
    properties: list[str],
    constraints: str | None,
    top: str,
    script_path: str,
    log_path: str,
    frontend: str = "verilog",
    plugin_path: str | None = None,
    defines: list[str] | None = None,
    params: list[tuple[str, str]] | None = None,
) -> dict | None:
    """Run yosys and return the parsed coverage summary.

    Returns None, with a logged warning, when yosys is missing or fails, so the FPV run continues without COI data.
    """
    script = build_yosys_script(
        sources=sources,
        incdirs=incdirs,
        properties=properties,
        constraints=constraints,
        top=top,
        frontend=frontend,
        plugin_path=plugin_path,
        defines=defines,
        params=params,
    )
    Path(script_path).write_text(script)
    cmd = [yosys_exe, "-s", script_path]
    work_dir = os.path.dirname(os.path.abspath(script_path))
    try:
        with open(log_path, "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            result = run_managed_process(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=work_dir,
            )
    except FileNotFoundError:
        log_event(
            logger,
            logging.WARNING,
            "fpv.coi_yosys_missing",
            verification=name,
            executable=yosys_exe,
        )
        return None

    if result.returncode != 0:
        log_event(
            logger,
            logging.WARNING,
            "fpv.coi_yosys_failed",
            verification=name,
            returncode=result.returncode,
            log=log_path,
        )
        return None

    if not os.path.isfile(log_path):
        return None
    log_text = Path(log_path).read_text()
    blocks = parse_stat_blocks(log_text)
    summary = compute_coverage(blocks)
    summary["log"] = log_path
    return summary
