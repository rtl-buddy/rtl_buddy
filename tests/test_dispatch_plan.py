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
    read_plan_master_seed,
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


def test_master_seed_is_recorded_once_at_plan_top_level(minimal_project: Path):
    configs = _suite_configs(minimal_project)
    plan = write_plan(
        minimal_project / "plan.json",
        "tests.yaml",
        configs,
        "nonce-9",
        master_seed=20260914,
    )

    payload = json.loads(plan.read_text())
    assert payload["master_seed"] == 20260914
    assert read_plan_master_seed(plan) == 20260914
    assert all("master_seed" not in test for test in payload["tests"])


def test_read_rejects_schema_mismatch(minimal_project: Path):
    plan = minimal_project / "plan.json"
    plan.write_text(json.dumps({"schema_version": 999, "tests": []}))
    with pytest.raises(FatalRtlBuddyError, match="schema_version"):
        read_plan_configs(plan)


def test_read_rejects_unreadable(minimal_project: Path):
    with pytest.raises(FatalRtlBuddyError, match="unreadable"):
        read_plan_configs(minimal_project / "does-not-exist.json")


@pytest.mark.parametrize("source,seed", [("master", 410729), ("default", 0)])
def test_plan_restores_seed_lock_before_build_preprocessing(
    minimal_project: Path, source, seed
):
    from rtl_buddy.seeding import SeedResolution

    cfg = next(iter(_suite_configs(minimal_project)))
    cfg.sim_rand_seed_plusarg = "stimulus_seed"
    cfg.set_resolved_seed(SeedResolution(seed, source, "tests.yaml::basic::single"))
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", [cfg], "tok")

    for restored in [read_plan_configs(plan)[0], read_plan_config(plan, "basic")]:
        restored.resolved_seed = 999
        restored.set_plusarg("stimulus_seed", 999)
        restored.ensure_resolved_seed_plusarg()
        assert restored.get_resolved_seed() == seed
        assert restored.get_plusarg("stimulus_seed") == seed
        assert restored.to_plan_dict()["resolved_seed"] == seed


@pytest.mark.parametrize(
    "source,seed", [("master", 0), ("fixed", -1), ("default", True)]
)
def test_plan_rejects_invalid_seed_state(minimal_project: Path, source, seed):
    cfg = next(iter(_suite_configs(minimal_project)))
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", [cfg], "tok")
    payload = json.loads(plan.read_text())
    payload["tests"][0].update(resolved_seed=seed, seed_source=source)
    plan.write_text(json.dumps(payload))
    with pytest.raises(FatalRtlBuddyError, match="dispatch plan seed.*invalid"):
        read_plan_configs(plan)


def test_mode_reservation_blocks_survive_the_round_trip(minimal_project: Path):
    """`resources.modes` is JSON, so a sim job resolves what the head did.

    The per-mode overrides (#634) live on the test and on its testbench,
    and both travel in the plan manifest — a plain mapping rather than a
    typed sub-block precisely so this stays JSON.
    """
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        .replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: {modes: {cov: {mem: 64G}}}\n",
        )
        .replace(
            "  - name: basic\n",
            '  - name: basic\n    resources: {modes: {cov: {time: "08:00:00"}}}\n',
        )
    )
    configs = SuiteConfig(path=str(tests_yaml)).get_tests()
    plan = write_plan(minimal_project / "plan.json", "tests.yaml", configs, "tok")

    payload = json.loads(plan.read_text())
    basic = next(t for t in payload["tests"] if t["name"] == "basic")
    assert basic["resources"]["modes"] == {"cov": {"time": "08:00:00"}}
    assert basic["tb"]["resources"]["modes"] == {"cov": {"mem": "64G"}}

    from rtl_buddy.config.dispatch import resolve_resources

    restored = read_plan_config(plan, "basic")
    resolved = resolve_resources(None, restored, builder_mode="cov")
    assert (resolved.mem, resolved.time) == ("64G", "08:00:00")
