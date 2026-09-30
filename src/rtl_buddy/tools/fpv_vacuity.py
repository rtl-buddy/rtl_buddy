"""Vacuity-cover synthesis for ``rb fpv``.

An implication ``a |-> b`` (or ``a |=> b``) is vacuously true when ``a`` never holds.
This module extracts the antecedents from property files and writes a sidecar module of
``cover property`` statements for them, which a secondary sby pass in ``cover`` mode
checks. Reachability per antecedent is reported in ``FpvResults``.

Only single-line antecedents are handled. Clocking and ``disable iff`` clauses are kept
when they are on the same line as the implication.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Matches one single-line `assert property (... <ant> |-> <cons>);`. Nested sequence
# operators are not parsed. The optional label is kept to name the cover.
_PROP_LINE_RE = re.compile(
    r"""^
    \s*
    (?P<label>[A-Za-z_][A-Za-z0-9_]*\s*:\s*)?   # optional `name: `
    assert\s+property\s*
    \(
    (?P<body>.*?)
    \)\s*;
    """,
    re.VERBOSE,
)

# The rightmost implication is the outer one; a nested `|->` in the consequent is ignored.
_IMPL_RE = re.compile(r"(?P<op>\|->|\|=>)")

# Split off the clocking event and `disable iff` so the antecedent is a plain boolean.
_CLOCKING_RE = re.compile(
    r"""^\s*
    (?P<clocking>@\s*\([^)]*\))?
    \s*
    (?P<disable>disable\s+iff\s*\([^)]*\))?
    \s*
    (?P<rest>.*)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class VacuityCandidate:
    """One cover derived from a `|->` or `|=>` property."""

    source_file: str
    source_line: int
    label: str | None
    clocking: str | None
    disable_iff: str | None
    antecedent: str
    operator: str  # `|->` or `|=>`

    def cover_name(self, index: int) -> str:
        base = (self.label or "implicand").rstrip(": ").strip()
        return f"cover_vacuity_{index}_{base}"


def extract_candidates(property_files: list[str]) -> list[VacuityCandidate]:
    """Walk each property file and return one candidate per `|->` / `|=>`."""
    candidates: list[VacuityCandidate] = []
    for path in property_files:
        if not os.path.isfile(path):
            logger.debug("fpv_vacuity.skip_missing path=%s", path)
            continue
        try:
            text = Path(path).read_text()
        except OSError as e:
            logger.debug("fpv_vacuity.read_failed path=%s err=%s", path, e)
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _PROP_LINE_RE.match(line)
            if match is None:
                continue
            body = match.group("body")
            impl = list(_IMPL_RE.finditer(body))
            if not impl:
                continue
            last = impl[-1]
            antecedent_raw = body[: last.start()].strip()
            operator = last.group("op")
            label = match.group("label")
            label = label.strip() if label else None

            clocking_match = _CLOCKING_RE.match(antecedent_raw)
            if clocking_match is None:
                continue
            clocking = clocking_match.group("clocking")
            disable = clocking_match.group("disable")
            rest = clocking_match.group("rest").strip()
            antecedent = _balance_parens(rest)
            if not antecedent:
                continue

            candidates.append(
                VacuityCandidate(
                    source_file=path,
                    source_line=lineno,
                    label=label,
                    clocking=clocking,
                    disable_iff=disable,
                    antecedent=antecedent,
                    operator=operator,
                )
            )
    return candidates


def _balance_parens(expr: str) -> str:
    """Strip fully wrapping parentheses so the antecedent is a bare expression."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    balanced = False
                    break
        if balanced:
            expr = expr[1:-1].strip()
        else:
            break
    return expr


# SV number literal, e.g. `1'b0`, `4'hAB`, `32'h1234_5678`.
_SV_NUM_LITERAL_RE = re.compile(r"(?:\d[\d_]*)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+")


def _scan_identifiers(text: str) -> list[str]:
    """Return the distinct identifiers in ``text`` in source order, skipping number literals and reserved words.

    Heuristic; it decides which ports the vacuity-cover module declares.
    """
    # Blank number literals first so `1'b0` does not yield `b0`.
    cleaned = _SV_NUM_LITERAL_RE.sub(" ", text)
    seen: dict[str, None] = {}
    for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*", cleaned):
        name = tok.group(0)
        if name in _SV_RESERVED:
            continue
        seen[name] = None
    return list(seen)


# Keywords never treated as port names; not exhaustive.
_SV_RESERVED = frozenset(
    {
        "and",
        "or",
        "not",
        "if",
        "else",
        "always",
        "always_ff",
        "always_comb",
        "posedge",
        "negedge",
        "logic",
        "wire",
        "reg",
        "bit",
        "int",
        "input",
        "output",
        "inout",
        "module",
        "endmodule",
        "property",
        "endproperty",
        "assert",
        "assume",
        "cover",
        "disable",
        "iff",
        "default",
        "clocking",
        "endclocking",
    }
)


def write_vacuity_module(
    candidates: list[VacuityCandidate],
    output_path: str,
    *,
    module_name: str = "rtl_buddy_vacuity_covers",
    bind_to: str | None = None,
) -> str:
    """Write a SystemVerilog module with one cover per candidate and return its path.

    The module has a port for every identifier in the antecedents, clocking and
    disable-iff clauses, plus `clk` and `rst_n`. With ``bind_to``, a `bind` of the module
    into that top is appended; slang needs it because it does not infer free identifiers.
    """
    ports: dict[str, None] = {"clk": None, "rst_n": None}
    for c in candidates:
        for blob in (c.antecedent, c.clocking or "", c.disable_iff or ""):
            for name in _scan_identifiers(blob):
                ports[name] = None
    port_list = list(ports)

    lines: list[str] = [
        f"// Auto-generated by rtl_buddy fpv_vacuity ({len(candidates)} covers)",
        "// One `cover property` per `|->` / `|=>` antecedent found in the",
        "// property set. A FAIL ('not reachable') flags a vacuous proof.",
        "",
        f"module {module_name} (",
        "  " + ",\n  ".join(f"input logic {p}" for p in port_list),
        ");",
    ]
    for index, c in enumerate(candidates, start=1):
        lines.append("")
        lines.append(
            f"  // {os.path.basename(c.source_file)}:{c.source_line} ({c.operator})"
        )
        property_parts: list[str] = []
        if c.clocking:
            property_parts.append(c.clocking)
        if c.disable_iff:
            property_parts.append(c.disable_iff)
        property_parts.append(c.antecedent)
        property_body = " ".join(property_parts)
        lines.append(f"  {c.cover_name(index)}: cover property ({property_body});")
    lines.append("")
    lines.append(f"endmodule  // {module_name}")
    if bind_to:
        lines.append("")
        # `.<port>` shorthand needs the DUT to have a net of the same name.
        conns = ", ".join(f".{p}" for p in port_list)
        lines.append(f"bind {bind_to} {module_name} u_rtl_buddy_vacuity ({conns});")
    lines.append("")
    Path(output_path).write_text("\n".join(lines))
    return output_path


# sby cover-mode lines. Mid-run: "## 0:00:00  Reached cover statement at top.cov.foo in step 3".
# Summary block at the end of the run:
#   "SBY <ts> [...] summary:   reached cover statement <hier> at <file>:<lines> step <N>"
#   "SBY <ts> [...] summary: unreached cover statements:"
#   "SBY <ts> [...] summary:   <hier> at <file>:<lines>"
# Both forms are matched, since some configurations print only the mid-run lines.
_REACHED_RE = re.compile(
    r"[Rr]eached cover statement(?:\s+at)?\s+(?P<hier>\S+)",
)
_UNREACHED_HEADER_RE = re.compile(
    r"[Uu]nreached cover statements?:",
)
_UNREACHED_INLINE_RE = re.compile(
    r"[Uu]nreached cover statement:?\s+(?P<hier>\S+)",
)
# Only valid inside an "unreached cover statements:" block.
_SUMMARY_HIER_RE = re.compile(
    r"summary:\s+(?P<hier>\S+)\s+at\s+\S+",
)


def parse_vacuity_log(log_text: str) -> dict[str, bool]:
    """Return ``{cover_name: reachable}`` from an sby cover-mode logfile; covers not mentioned are absent."""
    result: dict[str, bool] = {}
    in_unreached_block = False
    for line in log_text.splitlines():
        m = _REACHED_RE.search(line)
        if m:
            # The cover name is the last dotted segment of the hierarchical name.
            result[m.group("hier").split(".")[-1]] = True
            in_unreached_block = False
            continue

        m = _UNREACHED_INLINE_RE.search(line)
        if m:
            result.setdefault(m.group("hier").split(".")[-1], False)
            in_unreached_block = False
            continue

        if _UNREACHED_HEADER_RE.search(line):
            in_unreached_block = True
            continue

        if in_unreached_block:
            m = _SUMMARY_HIER_RE.search(line)
            if m:
                result.setdefault(m.group("hier").split(".")[-1], False)
            elif "summary:" not in line:
                in_unreached_block = False
    return result
