"""Round-trip tests for per-run result JSON artifacts.

Covers ``TestResults.to_json_dict`` and ``from_json_dict`` and the ``write_result_json``
and ``load_result_json`` envelope layer.
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


def _sim_verdict_fail() -> TestResults:
    """A FAIL the simulation itself reported, which is excusable."""
    return TestResults(
        name="t/results",
        results={"result": "FAIL", "name": "t", "desc": "mismatch at 120ns"},
    )


@pytest.mark.parametrize("strict", [False, True])
def test_round_trip_preserves_xfail_semantics(strict):
    failed = apply_xfail(_sim_verdict_fail(), strict=strict)
    assert TestResults.from_json_dict(failed.to_json_dict()).is_pass()

    passed = apply_xfail(TestPassResults(name="t/results"), strict=strict)
    clone = TestResults.from_json_dict(passed.to_json_dict())
    assert clone.is_pass() == (not strict)


@pytest.mark.parametrize(
    "result",
    [
        CompileFailResults(name="t/results"),
        SimTimeoutResults(name="t/results"),
        SetupFailResults(name="t/results", desc="Setup failed in preproc: boom"),
    ],
    ids=lambda r: type(r).__name__,
)
@pytest.mark.parametrize("strict", [False, True])
def test_round_trip_keeps_a_stage_failure_unexcusable(result, strict):
    """The non-excusable marker travels in the envelope's results dict, because
    ``from_json_dict`` rebuilds every kind as a plain ``TestResults``.
    """
    clone = TestResults.from_json_dict(result.to_json_dict())
    apply_xfail(clone, strict=strict)
    assert clone.results["result"] == "FAIL"
    assert clone.is_pass() is False
    assert clone.results["desc"].startswith("xfail not applied (")


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
    assert load_result_json(out, expected_run_token="abc123")["result"].is_pass()
    assert load_result_json(out)["result"].is_pass()


def test_stale_run_token_is_rejected_like_a_missing_file(tmp_path: Path):
    """An envelope stamped with a different run token is rejected like a missing file."""
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
    # An envelope with no token fails when the head expects one.
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
    assert load_result_json(out)["result"].is_pass()
    assert not out.with_name(out.name + ".tmp").exists()


def test_attach_telemetry_missing_file_is_noop(tmp_path: Path):
    from rtl_buddy.runner.result_io import attach_telemetry_json

    attach_telemetry_json(tmp_path / "nope.json", {"state": "X"})
    assert not (tmp_path / "nope.json").exists()


def test_attach_result_key_folds_into_the_runs_own_results(tmp_path: Path):
    """The key lands in the run's own ``results``, where `rb graph results` reads it."""
    from rtl_buddy.runner.result_io import attach_result_key

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    attach_result_key(out, "compile", {"duration_sec": 3.5, "builder": "verilator"})
    envelope = json.loads(out.read_text())
    assert envelope["result"]["results"]["compile"]["duration_sec"] == 3.5
    assert load_result_json(out)["result"].is_pass()
    assert not out.with_name(out.name + ".tmp").exists()


@pytest.mark.parametrize(
    "content",
    [None, "not json at all", '{"result": "a string, not a dict"}'],
)
def test_attach_result_key_degrades_instead_of_raising(tmp_path: Path, content):
    """A missing, corrupt or malformed envelope is left as found, with no exception."""
    from rtl_buddy.runner.result_io import attach_result_key

    out = tmp_path / "result.json"
    if content is None:
        attach_result_key(out, "compile", {"duration_sec": 1.0})
        assert not out.exists()
        return
    out.write_text(content)
    attach_result_key(out, "compile", {"duration_sec": 1.0})
    # No stray .tmp file remains.
    assert out.read_text() == content
    assert not out.with_name(out.name + ".tmp").exists()


def test_an_unserialisable_annotation_leaves_the_envelope_as_found(tmp_path: Path):
    """An unserialisable value leaves the envelope as found."""
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
    """A failed write (ENOSPC, EROFS) does not raise, so collection continues."""
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


def test_a_coverage_refresh_that_cannot_land_degrades_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed refresh returns None and leaves the envelope as found, without raising."""
    from rtl_buddy.runner import result_io
    from rtl_buddy.runner.result_io import refresh_result_json

    out = tmp_path / "result.json"
    write_result_json(
        out, test_name="t", run_id=1, results=TestPassResults(name="t/results")
    )
    before = out.read_text()

    def _erofs(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(result_io.Path, "write_text", _erofs)
    refreshed = refresh_result_json(out, CompileFailResults(name="t/results"))

    monkeypatch.undo()
    assert refreshed is None
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
    """An old envelope read by a new head lacks `builds` and still loads; the schema
    version is unchanged.
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
    """A new envelope read by an old head ignores the extra `builds` key."""
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
