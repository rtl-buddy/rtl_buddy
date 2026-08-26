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


def test_run_token_round_trips_and_matches(tmp_path: Path):
    out = tmp_path / "result.json"
    write_result_json(
        out,
        test_name="t",
        run_id=1,
        results=TestPassResults(name="t/results"),
        run_token="abc123",
    )
    assert json.loads(out.read_text())["run_token"] == "abc123"
    # Matching token loads normally.
    assert load_result_json(out, expected_run_token="abc123")["result"].is_pass()
    # No expectation → token ignored (legacy / non-dispatch callers).
    assert load_result_json(out)["result"].is_pass()


def test_stale_run_token_is_rejected_like_a_missing_file(tmp_path: Path):
    """A leftover envelope from an earlier run (different token) must not be
    mistaken for this run's result — this replaces the pre-unlink the head
    used to do, which blinded it on NFS (#362)."""
    out = tmp_path / "result.json"
    write_result_json(
        out,
        test_name="t",
        run_id=1,
        results=TestPassResults(name="t/results"),
        run_token="OLD-run",
    )
    with pytest.raises(FatalRtlBuddyError, match="different run"):
        load_result_json(out, expected_run_token="NEW-run")
    # A dispatch job that never stamped a token (None) also fails the check
    # when the head expects one.
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    with pytest.raises(FatalRtlBuddyError, match="different run"):
        load_result_json(out, expected_run_token="NEW-run")


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


# --------------------------------------------------- build envelope (#495)


def test_attach_result_key_folds_into_the_runs_own_results(tmp_path: Path):
    """`rb graph results` reads `result.results`, so that is where it goes."""
    from rtl_buddy.runner.result_io import attach_result_key

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    attach_result_key(out, "compile", {"duration_sec": 3.5, "builder": "verilator"})
    envelope = json.loads(out.read_text())
    assert envelope["result"]["results"]["compile"]["duration_sec"] == 3.5
    # The verdict is untouched and the envelope still loads.
    assert load_result_json(out)["result"].is_pass()
    assert not out.with_name(out.name + ".tmp").exists()


@pytest.mark.parametrize(
    "content",
    [None, "not json at all", '{"result": "a string, not a dict"}'],
)
def test_attach_result_key_degrades_instead_of_raising(tmp_path: Path, content):
    """An annotation must never re-score a collected run."""
    from rtl_buddy.runner.result_io import attach_result_key

    out = tmp_path / "result.json"
    if content is None:  # no envelope at all
        attach_result_key(out, "compile", {"duration_sec": 1.0})
        assert not out.exists()
        return
    out.write_text(content)
    attach_result_key(out, "compile", {"duration_sec": 1.0})
    # Left exactly as found — including no stray .tmp beside it.
    assert out.read_text() == content
    assert not out.with_name(out.name + ".tmp").exists()


def test_an_unserialisable_annotation_leaves_the_envelope_as_found(tmp_path: Path):
    """The value is the caller's problem, never the collected run's."""
    from rtl_buddy.runner.result_io import attach_result_key

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    before = out.read_text()
    attach_result_key(out, "compile", {"duration_sec": object()})
    assert out.read_text() == before
    assert load_result_json(out)["result"].is_pass()
    assert not out.with_name(out.name + ".tmp").exists()


def test_a_write_that_cannot_land_does_not_take_the_collection_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """ENOSPC/EROFS at collect time must not lose a finished fleet.

    The head performs one of these rewrites per collected row; an
    exception here would abandon every result already gathered and turn a
    fully finished run into a traceback.
    """
    from rtl_buddy.runner import result_io
    from rtl_buddy.runner.result_io import attach_telemetry_json

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    before = out.read_text()

    def _enospc(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(result_io.Path, "write_text", _enospc)
    attach_telemetry_json(out, {"state": "COMPLETED"})

    monkeypatch.undo()
    assert out.read_text() == before
    assert not out.with_name(out.name + ".tmp").exists()


def test_build_envelope_round_trips_its_compile_records(tmp_path: Path):
    from rtl_buddy.runner.result_io import (
        load_build_result_json,
        write_build_result_json,
    )

    out = tmp_path / "build.json"
    records = [
        {
            "test": "basic",
            "builder": "verilator",
            "duration_sec": 12.5,
            "reused": False,
            "group": "obj_dir_cafe",
        }
    ]
    write_build_result_json(out, built=["basic"], failed=[], builds=records)
    loaded = load_build_result_json(out)
    assert loaded["built"] == ["basic"]
    assert loaded["builds"] == records


def test_a_build_envelope_without_records_is_still_readable(tmp_path: Path):
    """Old envelope, new head: `builds` is additive, so it degrades (#495).

    The schema version deliberately does not move — bumping it would make
    an old head read None and lose the compile-fail parity it has today.
    """
    from rtl_buddy.runner.result_io import (
        BUILD_RESULT_SCHEMA_VERSION,
        load_build_result_json,
        write_build_result_json,
    )

    out = tmp_path / "build.json"
    write_build_result_json(out, built=["basic"], failed=["extra"])
    assert "builds" not in json.loads(out.read_text())
    assert json.loads(out.read_text())["schema_version"] == BUILD_RESULT_SCHEMA_VERSION

    loaded = load_build_result_json(out)
    assert loaded["failed"] == ["extra"]
    assert loaded["builds"] == []


def test_a_new_build_envelope_read_the_old_way_keeps_built_and_failed(
    tmp_path: Path,
):
    """New envelope, old head: the extra key is simply not looked at (#495).

    Simulated by dropping `builds` the way an older loader's fixed key set
    does, which is the whole claim `schema_version: 1` is making.
    """
    from rtl_buddy.runner.result_io import (
        BUILD_RESULT_SCHEMA_VERSION,
        write_build_result_json,
    )

    out = tmp_path / "build.json"
    write_build_result_json(
        out,
        built=["basic"],
        failed=["extra"],
        builds=[{"test": "basic", "builder": "verilator"}],
    )
    raw = json.loads(out.read_text())
    assert raw["schema_version"] == BUILD_RESULT_SCHEMA_VERSION
    old_view = {
        "built": list(raw.get("built") or []),
        "failed": list(raw.get("failed") or []),
    }
    assert old_view == {"built": ["basic"], "failed": ["extra"]}
