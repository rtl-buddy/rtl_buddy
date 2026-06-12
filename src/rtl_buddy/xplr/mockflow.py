"""Synthetic DSE backend with known optima — the ``rb xplr mock`` harness.

mockflow looks like an EDA flow (EDA-flavored knobs in, EDA-flavored
metrics out, P0-schema records throughout) but returns instantly and is
backed by standard multi-modal benchmark functions whose optimum / Pareto
front is known analytically. It exists so the rb xplr analysis surface
and any agent/optimizer loop can be developed and CI-tested without
multi-hour EDA turnaround — and *scored*: because the ground truth is
exact, "did the agent optimize?" becomes a pass/fail number (regret for
single-objective, hypervolume + distance-to-front for multi-objective).

Scenarios (declarative knob specs, dispatch by name):

* ``rastrigin`` — single-objective. The four numeric knobs each map
  linearly from their declared range onto ``[-5.12, 5.12]`` (range
  midpoint -> 0) and feed the n=4 Rastrigin function
  ``f(x) = 10n + sum(x_i^2 - 10*cos(2*pi*x_i))``; the reported metric is
  ``wns_ns = -f(x) / 10`` (maximize). The global optimum is therefore
  ``wns_ns = 0.0`` exactly at the numeric-knob midpoints, surrounded by
  a lattice of local optima. Categorical knobs never affect the
  objective — any feasible categorical combination attains the optimum.
* ``zdt1`` — multi-objective. The numeric knobs map linearly onto
  ``[0, 1]``; the first (``partition.cut``) is ZDT1's ``x1`` and the
  rest feed ``g = 1 + 9 * mean(x_2..x_n)``. Metrics are
  ``lut_pct = 100 * f1`` (minimize) and ``delay_ns = 10 * f2``
  (minimize) with ``f1 = x1`` and ``f2 = g * (1 - sqrt(f1 / g))``. The
  analytic Pareto front is ``delay_ns = 10 * (1 - sqrt(lut_pct / 100))``
  for ``lut_pct`` in ``[0, 100]``, attained when every non-``x1``
  numeric knob sits at its range minimum (``g = 1``).

Conventions baked in (and relied on by the tests):

* **Feasibility cliff**: one categorical combination per scenario
  reports ``routed = false`` with the objective metrics *omitted*, but
  ``outcome.status`` stays ``"success"``. Rationale: the flow itself ran
  to completion — failing to route is a property of the design point,
  not a flow crash — and the P3 analysis layer already treats a
  ``routed=false`` metric on a *successful* outcome as the infeasibility
  marker (``status="failed"`` would instead mean mockflow itself broke).
* **Cost / layer model**: every knob carries a ``layer``; a run "costs"
  ``wall_clock_s = 60 + sum(layer cost of every knob whose value
  differs from its default)`` with source=600s, flow=240s, impl=60s —
  so touching a source knob is 10x the cost of an impl knob. The cost
  is pure bookkeeping: ``wall_clock_s`` is reported with a unit but no
  direction, so it never silently joins Pareto dominance (opt in with
  ``--metrics wall_clock_s:min``).
* **Determinism**: metrics are a pure function of
  ``(scenario, knob values, seed)``. With ``noise == 0`` (default) the
  seed is irrelevant; with ``noise > 0`` a Gaussian term with that
  sigma is added to each *objective* metric (never to ``wall_clock_s``
  or ``routed``), drawn from an RNG seeded by the canonical string
  ``"{scenario}|seed={seed}|{knobs json}"`` — same inputs, same noise,
  no wall-clock randomness anywhere.
* **Scoring math**: regret is ``|best_found - global_opt|`` in
  objective space. Hypervolume is the 2D staircase area dominated by
  the non-dominated points up to the documented reference point
  (110, 110) — 2D only, which covers both shipped multi-objective
  metrics; >2 objectives would need a real HV algorithm.
  ``front_hypervolume`` and ``distance_to_front`` are computed against
  the analytic front sampled at 1001 points (a documented
  approximation; the sampling error is far below any decision
  threshold).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Any

from ..errors import FatalRtlBuddyError
from .schema import ABSENT, ExperimentRecord

TOOL_NAME = "mockflow"
TOOL_VERSION = "1.0"

WALL_CLOCK_BASE_S = 60.0
LAYER_COST_S = {"source": 600.0, "flow": 240.0, "impl": 60.0}

_RASTRIGIN_BOUND = 5.12
_HV_REFERENCE = (110.0, 110.0)
_FRONT_SAMPLES = 1001


# ---------------------------------------------------------------------------
# declarative scenario specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnobSpec:
    """One mockflow knob: EDA-flavored name, typed domain, cost layer."""

    name: str
    type: str  # "float" | "int" | "choice"
    layer: str  # "source" | "flow" | "impl" (schema knob layer enum)
    default: Any
    lo: float | None = None  # numeric types
    hi: float | None = None
    choices: tuple[str, ...] = ()  # choice type

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "layer": self.layer,
            "default": self.default,
        }
        if self.type == "choice":
            out["choices"] = list(self.choices)
        else:
            out["range"] = [self.lo, self.hi]
        return out


@dataclass(frozen=True)
class Scenario:
    """A named synthetic landscape: knobs, metrics, cliffs — all declarative.

    ``infeasible_when`` is a tuple of categorical combinations (knob
    name -> required choice); a run matching *all* entries of any one
    combination reports ``routed = false`` with objectives omitted.
    The first numeric knob of a multi-objective scenario is the
    benchmark's ``x1`` (position is meaningful — see ``_zdt1``).
    """

    name: str
    objective: str  # "single" | "multi"
    description: str
    knobs: tuple[KnobSpec, ...]
    metric_meta: dict[str, dict[str, str]]
    infeasible_when: tuple[dict[str, str], ...]


SCENARIOS: dict[str, Scenario] = {
    "rastrigin": Scenario(
        name="rastrigin",
        objective="single",
        description=(
            "single-objective Rastrigin landscape: many local optima, one "
            "global optimum (wns_ns = 0.0) at the numeric-knob midpoints; "
            "categorical knobs only gate feasibility"
        ),
        knobs=(
            KnobSpec("unroll_factor", "int", "source", default=2, lo=1, hi=9),
            KnobSpec("fifo_depth", "int", "flow", default=8, lo=8, hi=24),
            KnobSpec("place.effort", "float", "impl", default=0.9, lo=0.0, hi=1.0),
            KnobSpec(
                "clk_uncertainty_ns", "float", "impl", default=0.1, lo=0.0, hi=0.4
            ),
            KnobSpec(
                "place.directive",
                "choice",
                "impl",
                default="default",
                choices=("default", "aggressive", "congestion"),
            ),
            KnobSpec("retime", "choice", "flow", default="off", choices=("off", "on")),
        ),
        metric_meta={
            "wns_ns": {"direction": "max", "unit": "ns"},
            "wall_clock_s": {"unit": "s"},
        },
        infeasible_when=({"place.directive": "congestion", "retime": "on"},),
    ),
    "zdt1": Scenario(
        name="zdt1",
        objective="multi",
        description=(
            "multi-objective ZDT1 landscape: lut_pct (min) vs delay_ns (min) "
            "with the analytic Pareto front delay_ns = 10*(1 - sqrt(lut_pct/"
            "100)), attained at unroll_factor=1, fifo_depth=2"
        ),
        knobs=(
            KnobSpec("partition.cut", "float", "flow", default=0.5, lo=0.0, hi=1.0),
            KnobSpec("unroll_factor", "int", "source", default=3, lo=1, hi=9),
            KnobSpec("fifo_depth", "int", "flow", default=6, lo=2, hi=18),
            KnobSpec(
                "place.directive",
                "choice",
                "impl",
                default="default",
                choices=("default", "aggressive"),
            ),
            KnobSpec(
                "route.strategy",
                "choice",
                "impl",
                default="timing",
                choices=("timing", "congestion"),
            ),
        ),
        metric_meta={
            "lut_pct": {"direction": "min", "unit": "%"},
            "delay_ns": {"direction": "min", "unit": "ns"},
            "wall_clock_s": {"unit": "s"},
        },
        infeasible_when=(
            {"place.directive": "aggressive", "route.strategy": "congestion"},
        ),
    ),
}


def get_scenario(name: str) -> Scenario:
    """Look up a scenario; unknown names fail with the known list."""

    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise FatalRtlBuddyError(
            f"unknown mockflow scenario {name!r}; "
            f"available: {', '.join(sorted(SCENARIOS))}"
        )
    return scenario


# ---------------------------------------------------------------------------
# knob resolution
# ---------------------------------------------------------------------------


def resolve_knobs(scenario: Scenario, values: dict[str, Any]) -> dict[str, Any]:
    """Validate agent-provided knob values and fill defaults.

    Unknown knob names, type mismatches, out-of-range numerics, and
    unknown choices all fail loudly with the allowed domain. Float
    knobs accept ints and are canonicalized to float.
    """

    specs = {spec.name: spec for spec in scenario.knobs}
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise FatalRtlBuddyError(
            f"unknown knob(s) for mockflow scenario '{scenario.name}': "
            f"{', '.join(repr(k) for k in unknown)}; "
            f"available: {', '.join(specs)}"
        )
    return {
        spec.name: _check_value(scenario, spec, values.get(spec.name, spec.default))
        for spec in scenario.knobs
    }


def _check_value(scenario: Scenario, spec: KnobSpec, value: Any) -> Any:
    where = f"mockflow scenario '{scenario.name}', knob '{spec.name}'"
    if spec.type == "choice":
        if not isinstance(value, str) or value not in spec.choices:
            raise FatalRtlBuddyError(
                f"{where}: invalid choice {value!r}; choices: {', '.join(spec.choices)}"
            )
        return value
    if spec.type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise FatalRtlBuddyError(
                f"{where}: expected an integer, got {value!r} ({type(value).__name__})"
            )
    else:  # float
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise FatalRtlBuddyError(
                f"{where}: expected a number, got {value!r} ({type(value).__name__})"
            )
        value = float(value)
    if not (spec.lo <= value <= spec.hi):
        raise FatalRtlBuddyError(
            f"{where}: value {value!r} out of range [{spec.lo}, {spec.hi}]"
        )
    return value


def _numeric_specs(scenario: Scenario) -> list[KnobSpec]:
    return [spec for spec in scenario.knobs if spec.type != "choice"]


def _unit(spec: KnobSpec, value: float) -> float:
    """Map a numeric knob value linearly onto [0, 1]."""

    return (value - spec.lo) / (spec.hi - spec.lo)


def _canonical_default(spec: KnobSpec) -> Any:
    return float(spec.default) if spec.type == "float" else spec.default


# ---------------------------------------------------------------------------
# landscapes (benchmark functions dressed as EDA metrics)
# ---------------------------------------------------------------------------


def _rastrigin_objectives(
    scenario: Scenario, knobs: dict[str, Any]
) -> dict[str, float]:
    xs = [
        (2.0 * _unit(spec, knobs[spec.name]) - 1.0) * _RASTRIGIN_BOUND
        for spec in _numeric_specs(scenario)
    ]
    f = 10.0 * len(xs) + sum(x * x - 10.0 * math.cos(2.0 * math.pi * x) for x in xs)
    return {"wns_ns": -f / 10.0}


def _zdt1_objectives(scenario: Scenario, knobs: dict[str, Any]) -> dict[str, float]:
    numeric = _numeric_specs(scenario)
    x = [_unit(spec, knobs[spec.name]) for spec in numeric]
    f1 = x[0]
    g = 1.0 + 9.0 * sum(x[1:]) / (len(x) - 1)
    f2 = g * (1.0 - math.sqrt(f1 / g))
    return {"lut_pct": 100.0 * f1, "delay_ns": 10.0 * f2}


_OBJECTIVES = {"rastrigin": _rastrigin_objectives, "zdt1": _zdt1_objectives}


def _infeasible(scenario: Scenario, knobs: dict[str, Any]) -> bool:
    return any(
        all(knobs.get(name) == choice for name, choice in combo.items())
        for combo in scenario.infeasible_when
    )


def _wall_clock_s(scenario: Scenario, knobs: dict[str, Any]) -> float:
    cost = WALL_CLOCK_BASE_S
    for spec in scenario.knobs:
        if knobs[spec.name] != _canonical_default(spec):
            cost += LAYER_COST_S[spec.layer]
    return cost


def evaluate(
    scenario_name: str,
    values: dict[str, Any] | None = None,
    *,
    seed: int = 0,
    noise: float = 0.0,
) -> dict[str, Any]:
    """Evaluate one knob vector; instant, deterministic, EDA-dressed.

    Returns ``{scenario, knobs, routed, metrics, metric_meta}`` where
    ``knobs`` is the fully resolved absolute knob state (defaults
    filled). Infeasible categorical combinations report
    ``routed = false`` and omit the objective metrics; ``wall_clock_s``
    is always present (you paid for the run either way).
    """

    scenario = get_scenario(scenario_name)
    if noise < 0:
        raise FatalRtlBuddyError(f"--noise must be >= 0, got {noise}")
    resolved = resolve_knobs(scenario, values or {})
    routed = not _infeasible(scenario, resolved)
    metrics: dict[str, Any] = {
        "routed": routed,
        "wall_clock_s": _wall_clock_s(scenario, resolved),
    }
    if routed:
        objectives = _OBJECTIVES[scenario.name](scenario, resolved)
        if noise > 0:
            rng = random.Random(
                f"{scenario.name}|seed={seed}|{json.dumps(resolved, sort_keys=True)}"
            )
            objectives = {
                name: value + rng.gauss(0.0, noise)
                for name, value in sorted(objectives.items())
            }
        metrics.update(objectives)
    metric_meta = {
        name: dict(scenario.metric_meta[name])
        for name in metrics
        if name in scenario.metric_meta
    }
    return {
        "scenario": scenario.name,
        "knobs": resolved,
        "routed": routed,
        "metrics": metrics,
        "metric_meta": metric_meta,
    }


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------


def _midpoint(spec: KnobSpec) -> float | int:
    mid = (spec.lo + spec.hi) / 2.0
    return int(mid) if spec.type == "int" else mid


def optimum_knobs(scenario_name: str) -> dict[str, Any]:
    """The documented global-optimum knob vector (single-objective only).

    Numeric knobs at their range midpoint (the Rastrigin x=0 mapping),
    categorical knobs at their (feasible) defaults.
    """

    scenario = get_scenario(scenario_name)
    return {
        spec.name: (
            _canonical_default(spec) if spec.type == "choice" else _midpoint(spec)
        )
        for spec in scenario.knobs
    }


def zdt1_front(n: int = _FRONT_SAMPLES) -> list[tuple[float, float]]:
    """Sample the analytic ZDT1 front in dressed units (lut_pct, delay_ns)."""

    return [
        (100.0 * (i / (n - 1)), 10.0 * (1.0 - math.sqrt(i / (n - 1)))) for i in range(n)
    ]


def ground_truth(scenario_name: str) -> dict[str, Any]:
    """The analytic optimum (single-obj) / Pareto front (multi-obj)."""

    scenario = get_scenario(scenario_name)
    if scenario.name == "rastrigin":
        return {
            "objective": "single",
            "metric": "wns_ns",
            "direction": "max",
            "optimum": {
                "knobs": optimum_knobs(scenario.name),
                "metrics": {"wns_ns": 0.0},
            },
            "description": (
                "every numeric knob at its range midpoint maps to the "
                "Rastrigin global minimum x=0, so wns_ns = -f(x)/10 = 0.0; "
                "any feasible categorical combination attains it"
            ),
        }
    return {
        "objective": "multi",
        "metrics": ["lut_pct", "delay_ns"],
        "directions": {"lut_pct": "min", "delay_ns": "min"},
        "front": {
            "equation": (
                "delay_ns = 10 * (1 - sqrt(lut_pct / 100)) for lut_pct in [0, 100]"
            ),
            "attained_when": {"unroll_factor": 1, "fifo_depth": 2},
            "samples": [list(p) for p in zdt1_front(21)],
        },
        "reference_point": {
            "lut_pct": _HV_REFERENCE[0],
            "delay_ns": _HV_REFERENCE[1],
        },
        "description": (
            "ZDT1: the front is reached when every numeric knob other than "
            "partition.cut sits at its range minimum (g = 1); partition.cut "
            "sweeps the front"
        ),
    }


def scenario_info(scenario_name: str) -> dict[str, Any]:
    """The ``rb xplr mock info`` payload for one scenario."""

    scenario = get_scenario(scenario_name)
    return {
        "name": scenario.name,
        "objective": scenario.objective,
        "description": scenario.description,
        "knobs": [spec.to_dict() for spec in scenario.knobs],
        "metric_meta": {k: dict(v) for k, v in scenario.metric_meta.items()},
        "cost_model": {
            "wall_clock_base_s": WALL_CLOCK_BASE_S,
            "layer_cost_s": dict(LAYER_COST_S),
            "note": (
                "wall_clock_s = base + layer cost of every knob whose value "
                "differs from its default (synthetic; runs are instant)"
            ),
        },
        "infeasible_when": [dict(c) for c in scenario.infeasible_when],
        "ground_truth": ground_truth(scenario.name),
    }


# ---------------------------------------------------------------------------
# experiment-record integration (reuses the P1 register/attach paths)
# ---------------------------------------------------------------------------


def register_doc(
    scenario_name: str, provided: dict[str, Any], resolved: dict[str, Any]
) -> dict[str, Any]:
    """Build the ``rb xplr register`` manifest for one mockflow run.

    Only knobs the agent explicitly provided enter the knob manifest,
    each recorded as ``from = <scenario default>`` (mockflow keeps no
    per-agent history); ``config_snapshot`` carries the scenario name
    plus the full resolved absolute knob state, which is also what
    ``mock score`` keys on.
    """

    scenario = get_scenario(scenario_name)
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
        "config_snapshot": {"scenario": scenario.name, "knobs": dict(resolved)},
        "provenance": {"tools": [{"name": TOOL_NAME, "version": TOOL_VERSION}]},
    }


def outcome_doc(result: dict[str, Any]) -> dict[str, Any]:
    """Build the ``rb xplr attach-outcome`` document for one evaluation.

    Shaped exactly as a valid ``attach-outcome --json`` input, and
    exposed verbatim as the ``outcome`` member of the ``mock run``
    machine payload so a stateless evaluation can be piped straight
    into ``attach-outcome``. ``status`` is always ``"success"`` — the
    synthetic flow ran to completion; an infeasible point is
    ``routed: false``, not a failure (the agent may override ``status``
    with its own judgment before attaching).
    """

    return {
        "status": "success",
        "metrics": dict(result["metrics"]),
        "metric_meta": {k: dict(v) for k, v in result["metric_meta"].items()},
    }


def is_mockflow_record(
    record: ExperimentRecord, scenario_name: str | None = None
) -> bool:
    """True if the record was produced by ``rb xplr mock run --register``."""

    tools = record.provenance.tools
    if tools is ABSENT or not any(t.name == TOOL_NAME for t in tools):
        return False
    snapshot = record.config_snapshot
    if snapshot is ABSENT or not isinstance(snapshot, dict):
        return False
    if scenario_name is None:
        return snapshot.get("scenario") in SCENARIOS
    return snapshot.get("scenario") == scenario_name


def mockflow_scenarios(records: list[ExperimentRecord]) -> list[str]:
    """The scenarios with at least one mockflow experiment in the ledger."""

    return sorted(
        {r.config_snapshot["scenario"] for r in records if is_mockflow_record(r)}
    )


# ---------------------------------------------------------------------------
# scoring (pure math; 2D-only hypervolume — documented limitation)
# ---------------------------------------------------------------------------


def _dominates_2d(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """a dominates b under minimization of both coordinates."""

    return a[0] <= b[0] and a[1] <= b[1] and (a[0] < b[0] or a[1] < b[1])


def nondominated_2d(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Non-dominated subset (min/min), deduplicated, sorted by f1."""

    unique = sorted(set(points))
    return [p for p in unique if not any(_dominates_2d(q, p) for q in unique if q != p)]


def hypervolume_2d(
    points: list[tuple[float, float]], ref: tuple[float, float]
) -> float:
    """Staircase area dominated by ``points`` up to ``ref`` (min/min).

    Points not strictly better than the reference point in both
    coordinates contribute nothing. 2D only — enough for every shipped
    multi-objective scenario; more objectives would need a real
    hypervolume algorithm.
    """

    eligible = [p for p in points if p[0] < ref[0] and p[1] < ref[1]]
    hv = 0.0
    prev_f2 = ref[1]
    for f1, f2 in nondominated_2d(eligible):
        if f2 >= prev_f2:
            continue
        hv += (ref[0] - f1) * (prev_f2 - f2)
        prev_f2 = f2
    return hv


def distance_to_front(
    points: list[tuple[float, float]], front: list[tuple[float, float]]
) -> float:
    """Mean min Euclidean distance of ``points`` to the sampled front."""

    return sum(min(math.dist(p, q) for q in front) for p in points) / len(points)


def score_records(
    records: list[ExperimentRecord], scenario_name: str
) -> dict[str, Any]:
    """Score the ledger's mockflow experiments against the ground truth.

    Single-objective: regret of the best found point. Multi-objective:
    hypervolume of the found non-dominated set vs the documented
    reference point, normalized by the analytic front's hypervolume,
    plus mean distance-to-front. Raises when the ledger holds no
    mockflow experiment for the scenario.
    """

    scenario = get_scenario(scenario_name)
    matching = [r for r in records if is_mockflow_record(r, scenario_name)]
    if not matching:
        raise FatalRtlBuddyError(
            f"no mockflow '{scenario_name}' experiments in the ledger — run "
            f"`rb xplr mock run --scenario {scenario_name} --register` first"
        )
    feasible: list[tuple[str, dict[str, Any]]] = []
    for record in matching:
        if record.outcome.status != "success":
            continue
        metrics = record.outcome.metrics
        metrics = {} if metrics is ABSENT else dict(metrics)
        if metrics.get("routed") is not True:
            continue
        feasible.append((record.id, metrics))

    if scenario.objective == "single":
        truth = ground_truth(scenario_name)
        optimum = truth["optimum"]["metrics"][truth["metric"]]
        scored = [
            (exp_id, metrics[truth["metric"]])
            for exp_id, metrics in feasible
            if isinstance(metrics.get(truth["metric"]), (int, float))
            and not isinstance(metrics.get(truth["metric"]), bool)
        ]
        payload: dict[str, Any] = {
            "scenario": scenario_name,
            "objective": "single",
            "metric": truth["metric"],
            "n_experiments": len(matching),
            "n_feasible": len(scored),
            "optimum": optimum,
            "best": None,
            "regret": None,
        }
        if scored:
            best_id, best_value = max(scored, key=lambda item: item[1])
            payload["best"] = {"id": best_id, truth["metric"]: best_value}
            payload["regret"] = abs(optimum - best_value)
        return payload

    # multi-objective (zdt1)
    scored_points = [
        (exp_id, (float(metrics["lut_pct"]), float(metrics["delay_ns"])))
        for exp_id, metrics in feasible
        if all(
            isinstance(metrics.get(m), (int, float))
            and not isinstance(metrics.get(m), bool)
            for m in ("lut_pct", "delay_ns")
        )
    ]
    nd_points = nondominated_2d([point for _, point in scored_points])
    nd_set = set(nd_points)
    nondominated = [
        {"id": exp_id, "lut_pct": point[0], "delay_ns": point[1]}
        for exp_id, point in scored_points
        if point in nd_set
    ]
    front = zdt1_front()
    front_hv = hypervolume_2d(front, _HV_REFERENCE)
    hv = hypervolume_2d(nd_points, _HV_REFERENCE)
    return {
        "scenario": scenario_name,
        "objective": "multi",
        "metrics": ["lut_pct", "delay_ns"],
        "n_experiments": len(matching),
        "n_feasible": len(scored_points),
        "reference_point": {
            "lut_pct": _HV_REFERENCE[0],
            "delay_ns": _HV_REFERENCE[1],
        },
        "nondominated": nondominated,
        "hypervolume": hv,
        "front_hypervolume": front_hv,
        "hypervolume_ratio": hv / front_hv if front_hv else 0.0,
        "distance_to_front": (
            distance_to_front(nd_points, front) if nd_points else None
        ),
    }
