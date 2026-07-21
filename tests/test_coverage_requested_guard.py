"""Tests for #334 — fail loud when coverage is requested but no coverage
data was produced by any executed test.

``RtlBuddy.do_cmd_test`` and the regression command both raise
``FatalRtlBuddyError`` when: coverage output was requested, no executed
test produced raw coverage data, and at least one test was not skipped.
The ``minimal_project`` fixture's stub ``echo`` builder never produces
real coverage data, so combining it with ``-E comp`` (compile-only early
stop) reliably reproduces the "requested but missing" case.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_coverage_guard")


def test_coverage_merge_requested_with_no_data_raises_fatal_error(
    minimal_project: Path, capsys, monkeypatch
):
    """``rb --machine test basic --coverage-merge`` fires the guard (exit 2)."""
    rb = RtlBuddy(name="test_coverage_guard_merge")
    monkeypatch.setattr(
        "sys.argv",
        ["rb", "--machine", "-E", "comp", "test", "basic", "--coverage-merge"],
    )
    exit_code = rb.run()
    captured = capsys.readouterr()

    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 2
    assert "no coverage data" in payload["payload"]["error"]


def test_coverage_html_requested_with_no_data_raises_fatal_error(
    minimal_project: Path, capsys, monkeypatch
):
    """Same guard fires for ``--coverage-html``, not just ``--coverage-merge``."""
    rb = RtlBuddy(name="test_coverage_guard_html")
    monkeypatch.setattr(
        "sys.argv",
        ["rb", "--machine", "-E", "comp", "test", "basic", "--coverage-html"],
    )
    exit_code = rb.run()
    captured = capsys.readouterr()

    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "no coverage data" in payload["payload"]["error"]


def test_coverage_requested_but_all_tests_skipped_does_not_raise(
    minimal_project: Path,
):
    """If every test is skipped by the level window, the guard must not fire."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "--start-level", "10", "--coverage-merge"])
    assert result.exit_code == 0, result.output


def test_no_coverage_flags_never_triggers_guard(minimal_project: Path):
    """A normal compile-only run without any coverage flag is unaffected."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output


def test_regression_coverage_merge_requested_with_no_data_raises_fatal_error(
    minimal_project: Path, capsys, monkeypatch
):
    """The same guard applies to ``rb regression``.

    The fixture's "stub" builder only declares a "debug" mode, while
    regression defaults to builder-mode "reg" — override with the global
    ``-M debug`` so the run gets far enough to hit the coverage guard
    instead of failing earlier on a missing builder mode.
    """
    rb = RtlBuddy(name="test_coverage_guard_regression")
    monkeypatch.setattr(
        "sys.argv",
        [
            "rb",
            "--machine",
            "-M",
            "debug",
            "-E",
            "comp",
            "regression",
            "--coverage-merge",
        ],
    )
    exit_code = rb.run()
    captured = capsys.readouterr()

    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "no coverage data" in payload["payload"]["error"]
