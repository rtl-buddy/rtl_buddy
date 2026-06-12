"""Synthetic DSE backend with known optima — the ``rb xplr mock`` harness.

mockflow looks like an EDA flow (knobs such as ``fifo_depth`` and
``place.directive``, metrics such as ``wns_ns`` and ``lut_pct``) but is
backed by standard multi-modal benchmark functions with analytically
known optima, so agent loops and analysis code can be developed and
CI-tested instantly, and convergence can be *scored* (regret /
hypervolume) instead of eyeballed. Pure stdlib math; no license, no
subprocesses, no wall-clock randomness.

Scenarios (declarative knob specs, see :data:`SCENARIOS`):

* ``rastrigin`` — single-objective. Numeric knobs map linearly onto
  ``x_i in [-5.12, 5.12]`` (knob midpoint -> ``x_i = 0``); choice knobs
  add a fixed penalty per choice. ``wns_ns = -(rastrigin(x) +
  penalties) / 10`` (maximize), so the global optimum is ``wns_ns =
  0.0`` at the numeric midpoints + zero-penalty choices.
* ``zdt1`` — multi-objective. Numeric knobs map linearly onto ``x_i in
  [0, 1]`` (knob minimum -> ``x_i = 0``); objectives are ZDT1 dressed
  as ``lut_pct = 100 * f1`` and ``delay_ns = 10 * f2`` (both minimize).
  The analytic Pareto front is ``delay_ns = 10 * (1 - sqrt(lut_pct /
  100))`` for ``lut_pct in [0, 100]``, reached when every non-first
  numeric knob sits at its minimum and the choice penalty is zero.

Feasibility convention: certain choice combinations report ``routed:
false`` with the objective metrics omitted, while ``outcome.status``
stays ``"success"`` — the flow *ran to completion* and truthfully
reported an unroutable design point; ``"failed"`` is reserved for the
flow itself erroring. Optimizers must therefore filter on the
``routed`` metric, exactly as they would for a real P&R flow.

Synthetic cost model: ``wall_clock_s`` (fake — evaluation is instant)
is a base cost plus a per-knob charge for every knob whose value
differs from its default, weighted by the knob's ``layer``: changing a
``source`` knob (recompile everything) costs far more than a ``flow``
knob, which costs more than an ``impl`` knob.

Determinism: metrics are a pure function of ``(scenario, knob values,
seed)``. Optional Gaussian noise (``noise > 0``) on the objective
metrics is drawn from a :class:`random.Random` seeded with a canonical
string of exactly those inputs, so repeated identical calls — across
processes — return identical values. ``wall_clock_s`` and ``routed``
are never noisy.

Scoring (2-objective only; documented limitation — hypervolume uses
the 2D staircase formula): ``regret = |best_found - global_opt|`` in
objective space for single-objective scenarios; hypervolume vs the
scenario's documented reference point plus mean distance-to-front
(against the analytic front sampled at :data:`FRONT_SAMPLES` points)
for multi-objective scenarios.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..errors import FatalRtlBuddyError
from .schema import ABSENT, ExperimentRecord

MOCKFLOW_TOOL = {"name": "mockflow", "version": "1.0"}

# Synthetic cost model: base seconds + per-changed-knob charge by layer.
BASE_COST_S = 15.0
LAYER_COST_S = {"source": 600.0, "flow": 120.0, "impl": 30.0}

# How many points of the analytic Pareto front are sampled for
# distance-to-front and front-hypervolume.
FRONT_SAMPLES = 101


# ---------------------------------------------------------------------------
# declarative scenario specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnobSpec:
    """One mockflow knob: EDA-flavored name, type, range, layer, default.

    ``penalty`` (choice knobs only) is the per-choice objective penalty
    — part of the hidden landscape, so it is *not* exported by
    :func:`scenario_info` (the ground-truth block names the optimal
    choice instead).
    """

    name: str
    type: str  # "float" | "int" | "choice"
    layer: str  # "source" | "flow" | "impl"
    default: Any
    range: tuple[float, float] | None = None
    choices: tuple[str, ...] | None = None
    penalty: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "layer": self.layer,
            "default": self.default,
        }
        if self.range is not None:
            out["range"] = list(self.range)
        if self.choices is not None:
            out["choices"] = list(self.choices)
        return out


@dataclass(frozen=True)
class Scenario:
    """One named synthetic landscape."""

    name: str
    kind: str  # "single" | "multi"
    description: str
    knobs: tuple[KnobSpec, ...]
    # Each entry is a {knob_name: choice_value} combination that
    # reports routed=false (feasibility cliff).
    infeasible: tuple[dict[str, str], ...]


_RASTRIGIN = Scenario(
    name="rastrigin",
    kind="single",
    description=(
        "single-objective: maximize wns_ns over a Rastrigin landscape "
        "(many local optima, one global optimum at wns_ns = 0.0)"
    ),
    knobs=(
        KnobSpec("fifo_depth", "int", "source", 32, range=(2, 62)),
        KnobSpec("unroll_factor", "int", "source", 5, range=(1, 9)),
        KnobSpec("clk_uncertainty_ns", "float", "flow", 0.5, range=(0.0, 1.0)),
        KnobSpec(
            "partition.cut",
            "choice",
            "flow",
            "auto",
            choices=("auto", "min", "timing"),
            penalty={"auto": 0.0, "min": 2.0, "timing": 3.0},
        ),
        KnobSpec(
            "place.directive",
            "choice",
            "impl",
            "default",
            choices=("default", "explore", "aggressive", "quick"),
            penalty={"default": 1.0, "explore": 0.0, "aggressive": 2.0, "quick": 4.0},
        ),
    ),
    infeasible=({"partition.cut": "timing", "place.directive": "quick"},),
)

_ZDT1 = Scenario(
    name="zdt1",
    kind="multi",
    description=(
        "multi-objective: minimize (lut_pct, delay_ns) over ZDT1 with the "
        "analytic Pareto front delay_ns = 10 * (1 - sqrt(lut_pct / 100))"
    ),
    knobs=(
        KnobSpec("unroll_factor", "int", "source", 5, range=(0, 10)),
        KnobSpec("fifo_depth", "int", "source", 18, range=(2, 34)),
        KnobSpec("clk_uncertainty_ns", "float", "flow", 0.5, range=(0.0, 1.0)),
        KnobSpec(
            "place.directive",
            "choice",
            "impl",
            "balanced",
            choices=("balanced", "timing", "congestion"),
            penalty={"balanced": 0.0, "timing": 0.05, "congestion": 0.0},
        ),
    ),
    infeasible=({"place.directive": "congestion"},),
)

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (_RASTRIGIN, _ZDT1)}

# Documented hypervolume reference point per multi-objective scenario,
# in metric space (must be dominated by every feasible noise-free point).
REFERENCE_POINTS: dict[str, dict[str, float]] = {
    "zdt1": {"lut_pct": 110.0, "delay_ns": 11.0},
}

_METRIC_META = {
    "wns_ns": {"direction": "max", "unit": "ns"},
    "lut_pct": {"direction": "min", "unit": "%"},
    "delay_ns": {"direction": "min", "unit": "ns"},
    "wall_clock_s": {"direction": "min", "unit": "s"},
}


def get_scenario(name: str) -> Scenario:
    """Look up a scenario by name; unknown names fail with the known list."""

    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise FatalRtlBuddyError(
            f"unknown mockflow scenario {name!r}: "
            f"known scenarios: {', '.join(sorted(SCENARIOS))}"
        )
    return scenario


# ---------------------------------------------------------------------------
# knob resolution
# ---------------------------------------------------------------------------


def resolve_knobs(scenario: Scenario, doc: dict[str, Any]) -> dict[str, Any]:
    """Merge agent-provided knob values over the scenario defaults.

    ``doc`` maps knob name -> absolute value. Unknown knob names, wrong
    types, out-of-range numbers, and unknown choices all fail loudly.
    Returns the full resolved value map in knob declaration order.
    """

    specs = {k.name: k for k in scenario.knobs}
    unknown = sorted(set(doc) - set(specs))
    if unknown:
        raise FatalRtlBuddyError(
            f"mock run: unknown knob(s) for scenario '{scenario.name}': "
            f"{', '.join(repr(k) for k in unknown)}; "
            f"known knobs: {', '.join(specs)}"
        )
    resolved: dict[str, Any] = {}
    for spec in scenario.knobs:
        if spec.name not in doc:
            resolved[spec.name] = spec.default
            continue
        value = doc[spec.name]
        where = f"mock run: knob '{spec.name}'"
        if spec.type == "choice":
            if value not in spec.choices:
                raise FatalRtlBuddyError(
                    f"{where}: invalid choice {value!r}; "
                    f"choices: {', '.join(spec.choices)}"
                )
        elif spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FatalRtlBuddyError(f"{where} must be an integer, got {value!r}")
            _check_range(spec, value, where)
        elif spec.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FatalRtlBuddyError(f"{where} must be a number, got {value!r}")
            value = float(value)
            _check_range(spec, value, where)
        resolved[spec.name] = value
    return resolved


def _check_range(spec: KnobSpec, value: float, where: str) -> None:
    lo, hi = spec.range
    if not (lo <= value <= hi):
        raise FatalRtlBuddyError(f"{where} = {value!r} is out of range [{lo}, {hi}]")


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockOutcome:
    """One mockflow evaluation: the dressed-up metric view of a knob vector."""

    scenario: str
    values: dict[str, Any]
    seed: int
    noise: float
    routed: bool
    metrics: dict[str, Any]
    metric_meta: dict[str, dict[str, str]]


def evaluate(
    scenario: Scenario,
    values: dict[str, Any],
    *,
    seed: int = 0,
    noise: float = 0.0,
) -> MockOutcome:
    """Evaluate a fully-resolved knob vector. Pure and deterministic.

    ``values`` must come from :func:`resolve_knobs`. Noise (if any) is
    Gaussian with sigma ``noise``, applied to objective metrics only,
    drawn from an rng seeded by ``(scenario, values, seed)``.
    """

    if noise < 0:
        raise FatalRtlBuddyError(f"mock run: --noise must be >= 0, got {noise}")
    cost = _synthetic_cost(scenario, values)
    if not _is_routed(scenario, values):
        metrics: dict[str, Any] = {"routed": False, "wall_clock_s": cost}
        meta = {"wall_clock_s": dict(_METRIC_META["wall_clock_s"])}
        return MockOutcome(scenario.name, values, seed, noise, False, metrics, meta)

    rng = _rng(scenario.name, values, seed) if noise > 0 else None

    def _noisy(value: float) -> float:
        return value + rng.gauss(0.0, noise) if rng is not None else value

    metrics = {"routed": True, "wall_clock_s": cost}
    if scenario.kind == "single":
        metrics["wns_ns"] = _noisy(-_rastrigin_objective(scenario, values) / 10.0)
        names = ("wns_ns", "wall_clock_s")
    else:
        f1, f2 = _zdt1_objectives(scenario, values)
        metrics["lut_pct"] = _noisy(100.0 * f1)
        metrics["delay_ns"] = _noisy(10.0 * f2)
        names = ("lut_pct", "delay_ns", "wall_clock_s")
    meta = {name: dict(_METRIC_META[name]) for name in names}
    return MockOutcome(scenario.name, values, seed, noise, True, metrics, meta)


def _rng(scenario_name: str, values: dict[str, Any], seed: int) -> random.Random:
    """Deterministic rng from the canonical (scenario, values, seed) string."""

    key = json.dumps(
        {"scenario": scenario_name, "seed": seed, "knobs": values}, sort_keys=True
    )
    return random.Random(key)


def _is_routed(scenario: Scenario, values: dict[str, Any]) -> bool:
    for combo in scenario.infeasible:
        if all(values.get(name) == choice for name, choice in combo.items()):
            return False
    return True


def _synthetic_cost(scenario: Scenario, values: dict[str, Any]) -> float:
    cost = BASE_COST_S
    for spec in scenario.knobs:
        if values[spec.name] != spec.default:
            cost += LAYER_COST_S[spec.layer]
    return cost


def _choice_penalty(scenario: Scenario, values: dict[str, Any]) -> float:
    return sum(
        spec.penalty[values[spec.name]]
        for spec in scenario.knobs
        if spec.type == "choice"
    )


def _unit_interval(spec: KnobSpec, value: float) -> float:
    lo, hi = spec.range
    return (value - lo) / (hi - lo)


def _rastrigin_objective(scenario: Scenario, values: dict[str, Any]) -> float:
    """Rastrigin (A=10) over the scaled numeric knobs + choice penalties.

    Each numeric knob maps linearly onto ``x in [-5.12, 5.12]`` with its
    midpoint at ``x = 0``, so the global minimum (0.0) sits at the
    documented optimum: numeric midpoints + zero-penalty choices.
    """

    xs = [
        (_unit_interval(spec, values[spec.name]) - 0.5) * 2.0 * 5.12
        for spec in scenario.knobs
        if spec.type != "choice"
    ]
    a = 10.0
    f = a * len(xs) + sum(x * x - a * math.cos(2.0 * math.pi * x) for x in xs)
    return f + _choice_penalty(scenario, values)


def _zdt1_objectives(scenario: Scenario, values: dict[str, Any]) -> tuple[float, float]:
    """ZDT1 over the scaled numeric knobs; choice penalty lands on f2.

    The first numeric knob is ``x1``; the rest drive ``g``. Each maps
    linearly onto ``[0, 1]`` (knob minimum -> 0), so the Pareto front
    (g = 1) is reached with every non-first numeric knob at its minimum
    and a zero-penalty choice.
    """

    xs = [
        _unit_interval(spec, values[spec.name])
        for spec in scenario.knobs
        if spec.type != "choice"
    ]
    x1, rest = xs[0], xs[1:]
    g = 1.0 + 9.0 * sum(rest) / len(rest)
    f1 = x1
    f2 = g * (1.0 - math.sqrt(x1 / g)) + _choice_penalty(scenario, values)
    return f1, f2


# ---------------------------------------------------------------------------
# ground truth + info
# ---------------------------------------------------------------------------


def optimum_knobs(scenario: Scenario) -> dict[str, Any]:
    """The documented globally-optimal knob vector (single-objective)."""

    out: dict[str, Any] = {}
    for spec in scenario.knobs:
        if spec.type == "choice":
            out[spec.name] = min(spec.choices, key=lambda c: spec.penalty[c])
        else:
            lo, hi = spec.range
            mid = (lo + hi) / 2.0
            out[spec.name] = int(mid) if spec.type == "int" else mid
    return out


def front_samples(n: int = FRONT_SAMPLES) -> list[tuple[float, float]]:
    """Sample the analytic ZDT1 front in metric space (lut_pct, delay_ns)."""

    return [
        (100.0 * t, 10.0 * (1.0 - math.sqrt(t)))
        for t in (i / (n - 1) for i in range(n))
    ]


def ground_truth(scenario: Scenario) -> dict[str, Any]:
    """Analytic optimum / front description, for `mock info` and scoring."""

    if scenario.kind == "single":
        return {
            "kind": "single",
            "objective": {"metric": "wns_ns", "direction": "max"},
            "optimum_value": 0.0,
            "optimum_knobs": optimum_knobs(scenario),
            "mapping": (
                "numeric knobs scale linearly to x in [-5.12, 5.12] "
                "(midpoint -> 0); wns_ns = -(rastrigin(x) + choice "
                "penalties) / 10"
            ),
        }
    front_knobs: dict[str, Any] = {}
    numeric_seen = False
    for spec in scenario.knobs:
        if spec.type == "choice":
            front_knobs[spec.name] = min(spec.choices, key=lambda c: spec.penalty[c])
        elif not numeric_seen:
            numeric_seen = True
            lo, hi = spec.range
            front_knobs[spec.name] = f"free in [{lo}, {hi}] (sweeps the front)"
        else:
            front_knobs[spec.name] = spec.range[0]
    return {
        "kind": "multi",
        "objectives": [
            {"metric": "lut_pct", "direction": "min"},
            {"metric": "delay_ns", "direction": "min"},
        ],
        "front": "delay_ns = 10 * (1 - sqrt(lut_pct / 100)), lut_pct in [0, 100]",
        "front_knobs": front_knobs,
        "reference_point": dict(REFERENCE_POINTS[scenario.name]),
    }


def scenario_info(scenario: Scenario) -> dict[str, Any]:
    """The `mock info` block for one scenario."""

    return {
        "name": scenario.name,
        "kind": scenario.kind,
        "description": scenario.description,
        "knobs": [k.to_dict() for k in scenario.knobs],
        "metric_meta": {
            name: dict(_METRIC_META[name])
            for name in (
                ("wns_ns", "wall_clock_s")
                if scenario.kind == "single"
                else ("lut_pct", "delay_ns", "wall_clock_s")
            )
        },
        "infeasible": [dict(c) for c in scenario.infeasible],
        "cost_model": {
            "base_s": BASE_COST_S,
            "per_changed_knob_by_layer_s": dict(LAYER_COST_S),
        },
        "ground_truth": ground_truth(scenario),
    }


def scenario_infos(name: str | None = None) -> list[dict[str, Any]]:
    """Info blocks for one named scenario, or all of them."""

    if name is not None:
        return [scenario_info(get_scenario(name))]
    return [scenario_info(SCENARIOS[n]) for n in sorted(SCENARIOS)]


# ---------------------------------------------------------------------------
# ledger integration (`mock run --register` manifests)
# ---------------------------------------------------------------------------


def register_manifest(
    scenario: Scenario,
    provided: dict[str, Any],
    resolved: dict[str, Any],
    *,
    seed: int,
    noise: float,
) -> dict[str, Any]:
    """Build the `rb xplr register` manifest for one mockflow evaluation.

    Only the knobs the agent explicitly provided are recorded as deltas,
    each with ``from`` = the scenario default (mockflow is stateless —
    it has no previous-experiment notion). ``config_snapshot`` carries
    the full absolute knob state plus (scenario, seed, noise) so the
    evaluation can be reproduced exactly.
    """

    knobs = [
        {
            "name": spec.name,
            "from": spec.default,
            "to": resolved[spec.name],
            "layer": spec.layer,
        }
        for spec in scenario.knobs
        if spec.name in provided
    ]
    return {
        "knobs": knobs,
        "config_snapshot": {
            "scenario": scenario.name,
            "seed": seed,
            "noise": noise,
            "knobs": dict(resolved),
        },
        "provenance": {"tools": [dict(MOCKFLOW_TOOL)]},
    }


def outcome_manifest(outcome: MockOutcome) -> dict[str, Any]:
    """Build the `rb xplr attach-outcome` manifest for an evaluation."""

    return {
        "status": "success",
        "metrics": dict(outcome.metrics),
        "metric_meta": {k: dict(v) for k, v in outcome.metric_meta.items()},
    }


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def regret(best_value: float, optimum: float) -> float:
    """``|best_found - global_opt|`` in objective space."""

    return abs(best_value - optimum)


def pareto_front_2d(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Non-dominated subset of 2D min-min points, sorted by the first axis."""

    front: list[tuple[float, float]] = []
    best_y = math.inf
    for x, y in sorted(set(points)):
        if y < best_y:
            front.append((x, y))
            best_y = y
    return front


def hypervolume_2d(
    points: list[tuple[float, float]], ref: tuple[float, float]
) -> float:
    """Staircase hypervolume of 2D min-min points vs a reference point.

    Points that do not dominate ``ref`` contribute nothing. 2D only —
    that is all mockflow needs and the limitation is deliberate.
    """

    ref_x, ref_y = ref
    dominated = [(x, y) for x, y in points if x < ref_x and y < ref_y]
    hv = 0.0
    prev_y = ref_y
    for x, y in pareto_front_2d(dominated):
        hv += (ref_x - x) * (prev_y - y)
        prev_y = y
    return hv


def mean_distance_to_front(
    points: list[tuple[float, float]], front: list[tuple[float, float]]
) -> float:
    """Mean over ``points`` of the min Euclidean distance to ``front``."""

    if not points:
        raise FatalRtlBuddyError("distance-to-front needs at least one point")
    return sum(min(math.dist(p, f) for f in front) for p in points) / len(points)


def _mock_records(
    records: list[ExperimentRecord], scenario_name: str
) -> list[ExperimentRecord]:
    """Ledger records produced by `mock run --register` for one scenario."""

    out = []
    for record in records:
        tools = record.provenance.tools
        if tools is ABSENT or not any(t.name == "mockflow" for t in tools):
            continue
        snapshot = record.config_snapshot
        if snapshot is ABSENT or snapshot.get("scenario") != scenario_name:
            continue
        out.append(record)
    return out


def _feasible_metrics(
    records: list[ExperimentRecord], metric_names: tuple[str, ...]
) -> list[tuple[str, dict[str, float]]]:
    """(id, metrics) for successful, routed records carrying the objectives."""

    out = []
    for record in records:
        outcome = record.outcome
        if outcome.status != "success" or outcome.metrics is ABSENT:
            continue
        if outcome.metrics.get("routed") is not True:
            continue
        if not all(name in outcome.metrics for name in metric_names):
            continue
        out.append((record.id, {n: outcome.metrics[n] for n in metric_names}))
    return out


def score_scenario(
    records: list[ExperimentRecord], scenario: Scenario
) -> dict[str, Any] | None:
    """Score one scenario's mockflow records against ground truth.

    Returns ``None`` when the ledger holds no mockflow records for the
    scenario. When records exist but none is feasible (all fell off the
    cliff), the score still reports ``n_feasible: 0`` without
    regret/hypervolume keys — that *is* the scoring answer.
    """

    mine = _mock_records(records, scenario.name)
    if not mine:
        return None
    truth = ground_truth(scenario)
    if scenario.kind == "single":
        feasible = _feasible_metrics(mine, ("wns_ns",))
        score: dict[str, Any] = {
            "scenario": scenario.name,
            "kind": "single",
            "objective": "wns_ns",
            "direction": "max",
            "optimum": truth["optimum_value"],
            "n_records": len(mine),
            "n_feasible": len(feasible),
        }
        if feasible:
            best_id, best = max(feasible, key=lambda item: item[1]["wns_ns"])
            score["best"] = {"id": best_id, "wns_ns": best["wns_ns"]}
            score["regret"] = regret(best["wns_ns"], truth["optimum_value"])
        return score

    feasible = _feasible_metrics(mine, ("lut_pct", "delay_ns"))
    ref_map = REFERENCE_POINTS[scenario.name]
    ref = (ref_map["lut_pct"], ref_map["delay_ns"])
    score = {
        "scenario": scenario.name,
        "kind": "multi",
        "objectives": ["lut_pct", "delay_ns"],
        "reference_point": dict(ref_map),
        "n_records": len(mine),
        "n_feasible": len(feasible),
    }
    if feasible:
        points = [(m["lut_pct"], m["delay_ns"]) for _, m in feasible]
        nondom = pareto_front_2d(points)
        front = front_samples()
        hv = hypervolume_2d(points, ref)
        front_hv = hypervolume_2d(front, ref)
        score.update(
            n_nondominated=len(nondom),
            hypervolume=hv,
            front_hypervolume=front_hv,
            hypervolume_ratio=hv / front_hv,
            distance_to_front=mean_distance_to_front(nondom, front),
            nondominated=[{"lut_pct": x, "delay_ns": y} for x, y in nondom],
        )
    return score


def score_ledger(
    records: list[ExperimentRecord], scenario_name: str | None = None
) -> list[dict[str, Any]]:
    """Score every (or one named) scenario with mockflow records in the ledger.

    Fails loudly when nothing is scorable — an empty score would just
    make an agent loop think it converged on nothing.
    """

    names = [scenario_name] if scenario_name is not None else sorted(SCENARIOS)
    scores = []
    for name in names:
        score = score_scenario(records, get_scenario(name))
        if score is not None:
            scores.append(score)
    if not scores:
        what = f"scenario '{scenario_name}'" if scenario_name else "any scenario"
        raise FatalRtlBuddyError(
            f"mock score: the ledger has no mockflow experiments for {what} — "
            "run `rb xplr mock run --register` first"
        )
    return scores
