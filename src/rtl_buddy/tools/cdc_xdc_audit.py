"""Audit a Vivado XDC's CDC-relevant exceptions against rtl-buddy-cdc's
independently-derived crossing set (issue #290).

This is the *audit* half of the constraint loop (#291 is the *generation*
half). It is **not** a general XDC validator — pin/IO/placement/electrical
correctness stays Vivado's job. Scope is strictly the CDC subset:
``create_clock`` / ``create_generated_clock``, ``set_clock_groups
-asynchronous``, ``set_false_path``, ``set_max_delay -datapath_only``,
``set_bus_skew``.

The audit is a diff between the XDC's exceptions and the open engine's truth:

* **Completeness** — a crossing rtl-buddy-cdc finds, with no matching XDC
  exception → *unconstrained CDC* (the tool times a metastable-by-design path
  or the design gets false confidence).
* **Over-waive (dangerous)** — an XDC ``set_false_path`` / ``-asynchronous`` on
  a path rtl-buddy-cdc reports as *not* a safely synchronized crossing → the
  XDC **masks a real metastability / data-coherency bug**. This is where the
  independent open engine beats trusting the constraint file.
* **Missing bus skew** — a multi-bit crossing waived with a bare false-path /
  clock-group and no ``set_bus_skew`` → bit-to-bit skew incoherency is hidden.
* **Clock-graph consistency** — XDC ``create_clock`` disagreeing with the RTL
  clocking changes the derived domain set and can hide waived crossings.

Pure functions over the XDC text + the rtl-buddy-cdc domain map / report — no
subprocess — so the contract is testable against checked-in fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..constraints.tcl_reader import TclCommand, extract_names, read_commands
from ..errors import FatalRtlBuddyError

# CDC-relevant XDC commands. Everything else (set_property, IO/placement,
# create_pblock, ...) is ignored on purpose.
_INTEREST = frozenset(
    {
        "create_clock",
        "set_clock_groups",
        "set_false_path",
        "set_max_delay",
        "set_bus_skew",
    }
)

_EXCEPTION_KINDS = {
    "set_false_path": "false_path",
    "set_max_delay": "max_delay",
    "set_bus_skew": "bus_skew",
}


def _tokens(word: str | None) -> list[str]:
    """Pull the bare names out of one tokenized Tcl word.

    Handles ``[get_clocks clk_a]``, ``[get_clocks {clk_a clk_b}]``,
    ``[get_cells u_sync/* -filter {IS_SEQUENTIAL}]``,
    ``[get_cells -hierarchical u_sync/*]``, ``[get_pins [get_cells u_a]/C]``,
    ``{clk_a}`` and bare ``clk_a``. Flags, the ``get_*`` head (nested ones
    too) and ``-filter`` predicates are dropped; a trailing ``/*`` cell
    wildcard is trimmed to the instance token.
    """
    if word is None:
        return []
    return extract_names(word)


def _flag_value(cmd: TclCommand, flag: str) -> str | None:
    """First value word following ``flag``, or ``None`` when absent."""
    for i, word in enumerate(cmd.words):
        if word == flag and i + 1 < len(cmd.words):
            return cmd.words[i + 1]
    return None


def _flag_values(cmd: TclCommand, flag: str) -> list[str]:
    """Every value word following a repeated ``flag`` (e.g. ``-group``)."""
    out = []
    for i, word in enumerate(cmd.words):
        if word == flag and i + 1 < len(cmd.words):
            out.append(cmd.words[i + 1])
    return out


def _is_clocks(expr: str | None) -> bool:
    return expr is not None and "get_clocks" in expr


@dataclass
class PathException:
    kind: str  # false_path | max_delay | bus_skew
    datapath_only: bool
    from_clocks: list[str] = field(default_factory=list)
    to_clocks: list[str] = field(default_factory=list)
    from_cells: list[str] = field(default_factory=list)
    to_cells: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class XdcConstraints:
    clocks: dict = field(default_factory=dict)  # name -> period (float|None)
    async_clock_pairs: set = field(default_factory=set)  # frozenset({clkX, clkY})
    path_exceptions: list = field(default_factory=list)  # list[PathException]


def _split_target(expr: str | None):
    """Return (clocks, cells) token lists for a -from/-to expression."""
    toks = _tokens(expr)
    if _is_clocks(expr):
        return toks, []
    return [], toks


def _period(word: str | None) -> float | None:
    """``-period`` value as a float, or ``None`` when absent/unevaluated."""
    if word is None:
        return None
    value = word
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1].strip()
    try:
        return float(value)
    except ValueError:
        return None


def extract_cdc_constraints(
    xdc_text: str, *, source: str | None = None
) -> XdcConstraints:
    """Read the CDC-relevant subset of an XDC/SDC into :class:`XdcConstraints`.

    Reading goes through the Tcl word tokenizer (#642) rather than a regex per
    physical line, so ``\\``-continued commands, braced values and nested
    collections (``[get_pins [get_cells u_a]/C]``) are read the way Vivado
    reads them. ``source`` only names the file in the one-per-file
    ``constraints.tokenizer_skipped`` warning.
    """
    commands, _backend = read_commands(xdc_text, interest=_INTEREST, source=source)
    xc = XdcConstraints()

    for cmd in commands:
        if cmd.name == "create_clock":
            name = _flag_value(cmd, "-name")
            if name is None:
                continue
            names = _tokens(name)
            xc.clocks[names[0] if names else name] = _period(
                _flag_value(cmd, "-period")
            )
            continue

        if cmd.name == "set_clock_groups":
            if "-asynchronous" not in cmd.words and "-async" not in cmd.words:
                continue
            groups = [_tokens(g) for g in _flag_values(cmd, "-group")]
            # every cross-group clock pair is declared asynchronous
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    for a in groups[i]:
                        for b in groups[j]:
                            xc.async_clock_pairs.add(frozenset({a, b}))
            continue

        kind = _EXCEPTION_KINDS.get(cmd.name)
        if kind is None:
            continue
        fc, fcell = _split_target(_flag_value(cmd, "-from"))
        tc, tcell = _split_target(_flag_value(cmd, "-to"))
        xc.path_exceptions.append(
            PathException(
                kind=kind,
                datapath_only="-datapath_only" in cmd.words,
                from_clocks=fc,
                to_clocks=tc,
                from_cells=fcell,
                to_cells=tcell,
                raw=cmd.raw or "",
            )
        )
    return xc


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str  # blocker | warning | info
    kind: str  # unconstrained_crossing | over_waive | missing_bus_skew | clock_graph
    message: str
    src_clock: str = ""
    dst_clock: str = ""
    target: str = ""


@dataclass
class AuditResult:
    findings: list = field(default_factory=list)

    @property
    def blockers(self) -> list:
        return [f for f in self.findings if f.severity == "blocker"]

    def to_machine(self) -> list[dict]:
        return [
            {
                "severity": f.severity,
                "kind": f.kind,
                "message": f.message,
                "src_clock": f.src_clock,
                "dst_clock": f.dst_clock,
                "target": f.target,
            }
            for f in self.findings
        ]


def _inst_tail(path: str) -> str:
    """Leaf-relative instance token, matching the XDC get_cells convention."""
    parts = path.split(".")
    return parts[-1] if parts else path


def _cell_match(cells: list[str], crossing_inst: str) -> bool:
    """True if any XDC cell token addresses the crossing's dst instance."""
    leaf = _inst_tail(crossing_inst)
    rel = (
        "/".join(crossing_inst.split(".")[1:])
        if "." in crossing_inst
        else crossing_inst
    )
    for c in cells:
        c = c.strip()
        if not c:
            continue
        if c in (leaf, rel) or c.endswith("/" + leaf) or leaf == c:
            return True
        # the emitted form is the relative path of the instance itself
        if rel and (c == rel or rel.endswith(c) or c.endswith(rel)):
            return True
    return False


def _covers_clock_pair(pair, exceptions, kinds) -> bool:
    a, b = tuple(pair) if len(pair) == 2 else (next(iter(pair)), next(iter(pair)))
    for e in exceptions:
        if e.kind not in kinds:
            continue
        if (a in e.from_clocks and b in e.to_clocks) or (
            b in e.from_clocks and a in e.to_clocks
        ):
            return True
    return False


def audit_xdc(
    domain_map: dict,
    cdc_report: dict,
    xc: XdcConstraints,
    recognized_syncs: list[str] | None = None,
) -> AuditResult:
    """Diff the XDC's CDC exceptions against the verified crossing set.

    ``recognized_syncs`` is a list of instance-path regexes the user declares
    as real synchronizers the analyzer did not recognize structurally (e.g. a
    blackboxed ``xpm_cdc_*`` macro). A violation whose instance matches one is
    treated as a safe crossing: a correct XDC waiver of it is NOT a dangerous
    over-waive. (Completeness still applies — it is a real crossing.)
    """
    res = AuditResult()
    recognized = []
    for p in recognized_syncs or []:
        try:
            recognized.append(re.compile(p))
        except re.error as e:
            # User-supplied (cdc.yaml recognized-syncs / --recognize-sync) — a
            # bad pattern is a config error, not a traceback.
            raise FatalRtlBuddyError(
                f"--check-xdc: invalid recognized-syncs regex {p!r}: {e}"
            ) from e
    crossings = [
        c for c in domain_map.get("crossings", []) if c.get("async_per_sdc", True)
    ]

    # A flattening frontend (Yosys `flatten`) collapses every crossing's capture
    # instance to the design top, so cell-scoped exceptions in the XDC cannot be
    # matched to a specific crossing. Clock-level coverage (groups / clock-pair
    # false_path) is still audited correctly; warn only when the XDC actually
    # relies on cell scoping, so the user knows that half is not being verified.
    map_flattened = bool(crossings) and all(
        "." not in c.get("dst_source_instance_path", "") for c in crossings
    )
    xdc_has_cell_scope = any(e.from_cells or e.to_cells for e in xc.path_exceptions)
    if map_flattened and xdc_has_cell_scope:
        res.findings.append(
            Finding(
                severity="warning",
                kind="frontend_flattened",
                message=(
                    "the domain map flattened all crossings to the design top, so "
                    "cell-scoped XDC exceptions cannot be matched to a crossing — "
                    "only clock-level coverage was audited. Re-run the analysis with "
                    "a hierarchy-preserving frontend (frontend: slang) to audit "
                    "scoped constraints."
                ),
            )
        )

    # --- completeness + bus-skew on the SAFE crossing set ---
    for c in crossings:
        src, dst = c.get("src_clock"), c.get("dst_clock")
        inst = c.get("dst_source_instance_path", "")
        width = int(c.get("width", 1))
        pair = frozenset({src, dst})
        by_group = pair in xc.async_clock_pairs
        by_fp = _covers_clock_pair(pair, xc.path_exceptions, {"false_path"}) or any(
            e.kind == "false_path" and _cell_match(e.to_cells, inst)
            for e in xc.path_exceptions
        )
        # Only a `-datapath_only` max_delay is a valid async CDC exception; a
        # bare max_delay still times the launch->capture clock relationship,
        # so it does NOT count as covering a crossing.
        md_dp = [
            e for e in xc.path_exceptions if e.kind == "max_delay" and e.datapath_only
        ]
        by_md = _covers_clock_pair(pair, md_dp, {"max_delay"}) or any(
            _cell_match(e.to_cells, inst) for e in md_dp
        )
        covered = by_group or by_fp or by_md
        if not covered:
            res.findings.append(
                Finding(
                    severity="blocker",
                    kind="unconstrained_crossing",
                    message=(
                        f"verified {src} -> {dst} crossing at {_inst_tail(inst)} has "
                        "no CDC exception in the XDC — it will be timed as a real path "
                        "(false confidence or a timing failure)"
                    ),
                    src_clock=src,
                    dst_clock=dst,
                    target=_inst_tail(inst),
                )
            )
            continue
        if width > 1:
            has_skew = _covers_clock_pair(
                pair, xc.path_exceptions, {"bus_skew"}
            ) or any(
                e.kind == "bus_skew" and _cell_match(e.to_cells, inst)
                for e in xc.path_exceptions
            )
            if not has_skew:
                res.findings.append(
                    Finding(
                        severity="warning",
                        kind="missing_bus_skew",
                        message=(
                            f"{width}-bit {src} -> {dst} crossing at {_inst_tail(inst)} "
                            "is waived without set_bus_skew — bit-to-bit skew "
                            "incoherency is not bounded"
                        ),
                        src_clock=src,
                        dst_clock=dst,
                        target=_inst_tail(inst),
                    )
                )

    # --- over-waive on the UNSAFE crossing set (unsuppressed violations) ---
    for v in cdc_report.get("violations", []):
        c = v.get("crossing") or {}
        src, dst = c.get("src_clock"), c.get("dst_clock")
        if not src or not dst:
            continue
        inst = "/".join(v.get("instance_path", [])) or c.get("dst_flop", "")
        # A user-declared recognized synchronizer the analyzer missed
        # structurally (e.g. a blackboxed xpm_cdc_* macro): a correct XDC
        # waiver of it is not an over-waive, so skip it here. Completeness
        # above still requires it to be constrained.
        if recognized and any(
            r.search(inst) or r.search(c.get("dst_flop", "")) for r in recognized
        ):
            continue
        pair = frozenset({src, dst})
        # false_path / clock_groups make the tool IGNORE the path; max_delay
        # still times it, so it is not an over-waive.
        ignored = (
            pair in xc.async_clock_pairs
            or _covers_clock_pair(pair, xc.path_exceptions, {"false_path"})
            or any(
                e.kind == "false_path"
                and _cell_match(e.to_cells, inst.replace("/", "."))
                for e in xc.path_exceptions
            )
        )
        if ignored:
            res.findings.append(
                Finding(
                    severity="blocker",
                    kind="over_waive",
                    message=(
                        f"XDC waives the {src} -> {dst} path ({v.get('rule_id')}: "
                        f"{_inst_tail(inst)}) that rtl-buddy-cdc flags as NOT safely "
                        "synchronized — the constraint masks a real metastability bug"
                    ),
                    src_clock=src,
                    dst_clock=dst,
                    target=_inst_tail(inst),
                )
            )

    # --- clock-graph consistency ---
    rtl_clocks = {c.get("name"): c.get("period") for c in domain_map.get("clocks", [])}
    for name, period in xc.clocks.items():
        if name not in rtl_clocks:
            res.findings.append(
                Finding(
                    severity="warning",
                    kind="clock_graph",
                    message=(
                        f"XDC create_clock '{name}' is not a clock rtl-buddy-cdc "
                        "derived from the RTL — the domain set it implies may differ "
                        "(and could hide crossings)"
                    ),
                    target=name,
                )
            )
        elif (
            period is not None
            and rtl_clocks[name] is not None
            and abs(period - rtl_clocks[name]) > 1e-9
        ):
            res.findings.append(
                Finding(
                    severity="info",
                    kind="clock_graph",
                    message=(
                        f"clock '{name}' period differs: XDC {period} ns vs analysis "
                        f"SDC {rtl_clocks[name]} ns"
                    ),
                    target=name,
                )
            )
    for name in rtl_clocks:
        if name not in xc.clocks:
            res.findings.append(
                Finding(
                    severity="warning",
                    kind="clock_graph",
                    message=(
                        f"RTL clock '{name}' has no create_clock in the XDC — its "
                        "crossings cannot be constrained as asynchronous"
                    ),
                    target=name,
                )
            )
    return res
