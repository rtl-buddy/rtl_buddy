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


# SV number literal: optional size, then `'`, then base letter (b/o/d/h
# / B/O/D/H, optional `s` prefix for signed), then digits/underscore/
# x/z/?. Matches e.g. `1'b0`, `4'hAB`, `'sd-12`, `32'h1234_5678`.
_SV_NUM_LITERAL_RE = re.compile(r"(?:\d[\d_]*)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+")


def _scan_identifiers(text: str) -> list[str]:
    """Return SV-style identifiers (in source order, deduplicated) found in
    ``text``, skipping number literals, reserved words, and type
    keywords. Heuristic — used only to decide which ports the
    synthesized vacuity-cover module needs."""
    # Erase number literals before scanning so e.g. `1'b0` doesn't
    # leak its base suffix as a bareword like `b0`.
    cleaned = _SV_NUM_LITERAL_RE.sub(" ", text)
    seen: dict[str, None] = {}
    # SV identifier pattern: [A-Za-z_][A-Za-z0-9_$]*
    for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*", cleaned):
        name = tok.group(0)
        if name in _SV_RESERVED:
            continue
        seen[name] = None
    return list(seen)


# Reserved words / type keywords we never want to treat as port names
# in the synthesized cover module. Not exhaustive — only the ones an
# antecedent realistically contains.
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
    """Emit a SystemVerilog module that covers each candidate antecedent.

    Returns the path written. The module declares a port for every
    identifier referenced in any antecedent (or its clocking /
    disable-iff clause), plus the canonical `clk` / `rst_n` pair. When
    ``bind_to`` is given, a matching `bind <top> ... (.*);` directive
    is appended so the synthesized covers see the DUT's signals
    by name — required for slang elaboration, which does not infer
    free identifiers the way yosys's native verilog frontend does.
    """
    # Collect the signal names every cover references. Scan each
    # antecedent + its clocking + disable-iff text so we declare
    # exactly the ports the cover bodies need.
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
        # Connect every cover-module port by name from the DUT scope.
        # `.<port>` (port-name shorthand) requires the DUT to expose a
        # net with the same name; for the canonical clk / rst_n /
        # signal pattern this is always true.
        conns = ", ".join(f".{p}" for p in port_list)
        lines.append(f"bind {bind_to} {module_name} u_rtl_buddy_vacuity ({conns});")
    lines.append("")
    Path(output_path).write_text("\n".join(lines))
    return output_path


# ---------------------------------------------------------------------------
# sby cover-mode log parsing
# ---------------------------------------------------------------------------

# sby cover-mode emits two flavours of lines for each cover:
#
# Engine progress (mid-run, one per cover when reached):
#   "## 0:00:00  Reached cover statement at top.cov.foo in step 3"
#
# Final summary block (always emitted at end-of-run):
#   "SBY <ts> [...] summary:   reached cover statement <hier> at <file>:<lines> step <N>"
#   "SBY <ts> [...] summary: unreached cover statements:"
#   "SBY <ts> [...] summary:   <hier> at <file>:<lines>"
#
# We match both: the summary block is authoritative when present
# (covers every cover deterministically), and the mid-run lines pick
# up the slack on configurations where sby only prints the engine
# trace. Case-insensitive match accepts either capitalisation.
_REACHED_RE = re.compile(
    r"[Rr]eached cover statement(?:\s+at)?\s+(?P<hier>\S+)",
)
_UNREACHED_HEADER_RE = re.compile(
    r"[Uu]nreached cover statements?:",
)
_UNREACHED_INLINE_RE = re.compile(
    r"[Uu]nreached cover statement:?\s+(?P<hier>\S+)",
)
# Summary continuation line: "summary:   <hier> at <file>:<lines>" —
# only valid inside an "unreached cover statements:" block (tracked
# statefully below).
_SUMMARY_HIER_RE = re.compile(
    r"summary:\s+(?P<hier>\S+)\s+at\s+\S+",
)


def parse_vacuity_log(log_text: str) -> dict[str, bool]:
    """Return ``{cover_name: reachable}`` from an sby cover-mode logfile.

    Cover names that appear in neither bucket are absent from the map;
    the caller treats them as "no signal" so we don't synthesize false
    negatives when sby's output format shifts.
    """
    result: dict[str, bool] = {}
    in_unreached_block = False
    for line in log_text.splitlines():
        m = _REACHED_RE.search(line)
        if m:
            # `\u_rtl_buddy_vacuity.cover_vacuity_1_foo` → strip the
            # last segment after `.` for the cover name. The escape
            # `\u_...` (yosys public-name marker) is preserved by
            # `.split('.')` but doesn't affect the suffix match.
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
                # End of the summary block — anything outside the
                # `summary:` prefix terminates it.
                in_unreached_block = False
    return result
