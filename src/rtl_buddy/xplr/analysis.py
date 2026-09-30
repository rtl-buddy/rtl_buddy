"""Pure analysis views over the ``rb xplr`` ledger: Pareto frontier, pairwise diff and per-knob effect history.

Dominance rules:

* Only ``success`` experiments participate.
* A boolean metric ``routed`` that is ``false`` marks an experiment infeasible. Infeasible experiments are listed under ``infeasible``, not on the frontier.
* Dominance is computed over numeric metrics with a declared ``min``/``max`` direction, from ``metric_meta`` or ``--metrics``. An experiment missing one of them is excluded with a reason. Metrics without a direction are not used for dominance but are still reported on each member.
* X dominates Y if it is at least as good on every such metric and strictly better on one.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from ..errors import FatalRtlBuddyError
from .schema import ABSENT, ExperimentRecord, Knob, MetricMeta

DIRECTIONS = ("min", "max")
FEASIBILITY_METRIC = "routed"

_METRIC_NAME_RE = re.compile(r"^[^\s:*,+]+$")


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """True for JSON numbers, excluding bools."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _metrics_of(record: ExperimentRecord) -> dict[str, Any]:
    metrics = record.outcome.metrics
    return {} if metrics is ABSENT else dict(metrics)


def _meta_of(record: ExperimentRecord) -> dict[str, MetricMeta]:
    meta = record.outcome.metric_meta
    return {} if meta is ABSENT else dict(meta)


def _knobs_by_name(record: ExperimentRecord) -> dict[str, Knob]:
    return {knob.name: knob for knob in record.knobs}


def _assess(delta: float, direction: str | None) -> str:
    """Return equal, better, worse or unknown for a metric delta (B - A)."""

    if delta == 0:
        return "equal"
    if direction not in DIRECTIONS:
        return "unknown"
    improved = delta < 0 if direction == "min" else delta > 0
    return "better" if improved else "worse"


# ---------------------------------------------------------------------------
# option parsing
# ---------------------------------------------------------------------------


def parse_metric_directions(spec: str) -> dict[str, str]:
    """Parse a ``--metrics`` override: ``name:min,name2:max``."""

    directions: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, direction = item.partition(":")
        name = name.strip()
        direction = direction.strip()
        if not sep or not _METRIC_NAME_RE.match(name) or direction not in DIRECTIONS:
            raise FatalRtlBuddyError(
                f"invalid --metrics entry {item!r}: expected 'name:min' or "
                "'name:max' (comma-separated)"
            )
        if name in directions and directions[name] != direction:
            raise FatalRtlBuddyError(
                f"--metrics declares metric '{name}' as both "
                f"'{directions[name]}' and '{direction}'"
            )
        directions[name] = direction
    if not directions:
        raise FatalRtlBuddyError(
            f"invalid --metrics value {spec!r}: expected 'name:min,name2:max'"
        )
    return directions


def parse_preference(expr: str) -> dict[str, float]:
    """Parse a ``--prefer`` expression such as ``0.7*lut_pct+0.3*delay_ns`` into ``{metric: weight}``.

    Terms are comma- or plus-separated ``weight*metric``; a bare metric has weight 1.
    """

    weights: dict[str, float] = {}
    for term in re.split(r"[+,]", expr):
        term = term.strip()
        if not term:
            continue
        lhs, sep, rhs = term.partition("*")
        if sep:
            weight_text, name = lhs.strip(), rhs.strip()
        else:
            weight_text, name = "1", lhs.strip()
        try:
            weight = float(weight_text)
        except ValueError:
            weight = None
        if weight is None or not _METRIC_NAME_RE.match(name):
            raise FatalRtlBuddyError(
                f"invalid --prefer term {term!r}: expected 'weight*metric' "
                "(comma/plus-separated), e.g. '0.7*lut_pct+0.3*delay_ns'"
            )
        weights[name] = weights.get(name, 0.0) + weight
    if not weights:
        raise FatalRtlBuddyError(
            f"invalid --prefer expression {expr!r}: expected "
            "'weight*metric' terms, e.g. '0.7*lut_pct+0.3*delay_ns'"
        )
    return weights


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------


def _declared_directions(
    candidates: list[ExperimentRecord],
    overrides: dict[str, str] | None,
) -> dict[str, str]:
    """Merge record-level metric directions; overrides win."""

    directions: dict[str, str] = {}
    declared_by: dict[str, str] = {}
    for record in candidates:
        for name, meta in _meta_of(record).items():
            direction = meta.direction
            if direction not in DIRECTIONS:
                continue
            if name in directions and directions[name] != direction:
                if overrides and name in overrides:
                    continue  # the override settles it below
                raise FatalRtlBuddyError(
                    f"metric '{name}' is declared '{directions[name]}' by "
                    f"{declared_by[name]} but '{direction}' by {record.id} — "
                    "settle it with --metrics "
                    f"{name}:{direction}"
                )
            directions[name] = direction
            declared_by[name] = record.id
    if overrides:
        directions.update(overrides)
    return directions


def pareto_frontier(
    records: list[ExperimentRecord],
    *,
    direction_overrides: dict[str, str] | None = None,
    preference: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return the Pareto frontier as ``{metrics, frontier, dominated, infeasible, excluded}``.

    With ``preference`` (``{metric: weight}``), each frontier member gets a ``preference_score`` (lower is better) and the frontier is sorted by it; no member is dropped.
    """

    infeasible: list[str] = []
    excluded: list[dict[str, str]] = []
    candidates: list[ExperimentRecord] = []
    for record in records:
        status = record.outcome.status
        if status != "success":
            excluded.append(
                {"id": record.id, "reason": f"outcome.status is '{status}'"}
            )
            continue
        metrics = _metrics_of(record)
        if metrics.get(FEASIBILITY_METRIC) is False:
            infeasible.append(record.id)
            continue
        candidates.append(record)

    directions = _declared_directions(candidates, direction_overrides)
    dominance_metrics = sorted(
        name
        for name in directions
        if any(_is_number(_metrics_of(r).get(name)) for r in candidates)
    )
    if direction_overrides:
        missing = sorted(set(direction_overrides) - set(dominance_metrics))
        if missing:
            raise FatalRtlBuddyError(
                "--metrics names metric(s) with no numeric value in any "
                f"eligible experiment: {', '.join(missing)}"
            )
    if candidates and not dominance_metrics:
        raise FatalRtlBuddyError(
            "no numeric metric has a declared direction (record-level "
            "metric_meta or --metrics name:min,name2:max) — nothing to "
            "compute dominance over"
        )

    members: list[tuple[ExperimentRecord, dict[str, float]]] = []
    for record in candidates:
        metrics = _metrics_of(record)
        missing = [m for m in dominance_metrics if not _is_number(metrics.get(m))]
        if missing:
            excluded.append(
                {
                    "id": record.id,
                    "reason": "missing dominance metric(s): " + ", ".join(missing),
                }
            )
            continue
        members.append((record, {m: float(metrics[m]) for m in dominance_metrics}))

    def dominates(x: dict[str, float], y: dict[str, float]) -> bool:
        strictly_better = False
        for name in dominance_metrics:
            xv, yv = x[name], y[name]
            if directions[name] == "max":
                xv, yv = -xv, -yv
            if xv > yv:
                return False
            if xv < yv:
                strictly_better = True
        return strictly_better

    frontier: list[dict[str, Any]] = []
    dominated: list[dict[str, Any]] = []
    for record, values in members:
        dominated_by = sorted(
            other.id
            for other, other_values in members
            if other.id != record.id and dominates(other_values, values)
        )
        if dominated_by:
            dominated.append({"id": record.id, "dominated_by": dominated_by})
        else:
            frontier.append({"id": record.id, "metrics": _metrics_of(record)})

    if preference is not None:
        unknown = sorted(set(preference) - set(dominance_metrics))
        if unknown:
            raise FatalRtlBuddyError(
                "--prefer references metric(s) outside the dominance set "
                f"({', '.join(unknown)}); available: "
                f"{', '.join(dominance_metrics) or '(none)'}"
            )
        for member in frontier:
            member["preference_score"] = sum(
                weight
                * (
                    -float(member["metrics"][name])
                    if directions[name] == "max"
                    else float(member["metrics"][name])
                )
                for name, weight in preference.items()
            )
        frontier.sort(key=lambda m: (m["preference_score"], m["id"]))

    units: dict[str, str] = {}
    for record, _ in members:
        for name, meta in _meta_of(record).items():
            if name in dominance_metrics and isinstance(meta.unit, str):
                units.setdefault(name, meta.unit)
    metrics_out: list[dict[str, str]] = []
    for name in dominance_metrics:
        entry = {"name": name, "direction": directions[name]}
        if name in units:
            entry["unit"] = units[name]
        metrics_out.append(entry)

    return {
        "metrics": metrics_out,
        "frontier": frontier,
        "dominated": dominated,
        "infeasible": infeasible,
        "excluded": excluded,
    }


# ---------------------------------------------------------------------------
# pairwise diff (knobs and outcome)
# ---------------------------------------------------------------------------


def diff_records(a: ExperimentRecord, b: ExperimentRecord) -> dict[str, Any]:
    """Return the knob delta and direction-aware outcome delta between two experiments.

    Knobs are compared by name on their ``to`` values: ``added`` (only in B), ``reverted`` (only in A), ``changed`` and ``unchanged``. Both full manifests are included.
    """

    knobs_a = _knobs_by_name(a)
    knobs_b = _knobs_by_name(b)
    added = [knobs_b[n].to_dict() for n in sorted(set(knobs_b) - set(knobs_a))]
    reverted = [knobs_a[n].to_dict() for n in sorted(set(knobs_a) - set(knobs_b))]
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for name in sorted(set(knobs_a) & set(knobs_b)):
        if knobs_a[name].to == knobs_b[name].to:
            unchanged.append(name)
        else:
            changed.append(
                {
                    "name": name,
                    "a": {"from": knobs_a[name].from_, "to": knobs_a[name].to},
                    "b": {"from": knobs_b[name].from_, "to": knobs_b[name].to},
                }
            )
    knob_delta = {
        "added": added,
        "changed": changed,
        "reverted": reverted,
        "unchanged": unchanged,
        "manifest_a": [k.to_dict() for k in a.knobs],
        "manifest_b": [k.to_dict() for k in b.knobs],
    }

    metrics_a = _metrics_of(a)
    metrics_b = _metrics_of(b)
    meta = {name: m for name, m in _meta_of(a).items()}
    meta.update(_meta_of(b))  # B's declaration wins
    metric_rows: list[dict[str, Any]] = []
    common = sorted(set(metrics_a) & set(metrics_b))
    for name in common:
        va, vb = metrics_a[name], metrics_b[name]
        if not (_is_number(va) and _is_number(vb)):
            continue
        direction = None
        if name in meta and meta[name].direction in DIRECTIONS:
            direction = meta[name].direction
        delta = vb - va
        metric_rows.append(
            {
                "name": name,
                "a": va,
                "b": vb,
                "delta": delta,
                "direction": direction,
                "assessment": _assess(delta, direction),
            }
        )
    outcome_delta = {
        "status_a": a.outcome.status,
        "status_b": b.outcome.status,
        "metrics": metric_rows,
        "only_a": {n: metrics_a[n] for n in sorted(set(metrics_a) - set(metrics_b))},
        "only_b": {n: metrics_b[n] for n in sorted(set(metrics_b) - set(metrics_a))},
    }

    return {
        "a": a.id,
        "b": b.id,
        "knob_delta": knob_delta,
        "outcome_delta": outcome_delta,
    }


# ---------------------------------------------------------------------------
# per-knob effect history
# ---------------------------------------------------------------------------


def knob_effect(records: list[ExperimentRecord], name: str) -> dict[str, Any]:
    """Return the effect history of one knob.

    Each experiment that declares the knob yields ``{exp, status, from, to, rationale?, metrics_after, parent?, metrics_parent_delta?}``. ``metrics_parent_delta`` (child minus parent) is present when the parent is in the ledger with metrics.

    If no experiment declares the knob, the payload adds ``known_knobs`` and close-match ``suggestions``.
    """

    by_id = {record.id: record for record in records}
    effects: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r.id):
        knob = _knobs_by_name(record).get(name)
        if knob is None:
            continue
        entry: dict[str, Any] = {
            "exp": record.id,
            "status": record.outcome.status,
            "from": knob.from_,
            "to": knob.to,
        }
        if isinstance(knob.rationale, str):
            entry["rationale"] = knob.rationale
        entry["metrics_after"] = _metrics_of(record)
        parent_id = record.parent
        if isinstance(parent_id, str):
            entry["parent"] = parent_id
            parent = by_id.get(parent_id)
            if parent is not None:
                parent_metrics = _metrics_of(parent)
                deltas = {
                    metric: entry["metrics_after"][metric] - parent_metrics[metric]
                    for metric in sorted(
                        set(entry["metrics_after"]) & set(parent_metrics)
                    )
                    if _is_number(entry["metrics_after"][metric])
                    and _is_number(parent_metrics[metric])
                }
                if deltas:
                    entry["metrics_parent_delta"] = deltas
        effects.append(entry)
    payload: dict[str, Any] = {"knob": name, "effects": effects}
    if not effects:
        known = sorted({knob.name for record in records for knob in record.knobs})
        payload["known_knobs"] = known
        payload["suggestions"] = difflib.get_close_matches(name, known, n=3)
    return payload
