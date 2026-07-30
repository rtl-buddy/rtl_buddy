"""Round-trip tests for per-run result JSON artifacts (#351 P0).

Covers ``TestResults.to_json_dict`` / ``from_json_dict`` and the
``write_result_json`` / ``load_result_json`` envelope layer that a
dispatch backend relies on to collect remotely produced results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.result_io import (
    RESULT_JSON_SCHEMA_VERSION,
    load_result_json,
    write_result_json,
)
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    EarlyStopResults,
    FilelistFailResults,
    SetupFailResults,
    SimTimeoutResults,
    SkipResults,
    TestPassResults,
    TestResults,
)
from rtl_buddy.runner.xfail import apply_xfail


@pytest.mark.parametrize(
    "result",
    [
        TestPassResults(name="t/results"),
        CompileFailResults(name="t/results"),
        SimTimeoutResults(name="t/results"),
        SkipResults(name="t/results", desc="lvl 5 > cmd end_level 0"),
        FilelistFailResults(name="t/results", desc="missing file"),
        SetupFailResults(name="t/results", desc="Setup failed in sweep: boom"),
        EarlyStopResults(name="t/results", desc="Stopped early at compile"),
        TestResults(
            name="t/results",
            results={
                "result": "PASS",
                "name": "t",
                "desc": "with extras",
                "coverage": {"lines": 12},
                "assertions": {"enabled": True, "fired": 0},
            },
        ),
    ],
    ids=lambda r: type(r).__name__,
)
def test_round_trip_preserves_semantics(result):
    clone = TestResults.from_json_dict(result.to_json_dict())
    assert clone.name == result.name
    assert clone.results == result.results
    assert clone.is_pass() == result.is_pass()


@pytest.mark.parametrize("strict", [False, True])
def test_round_trip_preserves_xfail_semantics(strict):
    # FAIL->XFAIL always passes; PASS->XPASS passes only when non-strict.
    failed = apply_xfail(CompileFailResults(name="t/results"), strict=strict)
    assert TestResults.from_json_dict(failed.to_json_dict()).is_pass()

    passed = apply_xfail(TestPassResults(name="t/results"), strict=strict)
    clone = TestResults.from_json_dict(passed.to_json_dict())
    assert clone.is_pass() == (not strict)


def test_to_json_dict_is_json_serializable_and_kinded():
    d = SimTimeoutResults(name="t/results").to_json_dict()
    json.dumps(d)
    assert d["kind"] == "SimTimeoutResults"


@pytest.mark.parametrize("data", [None, [], {"name": "t"}, {"results": "FAIL"}])
def test_from_json_dict_rejects_malformed(data):
    with pytest.raises(ValueError):
        TestResults.from_json_dict(data)


def test_write_and_load_envelope(tmp_path: Path):
    out = tmp_path / "artefacts" / "t" / "run-0003" / "result.json"
    write_result_json(
        out, test_name="t", run_id=3, results=TestPassResults(name="t/results")
    )
    assert out.is_file()
    assert not out.with_name(out.name + ".tmp").exists()

    envelope = load_result_json(out)
    assert envelope["test"] == "t"
    assert envelope["run_id"] == 3
    assert envelope["schema_version"] == RESULT_JSON_SCHEMA_VERSION
    assert envelope["result"].is_pass()
    assert envelope["result"].results["result"] == "PASS"


def test_load_missing_file_fails_loud(tmp_path: Path):
    with pytest.raises(FatalRtlBuddyError, match="missing"):
        load_result_json(tmp_path / "nope.json")


def test_load_malformed_json_fails_loud(tmp_path: Path):
    bad = tmp_path / "result.json"
    bad.write_text("{not json")
    with pytest.raises(FatalRtlBuddyError, match="malformed"):
        load_result_json(bad)


def test_load_wrong_filetype_fails_loud(tmp_path: Path):
    bad = tmp_path / "result.json"
    bad.write_text(json.dumps({"rtl-buddy-filetype": "reg_config"}))
    with pytest.raises(FatalRtlBuddyError, match="not a test_result"):
        load_result_json(bad)


def test_load_unsupported_schema_version_fails_loud(tmp_path: Path):
    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=None, results=TestPassResults(name="t/results")
    )
    envelope = json.loads(out.read_text())
    envelope["schema_version"] = RESULT_JSON_SCHEMA_VERSION + 1
    out.write_text(json.dumps(envelope))
    with pytest.raises(FatalRtlBuddyError, match="schema_version"):
        load_result_json(out)


def test_attach_telemetry_round_trip(tmp_path: Path):
    from rtl_buddy.runner.result_io import attach_telemetry_json

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    attach_telemetry_json(out, {"state": "COMPLETED", "max_rss_bytes": 1024})
    envelope = json.loads(out.read_text())
    assert envelope["telemetry"]["max_rss_bytes"] == 1024
    # Result payload is untouched and still loads.
    assert load_result_json(out)["result"].is_pass()
    assert not out.with_name(out.name + ".tmp").exists()


def test_attach_telemetry_missing_file_is_noop(tmp_path: Path):
    from rtl_buddy.runner.result_io import attach_telemetry_json

    attach_telemetry_json(tmp_path / "nope.json", {"state": "X"})
    assert not (tmp_path / "nope.json").exists()
