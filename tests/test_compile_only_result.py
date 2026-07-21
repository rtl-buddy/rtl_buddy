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
result only when it is *not* a pass *and* its ``result`` value is *not*
``"NA"``. Net effect:

- ``PASS``/``SKIP``/``XFAIL``/non-strict ``XPASS`` (``is_pass()`` true) -> 0
- ``NA`` (any intentional early stop: pre/comp/sim) -> 0
- ``FAIL`` (real sim failure, or ``CompileFailResults`` /
  ``SimTimeoutResults`` / ``SetupFailResults`` / ``FilelistFailResults``)
  -> 1
- strict ``XPASS`` -> 1

Rationale: the exit code reflects whether rtl_buddy and the tools ran
properly, not the design-under-test verdict; ``NA`` means "ran fine, hand
checking required," so it must not fail the run.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    EarlyStopResults,
    SimTimeoutResults,
    SkipResults,
    TestPassResults,
)


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
