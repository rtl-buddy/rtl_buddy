"""Tests for the mockflow synthetic DSE backend (rb xplr mock, #304).

Two layers:

* pure-module tests — determinism, the documented optimum/front, the
  feasibility cliff, the cost/layer model, and the scoring math on
  hand-constructed points (no project, no CLI);
* CLI contract tests — ``mock info`` / ``mock run [--register]`` /
  ``mock score`` through ``RtlBuddy.run()`` in machine mode, including
  the P0-schema round trip of a registered run through the ledger.
"""

from __future__ import annotations

import io
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.xplr import mockflow
from rtl_buddy.xplr.schema import ExperimentRecord, validate_record


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """One rb invocation through RtlBuddy.run(); locks released after."""
    rb = RtlBuddy(name="test_xplr_mockflow")
    monkeypatch.setattr(sys, "argv", ["rb", *argv])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        code = rb.run()
    finally:
        rb._artifact_locks.release_all()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _envelope(out: str) -> dict:
    payload = json.loads(out)
    assert {"command", "exit_code", "meta", "payload"} <= set(payload)
    return payload


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=rb@test.invalid", "-c", "user.name=rb", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_project(minimal_project: Path) -> Path:
    """minimal_project turned into a clean git repo (artefacts ignored)."""
    (minimal_project / ".gitignore").write_text("artefacts/\nrtl_buddy.log\n")
    _git(minimal_project, "init", "-q", "-b", "main", ".")
    _git(minimal_project, "add", "-A")
    _git(minimal_project, "commit", "-q", "-m", "init")
    return minimal_project


def _mock_run(
    monkeypatch,
    capsys,
    scenario: str,
    knobs: dict | None = None,
    *,
    register: bool = False,
    extra: list[str] | None = None,
) -> dict:
    argv = ["--machine", "xplr", "mock", "run", "--scenario", scenario]
    if knobs is not None:
        argv += ["--json", "-"]
    if register:
        argv.append("--register")
    argv += extra or []
    code, out, _ = _run(
        argv,
        monkeypatch,
        capsys,
        stdin=json.dumps(knobs) if knobs is not None else None,
    )
    assert code == 0, out
    return _envelope(out)["payload"]


_RASTRIGIN_CLIFF = {"place.directive": "congestion", "retime": "on"}
_ZDT1_CLIFF = {"place.directive": "aggressive", "route.strategy": "congestion"}


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_evaluate_is_deterministic():
    knobs = {"unroll_factor": 7, "place.effort": 0.25}
    a = mockflow.evaluate("rastrigin", knobs)
    b = mockflow.evaluate("rastrigin", knobs)
    assert a == b


def test_noise_is_seeded_and_reproducible():
    knobs = {"unroll_factor": 7}
    a = mockflow.evaluate("rastrigin", knobs, seed=42, noise=0.5)
    b = mockflow.evaluate("rastrigin", knobs, seed=42, noise=0.5)
    c = mockflow.evaluate("rastrigin", knobs, seed=43, noise=0.5)
    exact = mockflow.evaluate("rastrigin", knobs)
    assert a == b
    assert a["metrics"]["wns_ns"] != c["metrics"]["wns_ns"]
    assert a["metrics"]["wns_ns"] != exact["metrics"]["wns_ns"]
    # noise never touches the cost model or feasibility
    assert a["metrics"]["wall_clock_s"] == exact["metrics"]["wall_clock_s"]
    assert a["metrics"]["routed"] is True


def test_zero_noise_ignores_seed():
    a = mockflow.evaluate("zdt1", {"partition.cut": 0.3}, seed=1)
    b = mockflow.evaluate("zdt1", {"partition.cut": 0.3}, seed=999)
    assert a == b


# ---------------------------------------------------------------------------
# known optimum / analytic front
# ---------------------------------------------------------------------------


def test_rastrigin_optimum_is_exact_and_beats_random_samples():
    truth = mockflow.ground_truth("rastrigin")
    opt_knobs = truth["optimum"]["knobs"]
    assert opt_knobs == {
        "unroll_factor": 5,
        "fifo_depth": 16,
        "place.effort": 0.5,
        "clk_uncertainty_ns": 0.2,
        "place.directive": "default",
        "retime": "off",
    }
    optimum = mockflow.evaluate("rastrigin", opt_knobs)["metrics"]["wns_ns"]
    assert optimum == pytest.approx(0.0, abs=1e-12)

    rng = random.Random(7)
    scenario = mockflow.SCENARIOS["rastrigin"]
    for _ in range(25):
        sample = {}
        for spec in scenario.knobs:
            if spec.type == "int":
                sample[spec.name] = rng.randint(int(spec.lo), int(spec.hi))
            elif spec.type == "float":
                sample[spec.name] = rng.uniform(spec.lo, spec.hi)
        wns = mockflow.evaluate("rastrigin", sample)["metrics"]["wns_ns"]
        assert wns < optimum  # f(x) > 0 away from the global minimum


def test_zdt1_points_land_on_the_analytic_front():
    for cut in (0.0, 0.25, 1.0):
        metrics = mockflow.evaluate(
            "zdt1", {"partition.cut": cut, "unroll_factor": 1, "fifo_depth": 2}
        )["metrics"]
        lut, delay = metrics["lut_pct"], metrics["delay_ns"]
        assert lut == pytest.approx(100.0 * cut)
        assert delay == pytest.approx(10.0 * (1.0 - (lut / 100.0) ** 0.5))
    # off the front: g > 1 strictly worsens delay for the same lut
    off = mockflow.evaluate("zdt1", {"partition.cut": 0.25, "fifo_depth": 18})
    assert off["metrics"]["delay_ns"] > 10.0 * (1.0 - 0.5)


# ---------------------------------------------------------------------------
# feasibility cliff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,cliff,objectives",
    [
        ("rastrigin", _RASTRIGIN_CLIFF, ("wns_ns",)),
        ("zdt1", _ZDT1_CLIFF, ("lut_pct", "delay_ns")),
    ],
)
def test_feasibility_cliff_drops_objectives(scenario, cliff, objectives):
    result = mockflow.evaluate(scenario, dict(cliff))
    assert result["routed"] is False
    assert result["metrics"]["routed"] is False
    for name in objectives:
        assert name not in result["metrics"]
        assert name not in result["metric_meta"]
    assert result["metrics"]["wall_clock_s"] > 0  # you still paid for the run
    # one leg of the combination alone stays feasible
    single = dict(list(cliff.items())[:1])
    assert mockflow.evaluate(scenario, single)["routed"] is True


# ---------------------------------------------------------------------------
# cost / layer model
# ---------------------------------------------------------------------------


def test_cost_model_charges_by_layer():
    base = mockflow.evaluate("rastrigin")["metrics"]["wall_clock_s"]
    assert base == mockflow.WALL_CLOCK_BASE_S
    impl = mockflow.evaluate("rastrigin", {"place.effort": 0.2})
    source = mockflow.evaluate("rastrigin", {"unroll_factor": 5})
    assert impl["metrics"]["wall_clock_s"] == base + mockflow.LAYER_COST_S["impl"]
    assert source["metrics"]["wall_clock_s"] == base + mockflow.LAYER_COST_S["source"]
    assert source["metrics"]["wall_clock_s"] > impl["metrics"]["wall_clock_s"]
    # setting a knob to its default costs nothing
    nop = mockflow.evaluate("rastrigin", {"place.effort": 0.9})
    assert nop["metrics"]["wall_clock_s"] == base


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,knobs,fragment",
    [
        ("nope", {}, "unknown mockflow scenario"),
        ("rastrigin", {"bogus_knob": 1}, "unknown knob"),
        ("rastrigin", {"unroll_factor": 99}, "out of range"),
        ("rastrigin", {"unroll_factor": 2.5}, "expected an integer"),
        ("rastrigin", {"unroll_factor": True}, "expected an integer"),
        ("rastrigin", {"place.effort": "high"}, "expected a number"),
        ("rastrigin", {"place.directive": "warp9"}, "invalid choice"),
    ],
)
def test_validation_fails_loudly(scenario, knobs, fragment):
    with pytest.raises(FatalRtlBuddyError, match=fragment):
        mockflow.evaluate(scenario, knobs)


def test_negative_noise_rejected():
    with pytest.raises(FatalRtlBuddyError, match="--noise"):
        mockflow.evaluate("rastrigin", {}, noise=-0.1)


# ---------------------------------------------------------------------------
# scoring math on hand-constructed points
# ---------------------------------------------------------------------------


def test_nondominated_2d():
    points = [(1.0, 3.0), (2.0, 1.0), (2.0, 2.0), (3.0, 3.0), (1.0, 3.0)]
    assert mockflow.nondominated_2d(points) == [(1.0, 3.0), (2.0, 1.0)]


def test_hypervolume_two_known_points():
    # rectangles [1,4]x[3,4] (area 3) U [2,4]x[1,4] (area 6, overlap 2) = 7
    points = [(1.0, 3.0), (2.0, 1.0)]
    assert mockflow.hypervolume_2d(points, (4.0, 4.0)) == pytest.approx(7.0)
    # a dominated point changes nothing; a point beyond ref contributes 0
    assert mockflow.hypervolume_2d(
        points + [(3.0, 3.5), (5.0, 0.5)], (4.0, 4.0)
    ) == pytest.approx(7.0)
    assert mockflow.hypervolume_2d([], (4.0, 4.0)) == 0.0


def test_distance_to_front_zero_on_front():
    front = mockflow.zdt1_front()
    on_front = [(0.0, 10.0), (25.0, 5.0), (100.0, 0.0)]
    assert mockflow.distance_to_front(on_front, front) == pytest.approx(0.0)
    assert mockflow.distance_to_front([(0.0, 11.0)], front) == pytest.approx(1.0)


def _record(exp_id: str, scenario: str, metrics: dict) -> ExperimentRecord:
    return ExperimentRecord.from_dict(
        {
            "schema_version": "1.0",
            "id": exp_id,
            "source": {"git_sha": "deadbeef"},
            "knobs": [],
            "config_snapshot": {"scenario": scenario, "knobs": {}},
            "outcome": {"status": "success", "metrics": metrics},
            "provenance": {
                "created": "2026-06-01T00:00:00+00:00",
                "tools": [{"name": "mockflow", "version": "1.0"}],
            },
        }
    )


def test_score_records_single_objective_regret():
    records = [
        _record("exp-0001", "rastrigin", {"routed": True, "wns_ns": -2.0}),
        _record("exp-0002", "rastrigin", {"routed": True, "wns_ns": -0.5}),
        _record("exp-0003", "rastrigin", {"routed": False, "wall_clock_s": 60.0}),
    ]
    score = mockflow.score_records(records, "rastrigin")
    assert score["objective"] == "single"
    assert score["n_experiments"] == 3
    assert score["n_feasible"] == 2
    assert score["best"] == {"id": "exp-0002", "wns_ns": -0.5}
    assert score["regret"] == pytest.approx(0.5)


def test_score_records_multi_objective_hypervolume():
    records = [
        _record("exp-0001", "zdt1", {"routed": True, "lut_pct": 0.0, "delay_ns": 10.0}),
        _record(
            "exp-0002", "zdt1", {"routed": True, "lut_pct": 100.0, "delay_ns": 0.0}
        ),
        _record(  # dominated by exp-0001
            "exp-0003", "zdt1", {"routed": True, "lut_pct": 50.0, "delay_ns": 50.0}
        ),
    ]
    score = mockflow.score_records(records, "zdt1")
    assert score["objective"] == "multi"
    assert score["n_feasible"] == 3
    assert [m["id"] for m in score["nondominated"]] == ["exp-0001", "exp-0002"]
    # two front-endpoint rectangles vs ref (110, 110):
    # [0,110]x[10,110] (11000) U [100,110]x[0,110] (1100, overlap 1000) = 11100
    assert score["hypervolume"] == pytest.approx(11100.0)
    assert 0.0 < score["hypervolume_ratio"] < 1.0
    assert score["hypervolume"] < score["front_hypervolume"]
    assert score["distance_to_front"] == pytest.approx(0.0, abs=1e-9)


def test_score_records_unknown_or_empty():
    with pytest.raises(FatalRtlBuddyError, match="no mockflow 'zdt1'"):
        mockflow.score_records([], "zdt1")
    with pytest.raises(FatalRtlBuddyError, match="unknown mockflow scenario"):
        mockflow.score_records([], "nope")


# ---------------------------------------------------------------------------
# CLI: mock info
# ---------------------------------------------------------------------------


def test_mock_info_lists_scenarios(minimal_project: Path, monkeypatch, capsys):
    code, out, _ = _run(["--machine", "xplr", "mock", "info"], monkeypatch, capsys)
    assert code == 0, out
    envelope = _envelope(out)
    assert envelope["command"] == "xplr mock info"
    names = [s["name"] for s in envelope["payload"]["scenarios"]]
    assert names == ["rastrigin", "zdt1"]
    for info in envelope["payload"]["scenarios"]:
        assert {"knobs", "metric_meta", "cost_model", "ground_truth"} <= set(info)
        for knob in info["knobs"]:
            assert knob["layer"] in ("source", "flow", "impl")


def test_mock_info_single_scenario_exposes_ground_truth(
    minimal_project: Path, monkeypatch, capsys
):
    code, out, _ = _run(
        ["--machine", "xplr", "mock", "info", "--scenario", "zdt1"],
        monkeypatch,
        capsys,
    )
    assert code == 0
    payload = _envelope(out)["payload"]
    assert payload["name"] == "zdt1"
    truth = payload["ground_truth"]
    assert truth["objective"] == "multi"
    assert truth["front"]["samples"][0] == [0.0, 10.0]
    assert truth["reference_point"] == {"lut_pct": 110.0, "delay_ns": 110.0}

    code, out, _ = _run(
        ["--machine", "xplr", "mock", "info", "--scenario", "nope"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "unknown mockflow scenario" in _envelope(out)["payload"]["error"]


# ---------------------------------------------------------------------------
# CLI: mock run
# ---------------------------------------------------------------------------


def test_mock_run_without_register_only_evaluates(
    minimal_project: Path, monkeypatch, capsys
):
    payload = _mock_run(monkeypatch, capsys, "rastrigin", {"unroll_factor": 7})
    assert payload["scenario"] == "rastrigin"
    assert payload["routed"] is True
    assert payload["knobs"]["unroll_factor"] == 7
    assert payload["knobs"]["fifo_depth"] == 8  # default filled in
    assert payload["metrics"]["wns_ns"] < 0
    assert payload["metric_meta"]["wns_ns"] == {"direction": "max", "unit": "ns"}
    assert "id" not in payload
    # nothing touched the ledger (no git repo needed either)
    assert not (minimal_project / "artefacts" / "xplr").exists()
    # deterministic across invocations
    again = _mock_run(monkeypatch, capsys, "rastrigin", {"unroll_factor": 7})
    assert again["metrics"] == payload["metrics"]


def test_mock_run_register_roundtrips_through_ledger(
    git_project: Path, monkeypatch, capsys
):
    payload = _mock_run(
        monkeypatch,
        capsys,
        "rastrigin",
        {"unroll_factor": 5, "place.effort": 0.5},
        register=True,
    )
    assert payload["id"] == "exp-0001"
    record = payload["record"]
    validate_record(record)  # P0 schema conformance
    assert record["outcome"]["status"] == "success"
    assert record["outcome"]["metrics"] == payload["metrics"]
    assert record["outcome"]["metric_meta"] == payload["metric_meta"]
    # only the provided knobs enter the manifest, from = scenario default
    assert record["knobs"] == [
        {"name": "unroll_factor", "from": 2, "to": 5, "layer": "source"},
        {"name": "place.effort", "from": 0.9, "to": 0.5, "layer": "impl"},
    ]
    assert record["config_snapshot"]["scenario"] == "rastrigin"
    assert record["config_snapshot"]["knobs"] == payload["knobs"]
    assert {"name": "mockflow", "version": "1.0"} in record["provenance"]["tools"]
    # persisted record matches the envelope byte-for-byte
    on_disk = json.loads(Path(payload["record_path"]).read_text())
    assert on_disk == record

    # readable back through the P1 surface
    code, out, _ = _run(["--machine", "xplr", "show", "exp-0001"], monkeypatch, capsys)
    assert code == 0
    assert _envelope(out)["payload"]["record"] == record


def test_mock_run_register_infeasible_point(git_project: Path, monkeypatch, capsys):
    payload = _mock_run(
        monkeypatch, capsys, "rastrigin", dict(_RASTRIGIN_CLIFF), register=True
    )
    record = payload["record"]
    validate_record(record)
    assert record["outcome"]["status"] == "success"  # the flow ran; routing failed
    assert record["outcome"]["metrics"]["routed"] is False
    assert "wns_ns" not in record["outcome"]["metrics"]


def test_mock_run_outcome_pipes_into_attach_outcome(
    git_project: Path, monkeypatch, capsys
):
    """payload.outcome is a verbatim-valid attach-outcome --json input."""
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 0, out
    exp_id = _envelope(out)["payload"]["id"]

    payload = _mock_run(monkeypatch, capsys, "zdt1", {"unroll_factor": 1})
    outcome = payload["outcome"]
    assert outcome["status"] == "success"
    assert outcome["metrics"] == payload["metrics"]
    assert outcome["metric_meta"] == payload["metric_meta"]
    # exactly the attach-outcome shape, nothing attach-outcome would reject
    assert set(outcome) == {"status", "metrics", "metric_meta"}

    code, out, _ = _run(
        ["--machine", "xplr", "attach-outcome", exp_id, "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps(outcome),
    )
    assert code == 0, out
    record = _envelope(out)["payload"]["record"]
    validate_record(record)
    assert record["outcome"]["status"] == "success"
    assert record["outcome"]["metrics"] == payload["metrics"]
    assert record["outcome"]["metric_meta"] == payload["metric_meta"]


def test_mock_run_register_source_sha_in_non_git_sandbox(
    minimal_project: Path, monkeypatch, capsys
):
    """--source-sha is the agent-declared pin path: verbatim, no dirty bit."""
    payload = _mock_run(
        monkeypatch,
        capsys,
        "rastrigin",
        {"unroll_factor": 5},
        register=True,
        extra=["--source-sha", "deadbeefcafe", "--source-branch", "sandbox"],
    )
    assert payload["id"] == "exp-0001"
    record = payload["record"]
    validate_record(record)
    assert record["source"] == {"git_sha": "deadbeefcafe", "branch": "sandbox"}
    assert "dirty" not in record["source"]  # unknowable for a declared sha


def test_mock_run_register_non_git_without_source_sha_exits_2(
    minimal_project: Path, monkeypatch, capsys
):
    code, out, _ = _run(
        ["--machine", "xplr", "mock", "run", "--scenario", "rastrigin", "--register"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "not a git repository" in error
    assert "--source-sha" in error  # the sandbox escape hatch is named


def test_mock_run_source_options_misuse_exits_2(
    minimal_project: Path, monkeypatch, capsys
):
    code, out, _ = _run(
        [
            "--machine",
            "xplr",
            "mock",
            "run",
            "--scenario",
            "rastrigin",
            "--source-sha",
            "deadbeefcafe",
        ],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "--register" in _envelope(out)["payload"]["error"]

    code, out, _ = _run(
        [
            "--machine",
            "xplr",
            "mock",
            "run",
            "--scenario",
            "rastrigin",
            "--register",
            "--source-branch",
            "sandbox",
        ],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "--source-sha" in _envelope(out)["payload"]["error"]


def test_mock_run_invalid_knob_exits_2(minimal_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        [
            "--machine",
            "xplr",
            "mock",
            "run",
            "--scenario",
            "rastrigin",
            "--json",
            "-",
        ],
        monkeypatch,
        capsys,
        stdin=json.dumps({"bogus_knob": 1}),
    )
    assert code == 2
    envelope = _envelope(out)
    assert envelope["command"] == "xplr mock run"
    assert "unknown knob" in envelope["payload"]["error"]


# ---------------------------------------------------------------------------
# CLI: mock score
# ---------------------------------------------------------------------------


def test_mock_score_reports_regret(git_project: Path, monkeypatch, capsys):
    truth = mockflow.ground_truth("rastrigin")
    _mock_run(monkeypatch, capsys, "rastrigin", {"unroll_factor": 7}, register=True)
    _mock_run(
        monkeypatch, capsys, "rastrigin", truth["optimum"]["knobs"], register=True
    )
    _mock_run(monkeypatch, capsys, "rastrigin", dict(_RASTRIGIN_CLIFF), register=True)

    code, out, _ = _run(
        ["--machine", "xplr", "mock", "score", "--scenario", "rastrigin"],
        monkeypatch,
        capsys,
    )
    assert code == 0, out
    envelope = _envelope(out)
    assert envelope["command"] == "xplr mock score"
    score = envelope["payload"]
    assert score["n_experiments"] == 3
    assert score["n_feasible"] == 2  # the cliff point is excluded
    assert score["best"]["id"] == "exp-0002"
    assert score["regret"] == pytest.approx(0.0, abs=1e-12)


def test_mock_score_multi_objective_via_ledger(git_project: Path, monkeypatch, capsys):
    for cut in (0.0, 0.5, 1.0):
        _mock_run(
            monkeypatch,
            capsys,
            "zdt1",
            {"partition.cut": cut, "unroll_factor": 1, "fifo_depth": 2},
            register=True,
        )
    _mock_run(monkeypatch, capsys, "zdt1", {"fifo_depth": 18}, register=True)

    code, out, _ = _run(
        ["--machine", "xplr", "mock", "score", "--scenario", "zdt1"],
        monkeypatch,
        capsys,
    )
    assert code == 0, out
    score = _envelope(out)["payload"]
    assert score["n_feasible"] == 4
    nd_ids = [m["id"] for m in score["nondominated"]]
    assert nd_ids == ["exp-0001", "exp-0002", "exp-0003"]
    assert 0.0 < score["hypervolume"] < score["front_hypervolume"]
    assert score["distance_to_front"] == pytest.approx(0.0, abs=1e-9)


def test_mock_score_all_scenarios_and_empty_ledger(
    git_project: Path, monkeypatch, capsys
):
    code, out, _ = _run(["--machine", "xplr", "mock", "score"], monkeypatch, capsys)
    assert code == 2
    assert "no mockflow experiments" in _envelope(out)["payload"]["error"]

    _mock_run(monkeypatch, capsys, "rastrigin", register=True)
    _mock_run(monkeypatch, capsys, "zdt1", register=True)
    code, out, _ = _run(["--machine", "xplr", "mock", "score"], monkeypatch, capsys)
    assert code == 0, out
    scores = _envelope(out)["payload"]["scenarios"]
    assert [s["scenario"] for s in scores] == ["rastrigin", "zdt1"]


def test_mock_help_lists_subcommands():
    import re

    from typer.testing import CliRunner

    # CI terminals (GitHub Actions) get rich help with ANSI styling that
    # splits option tokens; strip escapes before substring asserts.
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    rb = RtlBuddy(name="test_mock_help")
    result = CliRunner().invoke(rb.app, ["xplr", "mock", "--help"])
    assert result.exit_code == 0
    output = ansi.sub("", result.output)
    for sub in ("info", "run", "score"):
        assert sub in output
    result = CliRunner().invoke(rb.app, ["xplr", "mock", "run", "--help"])
    assert result.exit_code == 0
    output = ansi.sub("", result.output)
    for opt in (
        "--scenario",
        "--json",
        "--seed",
        "--noise",
        "--register",
        "--source-sha",
        "--source-branch",
    ):
        assert opt in output
