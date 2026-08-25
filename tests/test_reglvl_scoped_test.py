"""Tests for #339 — ``rb test`` gains long-form ``--reg-level`` /
``--start-level`` options, producing SKIP results for tests outside the
level window (same semantics as ``rb regression``).

The ``minimal_project`` fixture declares ``basic`` at reglvl 0 and
``extra`` at reglvl 5. ``-E comp`` keeps compiled tests at the neutral
early-stop result NA (see #336) instead of running the stub sim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope."""
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _results_by_name(payload: dict) -> dict:
    return {r["name"]: r for r in payload["payload"]["results"]}


def test_reg_level_zero_runs_basic_skips_extra(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_reglvl_stop_0")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "--reg-level", "0"]
    )
    assert result.exit_code == 0, result.output

    by_name = _results_by_name(_last_json(result.output))
    assert by_name["basic"]["result"] == "NA"
    assert by_name["extra"]["result"] == "SKIP"


def test_start_level_five_skips_basic_runs_extra(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_reglvl_start_5")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "--start-level", "5"]
    )
    assert result.exit_code == 0, result.output

    by_name = _results_by_name(_last_json(result.output))
    assert by_name["basic"]["result"] == "SKIP"
    assert by_name["extra"]["result"] == "NA"


def test_start_level_above_both_reglvls_skips_everything(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_reglvl_start_10")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "--start-level", "10"]
    )
    assert result.exit_code == 0, result.output

    by_name = _results_by_name(_last_json(result.output))
    assert by_name["basic"]["result"] == "SKIP"
    assert by_name["extra"]["result"] == "SKIP"


def test_multiple_test_names_run_once_in_command_line_order(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_multiple_names")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "extra", "basic"]
    )
    assert result.exit_code == 0, result.output

    rows = _last_json(result.output)["payload"]["results"]
    assert [row["name"] for row in rows] == ["extra", "basic"]
    assert [row["result"] for row in rows] == ["NA", "NA"]


def test_level_filtering_applies_only_to_selected_names(minimal_project: Path):
    runner = CliRunner()
    rb = RtlBuddy(name="test_selected_name_reglvl")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "extra", "--reg-level", "0"]
    )
    assert result.exit_code == 0, result.output

    rows = _last_json(result.output)["payload"]["results"]
    assert [(row["name"], row["result"]) for row in rows] == [("extra", "SKIP")]


def test_filter_uses_case_sensitive_regex_search_in_suite_order(
    minimal_project: Path,
):
    runner = CliRunner()
    rb = RtlBuddy(name="test_regex_filter")
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "--filter", "asi|xtr"]
    )
    assert result.exit_code == 0, result.output

    rows = _last_json(result.output)["payload"]["results"]
    assert [row["name"] for row in rows] == ["basic", "extra"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["basic", "missing"], "test_name missing not found"),
        (["basic", "basic"], "duplicate test name 'basic'"),
        (["basic", "--filter", "basic"], "mutually exclusive"),
        (["--filter", "["], "invalid --filter regex"),
        (["--filter", "BASIC"], "matched no tests"),
    ],
)
def test_invalid_test_selections_fail_before_running(
    minimal_project: Path, args: list[str], message: str
):
    runner = CliRunner()
    rb = RtlBuddy(name="test_invalid_selection")
    result = runner.invoke(rb.app, ["test", *args])

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert message in str(result.exception)


def test_help_declares_reg_level_without_stealing_rnd_last_short_flag(
    minimal_project: Path,
):
    """``-l`` must stay bound to ``--rnd-last``; ``--reg-level`` has no short
    flag (``-l`` is already taken on ``rb test``, unlike ``rb regression``)."""
    runner = CliRunner()
    rb = RtlBuddy(name="test_reglvl_help")
    result = runner.invoke(rb.app, ["test", "--help"])
    assert result.exit_code == 0, result.output

    # Strip rich's box-drawing/ANSI so line matching is layout-independent.
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    assert "--reg-level" in output
    assert "--start-level" in output

    # Match the short flag "-l" as its own token, not the "-l" inside
    # "--reg-level"/"--rnd-last" themselves (negative lookahead excludes a
    # following letter).
    short_flag_l = re.compile(r"-l(?![A-Za-z])")

    rnd_last_line = next(line for line in output.splitlines() if "--rnd-last" in line)
    assert short_flag_l.search(rnd_last_line)

    reg_level_line = next(line for line in output.splitlines() if "--reg-level" in line)
    assert not short_flag_l.search(reg_level_line)
