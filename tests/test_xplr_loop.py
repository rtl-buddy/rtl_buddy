"""Closed-loop test: a scripted coordinate-descent agent drives `xplr register`, `mock run` and `attach-outcome` through the machine-mode CLI.

The agent keeps its state in the ledger (`xplr frontier` and `xplr show`) and reads only knob domains from `mock info`, never `ground_truth`. Regret must fall on `rastrigin` and hypervolume must grow on `zdt1`. See `docs/concepts/xplr.md` for the manifest conventions.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy

# mockflow has no RTL, so the agent declares the source pin; `xplr register` takes any hex sha verbatim.
_PINNED_SHA = "feedc0de" * 5
_AGENT = "scripted-coordinate-descent"


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run one rb invocation through RtlBuddy.run() and release locks."""
    rb = RtlBuddy(name="test_xplr_loop")
    monkeypatch.setattr(sys, "argv", ["rb", *argv])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        code = rb.run()
    finally:
        rb._artifact_locks.release_all()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class ScriptedAgent:
    """Coordinate descent with a halving step schedule, driven through the CLI.

    Each sweep line-searches every numeric knob from the ledger-best point. Each evaluation is three CLI calls: register, ``mock run``, attach-outcome.
    """

    def __init__(self, scenario: str, monkeypatch, capsys):
        self.scenario = scenario
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        info = self._machine(["xplr", "mock", "info", "--scenario", scenario])
        # Knob domains only; ground_truth is reserved for `mock score`.
        self.specs = {k["name"]: k for k in info["knobs"]}
        self.defaults = {k["name"]: k["default"] for k in info["knobs"]}
        self.evals = 0

    def _machine(self, argv: list[str], *, stdin: str | None = None) -> dict:
        code, out, _ = _run(
            ["--machine", *argv], self.monkeypatch, self.capsys, stdin=stdin
        )
        assert code == 0, out
        envelope = json.loads(out)
        assert {"command", "exit_code", "meta", "payload"} <= set(envelope)
        return envelope["payload"]

    # -- loop primitives -----------------------------------------------------

    def evaluate(
        self,
        values: dict,
        *,
        parent: str | None,
        parent_knobs: dict,
        hypothesis: str,
        rationale: str,
    ) -> tuple[str, dict]:
        """Run register, mock run and attach-outcome for one knob vector."""
        resolved = {**self.defaults, **values}
        knobs = [
            {
                "name": name,
                "from": parent_knobs.get(name, self.defaults[name]),
                "to": value,
                "layer": self.specs[name]["layer"],
                "rationale": rationale,
            }
            for name, value in resolved.items()
            if value != parent_knobs.get(name, self.defaults[name])
        ]
        manifest = {
            "hypothesis": hypothesis,
            "knobs": knobs,
            "config_snapshot": {"scenario": self.scenario, "knobs": resolved},
            "source": {"git_sha": _PINNED_SHA},
            "provenance": {
                "tools": [{"name": "mockflow", "version": "1.0"}],
                "agent": _AGENT,
            },
        }
        if parent is not None:
            manifest["parent"] = parent
        exp_id = self._machine(
            ["xplr", "register", "--json", "-"], stdin=json.dumps(manifest)
        )["id"]
        run = self._machine(
            ["xplr", "mock", "run", "--scenario", self.scenario, "--json", "-"],
            stdin=json.dumps(resolved),
        )
        outcome = {
            "status": "success",
            "metrics": run["metrics"],
            "metric_meta": run["metric_meta"],
        }
        record = self._machine(
            ["xplr", "attach-outcome", exp_id, "--json", "-"],
            stdin=json.dumps(outcome),
        )["record"]
        self.evals += 1
        return exp_id, record["outcome"]["metrics"]

    def best_point(self) -> tuple[str, dict, dict]:
        """Return the ledger's current best: the frontier head and its knob state."""
        frontier = self._machine(["xplr", "frontier"])["frontier"]
        assert frontier, "frontier must never be empty once a run succeeded"
        best = frontier[0]
        record = self._machine(["xplr", "show", best["id"]])["record"]
        return best["id"], record["config_snapshot"]["knobs"], best["metrics"]

    def score(self) -> dict:
        return self._machine(["xplr", "mock", "score", "--scenario", self.scenario])

    # -- heuristic -----------------------------------------------------------

    def _clamp(self, name: str, value):
        spec = self.specs[name]
        lo, hi = spec["range"]
        value = max(lo, min(hi, value))
        return int(round(value)) if spec["type"] == "int" else value

    def sweep(self, steps: dict, *, objective) -> None:
        """Run one sweep of greedy line search per knob.

        ``objective(metrics)`` returns a number to minimize, +inf for infeasible points.
        """
        for name, step in steps.items():
            best_id, base, best_metrics = self.best_point()
            best_value = objective(best_metrics)
            for sign in (1, -1):
                current = base[name]
                while True:
                    candidate = self._clamp(name, current + sign * step)
                    if candidate == current:
                        break
                    _, metrics = self.evaluate(
                        {**{k: v for k, v in base.items()}, name: candidate},
                        parent=best_id,
                        parent_knobs=base,
                        hypothesis=(
                            f"probing {name} {'up' if sign > 0 else 'down'} "
                            f"by {step} from the frontier point {best_id}"
                        ),
                        rationale=(
                            f"coordinate descent: {name} moved "
                            f"{base[name]!r} -> {candidate!r} to test the "
                            "local slope of the objective"
                        ),
                    )
                    value = objective(metrics)
                    if value >= best_value:
                        break  # first regression ends this direction
                    best_value = value
                    current = candidate
                    best_id, base, _ = self.best_point()
                if best_value < objective(best_metrics):
                    break  # + direction improved; skip the - probe


def _feasible(metrics: dict) -> bool:
    return metrics.get("routed") is True


# ---------------------------------------------------------------------------
# rastrigin: regret must fall and end small
# ---------------------------------------------------------------------------


def test_rastrigin_loop_regret_decreases(minimal_project: Path, monkeypatch, capsys):
    agent = ScriptedAgent("rastrigin", monkeypatch, capsys)

    def objective(metrics: dict) -> float:
        if not _feasible(metrics) or "wns_ns" not in metrics:
            return float("inf")
        return -metrics["wns_ns"]  # wns_ns is max; sweep() minimizes

    # iteration 1: the baseline nobody tuned
    agent.evaluate(
        {},
        parent=None,
        parent_knobs=agent.defaults,
        hypothesis="baseline at tool defaults",
        rationale="anchor the search before touching anything",
    )
    regret_after_iter1 = agent.score()["regret"]
    assert regret_after_iter1 > 1.0  # defaults are genuinely bad

    numeric = [n for n, s in agent.specs.items() if s["type"] != "choice"]
    steps = {
        name: (agent.specs[name]["range"][1] - agent.specs[name]["range"][0]) / 4
        for name in numeric
    }
    regrets = [regret_after_iter1]
    for _ in range(3):  # K = 3 halving sweeps
        agent.sweep(steps, objective=objective)
        regrets.append(agent.score()["regret"])
        steps = {
            name: (
                max(1, round(step / 2))
                if agent.specs[name]["type"] == "int"
                else step / 2
            )
            for name, step in steps.items()
        }

    # regret falls from iteration 1 to K, and ends under a generous bound
    assert regrets[-1] < regrets[0]
    assert regrets[-1] < 2.0, regrets
    # the trail is monotone: best-so-far can never get worse
    assert all(b <= a for a, b in zip(regrets, regrets[1:]))
    # every probe is in the ledger with rationale: the reasoning trail
    score = agent.score()
    assert score["n_experiments"] == agent.evals


# ---------------------------------------------------------------------------
# zdt1: hypervolume must grow
# ---------------------------------------------------------------------------


def test_zdt1_loop_hypervolume_grows(minimal_project: Path, monkeypatch, capsys):
    agent = ScriptedAgent("zdt1", monkeypatch, capsys)

    def objective(metrics: dict) -> float:
        if not _feasible(metrics) or "delay_ns" not in metrics:
            return float("inf")
        return metrics["delay_ns"]  # push the front down at fixed lut_pct

    agent.evaluate(
        {},
        parent=None,
        parent_knobs=agent.defaults,
        hypothesis="baseline at tool defaults",
        rationale="anchor the search before touching anything",
    )
    hv_after_iter1 = agent.score()["hypervolume"]
    assert hv_after_iter1 > 0

    # phase 1: drive delay_ns down at the default partition.cut
    numeric = [n for n, s in agent.specs.items() if s["type"] != "choice"]
    steps = {
        name: (agent.specs[name]["range"][1] - agent.specs[name]["range"][0]) / 4
        for name in numeric
        if name != "partition.cut"
    }
    agent.sweep(steps, objective=objective)
    hv_mid = agent.score()["hypervolume"]

    # phase 2: spread the front by sweeping partition.cut from the best
    best_id, base, _ = agent.best_point()
    for cut in (0.1, 0.9):
        agent.evaluate(
            {**base, "partition.cut": cut},
            parent=best_id,
            parent_knobs=base,
            hypothesis=(
                "knob-effect history says only partition.cut trades lut_pct "
                "against delay_ns; populate the front at cut="
                f"{cut}"
            ),
            rationale=f"spread the frontier: partition.cut -> {cut}",
        )

    final = agent.score()
    # hypervolume grows from iteration 1 to K ...
    assert hv_mid > hv_after_iter1
    assert final["hypervolume"] > hv_mid
    # ... and converges toward the analytic front (generous bounds)
    assert final["hypervolume_ratio"] > 0.6
    assert final["distance_to_front"] < 5.0
    assert len(final["nondominated"]) >= 3
