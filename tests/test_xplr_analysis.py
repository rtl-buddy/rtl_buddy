"""Contract tests for the rb xplr P3 analysis surface (#299).

frontier / diff / knob-effect, exercised against hand-built fixture
ledgers through the machine-mode JSON envelope — the same contract the
agent consumes. The Pareto cases use the primer example: with both
metrics minimized, A(40,9.0) G(50,7.0) B(55,6.0) C(70,5.0) F(85,4.8)
are non-dominated while D(60,8.0) and E(45,9.5) are dominated.

The git-diff part of ``rb xplr diff`` is tested against two real
commits made in a tmp git repo; everything else needs no git at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.xplr.schema import validate_record


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> tuple[int, str, str]:
    """Run one rb invocation through RtlBuddy.run(); return (code, out, err)."""
    rb = RtlBuddy(name="test_xplr_analysis")
    monkeypatch.setattr(sys, "argv", ["rb", *argv])
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


def _machine(argv: list[str], monkeypatch, capsys, *, expect_code: int = 0) -> dict:
    code, out, _ = _run(["--machine", *argv], monkeypatch, capsys)
    assert code == expect_code, out
    envelope = _envelope(out)
    assert envelope["exit_code"] == expect_code
    return envelope["payload"]


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
    """minimal_project turned into a clean git repo (artefacts gitignored)."""
    (minimal_project / ".gitignore").write_text("artefacts/\nrtl_buddy.log\n")
    _git(minimal_project, "init", "-q", "-b", "main", ".")
    _git(minimal_project, "add", "-A")
    _git(minimal_project, "commit", "-q", "-m", "init")
    return minimal_project


def _record(
    exp_id: str,
    *,
    status: str = "success",
    metrics: dict | None = None,
    metric_meta: dict | None = None,
    knobs: list | None = None,
    parent: str | None = None,
    git_sha: str = "1111111",
) -> dict:
    record: dict = {
        "schema_version": "1.0",
        "id": exp_id,
        "source": {"git_sha": git_sha},
        "knobs": knobs or [],
        "outcome": {"status": status},
        "provenance": {"created": "2026-06-11T10:00:00+08:00"},
    }
    if parent is not None:
        record["parent"] = parent
    if metrics is not None:
        record["outcome"]["metrics"] = metrics
    if metric_meta is not None:
        record["outcome"]["metric_meta"] = metric_meta
    return record


def _write_ledger(project: Path, records: list[dict]) -> None:
    for record in records:
        validate_record(record)  # fixtures must honour the P0 contract
        path = project / "artefacts" / "xplr" / record["id"] / "record.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")


# the Pareto-primer landscape: (lut_pct, delay_ns), both minimized
_PRIMER = {
    "exp-0001": (40, 9.0),  # A  frontier
    "exp-0002": (55, 6.0),  # B  frontier
    "exp-0003": (70, 5.0),  # C  frontier
    "exp-0004": (60, 8.0),  # D  dominated by B and G
    "exp-0005": (45, 9.5),  # E  dominated by A
    "exp-0006": (85, 4.8),  # F  frontier
    "exp-0007": (50, 7.0),  # G  frontier
}
_PRIMER_FRONTIER = ["exp-0001", "exp-0002", "exp-0003", "exp-0006", "exp-0007"]
_META_MIN = {
    "lut_pct": {"direction": "min", "unit": "percent"},
    "delay_ns": {"direction": "min", "unit": "ns"},
}


def _primer_records() -> list[dict]:
    return [
        _record(
            exp_id,
            metrics={"lut_pct": lut, "delay_ns": delay, "routed": True},
            metric_meta=_META_MIN,
        )
        for exp_id, (lut, delay) in _PRIMER.items()
    ]


# ---------------------------------------------------------------------------
# frontier
# ---------------------------------------------------------------------------


def test_frontier_pareto_primer(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _primer_records())
    payload = _machine(["xplr", "frontier"], monkeypatch, capsys)

    assert payload["metrics"] == [
        {"name": "delay_ns", "direction": "min", "unit": "ns"},
        {"name": "lut_pct", "direction": "min", "unit": "percent"},
    ]
    assert [m["id"] for m in payload["frontier"]] == _PRIMER_FRONTIER
    member = payload["frontier"][0]
    assert member["metrics"] == {"lut_pct": 40, "delay_ns": 9.0, "routed": True}
    assert "preference_score" not in member  # only with --prefer
    assert payload["dominated"] == [
        {"id": "exp-0004", "dominated_by": ["exp-0002", "exp-0007"]},
        {"id": "exp-0005", "dominated_by": ["exp-0001"]},
    ]
    assert payload["infeasible"] == []
    assert payload["excluded"] == []


def test_frontier_infeasible_and_excluded(minimal_project: Path, monkeypatch, capsys):
    records = _primer_records()
    records.append(  # routed=false -> infeasible, never on the frontier
        _record(
            "exp-0008",
            metrics={"lut_pct": 1, "delay_ns": 0.1, "routed": False},
            metric_meta=_META_MIN,
        )
    )
    records.append(  # missing a dominance metric -> excluded with reason
        _record("exp-0009", metrics={"lut_pct": 5}, metric_meta=_META_MIN)
    )
    records.append(_record("exp-0010", status="pending"))  # not a success
    records.append(_record("exp-0011", status="failed"))
    _write_ledger(minimal_project, records)

    payload = _machine(["xplr", "frontier"], monkeypatch, capsys)
    assert [m["id"] for m in payload["frontier"]] == _PRIMER_FRONTIER
    assert payload["infeasible"] == ["exp-0008"]
    excluded = {e["id"]: e["reason"] for e in payload["excluded"]}
    assert "delay_ns" in excluded["exp-0009"]
    assert "pending" in excluded["exp-0010"]
    assert "failed" in excluded["exp-0011"]
    assert set(excluded) == {"exp-0009", "exp-0010", "exp-0011"}


def test_frontier_max_direction_metric(minimal_project: Path, monkeypatch, capsys):
    meta = {
        "lut_pct": {"direction": "min"},
        "wns_ns": {"direction": "max", "unit": "ns"},
    }
    _write_ledger(
        minimal_project,
        [
            _record(
                "exp-0001", metrics={"lut_pct": 50, "wns_ns": 0.5}, metric_meta=meta
            ),
            _record(
                "exp-0002", metrics={"lut_pct": 60, "wns_ns": 0.2}, metric_meta=meta
            ),
            _record(
                "exp-0003", metrics={"lut_pct": 40, "wns_ns": -0.1}, metric_meta=meta
            ),
        ],
    )
    payload = _machine(["xplr", "frontier"], monkeypatch, capsys)
    # exp-0002 is worse on both (wns is maximized); exp-0003 trades wns for lut
    assert [m["id"] for m in payload["frontier"]] == ["exp-0001", "exp-0003"]
    assert payload["dominated"] == [{"id": "exp-0002", "dominated_by": ["exp-0001"]}]


def test_frontier_requires_directions_unless_overridden(
    minimal_project: Path, monkeypatch, capsys
):
    records = [
        _record(exp_id, metrics={"lut_pct": lut, "delay_ns": delay})
        for exp_id, (lut, delay) in _PRIMER.items()
    ]
    _write_ledger(minimal_project, records)

    payload = _machine(["xplr", "frontier"], monkeypatch, capsys, expect_code=2)
    assert "direction" in payload["error"]
    assert "--metrics" in payload["error"]

    payload = _machine(
        ["xplr", "frontier", "--metrics", "lut_pct:min,delay_ns:min"],
        monkeypatch,
        capsys,
    )
    assert [m["id"] for m in payload["frontier"]] == _PRIMER_FRONTIER
    assert payload["metrics"] == [
        {"name": "delay_ns", "direction": "min"},
        {"name": "lut_pct", "direction": "min"},
    ]


def test_frontier_metrics_override_beats_record_meta(
    minimal_project: Path, monkeypatch, capsys
):
    meta = {"wns_ns": {"direction": "min"}}  # a record-level mistake
    _write_ledger(
        minimal_project,
        [
            _record("exp-0001", metrics={"wns_ns": 0.5}, metric_meta=meta),
            _record("exp-0002", metrics={"wns_ns": 0.2}, metric_meta=meta),
        ],
    )
    payload = _machine(
        ["xplr", "frontier", "--metrics", "wns_ns:max"], monkeypatch, capsys
    )
    assert [m["id"] for m in payload["frontier"]] == ["exp-0001"]
    assert payload["dominated"] == [{"id": "exp-0002", "dominated_by": ["exp-0001"]}]


def test_frontier_preference_sorts_without_dropping(
    minimal_project: Path, monkeypatch, capsys
):
    _write_ledger(minimal_project, _primer_records())
    payload = _machine(
        ["xplr", "frontier", "--prefer", "0.1*lut_pct+0.9*delay_ns"],
        monkeypatch,
        capsys,
    )
    # every non-dominated point is kept, just reordered by the scalar score
    assert sorted(m["id"] for m in payload["frontier"]) == _PRIMER_FRONTIER
    assert [m["id"] for m in payload["frontier"]] == [
        "exp-0002",  # B: 0.1*55 + 0.9*6.0 = 10.9
        "exp-0007",  # G: 11.3
        "exp-0003",  # C: 11.5
        "exp-0001",  # A: 12.1
        "exp-0006",  # F: 12.82
    ]
    assert payload["frontier"][0]["preference_score"] == pytest.approx(10.9)
    assert payload["dominated"][0]["id"] == "exp-0004"


def test_frontier_preference_normalizes_max_metrics(
    minimal_project: Path, monkeypatch, capsys
):
    meta = {"wns_ns": {"direction": "max"}, "lut_pct": {"direction": "min"}}
    _write_ledger(
        minimal_project,
        [
            _record(
                "exp-0001", metrics={"wns_ns": 0.5, "lut_pct": 50}, metric_meta=meta
            ),
            _record(
                "exp-0002", metrics={"wns_ns": -0.1, "lut_pct": 40}, metric_meta=meta
            ),
        ],
    )
    payload = _machine(
        ["xplr", "frontier", "--prefer", "1*wns_ns"], monkeypatch, capsys
    )
    # max metrics are negated before weighting, so higher wns wins (lower score)
    assert [m["id"] for m in payload["frontier"]] == ["exp-0001", "exp-0002"]
    assert payload["frontier"][0]["preference_score"] == pytest.approx(-0.5)


def test_frontier_bad_inputs_exit_2(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _primer_records())

    payload = _machine(
        ["xplr", "frontier", "--metrics", "lut_pct:upward"],
        monkeypatch,
        capsys,
        expect_code=2,
    )
    assert "lut_pct:upward" in payload["error"]

    payload = _machine(
        ["xplr", "frontier", "--prefer", "fast*2*lut_pct"],
        monkeypatch,
        capsys,
        expect_code=2,
    )
    assert "weight*metric" in payload["error"]

    payload = _machine(
        ["xplr", "frontier", "--prefer", "1*bogus_metric"],
        monkeypatch,
        capsys,
        expect_code=2,
    )
    assert "bogus_metric" in payload["error"]
    assert "lut_pct" in payload["error"]  # names what is available


def test_error_envelope_reports_full_subcommand(
    minimal_project: Path, monkeypatch, capsys
):
    """Exit-2 envelopes name the subcommand, matching the success path."""
    _write_ledger(minimal_project, _primer_records())

    code, out, _ = _run(
        ["--machine", "xplr", "frontier", "--metrics", "lut_pct:upward"],
        monkeypatch,
        capsys,
    )
    assert code == 2  # bad direction in the --metrics override
    envelope = _envelope(out)
    assert envelope["command"] == "xplr frontier"

    code, out, _ = _run(
        ["--machine", "xplr", "diff", "exp-0001", "exp-9999"], monkeypatch, capsys
    )
    assert code == 2
    envelope = _envelope(out)
    assert envelope["command"] == "xplr diff"


def test_frontier_empty_ledger(minimal_project: Path, monkeypatch, capsys):
    payload = _machine(["xplr", "frontier"], monkeypatch, capsys)
    assert payload == {
        "metrics": [],
        "frontier": [],
        "dominated": [],
        "infeasible": [],
        "excluded": [],
    }


def test_frontier_human_mode(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _primer_records())
    code, out, err = _run(["xplr", "frontier"], monkeypatch, capsys)
    assert code == 0
    text = out + err
    assert "exp-0001" in text
    assert "lut_pct (min)" in text


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


_DIFF_META = {
    "lut_pct": {"direction": "min"},
    "wns_ns": {"direction": "max", "unit": "ns"},
}


def _diff_pair(sha_a: str = "aaaaaaa", sha_b: str = "bbbbbbb") -> list[dict]:
    a = _record(
        "exp-0001",
        git_sha=sha_a,
        knobs=[
            {"name": "synth.strategy", "from": "area", "to": "speed"},
            {"name": "rtl.DEPTH", "from": 2, "to": 3, "rationale": "pipeline"},
            {"name": "place.seed", "from": 0, "to": 7},
        ],
        metrics={"lut_pct": 50, "wns_ns": -0.2, "routed": True, "ffs": 1200},
        metric_meta=_DIFF_META,
    )
    b = _record(
        "exp-0002",
        git_sha=sha_b,
        knobs=[
            {"name": "synth.strategy", "from": "area", "to": "speed"},  # unchanged
            {"name": "rtl.DEPTH", "from": 3, "to": 4},  # changed (by "to")
            {"name": "synth.retime", "from": False, "to": True},  # added
        ],
        metrics={"lut_pct": 60, "wns_ns": 0.1, "routed": True, "bram": 12},
        metric_meta=_DIFF_META,
    )
    return [a, b]


def test_diff_knob_and_outcome_delta(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _diff_pair())
    payload = _machine(["xplr", "diff", "exp-0001", "exp-0002"], monkeypatch, capsys)

    assert payload["a"] == "exp-0001" and payload["b"] == "exp-0002"
    knobs = payload["knob_delta"]
    assert [k["name"] for k in knobs["added"]] == ["synth.retime"]
    assert [k["name"] for k in knobs["reverted"]] == ["place.seed"]
    assert knobs["changed"] == [
        {
            "name": "rtl.DEPTH",
            "a": {"from": 2, "to": 3},
            "b": {"from": 3, "to": 4},
        }
    ]
    assert knobs["unchanged"] == ["synth.strategy"]
    assert [k["name"] for k in knobs["manifest_a"]] == [
        "synth.strategy",
        "rtl.DEPTH",
        "place.seed",
    ]
    assert len(knobs["manifest_b"]) == 3

    outcome = payload["outcome_delta"]
    assert outcome["status_a"] == "success" and outcome["status_b"] == "success"
    rows = {row["name"]: row for row in outcome["metrics"]}
    assert set(rows) == {"lut_pct", "wns_ns"}  # routed is boolean: no delta
    assert rows["lut_pct"]["delta"] == 10
    assert rows["lut_pct"]["assessment"] == "worse"  # min metric went up
    assert rows["wns_ns"]["delta"] == pytest.approx(0.3)
    assert rows["wns_ns"]["assessment"] == "better"  # max metric went up
    assert outcome["only_a"] == {"ffs": 1200}
    assert outcome["only_b"] == {"bram": 12}

    # no git repo knows these shas: the source diff degrades to a note
    source = payload["source"]
    assert source["a"]["git_sha"] == "aaaaaaa"
    assert source["b"]["git_sha"] == "bbbbbbb"
    assert source["stat"] is None
    assert "unknown" in source["note"]


def test_diff_git_stat_and_patch(git_project: Path, monkeypatch, capsys):
    sha_a = _git(git_project, "rev-parse", "HEAD")
    (git_project / "tests.yaml").write_text("# perturbed by exp-0002\n")
    _git(git_project, "commit", "-aqm", "perturb tests.yaml")
    sha_b = _git(git_project, "rev-parse", "HEAD")
    _write_ledger(git_project, _diff_pair(sha_a, sha_b))

    payload = _machine(["xplr", "diff", "exp-0001", "exp-0002"], monkeypatch, capsys)
    assert "tests.yaml" in payload["source"]["stat"]
    assert "patch" not in payload["source"]

    payload = _machine(
        ["xplr", "diff", "exp-0001", "exp-0002", "--patch"], monkeypatch, capsys
    )
    assert "diff --git" in payload["source"]["patch"]
    assert "perturbed by exp-0002" in payload["source"]["patch"]


def test_diff_identical_sources_noted(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _diff_pair("ccccccc", "ccccccc"))
    payload = _machine(["xplr", "diff", "exp-0001", "exp-0002"], monkeypatch, capsys)
    assert payload["source"]["stat"] == ""
    assert "same source revision" in payload["source"]["note"]


def test_diff_unknown_experiment_exits_2(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _diff_pair())
    payload = _machine(
        ["xplr", "diff", "exp-0001", "exp-0042"], monkeypatch, capsys, expect_code=2
    )
    assert "unknown experiment id 'exp-0042'" in payload["error"]


def test_diff_human_mode(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _diff_pair())
    code, out, err = _run(["xplr", "diff", "exp-0001", "exp-0002"], monkeypatch, capsys)
    assert code == 0
    text = out + err
    assert "diff exp-0001..exp-0002" in text
    assert "rtl.DEPTH" in text
    assert "wns_ns" in text


# ---------------------------------------------------------------------------
# knob-effect
# ---------------------------------------------------------------------------


def _effect_chain() -> list[dict]:
    return [
        _record("exp-0001", metrics={"lut_pct": 60, "delay_ns": 8.0}),
        _record(
            "exp-0002",
            parent="exp-0001",
            knobs=[
                {
                    "name": "rtl.DEPTH",
                    "from": 2,
                    "to": 3,
                    "rationale": "one more pipeline stage",
                }
            ],
            metrics={"lut_pct": 64, "delay_ns": 7.0, "routed": True},
        ),
        _record(
            "exp-0003",
            parent="exp-0002",
            knobs=[{"name": "rtl.DEPTH", "from": 3, "to": 4}],
            metrics={"lut_pct": 70, "delay_ns": 6.5},
        ),
        _record(
            "exp-0004",
            knobs=[{"name": "synth.strategy", "from": "area", "to": "speed"}],
            metrics={"lut_pct": 55, "delay_ns": 7.5},
        ),
        _record(
            "exp-0005",
            parent="exp-9999",  # parent not in the ledger: no delta
            knobs=[{"name": "rtl.DEPTH", "from": 4, "to": 5}],
            status="failed",
        ),
    ]


def test_knob_effect_history_with_parent_deltas(
    minimal_project: Path, monkeypatch, capsys
):
    _write_ledger(minimal_project, _effect_chain())
    payload = _machine(["xplr", "knob-effect", "rtl.DEPTH"], monkeypatch, capsys)

    assert payload["knob"] == "rtl.DEPTH"
    effects = payload["effects"]
    assert [e["exp"] for e in effects] == ["exp-0002", "exp-0003", "exp-0005"]

    first = effects[0]
    assert first["from"] == 2 and first["to"] == 3
    assert first["rationale"] == "one more pipeline stage"
    assert first["parent"] == "exp-0001"
    assert first["metrics_after"] == {"lut_pct": 64, "delay_ns": 7.0, "routed": True}
    assert first["metrics_parent_delta"] == {
        "delay_ns": pytest.approx(-1.0),
        "lut_pct": 4,
    }

    second = effects[1]
    assert second["metrics_parent_delta"] == {
        "delay_ns": pytest.approx(-0.5),
        "lut_pct": 6,
    }
    assert "rationale" not in second  # absent stays absent

    third = effects[2]
    assert third["status"] == "failed"
    assert third["parent"] == "exp-9999"
    assert third["metrics_after"] == {}
    assert "metrics_parent_delta" not in third  # parent unknown to the ledger

    # the knob was tried: no self-correction hints in the payload
    assert "known_knobs" not in payload
    assert "suggestions" not in payload


def test_knob_effect_typo_gets_known_knobs_and_suggestions(
    minimal_project: Path, monkeypatch, capsys
):
    """A knob declared nowhere exits 0 with empty effects + correction hints."""
    _write_ledger(minimal_project, _effect_chain())
    payload = _machine(["xplr", "knob-effect", "rtl.DEPHT"], monkeypatch, capsys)
    assert payload["knob"] == "rtl.DEPHT"
    assert payload["effects"] == []
    assert payload["known_knobs"] == ["rtl.DEPTH", "synth.strategy"]
    assert payload["suggestions"] == ["rtl.DEPTH"]


def test_knob_effect_unknown_knob_is_empty(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _effect_chain())
    payload = _machine(["xplr", "knob-effect", "no.such.knob"], monkeypatch, capsys)
    assert payload["knob"] == "no.such.knob"
    assert payload["effects"] == []
    assert payload["known_knobs"] == ["rtl.DEPTH", "synth.strategy"]
    assert payload["suggestions"] == []  # nothing close: list everything instead


def test_knob_effect_unknown_knob_human_hint(
    minimal_project: Path, monkeypatch, capsys
):
    _write_ledger(minimal_project, _effect_chain())
    code, out, err = _run(["xplr", "knob-effect", "rtl.DEPHT"], monkeypatch, capsys)
    assert code == 0
    text = out + err
    assert "no experiment" in text
    assert "rtl.DEPTH" in text  # the close match is suggested


def test_knob_effect_human_mode(minimal_project: Path, monkeypatch, capsys):
    _write_ledger(minimal_project, _effect_chain())
    code, out, err = _run(["xplr", "knob-effect", "rtl.DEPTH"], monkeypatch, capsys)
    assert code == 0
    text = out + err
    assert "exp-0002" in text
    assert "rtl.DEPTH" in text


# ---------------------------------------------------------------------------
# help text
# ---------------------------------------------------------------------------


def test_xplr_help_lists_analysis_commands():
    import re

    from typer.testing import CliRunner

    # CI terminals (GitHub Actions) get rich help with ANSI styling that
    # splits option tokens; strip escapes before substring asserts.
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    rb = RtlBuddy(name="test_xplr_analysis_help")
    result = CliRunner().invoke(rb.app, ["xplr", "--help"])
    assert result.exit_code == 0
    output = ansi.sub("", result.output)
    for sub in ("diff", "frontier", "knob-effect"):
        assert sub in output
    assert "--root" in output  # group-level project-root anchor
    result = CliRunner().invoke(rb.app, ["xplr", "frontier", "--help"])
    assert result.exit_code == 0
    output = ansi.sub("", result.output)
    assert "--metrics" in output
    assert "--prefer" in output
