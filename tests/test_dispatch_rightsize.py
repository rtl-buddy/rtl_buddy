"""Reservation right-sizing analysis tests (#351 P3).

Pure-function coverage of ``dispatch.rightsize``: per-test aggregation
across seeds, over/under classification, TIMEOUT/OOM pairing, formatting,
guardrails, and the Verilator-only gate on time advice.
"""

from __future__ import annotations

from rtl_buddy.config.dispatch import RightsizeConfigFile
from rtl_buddy.dispatch.rightsize import (
    analyze_suite_reservations,
    format_mem,
    format_time,
)
from rtl_buddy.runner.test_results import TestPassResults, SimTimeoutResults

_CFG = RightsizeConfigFile()  # 0.5 / 0.9 / 1.5 defaults


def _row(
    test,
    telemetry,
    *,
    run_id=None,
    builder="verilator",
    passing=True,
    compile_in_job=False,
    governed_by=None,
):
    results = (
        TestPassResults(name=test + "/results")
        if passing
        else SimTimeoutResults(name=test + "/results")
    )
    if telemetry is not None:
        results.results["telemetry"] = telemetry
    return {
        "test_name": test,
        "randmode_i": run_id,
        "results": results,
        "builder": builder,
        "compile_in_job": compile_in_job,
        "governed_by": governed_by or {},
    }


def _analyze(rows, cfg=_CFG, families=None, reg_level=0, root_config_path=None):
    return analyze_suite_reservations(
        rows,
        suite_display="verif/blk/tests.yaml",
        suite_config_path="verif/blk/tests.yaml",
        rightsize_cfg=cfg,
        reg_level=reg_level,
        simulator_family_of=(families or {"verilator": "verilator"}).get,
        root_config_path=root_config_path,
    )


def test_over_reserved_mem_suggests_reduction():
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1700,
                "timelimit_s": 3600,
                "req_mem_bytes": 24 * 2**30,
                "max_rss_bytes": 3 * 2**30,
            },
        )
    ]
    findings = _analyze(rows)
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.direction == "reduce"
    assert mem.reserved == "24G"
    assert mem.suggested == "5G"  # ceil(3G * 1.5 / 1G)
    assert 0.12 < mem.utilization < 0.13
    assert mem.edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=t].resources.mem",
    }
    # 1700/3600 = 47% < 50% over-threshold → time is also over-reserved.
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.direction == "reduce"


def test_reduce_suppressed_when_savings_too_small():
    # With over-threshold 0.6, a 56%-utilized limit is "over-reserved",
    # but peak*margin lands above 75% of the reservation — the reduce
    # would be churn, so no finding.
    cfg = RightsizeConfigFile(over_threshold=0.6)
    rows = [
        _row(
            "t",
            {"state": "COMPLETED", "elapsed_s": 2000, "timelimit_s": 3600},
        )
    ]
    assert not [f for f in _analyze(rows, cfg=cfg) if f.resource == "time"]


def test_near_limit_time_suggests_raise():
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 3500,
                "timelimit_s": 3600,
                "req_mem_bytes": 2**30,
                "max_rss_bytes": int(0.6 * 2**30),
            },
        )
    ]
    findings = _analyze(rows)
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.direction == "raise"
    assert time_f.suggested == format_time(3500 * 1.5)
    # mem util 60% is between thresholds → no mem advice.
    assert not [f for f in findings if f.resource == "mem"]


def test_timeout_kill_forces_time_raise_even_without_elapsed():
    rows = [
        _row(
            "t",
            {"state": "TIMEOUT", "timelimit_s": 3600},
            passing=False,
        )
    ]
    (time_f,) = _analyze(rows)
    assert time_f.resource == "time" and time_f.direction == "raise"
    assert time_f.peak.startswith(">")
    assert time_f.suggested == format_time(3600 * 1.5)
    assert time_f.states == ["TIMEOUT"]


def test_oom_kill_forces_mem_raise():
    rows = [
        _row(
            "t",
            {
                "state": "OUT_OF_MEMORY",
                "elapsed_s": 10,
                "timelimit_s": 3600,
                "req_mem_bytes": 2 * 2**30,
            },
            passing=False,
        )
    ]
    findings = _analyze(rows)
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.direction == "raise"
    assert mem.suggested == "3072M"  # 2G * 1.5


def test_aggregation_uses_peak_across_seeds():
    telemetry = {
        "state": "COMPLETED",
        "timelimit_s": 3600,
        "req_mem_bytes": 8 * 2**30,
    }
    rows = [
        _row(
            "t", {**telemetry, "elapsed_s": 10, "max_rss_bytes": 100 * 2**20}, run_id=1
        ),
        _row(
            "t", {**telemetry, "elapsed_s": 20, "max_rss_bytes": 900 * 2**20}, run_id=2
        ),
        _row(
            "t", {**telemetry, "elapsed_s": 15, "max_rss_bytes": 200 * 2**20}, run_id=3
        ),
    ]
    findings = _analyze(rows)
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.runs == 3
    assert mem.peak == "900M"
    assert mem.suggested == "1350M"  # 900M * 1.5


def test_cpu_efficiency_suggests_fewer_cpus():
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "total_cpu_s": 1000.0,  # 12.5% efficiency on 8 cpus
            },
        )
    ]
    findings = _analyze(rows)
    (cpu,) = [f for f in findings if f.resource == "cpus"]
    assert cpu.direction == "reduce"
    assert cpu.suggested == "2"  # ceil(8 * 0.125 * 1.5)


def test_single_cpu_never_gets_cpu_advice():
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 1,
                "total_cpu_s": 10.0,
            },
        )
    ]
    assert not [f for f in _analyze(rows) if f.resource == "cpus"]


def test_time_advice_gated_off_non_verilator_families():
    # A VCS -licqueue wait would masquerade as compute time (#329):
    # time advice must be suppressed, mem advice unaffected.
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 10,
                "timelimit_s": 3600,
                "req_mem_bytes": 24 * 2**30,
                "max_rss_bytes": 2**30,
            },
            builder="vcs-turbo",
        )
    ]
    findings = _analyze(rows, families={"vcs-turbo": "vcs"})
    assert not [f for f in findings if f.resource == "time"]
    assert [f for f in findings if f.resource == "mem"]


def test_rows_without_telemetry_are_ignored():
    rows = [_row("t", None), {"test_name": "u", "randmode_i": None, "results": None}]
    assert _analyze(rows) == []


def test_sub_second_run_gets_floored_time_reduction():
    rows = [
        _row(
            "t",
            {"state": "COMPLETED", "elapsed_s": 0, "timelimit_s": 3600},
        )
    ]
    (time_f,) = _analyze(rows)
    assert time_f.direction == "reduce"
    assert time_f.suggested == "00:05:00"  # floor, never below 5 minutes


def test_advice_carries_run_count_and_reg_level():
    rows = [
        _row(
            "t",
            {"state": "COMPLETED", "elapsed_s": 0, "timelimit_s": 3600},
        )
    ]
    (finding,) = _analyze(rows, reg_level=1000)
    event = finding.as_event()
    assert event["event"] == "reservation-advice"
    assert event["reg_level"] == 1000
    assert event["runs"] == 1


def test_formatters():
    assert format_mem(3 * 2**30) == "3072M"
    assert format_mem(24 * 2**30) == "24G"
    assert format_mem(int(4.2 * 2**30)) == "5G"
    assert format_time(59) == "00:01:00"
    assert format_time(3600) == "01:00:00"
    assert format_time(90 * 60 + 1) == "01:31:00"


# ------------------------------------------------ P3 review: correctness


def test_cpu_efficiency_uses_best_per_run_not_mixed_maxes():
    # One fully-efficient run + one idle run must NOT yield cpus-reduce
    # advice — the busy run vetoes it. (Deriving eff from max(cpu)/max(elapsed)
    # would wrongly suggest shrinking.)
    common = {"state": "COMPLETED", "timelimit_s": 3600, "alloc_cpus": 8}
    rows = [
        _row("t", {**common, "elapsed_s": 100, "total_cpu_s": 800.0}, run_id=1),
        _row("t", {**common, "elapsed_s": 1000, "total_cpu_s": 50.0}, run_id=2),
    ]
    assert not [f for f in _analyze(rows) if f.resource == "cpus"]


def test_timeout_kill_raises_time_even_off_verilator():
    # A TIMEOUT kill is a reservation fact, not a licqueue-contaminated
    # measurement — time-raise advice fires regardless of family.
    rows = [
        _row(
            "t", {"state": "TIMEOUT", "timelimit_s": 3600}, builder="vcs", passing=False
        )
    ]
    findings = _analyze(rows, families={"vcs": "vcs"})
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.direction == "raise"


def test_util_time_advice_denylist_only_vcs():
    # Icarus (not vcs) keeps util-based time advice; vcs loses it.
    tele = {"state": "COMPLETED", "elapsed_s": 10, "timelimit_s": 3600}
    ica = _analyze([_row("t", tele, builder="ica")], families={"ica": "icarus"})
    assert [f for f in ica if f.resource == "time"]
    vcs = _analyze([_row("t", tele, builder="v")], families={"v": "vcs"})
    assert not [f for f in vcs if f.resource == "time"]


def test_unknown_family_none_keeps_time_advice():
    # simulator_family_of returning None (unresolvable builder) must not
    # crash and must not suppress advice (None != "vcs").
    rows = [_row("t", {"state": "COMPLETED", "elapsed_s": 10, "timelimit_s": 3600})]
    findings = analyze_suite_reservations(
        rows,
        suite_display="verif/blk/tests.yaml",
        suite_config_path="/abs/verif/blk/tests.yaml",
        rightsize_cfg=_CFG,
        reg_level=0,
        simulator_family_of=lambda name: None,
    )
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.direction == "reduce"
    # Absolute config path flows into the edit hint for machine consumers.
    assert time_f.edit_hint["file"] == "/abs/verif/blk/tests.yaml"


# ------------------------------- #358: compile-vs-sim attribution


def _oom_row(test="t", **kwargs):
    # elapsed/limit lands between over-threshold and near-limit, so memory is
    # the only resource with advice and the assertions stay unambiguous.
    return _row(
        test,
        {
            "state": "OUT_OF_MEMORY",
            "elapsed_s": 2000,
            "timelimit_s": 3600,
            "req_mem_bytes": 16 * 2**30,
            "alloc_cpus": 1,
        },
        **kwargs,
    )


def test_sim_only_advice_is_labeled_sim_and_hints_at_the_test():
    findings = _analyze([_oom_row()], root_config_path="/p/root_config.yaml")
    assert [f.phase for f in findings] == ["sim"]
    assert findings[0].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=t].resources.mem",
    }


def test_compile_governed_field_hints_at_the_dispatch_compile_block():
    """Editing the test's mem would not move an allocation the compile sized."""
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "compile"})],
        root_config_path="/p/root_config.yaml",
    )
    assert [f.phase for f in findings] == ["compile+sim"]
    assert findings[0].edit_hint == {
        "file": "/p/root_config.yaml",
        "path": "cfg-dispatch.compile.mem",
    }


def test_in_job_compile_still_hints_at_the_test_for_sim_governed_fields():
    """Only the fields the compile actually won are redirected."""
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "test", "time": "compile"})],
        root_config_path="/p/root_config.yaml",
    )
    assert findings[0].resource == "mem"
    assert findings[0].phase == "compile+sim"
    assert findings[0].edit_hint["path"] == "tests[name=t].resources.mem"


def test_compile_governed_hint_falls_back_without_a_root_config_path():
    """No root_config.yaml to name means the per-test hint, not a broken one."""
    findings = _analyze([_oom_row(compile_in_job=True, governed_by={"mem": "compile"})])
    assert findings[0].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=t].resources.mem",
    }


def test_phase_travels_into_the_machine_event():
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "compile"})],
        root_config_path="/p/root_config.yaml",
    )
    event = findings[0].as_event()
    assert event["phase"] == "compile+sim"
    assert event["edit_hint"]["path"] == "cfg-dispatch.compile.mem"


def test_rows_without_the_new_keys_still_analyze():
    """Pre-#358 rows (no compile_in_job/governed_by) must not KeyError."""
    row = _oom_row()
    del row["compile_in_job"]
    del row["governed_by"]
    findings = _analyze([row], root_config_path="/p/root_config.yaml")
    assert [f.phase for f in findings] == ["sim"]


def test_governance_is_per_test_not_leaked_across_tests():
    """One test's compile-governed hint must not retarget another's."""
    rows = [
        _oom_row(compile_in_job=True, governed_by={"mem": "compile"}),
        _oom_row("u"),
    ]
    findings = {
        f.test: f for f in _analyze(rows, root_config_path="/p/root_config.yaml")
    }
    assert findings["t"].edit_hint["path"] == "cfg-dispatch.compile.mem"
    assert findings["u"].edit_hint["path"] == "tests[name=u].resources.mem"
