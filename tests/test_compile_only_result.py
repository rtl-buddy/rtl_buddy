"""Tests for #336 — a compile-only run (``-E comp``) is a neutral early
stop, not a pass and not a failure, and the process exit code is decoupled
from the PASS/FAIL verdict.

There is no ``CompilePassResults`` class and no ``"COMPILED"`` result.
A successful compile-only (or pre-only) early stop reports
``EarlyStopResults`` (``src/rtl_buddy/runner/test_results.py``):
``result: "NA"``, ``desc: "Stopped early at compile"`` (or the analogous
preproc description). ``xfail._BASE_PASS`` is back to
``("PASS", "SKIP", "XFAIL")`` — ``NA`` is not a pass.

Instead, ``RtlBuddy._exit_code_from_results`` contributes exit 1 for a
result only when it is *not* a pass *and* not an intentional early stop.
Net effect:

- ``PASS``/``SKIP``/``XFAIL``/non-strict ``XPASS`` (``is_pass()`` true) -> 0
- ``NA`` from an intentional early stop (pre/comp/sim), which carries
  ``early_stop: true`` -> 0
- ``NA`` meaning "no verdict was produced" (#546) -> 1
- ``FAIL`` (real sim failure, or ``CompileFailResults`` /
  ``SimTimeoutResults`` / ``SetupFailResults`` / ``FilelistFailResults``)
  -> 1
- strict ``XPASS`` -> 1

Rationale: the exit code reflects whether rtl_buddy and the tools ran
properly, not the design-under-test verdict; an intentional early stop
means "ran fine, hand checking required," so it must not fail the run.

#546 split the two ``NA``s that #336 had lumped together. An aborted
simulation produces no PASS/FAIL banner and so also reports ``NA``, and
the blanket exemption made a dispatched run whose sim job exited 1 still
exit 0. Only ``EarlyStopResults`` -- the stop rtl_buddy was asked for --
carries ``early_stop: true``, and only that is exempt; an unknown ``NA``
fails the run, on the in-process path and through a result envelope
alike. A nonzero simulator exit with no verdict in its transcript is
re-graded to ``FAIL`` by ``TestRunner`` before it ever gets that far.
"""

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


# Aliased on import, like test_setup_failures.py: a bare ``TestResults`` /
# ``TestRunner`` in a test module is collected as a test class by pytest.
def _unknown_na(name: str = "smoke") -> RtlBuddyTestResults:
    """The result an aborted simulation leaves: no verdict, no early stop."""
    return RtlBuddyTestResults(name, {"result": "NA", "desc": "test result unknown"})


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope.

    ``CliRunner`` interleaves stdout and stderr into ``result.output``, and
    the compile progress text ("Compiling basic") precedes the JSON
    envelope on the wire.
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
    # The machine row carries the discriminator, so automation does not
    # have to parse the human desc to tell this NA from an unknown one.
    assert results[0]["early_stop"] is True


def test_cli_compile_only_run_exits_zero_human(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_compile_only_human")
    result = runner.invoke(rb.app, ["-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output


def test_cli_pre_early_stop_exits_zero_with_na_result_machine(
    minimal_project: Path,
):
    """The same NA/exit-0 treatment applies at the preproc early stop."""
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
    """Unit tests on ``RtlBuddy._exit_code_from_results`` — the core of the
    #336 redesign. It takes a list of ``{"results": <TestResults>, ...}``
    dicts and combines each result's contribution with bitwise OR."""

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
        """#546: an NA nobody asked for is an unknown outcome, so it fails.

        The issue's reproducer verbatim -- ``is_pass()`` was already false
        here; the exemption was throwing that away.
        """
        result = _unknown_na()
        assert result.is_pass() is False
        assert self._exit_code(result) == 1

    def test_unknown_na_beside_a_pass_is_exit_one(self):
        assert self._exit_code(TestPassResults("a"), _unknown_na("b")) == 1


class TestEarlyStopSurvivesTheResultEnvelope:
    """#546 through the dispatched path: the head grades a *loaded*
    envelope, so the early-stop marker has to round-trip
    ``write_result_json`` / ``load_result_json`` (``runner/result_io.py``)
    or every dispatched compile-only run would start failing."""

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
        """The observed dispatched failure: the sim job's envelope says NA
        with no early-stop marker, and the head must fail the run."""
        loaded = self._round_trip(tmp_path, _unknown_na())["result"]
        assert loaded.results["result"] == "NA"
        assert "early_stop" not in loaded.results
        assert self._exit_code(loaded) == 1

    def test_envelope_from_an_older_rtl_buddy_is_exit_one(self, tmp_path: Path):
        """No marker at all (an envelope written before #546) reads as
        unknown -- the safe side of an additive field."""
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


class _AbortingSim:
    """A sim whose executable dies without printing a verdict (#546).

    ``returncode`` is what the simulator exited with; ``transcript`` is
    what it left in ``test.log``, post-processed by the real ``VlogPost``
    so the NA under test is the one the tool actually produces.
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
        # Mirrors the contract ``VlogSim.post`` implements: parse the
        # transcript, then grade an unknown verdict against the exit
        # status the caller hands over, so the result is final before it
        # is announced (#574 review).
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
    """#546: a simulator that aborted (nonzero exit, no PASS/FAIL banner in
    the transcript) is a failure, not an outcome to hand-check. A simulator
    exit code is still not a verdict on its own, so a transcript that did
    state one keeps it."""

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
        """A simulator killed by a signal comes back negative (SIGABRT is
        -6, which is what an abort looks like), and "exited -6" would
        misreport it."""
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

    def test_sim_stop_with_a_nonzero_exit_is_a_failure(
        self, tmp_path: Path, monkeypatch
    ):
        """Devin review on rtl_buddy#574: `-E sim` skips post-processing,
        so it never reached the re-grading and a crashed simulator came
        back as a successful early stop."""
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=1, transcript="Aborting...\n")
        runner = _runner_with(sim, monkeypatch)
        runner.run_depth = RunDepth.SIM
        result = runner.run()
        assert result.results["result"] == "FAIL"
        assert "exited 1" in result.results["desc"]
        assert "early_stop" not in result.results
        assert is_run_failure(result)

    def test_sim_stop_with_a_nonzero_exit_fails_each_run_of_a_multi_run(
        self, tmp_path: Path, monkeypatch
    ):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=-6, transcript="Aborting...\n")
        runner = _runner_with(sim, monkeypatch, run_id=1)
        runner.run_depth = RunDepth.SIM
        results = runner.run_multiple([1, 2])
        graded = list(results.values()) if isinstance(results, dict) else list(results)
        assert graded, "run_multiple returned no runs"
        for result in graded:
            assert result.results["result"] == "FAIL"
            assert "killed by signal 6" in result.results["desc"]
            assert is_run_failure(result)

    def test_pass_banner_with_nonzero_exit_stays_pass(
        self, tmp_path: Path, monkeypatch
    ):
        setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
        sim = _AbortingSim(tmp_path, returncode=3, transcript="PASS smoke completed\n")
        result = _runner_with(sim, monkeypatch).run()
        assert result.results["result"] == "PASS"

    def test_clean_exit_without_markers_stays_na(self, tmp_path: Path, monkeypatch):
        """A simulator that exited 0 having said nothing is still unknown --
        NA, reviewed by hand -- and re-grading it FAIL would be a new
        verdict rule rather than the #546 fix. It fails the run either way
        now, through the exit-code rule."""
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
        """The early stop returns before the simulation, so it keeps its
        intentional NA and its exit 0."""
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
    """The one implementation of the rule, shared by ``VlogSim.post`` and
    the ``-E sim`` stop. It reports whether it re-graded, so a caller can
    tell a graded row from an untouched one."""

    def test_reports_whether_it_regraded(self):
        unknown = {"result": "NA", "desc": "test result unknown"}
        assert grade_unknown_sim_exit(unknown, 1, test="basic") is True
        assert unknown["result"] == "FAIL"
        # Idempotent: the row is no longer unknown, so a second pass is a
        # no-op rather than a desc that names the exit code twice.
        assert grade_unknown_sim_exit(unknown, 1, test="basic") is False
        assert unknown["desc"] == (
            "Sim exited 1 with no PASS/FAIL verdict in the transcript"
        )

    def test_a_clean_or_absent_exit_status_grades_nothing(self):
        for returncode in (0, None):
            unknown = {"result": "NA", "desc": "test result unknown"}
            assert grade_unknown_sim_exit(unknown, returncode, test="basic") is False
            assert unknown["result"] == "NA"

    def test_an_early_stop_marker_does_not_survive_the_regrade(self):
        stopped = {"result": "NA", "desc": "Stopped early at sim", "early_stop": True}
        assert grade_unknown_sim_exit(stopped, -6, test="basic") is True
        assert stopped["result"] == "FAIL"
        assert "early_stop" not in stopped


class TestPostprocCompletedCarriesTheFinalVerdict:
    """#574 review: ``postproc.completed`` is the authoritative record of a
    run's verdict for JSONL consumers (``docs/agents.md``), so the
    re-grading has to happen inside ``VlogSim.post`` — before that event
    is emitted -- rather than afterwards in the runner. Otherwise the log
    says ``NA`` while the envelope and the exit code say ``FAIL``."""

    def _sim(self, tmp_path: Path, transcript: str) -> VlogSim:
        """A ``VlogSim`` reduced to what ``post()`` reads: the real method
        under test, with the filesystem and config surface stubbed."""
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

    def _events(self, log_path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]

    def test_completed_event_carries_the_regraded_fail(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "Aborting...\n").post(sim_returncode=1)

        assert results.results["result"] == "FAIL"
        events = self._events(log_path)
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

        names = [e.get("event") for e in self._events(log_path)]
        assert "sim.unknown_verdict" in names, names
        assert names.index("sim.unknown_verdict") < names.index("postproc.completed"), (
            names
        )

    def test_a_clean_exit_still_completes_as_na(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "simulation done\n").post(sim_returncode=0)

        assert results.results["result"] == "NA"
        events = self._events(log_path)
        assert not [e for e in events if e.get("event") == "sim.unknown_verdict"]
        completed = [e for e in events if e.get("event") == "postproc.completed"]
        assert completed[0]["result"] == "NA"

    def test_a_pass_banner_is_never_regraded(self, tmp_path: Path):
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "PASS smoke completed\n").post(sim_returncode=3)

        assert results.results["result"] == "PASS"
        completed = [
            e for e in self._events(log_path) if e.get("event") == "postproc.completed"
        ]
        assert completed[0]["result"] == "PASS"

    def test_no_returncode_offered_grades_nothing(self, tmp_path: Path):
        """Every other caller of ``post()`` keeps the ``None`` default and
        the behaviour it always had."""
        log_path = tmp_path / "rtl_buddy.log"
        setup_logging(color=False, machine=True, log_path=log_path)
        results = self._sim(tmp_path, "Aborting...\n").post()

        assert results.results["result"] == "NA"
        completed = [
            e for e in self._events(log_path) if e.get("event") == "postproc.completed"
        ]
        assert completed[0]["result"] == "NA"


def test_unknown_verdict_event_has_a_human_message():
    """`sim.unknown_verdict` is logged at ERROR, so it needs a dedicated
    console line rather than the generic fallback (#546)."""
    from rtl_buddy.logging_utils import _human_message

    exited = _human_message(
        "sim.unknown_verdict", {"test": "smoke", "run_id": None, "returncode": 1}
    )
    assert "smoke" in exited and "exited 1" in exited and "FAIL" in exited
    signalled = _human_message(
        "sim.unknown_verdict", {"test": "smoke", "returncode": -6}
    )
    assert "killed by signal 6" in signalled
