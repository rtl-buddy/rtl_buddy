"""Tests for the guard that fails when coverage is requested but no executed test produced coverage data.

``RtlBuddy.do_cmd_test`` and the regression command raise ``FatalRtlBuddyError`` in that case unless every test was skipped. The stub ``echo`` builder with ``-E comp`` reproduces it.
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
    """``--coverage-html`` fires the guard too."""
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


def test_coverage_source_summary_requested_with_no_data_raises_fatal_error(
    minimal_project: Path, capsys, monkeypatch
):
    """``--coverage-source-summary`` is a coverage output like the rest, so a run with no model fails rather than printing nothing."""
    rb = RtlBuddy(name="test_coverage_guard_source")
    monkeypatch.setattr(
        "sys.argv",
        [
            "rb",
            "--machine",
            "-E",
            "comp",
            "test",
            "basic",
            "--coverage-source-summary",
        ],
    )
    exit_code = rb.run()
    captured = capsys.readouterr()

    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "no coverage data" in payload["payload"]["error"]


def test_regression_coverage_source_summary_requested_with_no_data_raises(
    minimal_project: Path, capsys, monkeypatch
):
    """``rb regression`` applies the same guard to ``--coverage-source-summary``."""
    rb = RtlBuddy(name="test_coverage_guard_source_regression")
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
            "--coverage-source-summary",
        ],
    )
    exit_code = rb.run()
    captured = capsys.readouterr()

    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "no coverage data" in payload["payload"]["error"]


def _spy_on_build_metadata(monkeypatch):
    """Let a coverage flag reach the reporter on a project with no simulator.

    The stub builder produces no coverage database, so the test pretends one exists to reach the reporter call and capture its arguments.
    """
    from rtl_buddy.tools.coverage import CoverageReporter

    captured: dict = {}
    monkeypatch.setattr(
        CoverageReporter, "collect_paths", lambda self, results: ["stub.dat"]
    )

    def spy(self, suite_results, **kwargs):
        captured.update(kwargs)
        return [], {"merged": None, "dir_summary": []}

    monkeypatch.setattr(CoverageReporter, "build_metadata", spy)
    return captured


def test_test_passes_the_source_summary_flag_to_the_reporter(
    minimal_project: Path, monkeypatch
):
    """`rb test` passes `--coverage-source-summary` to the reporter."""
    captured = _spy_on_build_metadata(monkeypatch)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["-E", "comp", "test", "basic", "--coverage-source-summary"]
    )

    assert result.exit_code == 0, result.output
    assert captured["source_summary"] is True


def test_regression_passes_the_source_summary_flag_to_the_reporter(
    minimal_project: Path, monkeypatch
):
    """`rb regression`, which has its own flag list, passes it too."""
    captured = _spy_on_build_metadata(monkeypatch)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["-M", "debug", "-E", "comp", "regression", "--coverage-source-summary"],
    )

    assert result.exit_code == 0, result.output
    assert captured["source_summary"] is True


def test_coverage_requested_but_all_tests_skipped_does_not_raise(
    minimal_project: Path,
):
    """The guard does not fire when the level window skips every test."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "--start-level", "10", "--coverage-merge"])
    assert result.exit_code == 0, result.output


def test_no_coverage_flags_never_triggers_guard(minimal_project: Path):
    """A compile-only run without coverage flags is unaffected."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output


def test_regression_coverage_merge_requested_with_no_data_raises_fatal_error(
    minimal_project: Path, capsys, monkeypatch
):
    """``rb regression`` applies the same guard.

    The stub builder only declares "debug" while regression defaults to builder-mode "reg", so the test passes ``-M debug``.
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
