"""Audit the CDC-relevant exceptions in a Vivado XDC against the rtl-buddy-cdc crossing set.

Only ``create_clock``, ``set_clock_groups -asynchronous``, ``set_false_path``,
``set_max_delay`` and ``set_bus_skew`` are read. Findings:

* unconstrained crossing (blocker): a crossing with no matching XDC exception.
* over-waive (blocker): a false path or asynchronous group on a path rtl-buddy-cdc
  reports as not safely synchronized.
* missing bus skew (warning): a multi-bit crossing waived without ``set_bus_skew``.
* clock graph (warning or info): XDC clocks that differ from the RTL clocks.

Pure functions over the XDC text, the domain map and the report.
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
    """Return the bare names in a Tcl word such as ``[get_clocks clk_a]`` or ``[get_cells u_sync/*]``.

    Flags, ``get_*`` heads and ``-filter`` predicates are dropped and a trailing ``/*`` is trimmed.
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
    """Every value word following a repeated ``flag`` such as ``-group``."""
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
    """Read the CDC-relevant commands of an XDC/SDC into :class:`XdcConstraints`.

    The constraint reader handles continued lines, braces and nested collections.
    A ``-period`` computed with ``$var`` or ``[expr]`` is a number only when the
    reader's ``tcl`` backend evaluates it, otherwise ``None``. ``source`` names the
    file in reader warnings.
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
    """Diff the XDC's CDC exceptions against the crossing set and report violations.

    ``recognized_syncs`` are instance-path regexes for synchronizers the analyzer did not
    recognize (e.g. a blackboxed ``xpm_cdc_*``). Waiving a matching violation is not an
    over-waive, but the crossing must still be constrained.
    """
    res = AuditResult()
    recognized = []
    for p in recognized_syncs or []:
        try:
            recognized.append(re.compile(p))
        except re.error as e:
            raise FatalRtlBuddyError(
                f"--check-xdc: invalid recognized-syncs regex {p!r}: {e}"
            ) from e
    crossings = [
        c for c in domain_map.get("crossings", []) if c.get("async_per_sdc", True)
    ]

    # A flattening frontend (Yosys `flatten`) reports every capture instance as the top, so
    # cell-scoped exceptions cannot be matched; clock-level coverage is still audited.
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
        # A bare max_delay still times the path, so only `-datapath_only` covers a crossing.
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

    for v in cdc_report.get("violations", []):
        c = v.get("crossing") or {}
        src, dst = c.get("src_clock"), c.get("dst_clock")
        if not src or not dst:
            continue
        inst = "/".join(v.get("instance_path", [])) or c.get("dst_flop", "")
        if recognized and any(
            r.search(inst) or r.search(c.get("dst_flop", "")) for r in recognized
        ):
            continue
        pair = frozenset({src, dst})
        # max_delay still times the path, so it is not a waiver.
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
