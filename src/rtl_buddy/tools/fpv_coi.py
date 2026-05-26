"""Cone-of-influence (COI) coverage for ``rb fpv``.

After the primary sby pass, run a separate yosys invocation against
the same design + properties to compute:

- the total cell count (per module + design-wide)
- the union of every `$assert` cell's COI (the cells that any
  assertion's logic transitively depends on)
- coverage = COI_cells / total_cells

Logic outside any property's COI is provably unverified by the
property set — this gives users a direct "what's still uncovered"
signal that simulation coverage doesn't reach.

The pass uses yosys's existing selection language: `t:$assert %ci*`
selects every cell reachable backward through the design from an
`$assert` cell (`%ci*` is the transitive input-cone operator — it
repeats `%ci` until fixpoint).

Scope today:

- Aggregate (design-wide) coverage + per-module rollup. Per-property
  COI is not split out — yosys's `$assert` cells don't carry an
  identifier that maps back to source-level property names without
  a custom frontend pass.
- Uses the default `yosys` on PATH. A configurable path lands when
  the next backend (jaspergold / vcformal) needs one too.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..logging_utils import log_event
from ..process_utils import run_managed_process


# yosys's `stat` output uses one block per module:
#   === <module> ===
#
#      Number of wires: 12
#      Number of cells: 8
#        $assert  3
#        ...
# Module header: `=== <name> ===`. yosys's `stat` decorates the name
# with `(partially selected)` when a non-full selection is active —
# so the inner pattern is "anything except newline and `=`" rather
# than a single non-whitespace token. The captured name is trimmed
# below.
_STAT_BLOCK_RE = re.compile(
    r"===\s*(?P<module>[^\n=]+?)\s*===\s*\n"
    r"(?P<body>.*?)(?====\s*[^\n=]+\s*===|\Z)",
    re.DOTALL,
)
# yosys's `stat` prints counts as `<N> cells` / `<N> wires` with leading
# whitespace and a header line "+----------Local Count, ...". The
# anchored regex below targets the standalone "<N> cells" line and
# ignores both the header and the per-cell-type breakdown that
# follows (`1   $add`, etc.).
_CELLS_RE = re.compile(r"^\s*(?P<n>\d+)\s+cells\s*$", re.MULTILINE)
_WIRES_RE = re.compile(r"^\s*(?P<n>\d+)\s+wires\s*$", re.MULTILINE)


# Markers we emit in the yosys script so we can locate the two `stat`
# blocks deterministically. yosys's `log` command echoes verbatim so a
# unique sentinel survives any module-name collisions.
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


def build_yosys_script(
    *,
    sources: list[str],
    incdirs: list[str],
    properties: list[str],
    constraints: str | None,
    top: str,
    frontend: str = "verilog",
    plugin_path: str | None = None,
) -> str:
    """Render the yosys script that runs the COI analysis.

    Order mirrors the sby script (sources → constraints → properties)
    so the assertion cells exist in the same context they're proved
    in. The `frontend` arg picks the same SystemVerilog parser the
    sby pass used — using a different frontend here would risk
    `$check` cells disappearing under `t:$check` selection because
    slang's `bind` resolution and verilog-frontend's diverge.
    """
    lines: list[str] = []
    if frontend == "slang":
        if not plugin_path:
            raise ValueError(
                "fpv_coi: frontend='slang' requires a non-empty plugin_path"
            )
        lines.append(f"plugin -i {plugin_path}")
    for inc in incdirs:
        lines.append(f"verilog_defaults -add -I {inc}")
    constraint_files = [constraints] if constraints else []
    all_files = list(sources) + constraint_files + list(properties)
    if frontend == "slang":
        # Single `read_slang --top <name> <files...>` so `bind`
        # directives at compilation-unit scope see every declared
        # module — matches the SbyFpv slang renderer.
        src_args = " ".join(all_files)
        lines.append(f"read_slang --top {top} {src_args}")
    else:
        for src in all_files:
            lines.append(f"read -sv -formal {src}")
    # `prep -flatten -top` mirrors what sby itself runs for proof:
    # hierarchy + proc + opt while preserving formal cells, then
    # collapses everything into the top module. Flattening matters
    # here because `bind`-style property modules elaborate as
    # submodules — without flatten, `$assert` cells live in those
    # submodules and yosys's default `stat` (which only counts the
    # top module) silently reports zero. Flattening trades the
    # per-submodule rollup for a correct aggregate count.
    lines.append(f"prep -flatten -top {top}")

    lines.append(f"log === {_MARK_TOTAL} ===")
    lines.append("stat")

    # Select every assertion cell and walk back through its cone of
    # influence. Modern yosys (>= 2024) unifies asserts/assumes/covers
    # into a single `$check` cell type with a `FLAVOR` parameter
    # ("assert" / "assume" / "cover" / "live" / "fair"). Older yosys
    # versions still emit dedicated `$assert` / `$assume` cells. We
    # union both so the COI walk works on either generation.
    #
    # `%ci*` is yosys's transitive input-cone operator: repeats `%ci`
    # until fixpoint — exactly the cone of influence of the assertion
    # cells. (Plain `%ci` walks only one step.)
    lines.append("select -set property_cells t:$assert t:$check r:FLAVOR=assert %i %u")
    lines.append("select -set property_coi @property_cells %ci*")
    lines.append("select @property_coi")
    lines.append(f"log === {_MARK_SELECTED} ===")
    lines.append("stat")

    # Dead-assume analysis (#135): count assume cells total, then
    # count those that intersect with the assertion COI. The delta is
    # the structural lower bound on "assumes constraining signals no
    # assertion observes" — a flag for environment-spec drift. Same
    # `$check` / `$assume` dual-selector applies.
    lines.append("select -set all_assumes t:$assume t:$check r:FLAVOR=assume %i %u")
    lines.append("select @all_assumes")
    lines.append(f"log === {_MARK_ASSUMES_TOTAL} ===")
    lines.append("stat")
    lines.append("select @all_assumes @property_coi %i")
    lines.append(f"log === {_MARK_ASSUMES_IN_COI} ===")
    lines.append("stat")
    return "\n".join(lines) + "\n"


def parse_stat_blocks(log_text: str) -> dict[str, dict[str, dict[str, int]]]:
    """Parse the marked `stat`/`stat -selection` blocks from a yosys log.

    Returns ``{marker: {module: {"cells": N, "wires": M}}}`` for each of
    our markers. A missing marker becomes an empty dict so callers can
    detect partial output.
    """
    out: dict[str, dict[str, dict[str, int]]] = {m: {} for m in _ALL_MARKERS}

    # Slice the log into one section per marker. The closest following
    # marker (in order of appearance, not in our enumeration) bounds
    # each slice — preserves order across the four markers without
    # depending on the enumeration order.
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
            # Strip yosys's `(partially selected)` decoration so the
            # selected stat for module `dut` rolls up against the
            # baseline stat for the same module.
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
    """Compute aggregate + per-module COI coverage from parsed blocks.

    Also rolls up the dead-assume analysis (#135): total `$assume`
    cells vs the subset that intersect with the assertion COI. The
    delta is the structural lower bound on "assumes constraining
    signals no assertion observes."
    """
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
) -> dict | None:
    """Run yosys and return the parsed coverage summary, or None on error.

    Soft-failure semantics: if yosys is missing or the script errors,
    log a warning and return None so the caller falls back to "no COI
    data" rather than failing the whole FPV run.
    """
    script = build_yosys_script(
        sources=sources,
        incdirs=incdirs,
        properties=properties,
        constraints=constraints,
        top=top,
        frontend=frontend,
        plugin_path=plugin_path,
    )
    Path(script_path).write_text(script)
    cmd = [yosys_exe, "-s", script_path]
    try:
        with open(log_path, "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            result = run_managed_process(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
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
