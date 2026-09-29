"""Tests for compile-only early stops and exit-code grading.

A successful early stop reports ``EarlyStopResults`` (``NA``, ``early_stop: true``) and exits 0, while an ``NA`` without the marker, ``FAIL`` and strict ``XPASS`` exit 1. A nonzero simulator exit with no verdict is re-graded to ``FAIL``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from rtl_buddy.logging_utils import setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import load_result_json, write_result_json
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    EarlyStopResults,
    SimTimeoutResults,
    SkipResults,
    TestPassResults,
    TestResults as RtlBuddyTestResults,
    is_run_failure,
)
from rtl_buddy.runner.test_runner import RunDepth, TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.vlog_post import VlogPost, grade_unknown_sim_exit
from rtl_buddy.tools.vlog_sim import VlogSim


# Aliased on import: a bare ``TestResults`` or ``TestRunner`` in a test module is collected as a test class.
def _unknown_na(name: str = "smoke") -> RtlBuddyTestResults:
    """The result an aborted simulation leaves: no verdict, no early stop."""
    return RtlBuddyTestResults(name, {"result": "NA", "desc": "test result unknown"})


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope.

    ``CliRunner`` interleaves stdout and stderr, and compile progress text precedes the envelope.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_cli_compile_only_run_exits_zero_with_na_result_machine(
    minimal_project: Path,
):
    runner = CliRunner()
    rb = RtlBuddy(name="test_compile_only_machine")
    result = runner.invoke(rb.app, ["--machine", "-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    assert payload["exit_code"] == 0
    results = payload["payload"]["results"]
    assert len(results) == 1
    assert results[0]["name"] == "basic"
    assert results[0]["result"] == "NA"
    assert results[0]["desc"] == "Stopped early at compile"
    assert results[0]["early_stop"] is True


def test_cli_compile_only_run_exits_zero_human(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_compile_only_human")
    result = runner.invoke(rb.app, ["-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output


def test_cli_pre_early_stop_exits_zero_with_na_result_machine(
    minimal_project: Path,
):
    """The preproc early stop also gives NA and exit 0."""
    runner = CliRunner()
    rb = RtlBuddy(name="test_pre_only_machine")
    result = runner.invoke(rb.app, ["--machine", "-E", "pre", "test", "basic"])
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    assert payload["exit_code"] == 0
    results = payload["payload"]["results"]
    assert len(results) == 1
    assert results[0]["name"] == "basic"
    assert results[0]["result"] == "NA"


class TestExitCodeDecoupledFromVerdict:
    """Unit tests on ``RtlBuddy._exit_code_from_results``, which ORs each result's exit contribution."""

    def _exit_code(self, *results):
        rb = RtlBuddy(name="test_exit_code_decoupling")
        suite_results = [{"results": r} for r in results]
        return rb._exit_code_from_results(suite_results)

    def test_early_stop_na_is_exit_zero(self):
        assert self._exit_code(EarlyStopResults("basic", desc="x")) == 0

    def test_compile_fail_is_exit_one(self):
        assert self._exit_code(CompileFailResults("basic")) == 1

    def test_sim_timeout_is_exit_one(self):
        assert self._exit_code(SimTimeoutResults("basic")) == 1

    def test_pass_is_exit_zero(self):
        assert self._exit_code(TestPassResults("basic")) == 0

    def test_skip_is_exit_zero(self):
        assert self._exit_code(SkipResults("basic", "x")) == 0

    def test_mixed_list_with_one_fail_is_exit_one(self):
        assert (
            self._exit_code(
                TestPassResults("a"),
                SkipResults("b", "x"),
                EarlyStopResults("c", desc="x"),
                CompileFailResults("d"),
            )
            == 1
        )

    def test_unknown_na_is_exit_one(self):
        """An NA without the early-stop marker is an unknown outcome and exits 1."""
        result = _unknown_na()
        assert result.is_pass() is False
        assert self._exit_code(result) == 1

    def test_unknown_na_beside_a_pass_is_exit_one(self):
        assert self._exit_code(TestPassResults("a"), _unknown_na("b")) == 1


class TestEarlyStopSurvivesTheResultEnvelope:
    """The early-stop marker survives ``write_result_json`` and ``load_result_json`` for dispatched runs."""

    def _round_trip(self, tmp_path: Path, results):
        path = write_result_json(
            tmp_path / "result.json",
            test_name="smoke",
            run_id=None,
            results=results,
            run_token="tok",
        )
        return load_result_json(path)

    def _exit_code(self, results):
        rb = RtlBuddy(name="test_envelope_exit_code")
        return rb._exit_code_from_results([{"results": results}])

    def test_envelope_schema_version_is_unchanged(self, tmp_path: Path):
        path = write_result_json(
            tmp_path / "result.json",
            test_name="smoke",
            run_id=None,
            results=EarlyStopResults("smoke", desc="Stopped early at compile"),
        )
        assert json.loads(path.read_text())["schema_version"] == 1

    def test_early_stop_envelope_is_exit_zero(self, tmp_path: Path):
        envelope = self._round_trip(
            tmp_path, EarlyStopResults("smoke", desc="Stopped early at compile")
        )
        loaded = envelope["result"]
        assert loaded.results["result"] == "NA"
        assert loaded.results["early_stop"] is True
        assert self._exit_code(loaded) == 0

    def test_unknown_na_envelope_is_exit_one(self, tmp_path: Path):
        """A sim job envelope with NA and no early-stop marker fails the run."""
        loaded = self._round_trip(tmp_path, _unknown_na())["result"]
        assert loaded.results["result"] == "NA"
        assert "early_stop" not in loaded.results
        assert self._exit_code(loaded) == 1

    def test_envelope_from_an_older_rtl_buddy_is_exit_one(self, tmp_path: Path):
        """An envelope without the marker reads as unknown and exits 1."""
        path = tmp_path / "old.json"
        path.write_text(
            json.dumps(
                {
                    "rtl-buddy-filetype": "test_result",
                    "schema_version": 1,
                    "rtl_buddy_version": "6.45.0",
                    "run_token": None,
                    "test": "smoke",
                    "run_id": None,
                    "result": {
                        "kind": "TestResults",
                        "name": "smoke",
                        "results": {"result": "NA", "desc": "test result unknown"},
                    },
                }
            )
        )
        assert self._exit_code(load_result_json(path)["result"]) == 1


def _events(log_path: Path) -> list[dict]:
    """The JSONL records rtl_buddy wrote to its file log."""
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


class _AbortingSim:
    """A sim whose executable dies without printing a verdict.

    ``returncode`` is the simulator's exit status; ``transcript`` is written to ``test.log`` and post-processed by the real ``VlogPost``.
    """

    def __init__(self, tmp_path: Path, *, returncode: int, transcript: str):
        self._log = tmp_path / "test.log"
        self._log.write_text(transcript)
        self._returncode = returncode

    def pre(self, **_kwargs):
        return None

    def clear_retry_transcripts(self, run_ids):
        pass

    def compile(self):
        return 0

    def execute(self, **_kwargs):
        return self._returncode

    def post(self, run_id=None, sim_returncode=None):
        # Mirrors ``VlogSim.post``: parse the transcript, then grade an unknown verdict against the exit status.
        results = VlogPost(name="basic", path=str(self._log)).get_results()
        grade_unknown_sim_exit(
            results.results, sim_returncode, test="basic", run_id=run_id
        )
        return results


class _RunnerTestCfg:
    def get_name(self):
        return "basic"


def _runner_with(sim, monkeypatch, *, run_id=None):
    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=object(),
        test_cfg=_RunnerTestCfg(),
        rtl_builder_mode="debug",
        test_runner_mode={"sim_to_stdout": True},
        run_id=run_id,
        run_depth=RunDepth.POST,
    )
    monkeypatch.setattr(runner, "_create_vlog_sim", lambda: sim)
    return runner


class TestNonzeroSimExitWithoutVerdict:
    """A simulator that aborts (nonzero exit, no PASS/FAIL banner) fails; a transcript that states a verdict keeps it."""

    def test_unmarked_abort_is_graded_fail(self, tmp_path: Path, monkeypatch):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(
            tmp_path,
            returncode=1,
            transcript="running...\nNull pointer dereferenced\nAborting...\n",
        )
        result = _runner_with(sim, monkeypatch).run()
        assert result.results["result"] == "FAIL"
        assert result.results["desc"] == (
            "Sim exited 1 with no PASS/FAIL verdict in the transcript"
        )
        assert result.is_pass() is False

    def test_unmarked_abort_makes_the_cli_exit_one(self, tmp_path: Path, monkeypatch):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=1, transcript="Aborting...\n")
        result = _runner_with(sim, monkeypatch).run()
        rb = RtlBuddy(name="test_unmarked_abort_exit_code")
        assert rb._exit_code_from_results([{"results": result}]) == 1

    def test_signal_death_names_the_signal(self, tmp_path: Path, monkeypatch):
        """A signal death comes back negative (SIGABRT is -6), and the message names the signal."""
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=-6, transcript="Aborting...\n")
        result = _runner_with(sim, monkeypatch).run()
        assert result.results["result"] == "FAIL"
        assert result.results["desc"] == (
            "Sim killed by signal 6 with no PASS/FAIL verdict in the transcript"
        )

    def test_sim_stop_with_a_clean_exit_is_still_an_early_stop(
        self, tmp_path: Path, monkeypatch
    ):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=0, transcript="running...\n")
        runner = _runner_with(sim, monkeypatch)
        runner.run_depth = RunDepth.SIM
        result = runner.run()
        assert result.results["result"] == "NA"
        assert result.results["desc"] == "Stopped early at sim"
        assert not is_run_failure(result)

    def test_sim_stop_with_a_nonzero_exit_is_a_stage_failure(
        self, tmp_path: Path, monkeypatch
    ):
        """`-E sim` with a nonzero simulator exit is a stage failure.

        `-E sim` skips post-processing and nothing reads the transcript, so the row must not claim a verdict is missing.
        """
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        sim = _AbortingSim(
            tmp_path, returncode=1, transcript="FAIL replay seed missing\n"
        )
        runner = _runner_with(sim, monkeypatch)
        runner.run_depth = RunDepth.SIM
        result = runner.run()

        assert result.results["result"] == "FAIL"
        assert result.results["desc"] == (
            "Sim exited 1 before the -E sim stop; transcript not post-processed"
        )
        assert "early_stop" not in result.results
        assert is_run_failure(result)

        events = _events(log_path)
        assert "sim.unknown_verdict" not in [e.get("event") for e in events]
        stage = [e for e in events if e.get("event") == "sim.stage_failed"]
        assert len(stage) == 1
        assert stage[0]["returncode"] == 1
        assert stage[0]["stage"] == "sim"
        # The transcript is never read here, so its verdict is neither reported nor contradicted.
        assert "postproc.completed" not in [e.get("event") for e in events]

    def test_sim_stop_with_a_nonzero_exit_fails_each_run_of_a_multi_run(
        self, tmp_path: Path, monkeypatch
    ):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        sim = _AbortingSim(tmp_path, returncode=-6, transcript="Aborting...\n")
        runner = _runner_with(sim, monkeypatch, run_id=1)
        runner.run_depth = RunDepth.SIM
        results = runner.run_multiple([1, 2])
        graded = list(results.values()) if isinstance(results, dict) else list(results)
        assert graded, "run_multiple returned no runs"
        for result in graded:
            assert result.results["result"] == "FAIL"
            assert result.results["desc"] == (
                "Sim killed by signal 6 before the -E sim stop; "
                "transcript not post-processed"
            )
            assert is_run_failure(result)

        events = _events(log_path)
        assert "sim.unknown_verdict" not in [e.get("event") for e in events]
        assert [
            e["run_id"] for e in events if e.get("event") == "sim.stage_failed"
        ] == [
            1,
            2,
        ]

    def test_pass_banner_with_nonzero_exit_stays_pass(
        self, tmp_path: Path, monkeypatch
    ):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=3, transcript="PASS smoke completed\n")
        result = _runner_with(sim, monkeypatch).run()
        assert result.results["result"] == "PASS"

    def test_clean_exit_without_markers_stays_na(self, tmp_path: Path, monkeypatch):
        """A simulator that exits 0 with no markers stays NA and fails the run through the exit-code rule."""
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=0, transcript="simulation done\n")
        result = _runner_with(sim, monkeypatch).run()
        assert result.results["result"] == "NA"
        assert "early_stop" not in result.results

    def test_run_multiple_grades_each_run(self, tmp_path: Path, monkeypatch):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=1, transcript="Aborting...\n")
        results = _runner_with(sim, monkeypatch, run_id=1).run_multiple([1, 2])
        assert [r.results["result"] for r in results] == ["FAIL", "FAIL"]

    def test_compile_only_stop_is_untouched_by_a_nonzero_exit(
        self, tmp_path: Path, monkeypatch
    ):
        """A compile-only stop returns before simulation, so it keeps NA and exit 0."""
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=1, transcript="Aborting...\n")
        runner = _runner_with(sim, monkeypatch)
        runner.run_depth = RunDepth.COMP
        result = runner.run()
        assert result.results["result"] == "NA"
        assert result.results["early_stop"] is True
        rb = RtlBuddy(name="test_compile_only_still_zero")
        assert rb._exit_code_from_results([{"results": result}]) == 0


class TestGradeUnknownSimExit:
    """The rule shared by ``VlogSim.post`` and the ``-E sim`` stop; it reports whether it re-graded."""

    def test_reports_whether_it_regraded(self):
        unknown = {"result": "NA", "desc": "test result unknown"}
        assert grade_unknown_sim_exit(unknown, 1, test="basic") is True
        assert unknown["result"] == "FAIL"
        # A second pass is a no-op, so the desc does not name the exit code twice.
        assert grade_unknown_sim_exit(unknown, 1, test="basic") is False
        assert unknown["desc"] == (
            "Sim exited 1 with no PASS/FAIL verdict in the transcript"
        )

    def test_a_clean_or_absent_exit_status_grades_nothing(self):
        for returncode in (0, None):
            unknown = {"result": "NA", "desc": "test result unknown"}
            assert grade_unknown_sim_exit(unknown, returncode, test="basic") is False
            assert unknown["result"] == "NA"


class TestPostprocCompletedCarriesTheFinalVerdict:
    """``postproc.completed`` carries the final verdict, so re-grading happens inside ``VlogSim.post`` before the event."""

    def _sim(self, tmp_path: Path, transcript: str) -> VlogSim:
        """A ``VlogSim`` reduced to what ``post()`` reads, with filesystem and config stubbed."""
        log = tmp_path / "test.log"
        log.write_text(transcript)
        err = tmp_path / "test.err"
        err.write_text("")
        sim = VlogSim.__new__(VlogSim)
        sim.test_name = "basic"
        sim.run_id = None
        sim.test_cfg = SimpleNamespace(uvm=None)
        sim._get_log_path = lambda run_id=None: str(log)
        sim._get_err_path = lambda run_id=None: str(err)
        sim._assertions_enabled = lambda: False
        sim._coverage_enabled = lambda: False
        return sim

    def test_completed_event_carries_the_regraded_fail(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "Aborting...\n").post(sim_returncode=1)

        assert results.results["result"] == "FAIL"
        events = _events(log_path)
        completed = [e for e in events if e.get("event") == "postproc.completed"]
        assert len(completed) == 1
        assert completed[0]["result"] == "FAIL"
        assert completed[0]["desc"] == (
            "Sim exited 1 with no PASS/FAIL verdict in the transcript"
        )

    def test_unknown_verdict_is_logged_before_the_completed_event(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        self._sim(tmp_path, "Aborting...\n").post(sim_returncode=-6)

        names = [e.get("event") for e in _events(log_path)]
        assert "sim.unknown_verdict" in names, names
        assert names.index("sim.unknown_verdict") < names.index("postproc.completed"), (
            names
        )

    def test_a_clean_exit_still_completes_as_na(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "simulation done\n").post(sim_returncode=0)

        assert results.results["result"] == "NA"
        events = _events(log_path)
        assert not [e for e in events if e.get("event") == "sim.unknown_verdict"]
        completed = [e for e in events if e.get("event") == "postproc.completed"]
        assert completed[0]["result"] == "NA"

    def test_a_pass_banner_is_never_regraded(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "PASS smoke completed\n").post(sim_returncode=3)

        assert results.results["result"] == "PASS"
        completed = [
            e for e in _events(log_path) if e.get("event") == "postproc.completed"
        ]
        assert completed[0]["result"] == "PASS"

    def test_no_returncode_offered_grades_nothing(self, tmp_path: Path):
        """Callers that keep the ``None`` default get no re-grading."""
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "Aborting...\n").post()

        assert results.results["result"] == "NA"
        completed = [
            e for e in _events(log_path) if e.get("event") == "postproc.completed"
        ]
        assert completed[0]["result"] == "NA"


def test_unknown_verdict_event_has_a_human_message():
    """`sim.unknown_verdict` is logged at ERROR and needs a dedicated console line."""
    from rtl_buddy.logging_utils import _human_message

    exited = _human_message(
        "sim.unknown_verdict", {"test": "smoke", "run_id": None, "returncode": 1}
    )
    assert "smoke" in exited and "exited 1" in exited and "FAIL" in exited
    signalled = _human_message(
        "sim.unknown_verdict", {"test": "smoke", "returncode": -6}
    )
    assert "killed by signal 6" in signalled


def test_stage_failed_event_has_a_human_message():
    """`sim.stage_failed` is logged at ERROR and needs its own console line."""
    from rtl_buddy.logging_utils import _human_message

    exited = _human_message(
        "sim.stage_failed",
        {"test": "smoke", "run_id": None, "stage": "sim", "returncode": 1},
    )
    assert "smoke" in exited and "exited 1" in exited and "FAIL" in exited
    # The line must not claim the transcript has no verdict, since nothing read it.
    assert "no PASS/FAIL" not in exited
    signalled = _human_message(
        "sim.stage_failed", {"test": "smoke", "stage": "sim", "returncode": -6}
    )
    assert "killed by signal 6" in signalled
