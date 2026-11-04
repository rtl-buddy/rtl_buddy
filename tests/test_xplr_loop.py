"""Machine-mode experiment trails preserve outcomes, parents and frontier scores."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy

# mockflow has no RTL, so the agent declares the source pin; `xplr register` takes any hex sha verbatim.
_PINNED_SHA = "feedc0de" * 5
_AGENT = "workflow-contract"


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


class ExperimentTrail:
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
        if parent is not None:
            assert record["parent"] == parent
        assert record["hypothesis"] == hypothesis
        assert all(k["rationale"] == rationale for k in record["knobs"])
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


def test_rastrigin_trail_keeps_the_best_outcome(
    minimal_project: Path, monkeypatch, capsys
):
    trail = ExperimentTrail("rastrigin", monkeypatch, capsys)
    baseline, _ = trail.evaluate(
        {},
        parent=None,
        parent_knobs=trail.defaults,
        hypothesis="baseline",
        rationale="record the defaults",
    )
    initial = trail.score()
    assert initial["regret"] > 1.0
    assert trail.best_point()[0] == baseline

    optimum = {
        "unroll_factor": 5,
        "fifo_depth": 16,
        "place.effort": 0.5,
        "clk_uncertainty_ns": 0.2,
    }
    best, _ = trail.evaluate(
        optimum,
        parent=baseline,
        parent_knobs=trail.defaults,
        hypothesis="numeric midpoints minimize Rastrigin",
        rationale="exercise an improving outcome",
    )
    improved = trail.score()
    assert improved["regret"] == pytest.approx(0.0)
    best_id, knobs, _ = trail.best_point()
    assert best_id == best
    assert knobs == {**trail.defaults, **optimum}

    trail.evaluate(
        {},
        parent=best,
        parent_knobs=knobs,
        hypothesis="restore the baseline",
        rationale="retain a worse outcome without replacing the best",
    )
    final = trail.score()
    assert final["regret"] == improved["regret"]
    assert final["n_experiments"] == trail.evals == 3
    assert trail.best_point()[0] == best


def test_zdt1_trail_expands_the_frontier(minimal_project: Path, monkeypatch, capsys):
    trail = ExperimentTrail("zdt1", monkeypatch, capsys)
    baseline, _ = trail.evaluate(
        {},
        parent=None,
        parent_knobs=trail.defaults,
        hypothesis="baseline",
        rationale="record the defaults",
    )
    initial = trail.score()["hypervolume"]
    assert initial > 0

    optimum = {"unroll_factor": 1, "fifo_depth": 2}
    best, _ = trail.evaluate(
        optimum,
        parent=baseline,
        parent_knobs=trail.defaults,
        hypothesis="lower delay at the same cut",
        rationale="exercise a dominating outcome",
    )
    improved = trail.score()["hypervolume"]
    assert improved > initial
    best_id, knobs, _ = trail.best_point()
    assert best_id == best

    for cut in (0.1, 0.9):
        trail.evaluate(
            {**optimum, "partition.cut": cut},
            parent=best,
            parent_knobs=knobs,
            hypothesis="trade area for delay",
            rationale=f"evaluate partition cut {cut}",
        )
    final = trail.score()
    assert final["hypervolume"] > improved
    assert final["hypervolume_ratio"] > 0.6
    assert final["distance_to_front"] < 5.0
    assert len(final["nondominated"]) == 3
    assert final["n_experiments"] == trail.evals == 4
