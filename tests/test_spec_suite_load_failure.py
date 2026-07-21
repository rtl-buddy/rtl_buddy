"""Tests for #337 — a suite that fails to load must not silently read as
"clean, uncovered, exit 0".

``discover_suite_tests(root)`` now returns ``(results, failures)``. In
``rb spec check-coverage``, any suite load failure forces a nonzero exit
and surfaces ``suite_load_failures`` in the machine payload — an item is
only truly "uncovered" if no loadable suite covers it.

Fixture (``tests/fixtures/spec_trace_broken/``):
  spec/demo/specs.yaml       -- one block "demo" with coverage item DEMO-1
  verif_mixed/good_suite/    -- loads fine, does not cover DEMO-1
  verif_mixed/broken_suite/  -- covers DEMO-1 but references a model_path
                                that does not exist, so SuiteConfig raises
  verif_clean/good_suite/    -- loads fine and covers DEMO-1 (contrast case)
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.spec_trace import discover_suite_tests

_FIXTURE = Path(__file__).parent / "fixtures" / "spec_trace_broken"


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope.

    ``CliRunner`` interleaves stdout and stderr into ``result.output``, and
    the load-failure warning logs precede the JSON envelope on the wire.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_discover_suite_tests_reports_load_failures():
    results, failures = discover_suite_tests(str(_FIXTURE / "verif_mixed"))

    assert len(results) == 1
    _, test = results[0]
    assert test.name == "good_test"

    assert len(failures) == 1
    assert failures[0].endswith("broken_suite/tests.yaml")


def test_check_coverage_machine_reports_load_failures_and_nonzero_exit():
    runner = CliRunner()
    rb = RtlBuddy(name="test_spec_load_failure_machine")
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            str(_FIXTURE / "spec"),
            "--verif-dir",
            str(_FIXTURE / "verif_mixed"),
        ],
    )
    assert result.exit_code != 0

    payload = _last_json(result.output)
    assert payload["exit_code"] != 0
    assert payload["payload"]["suite_load_failures"], (
        "a broken suite must be surfaced, not silently dropped"
    )
    # The declared item must not be reported as a clean "uncovered" result
    # riding on an exit_code of 0.
    items = payload["payload"]["items"]
    assert any(item["id"] == "DEMO-1" for item in items)


def test_check_coverage_all_suites_load_fine_exits_zero_no_failures():
    runner = CliRunner()
    rb = RtlBuddy(name="test_spec_load_ok_machine")
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            str(_FIXTURE / "spec"),
            "--verif-dir",
            str(_FIXTURE / "verif_clean"),
        ],
    )
    assert result.exit_code == 0, result.output

    payload = _last_json(result.output)
    assert payload["exit_code"] == 0
    assert payload["payload"]["suite_load_failures"] == []
    items = payload["payload"]["items"]
    demo_item = next(item for item in items if item["id"] == "DEMO-1")
    assert demo_item["covered"] is True
