"""Typer CliRunner smoke tests for the rtl_buddy CLI.

These tests target subcommands that do not need a project root
(``docs``, ``skill``, ``--version``, ``--help``). The goal is to give
CLI-wiring coverage and catch regressions in option parsing and exit
codes without spinning up RootConfig.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_cli")


def test_version_flag_prints_version():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("rtl_buddy v")


def test_help_lists_subcommands():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("test", "regression", "synth", "cdc", "docs", "skill", "spec"):
        assert cmd in result.output, f"{cmd} missing from --help output"


def test_no_args_shows_help():
    """Typer is configured with no_args_is_help=True; bare invocation should
    print help and exit non-zero (Typer's standard behavior for help)."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, [])
    # exit_code may be 0 or 2 depending on Typer version; just confirm help shown.
    assert "Usage:" in result.output or "usage:" in result.output.lower()


def test_docs_list_human():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["docs", "list"])
    assert result.exit_code == 0, result.output
    # Each line is "<slug> - <title>: <description>"; at least one slug present.
    assert " - " in result.output
    assert ":" in result.output


def test_docs_list_machine_outputs_json():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--machine", "docs", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "docs list"
    assert payload["exit_code"] == 0
    assert isinstance(payload["payload"]["pages"], list)
    assert payload["payload"]["pages"], "expected at least one bundled docs page"
    # Each entry should have slug/title/description keys.
    first = payload["payload"]["pages"][0]
    for key in ("slug", "title", "description"):
        assert key in first, f"{key} missing from page list item"


def test_docs_show_known_slug_prints_content():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["docs", "show", "agents"])
    assert result.exit_code == 0, result.output
    assert result.output.strip(), "expected non-empty content"


def test_docs_show_unknown_slug_exits_nonzero():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["docs", "show", "does/not/exist"])
    assert result.exit_code != 0
    # Typer 0.26+ leaves the ClickException on result.exception rather than
    # mixing its text into result.output (stdout); accept either location.
    assert "Unknown docs page" in result.output + str(result.exception)


def test_docs_show_unknown_anchor_exits_nonzero():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["docs", "show", "agents#zzz-not-a-real-anchor"])
    assert result.exit_code != 0
    assert "Unknown section" in result.output + str(result.exception)


def test_docs_show_machine_emits_json():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--machine", "docs", "show", "agents"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in ("slug", "title", "content"):
        assert key in payload, f"{key} missing from page show payload"


def test_skill_help_lists_subcommands():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["skill", "--help"])
    assert result.exit_code == 0, result.output
    assert "print-gitignore" in result.output


def test_skill_print_gitignore_outputs_snippet():
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["skill", "print-gitignore"])
    assert result.exit_code == 0, result.output
    assert ".claude/skills/rtl_buddy/" in result.output
    assert ".agents/skills/rtl_buddy/" in result.output
