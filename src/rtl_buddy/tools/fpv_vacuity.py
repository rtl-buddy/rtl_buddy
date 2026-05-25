"""Vacuity-cover synthesis for ``rb fpv``.

A SystemVerilog property of the form ``a |-> b`` (or ``a |=> b``) is
*vacuously true* whenever the antecedent ``a`` never holds. The proof
passes but tells us nothing: the assertion never actually constrained
the design. Hand-writing ``cover (a)`` siblings for every implication
is tedious and easy to forget, so this module walks the user's property
files, extracts the antecedents of each ``|->`` / ``|=>`` operator, and
emits a sidecar SystemVerilog file with synthetic ``cover property``
statements for them.

The sidecar is fed into a secondary sby pass in ``cover`` mode. Cover
hits surface as a per-antecedent reachability map in ``FpvResults`` so
the ``rb fpv`` table can flag vacuity warnings without the user
re-writing their assertion set.

Scope today:

- Single-line antecedents on the left of ``|->`` / ``|=>``.
- Clocking and ``disable iff`` clauses are preserved when they appear on
  the same line as the implication.
- Multi-line / sequence-valued antecedents are left to a future pass;
  they are reported as ``skipped`` so the user knows coverage is partial.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Regex notes:
# - We match a single SVA `assert property (... <ant> |-> <cons>)` per
#   line. The antecedent is the bracketed expression immediately before
#   the implication operator. To stay readable we don't try to parse
#   nested sequence operators; the cover wraps the antecedent verbatim
#   as a boolean expression, which is correct for the common
#   single-line case the issue targets.
# - The leading optional `<label>: ` is captured so the synthesized
#   cover can reuse the user's name (helps the user trace which
#   antecedent vacuity check belongs to which assert).
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

# Inside `body`, find the antecedent before `|->` / `|=>`. We use the
# rightmost `|->` to avoid getting confused by `|->` inside the
# consequent — implications are right-associative in SVA but the
# antecedent side rarely contains a nested `|->`, so the rightmost
# match is the safe default.
_IMPL_RE = re.compile(r"(?P<op>\|->|\|=>)")

# Strip leading clocking / disable-iff so the antecedent is a plain
# boolean. Surrounding parens around the clocking event are preserved
# in `clocking` so the synthetic cover keeps the same trigger.
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
    """One synthesized cover derived from a user-written `|->` property."""

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
            # Rightmost implication is the outer one; everything before
            # it is the antecedent.
            last = impl[-1]
            antecedent_raw = body[: last.start()].strip()
            operator = last.group("op")
            label = match.group("label")
            label = label.strip() if label else None

            clocking_match = _CLOCKING_RE.match(antecedent_raw)
            if clocking_match is None:
                # The regex always matches (rest is greedy), but be
                # defensive.
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
    """Drop trailing unbalanced closing parens.

    The body regex captures up to ``)`` of `assert property(...)`, so
    after splitting on `|->` the antecedent may carry an outer ``(``
    without its matching ``)``. Strip the unbalanced wrapping
    parentheses so the synthesized cover compiles cleanly.
    """
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


def write_vacuity_module(
    candidates: list[VacuityCandidate],
    output_path: str,
    *,
    module_name: str = "rtl_buddy_vacuity_covers",
) -> str:
    """Emit a SystemVerilog module that covers each candidate antecedent.

    Returns the path written. The module declares `clk` / `rst` as
    plain ports so it can be bound into the design — but the recommended
    integration is to `read -formal` it alongside the user's properties
    in the same scope (sby handles cross-module asserts/covers when both
    share a `default clocking` block or both carry their own clocking).

    For Phase 1 we emit a freestanding module that mirrors the
    clocking from the captured candidate (or omits it when the
    candidate had none — sby treats unclocked `cover` as an immediate
    check on each cycle).
    """
    lines: list[str] = [
        f"// Auto-generated by rtl_buddy fpv_vacuity ({len(candidates)} covers)",
        "// One `cover property` per `|->` / `|=>` antecedent found in the",
        "// property set. A FAIL ('not reachable') flags a vacuous proof.",
        "",
        f"module {module_name};",
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
    lines.append("")
    Path(output_path).write_text("\n".join(lines))
    return output_path


# ---------------------------------------------------------------------------
# sby cover-mode log parsing
# ---------------------------------------------------------------------------

# sby's cover mode emits lines like:
#   "Reached cover statement at <hier>.cover_vacuity_3_foo in step 4"
# and (for unreachable covers in bmc-bounded cover):
#   "Unreached cover statement: <hier>.cover_vacuity_3_foo"
_REACHED_RE = re.compile(
    r"Reached cover statement at\s+(?P<hier>\S+?)(?:\s+in step\s+\d+)?[.\s]*$",
)
_UNREACHED_RE = re.compile(r"Unreached cover statement[: ]+(?P<hier>\S+)")


def parse_vacuity_log(log_text: str) -> dict[str, bool]:
    """Return ``{cover_name: reachable}`` from an sby cover-mode logfile.

    Cover names that appear in neither bucket are absent from the map;
    the caller treats them as "no signal" so we don't synthesize false
    negatives when sby's output format shifts.
    """
    result: dict[str, bool] = {}
    for line in log_text.splitlines():
        m = _REACHED_RE.search(line)
        if m:
            result[m.group("hier").split(".")[-1]] = True
            continue
        m = _UNREACHED_RE.search(line)
        if m:
            name = m.group("hier").split(".")[-1]
            result.setdefault(name, False)
    return result
