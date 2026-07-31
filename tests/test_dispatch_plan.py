"""Dispatch plan manifest (#351): the head expands sweeps once and writes
each runnable TestConfig here so the build/sim jobs never re-run the hook.

These tests exercise the manifest IO and the fidelity of the round trip
against real TestConfigs loaded from the ``minimal_project`` fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.config import SuiteConfig
from rtl_buddy.dispatch.plan import (
    PLAN_SCHEMA_VERSION,
    read_plan_config,
    read_plan_configs,
    write_plan,
)
from rtl_buddy.errors import FatalRtlBuddyError


def _suite_configs(minimal_project: Path):
    return SuiteConfig(path="tests.yaml").get_tests()


def test_write_then_read_roundtrips_configs(minimal_project: Path):
    configs = _suite_configs(minimal_project)
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", configs, "tok")

    reloaded = read_plan_configs(plan)
    assert [c.get_name() for c in reloaded] == [c.get_name() for c in configs]
    # Full-fidelity: the reglvl a sim job resolves must match the head's.
    for before, after in zip(configs, reloaded):
        assert after.get_reglvl("verilator") == before.get_reglvl("verilator")
        assert after.get_testbench().get_name() == before.get_testbench().get_name()


def test_read_plan_config_by_name_and_miss(minimal_project: Path):
    configs = _suite_configs(minimal_project)
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", configs, "tok")

    assert read_plan_config(plan, "extra").get_name() == "extra"
    # A name absent from the plan is None (caller falls back to expansion),
    # not an error.
    assert read_plan_config(plan, "ghost") is None


def test_plan_is_json_and_ordered(minimal_project: Path):
    configs = _suite_configs(minimal_project)
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", configs, "tok-123")

    payload = json.loads(plan.read_text())
    assert payload["schema_version"] == PLAN_SCHEMA_VERSION
    assert payload["suite_config"] == "tests.yaml"
    assert payload["run_token"] == "tok-123"
    assert [t["name"] for t in payload["tests"]] == ["basic", "extra"]


def test_read_plan_token_roundtrips_and_defaults(minimal_project: Path):
    configs = _suite_configs(minimal_project)
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", configs, "nonce-9")
    from rtl_buddy.dispatch.plan import read_plan_token

    assert read_plan_token(plan) == "nonce-9"
    # A legacy plan without a token reads back None (no crash), so a job
    # falls back to an unstamped envelope rather than failing.
    legacy = minimal_project / "legacy.json"
    legacy.write_text(json.dumps({"schema_version": PLAN_SCHEMA_VERSION, "tests": []}))
    assert read_plan_token(legacy) is None


def test_read_rejects_schema_mismatch(minimal_project: Path):
    plan = minimal_project / "plan.json"
    plan.write_text(json.dumps({"schema_version": 999, "tests": []}))
    with pytest.raises(FatalRtlBuddyError, match="schema_version"):
        read_plan_configs(plan)


def test_read_rejects_unreadable(minimal_project: Path):
    with pytest.raises(FatalRtlBuddyError, match="unreadable"):
        read_plan_configs(minimal_project / "does-not-exist.json")
