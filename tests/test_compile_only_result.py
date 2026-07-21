"""Tests for #336 — a compile-only run (``-E comp``) is a success, not a
neutral "early stop".

``CompilePassResults`` (``src/rtl_buddy/runner/test_results.py``) reports
``result: "COMPILED"`` / ``stage: "compile"`` and is treated as a pass by
both ``TestResults.is_pass()`` and the xfail-aware ``_BASE_PASS`` tuple.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import CompilePassResults
from rtl_buddy.runner.xfail import is_pass_with_xfail


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope.

    ``CliRunner`` interleaves stdout and stderr into ``result.output``, and
    the compile progress text ("Compiling basic") precedes the JSON
    envelope on the wire.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_compile_pass_results_shape():
    r = CompilePassResults(name="basic/results")
    assert r.results["result"] == "COMPILED"
    assert r.results["stage"] == "compile"
    assert r.results["desc"] == "Stopped early at compile"
    assert r.is_pass() is True


def test_compile_pass_results_custom_desc():
    r = CompilePassResults(name="basic/results", desc="custom desc")
    assert r.results["desc"] == "custom desc"


def test_compiled_counts_as_pass_in_base_pass_tuple():
    assert is_pass_with_xfail({"result": "COMPILED"}) is True


def test_cli_compile_only_run_exits_zero_with_compiled_result_machine(
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
    assert results[0]["result"] == "COMPILED"


def test_cli_compile_only_run_exits_zero_human(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_compile_only_human")
    result = runner.invoke(rb.app, ["-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output
    assert "COMPILED" in result.output


def test_repeated_compile_pass_results_are_all_compiled_and_pass():
    """``rb test`` has no repeat/count flag to drive the repeated-run path
    end-to-end (see ``test_runner.py``'s ``run_ids`` list-comprehension
    branch), so exercise the same construction directly instead."""
    run_ids = [1, 2, 3]
    results = [CompilePassResults(name="basic/results") for _ in run_ids]
    assert len(results) == 3
    assert all(r.results["result"] == "COMPILED" for r in results)
    assert all(r.is_pass() for r in results)
