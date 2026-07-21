"""Tests for #340 — block filtering for spec traceability.

``rb spec check-design`` and ``rb spec check-coverage`` gained a
repeatable ``--block NAME`` option. Only matching blocks are included
(human + machine); an unknown block name raises
``FatalRtlBuddyError("Unknown spec block(s): ...")`` (a nonzero exit),
not an empty success.

Fixture (``tests/fixtures/spec_block_filter/``):
  spec/demo/specs.yaml   -- block "demo" with coverage item DEMO-1
  spec/other/specs.yaml  -- block "other" with coverage item OTHER-1
  verif/dummy_suite/tests.yaml -- loadable, empty (no tests/testbenches)
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURE = Path(__file__).parent / "fixtures" / "spec_block_filter"
_SPEC = str(_FIXTURE / "spec")
_VERIF = str(_FIXTURE / "verif")


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope."""
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_check_coverage_block_filter_includes_only_matching_block():
    runner = CliRunner()
    rb = RtlBuddy(name="test_block_filter_coverage_demo")
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            _SPEC,
            "--verif-dir",
            _VERIF,
            "--block",
            "demo",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    items = payload["payload"]["items"]
    assert {item["block"] for item in items} == {"demo"}
    assert any(item["id"] == "DEMO-1" for item in items)


def test_check_design_block_filter_includes_only_matching_block():
    runner = CliRunner()
    rb = RtlBuddy(name="test_block_filter_design_demo")
    result = runner.invoke(
        rb.app,
        ["--machine", "spec", "check-design", "--spec-dir", _SPEC, "--block", "demo"],
    )
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    blocks = payload["payload"]["blocks"]
    assert {b["block"] for b in blocks} == {"demo"}


def test_unknown_block_name_raises_fatal_error():
    runner = CliRunner()
    rb = RtlBuddy(name="test_block_filter_unknown")
    result = runner.invoke(
        rb.app,
        [
            "spec",
            "check-coverage",
            "--spec-dir",
            _SPEC,
            "--verif-dir",
            _VERIF,
            "--block",
            "nonesuch",
        ],
    )
    assert result.exit_code != 0
    assert result.exception is not None
    assert "Unknown spec block(s)" in str(result.exception)


def test_repeatable_block_flag_includes_both_named_blocks():
    runner = CliRunner()
    rb = RtlBuddy(name="test_block_filter_both")
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            _SPEC,
            "--verif-dir",
            _VERIF,
            "--block",
            "demo",
            "--block",
            "other",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    items = payload["payload"]["items"]
    assert {item["block"] for item in items} == {"demo", "other"}
