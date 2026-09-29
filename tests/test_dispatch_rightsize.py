"""Tests for ``dispatch.rightsize``.

Covers per-test aggregation across seeds, over/under classification,
TIMEOUT/OOM pairing, formatting, guardrails and the Verilator-only gate on time
advice.
"""

from __future__ import annotations

import logging

import pytest

from rtl_buddy.config.dispatch import (
    DispatchCompileFile,
    DispatchConfigFile,
    RightsizeConfigFile,
    TestbenchCompileFile as TbCompile,
    aggregate_compile_resources,
)
from rtl_buddy.config.dispatch import greedy_schedule, time_to_seconds
from rtl_buddy.dispatch.rightsize import (
    BUILD_JOB_ROW,
    VERILATE_JOB_ROW,
    RightsizeFinding,
    _override_note,
    analyze_build_reservation,
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
    compile_floor=None,
    requested_cpus=None,
    cpus_override=None,
    submitted_cpus_per_task=None,
    compile_origins=None,
    compile_testbench=None,
    resource_modes=None,
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
        "compile_floor": compile_floor or {},
        # What the head resolved and submitted as `--cpus-per-task`.
        "requested_cpus": requested_cpus,
        # ...and the `sbatch-args` entry that superseded it, if any.
        "cpus_override": cpus_override,
        # The generated `--cpus-per-task`, still in force under a task count.
        "submitted_cpus_per_task": submitted_cpus_per_task,
        # Which tests.yaml layer won each compile field for this test, and the testbench
        # whose `compile:` block did.
        "compile_origins": compile_origins,
        "compile_testbench": compile_testbench,
        # Which sim fields this run's builder mode governed.
        "resource_modes": resource_modes or {},
    }


def _analyze(
    rows,
    cfg=_CFG,
    families=None,
    reg_level=0,
    root_config_path=None,
    accounting_interval_s=None,
    compile_origins=None,
    sbatch_args_config_path=None,
):
    return analyze_suite_reservations(
        rows,
        suite_display="verif/blk/tests.yaml",
        suite_config_path="verif/blk/tests.yaml",
        rightsize_cfg=cfg,
        reg_level=reg_level,
        simulator_family_of=(families or {"verilator": "verilator"}).get,
        root_config_path=root_config_path,
        accounting_interval_s=accounting_interval_s,
        compile_origins=compile_origins,
        sbatch_args_config_path=sbatch_args_config_path,
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
    # 1700/3600 = 47% < 50% over-threshold: time is also over-reserved.
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.direction == "reduce"


def test_reduce_suppressed_when_savings_too_small():
    # With over-threshold 0.6 a 56%-utilized limit is over-reserved, but peak*margin
    # lands above 75% of the reservation, so the reduce would be churn.
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
    # mem util 60% is between thresholds: no mem advice.
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
    # A VCS -licqueue wait would masquerade as compute time: time advice is suppressed,
    # mem advice is not.
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


def test_cpu_efficiency_uses_best_per_run_not_mixed_maxes():
    # One fully efficient run and one idle run must not yield cpus-reduce advice: the
    # busy run vetoes it.
    common = {"state": "COMPLETED", "timelimit_s": 3600, "alloc_cpus": 8}
    rows = [
        _row("t", {**common, "elapsed_s": 100, "total_cpu_s": 800.0}, run_id=1),
        _row("t", {**common, "elapsed_s": 1000, "total_cpu_s": 50.0}, run_id=2),
    ]
    assert not [f for f in _analyze(rows) if f.resource == "cpus"]


def test_timeout_kill_raises_time_even_off_verilator():
    # A TIMEOUT kill is a reservation fact, so time-raise advice fires regardless of
    # family.
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
    # An unresolvable builder (family None) neither crashes nor suppresses advice.
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
    # An absolute config path flows into the edit hint for machine consumers.
    assert time_f.edit_hint["file"] == "/abs/verif/blk/tests.yaml"


def _oom_row(test="t", **kwargs):
    # elapsed/limit lands between over-threshold and near-limit, so only memory has
    # advice.
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
    """Only the fields the compile won are redirected."""
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "test", "time": "compile"})],
        root_config_path="/p/root_config.yaml",
    )
    assert findings[0].resource == "mem"
    assert findings[0].phase == "compile+sim"
    assert findings[0].edit_hint["path"] == "tests[name=t].resources.mem"


def test_compile_governed_hint_falls_back_without_a_root_config_path():
    """Without a root_config.yaml to name, the hint is the per-test one."""
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
    """Rows without compile_in_job/governed_by do not raise KeyError."""
    row = _oom_row()
    del row["compile_in_job"]
    del row["governed_by"]
    findings = _analyze([row], root_config_path="/p/root_config.yaml")
    assert [f.phase for f in findings] == ["sim"]


def test_governance_is_per_test_not_leaked_across_tests():
    """One test's compile-governed hint does not retarget another's."""
    rows = [
        _oom_row(compile_in_job=True, governed_by={"mem": "compile"}),
        _oom_row("u"),
    ]
    findings = {
        f.test: f for f in _analyze(rows, root_config_path="/p/root_config.yaml")
    }
    assert findings["t"].edit_hint["path"] == "cfg-dispatch.compile.mem"
    assert findings["u"].edit_hint["path"] == "tests[name=u].resources.mem"


def test_compile_governed_field_the_suite_won_hints_at_the_suite_config():
    """cfg-dispatch is the layer a suite `compile:` block overrides.

    In the in-job-compile case a suite that sets `compile.mem: 48G` and OOMs must
    be sent to its own tests.yaml, since raising `cfg-dispatch.compile.mem` leaves
    the 48G in place.
    """
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "compile"})],
        root_config_path="/p/root_config.yaml",
        compile_origins={"mem": "suite"},
    )
    assert [f.phase for f in findings] == ["compile+sim"]
    assert findings[0].direction == "raise"
    assert findings[0].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "compile.mem",
    }


def test_suite_compile_attribution_is_per_field_within_one_run():
    """Only the fields the suite block set move to its tests.yaml."""
    findings = {
        f.resource: f
        for f in _analyze(
            [
                _over_reserved_row(
                    compile_in_job=True,
                    compile_floor={"mem": "4G", "time": "00:30:00"},
                )
            ],
            root_config_path="/p/root_config.yaml",
            compile_origins={"mem": "suite"},
        )
    }
    # The suite set mem, so its own file holds the binding floor.
    assert findings["mem"].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "compile.mem",
    }
    # time still comes from cfg-dispatch, in the same run.
    assert findings["time"].edit_hint == {
        "file": "/p/root_config.yaml",
        "path": "cfg-dispatch.compile.time",
    }


def test_compile_governed_field_a_testbench_won_hints_at_that_entry():
    """A testbench's own `compile:` beats the suite block it overrides.

    The suite-level `compile.mem` is the value that lost, so editing it moves
    nothing.
    """
    findings = _analyze(
        [
            _oom_row(
                compile_in_job=True,
                governed_by={"mem": "compile"},
                compile_origins={"mem": "testbench"},
                compile_testbench="tb_chip_t1",
            )
        ],
        root_config_path="/p/root_config.yaml",
        compile_origins={"mem": "suite"},
    )
    assert findings[0].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "testbenches[name=tb_chip_t1].compile.mem",
    }


def test_testbench_and_suite_compile_attribution_coexist_in_one_run():
    """One run, two tests, two layers, each named where its value lives."""
    findings = {
        f.test: f
        for f in _analyze(
            [
                _oom_row(
                    test="big",
                    compile_in_job=True,
                    governed_by={"mem": "compile"},
                    compile_origins={"mem": "testbench"},
                    compile_testbench="tb_chip_t1",
                ),
                _oom_row(
                    test="small",
                    compile_in_job=True,
                    governed_by={"mem": "compile"},
                    compile_origins={"mem": "suite"},
                    compile_testbench="tb_chip_small",
                ),
            ],
            root_config_path="/p/root_config.yaml",
        )
    }
    assert findings["big"].edit_hint["path"] == (
        "testbenches[name=tb_chip_t1].compile.mem"
    )
    # The small geometry declared no block, so the inherited suite key is the one to
    # edit.
    assert findings["small"].edit_hint["path"] == "compile.mem"


def test_a_row_without_its_own_origins_falls_back_to_the_suite_map():
    """Rows without per-testbench blocks keep the suite-level attribution."""
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "compile"})],
        root_config_path="/p/root_config.yaml",
        compile_origins={"mem": "suite"},
    )
    assert findings[0].edit_hint["path"] == "compile.mem"


def test_suite_won_attribution_never_touches_a_sim_governed_field():
    """An origin map is about the compile layer, not the test's resources."""
    findings = _analyze(
        [_oom_row(compile_in_job=True, governed_by={"mem": "test"})],
        root_config_path="/p/root_config.yaml",
        compile_origins={"mem": "suite"},
    )
    assert findings[0].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=t].resources.mem",
    }


def _over_reserved_row(**kwargs):
    """10s of 1h, 1G of 8G: both resources look over-reserved."""
    return _row(
        "t",
        {
            "state": "COMPLETED",
            "elapsed_s": 10,
            "timelimit_s": 3600,
            "req_mem_bytes": 8 * 2**30,
            "max_rss_bytes": 2**30,
        },
        **kwargs,
    )


def test_reduce_is_clamped_up_to_the_compile_floor_and_reattributed():
    findings = {
        f.resource: f
        for f in _analyze(
            [
                _over_reserved_row(
                    compile_in_job=True,
                    compile_floor={"mem": "4G", "time": "00:30:00"},
                )
            ],
            root_config_path="/p/root_config.yaml",
        )
    }
    # Unclamped these would be the 5-minute / 128M floors; the compile needs more.
    assert findings["time"].suggested == "00:30:00"
    assert findings["time"].edit_hint["path"] == "cfg-dispatch.compile.time"
    assert findings["mem"].suggested == "4G"
    assert findings["mem"].edit_hint["path"] == "cfg-dispatch.compile.mem"
    assert findings["mem"].direction == "reduce"


def test_reduce_is_dropped_when_the_floor_equals_the_reservation():
    """A suggestion the clamp returns to the current value is not emitted.

    It would never retire and could not be told apart from a wrong suggestion.
    """
    findings = _analyze(
        [
            _over_reserved_row(
                compile_in_job=True,
                # Exactly the reserved values from the telemetry above.
                compile_floor={"mem": "8G", "time": "01:00:00"},
            )
        ],
        root_config_path="/p/root_config.yaml",
    )
    assert findings == []


def test_floor_does_not_apply_without_an_in_job_compile():
    """A shared-build job's allocation never covered a compile."""
    findings = {f.resource: f for f in _analyze([_over_reserved_row()])}
    assert findings["time"].suggested == "00:05:00"  # 10s x 1.5 -> 5-min floor
    assert findings["mem"].suggested == "1536M"  # 1G x 1.5, above the 128M floor
    assert findings["time"].phase == "sim"


def test_floor_leaves_a_suggestion_above_it_untouched():
    findings = {
        f.resource: f
        for f in _analyze(
            [
                _over_reserved_row(
                    compile_in_job=True,
                    compile_floor={"mem": "64M", "time": "00:01:00"},
                )
            ],
            root_config_path="/p/root_config.yaml",
        )
    }
    assert findings["time"].suggested == "00:05:00"
    assert findings["mem"].suggested == "1536M"
    # The floor never bound, so the test's own fields still own these.
    assert findings["mem"].edit_hint["path"] == "tests[name=t].resources.mem"


def test_cpus_reduce_is_dropped_when_the_compile_needs_them():
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 2000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "total_cpu_s": 400.0,  # 2.5% efficiency over 8 cpus
            },
            compile_in_job=True,
            compile_floor={"cpus": 8},
        )
    ]
    # The compile wants all 8, so there is no reduction to make.
    assert [
        f for f in _analyze(rows, root_config_path="/p/x.yaml") if f.resource == "cpus"
    ] == []


def test_oom_raise_still_fires_on_a_compile_governed_field():
    """The floor only clamps reductions; a kill still raises."""
    findings = _analyze(
        [
            _row(
                "t",
                {
                    "state": "OUT_OF_MEMORY",
                    "elapsed_s": 2000,
                    "timelimit_s": 3600,
                    "req_mem_bytes": 8 * 2**30,
                },
                compile_in_job=True,
                governed_by={"mem": "compile"},
                compile_floor={"mem": "8G"},
            )
        ],
        root_config_path="/p/root_config.yaml",
    )
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.direction == "raise"
    assert mem.suggested == "12G"
    assert mem.edit_hint["path"] == "cfg-dispatch.compile.mem"


def _short_job(max_rss_bytes=5 * 2**20, elapsed_s=8, **extra):
    """A test that finished well inside a 30 s accounting interval."""
    return {
        "state": "COMPLETED",
        "elapsed_s": elapsed_s,
        "timelimit_s": 3600,
        "alloc_cpus": 1,
        "req_mem_bytes": 4 * 2**30,
        "max_rss_bytes": max_rss_bytes,
        **extra,
    }


def test_mem_advice_is_suppressed_for_a_job_shorter_than_the_sample_interval():
    """MaxRSS on a job never sampled is no peak, so no memory advice is given."""
    rows = [_row("fast", _short_job())]

    assert "mem" not in [f.resource for f in _analyze(rows, accounting_interval_s=30)]
    # Only memory: elapsed time is measured directly, so time advice stands.
    assert "time" in [f.resource for f in _analyze(rows, accounting_interval_s=30)]
    # The same numbers with adequate sampling produce advice.
    assert "mem" in [f.resource for f in _analyze(rows, accounting_interval_s=1)]


def test_mem_advice_survives_when_the_interval_is_unknown():
    """No interval means no evidence the peak is untrustworthy."""
    rows = [_row("fast", _short_job())]
    assert "mem" in [f.resource for f in _analyze(rows)]


def test_an_oom_kill_still_raises_however_coarse_the_sampling(caplog):
    """A kill is a fact about the reservation, not a measurement of it."""
    import logging

    rows = [_row("oom", _short_job(state="OUT_OF_MEMORY"), passing=False)]

    with caplog.at_level(logging.WARNING):
        findings = [
            f for f in _analyze(rows, accounting_interval_s=30) if f.resource == "mem"
        ]

    assert [f.direction for f in findings] == ["raise"]
    # Advice was given, so it is not also reported as an omission.
    assert "memory advice omitted" not in caplog.text


def test_nothing_is_reported_omitted_when_there_was_nothing_to_advise(caplog):
    """No reservation and no peak are unrelated reasons for silence.

    Those tests are not named in "memory advice omitted".
    """
    import logging

    rows = [
        _row(
            "no_reservation", {"state": "COMPLETED", "elapsed_s": 2, "timelimit_s": 60}
        ),
        _row(
            "no_peak",
            {
                "state": "COMPLETED",
                "elapsed_s": 2,
                "timelimit_s": 60,
                "req_mem_bytes": 4 * 2**30,
            },
        ),
    ]

    with caplog.at_level(logging.WARNING):
        _analyze(rows, accounting_interval_s=30)

    assert "memory advice omitted" not in caplog.text


def test_the_longest_run_decides_whether_a_test_was_sampled():
    """One sampled run makes the test's peak meaningful across its seeds."""
    rows = [
        _row("mixed", _short_job(elapsed_s=2), run_id=1),
        _row("mixed", _short_job(elapsed_s=90, max_rss_bytes=3900 * 2**20), run_id=2),
    ]

    findings = [
        f for f in _analyze(rows, accounting_interval_s=30) if f.resource == "mem"
    ]
    assert [f.direction for f in findings] == ["raise"]


def test_the_omission_is_logged_rather_than_left_silent(caplog):
    """Empty advice does not silently mean "the numbers were unusable"."""
    import logging

    rows = [_row("fast", _short_job()), _row("slow", _short_job(elapsed_s=600))]

    with caplog.at_level(logging.WARNING):
        _analyze(rows, accounting_interval_s=30)

    assert "memory advice omitted" in caplog.text
    assert "fast" in caplog.text
    assert "slow" not in caplog.text


class _Res:
    """Stand-in for the resolved per-build JobResources."""

    def __init__(self, cpus):
        self.cpus = cpus


# What the build envelope says the job did; the default is one real compile.
_COMPILED = {"records": 2, "compiled": 1, "compiled_sec": 55.0}


def _build_advice(
    telemetry,
    *,
    parallel=1,
    cpus=4,
    cfg=_CFG,
    root="root_config.yaml",
    compile_work=_COMPILED,
    accounting_interval_s=None,
    compile_origins=None,
    suite_config_hint=None,
    cpus_override=None,
    sbatch_args_config_path=None,
    phase="compile",
):
    return analyze_build_reservation(
        telemetry,
        _Res(cpus),
        parallel,
        cfg,
        "verif/blk/tests.yaml",
        root,
        compile_work=compile_work,
        accounting_interval_s=accounting_interval_s,
        compile_origins=compile_origins,
        suite_config_hint=suite_config_hint,
        cpus_override=cpus_override,
        sbatch_args_config_path=sbatch_args_config_path,
        phase=phase,
    )


def test_no_build_telemetry_means_no_build_advice():
    """local-parallel reports none, and a verdict from nothing is a guess."""
    assert _build_advice(None) == []
    assert _build_advice({}) == []


def test_an_over_reserved_build_job_gets_a_compile_phase_row():
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200}
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.phase == "compile"
    assert time_a.test == "(build job)"
    assert time_a.direction == "reduce"
    assert time_a.reserved == "02:00:00"
    # 60s x 1.5 is under the 5-minute floor.
    assert time_a.suggested == "00:05:00"
    assert time_a.edit_hint == {
        "path": "cfg-dispatch.compile.time",
        "file": "root_config.yaml",
    }


def test_a_timed_out_build_job_raises_its_limit():
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 7200, "timelimit_s": 7200}
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"
    assert time_a.suggested == "03:00:00"
    assert time_a.states == ["TIMEOUT"]


def test_a_serial_build_job_gets_per_build_cpus_advice():
    """One slot, so the whole-job ratio is the per-build one."""
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 200,  # 0.25 efficiency
        },
        parallel=1,
        cpus=8,
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    # ceil(8 x 0.25 x 1.5) = 3; with one build in flight that is the per-build figure.
    assert cpus_a.reserved == "8"
    assert cpus_a.suggested == "3"
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.compile.cpus"
    assert "the build job reserved 8." in cpus_a.edit_hint["note"]
    # The `parallel` lever has nothing to say to a job already at 1.
    assert "compile.parallel" not in cpus_a.edit_hint["note"]


def test_a_parallel_build_job_withholds_its_cpus_advice(caplog):
    """Whole-job utilization does not decompose into per-build cpus.

    With N slots the ratio also carries the tail: unequal builds, or fewer distinct
    compile keys than slots, leave reserved cpus idle. Dividing by `parallel` could
    advise shrinking cpus the longest compile needed, so the cpus row is withheld.
    Time advice is untouched.
    """
    import logging

    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "alloc_cpus": 16,
        "total_cpu_s": 200,  # 0.125 efficiency
    }
    with caplog.at_level(logging.INFO):
        findings = _build_advice(telemetry, parallel=4, cpus=4)

    assert [f for f in findings if f.resource == "cpus"] == []
    assert [f.resource for f in findings] == ["time"]
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "parallel-utilization-ambiguous"
    assert record.rtl_fields["parallel"] == 4
    assert record.rtl_fields["efficiency"] == 0.125
    assert "no cpus advice for the build job" in caplog.text
    assert "cfg-dispatch.compile.parallel" in caplog.text
    # Nothing said the suite owns the key, so the root one governs.
    assert record.rtl_fields["parallel_origin"] == "cfg-dispatch.compile.parallel"


def test_withheld_cpus_advice_names_the_suite_key_when_the_suite_owns_it(caplog):
    """The line ends in "size <key>", so it names the key that governs.

    A suite whose `compile:` block sets `parallel` is not moved by sizing
    `cfg-dispatch.compile.parallel`.
    """
    import logging

    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 7200,
                "alloc_cpus": 16,
                "total_cpu_s": 200,
            },
            parallel=4,
            cpus=4,
            compile_origins={"parallel": "suite"},
            suite_config_hint="/proj/verif/blk/blk_suite.yaml",
        )

    assert [f for f in findings if f.resource == "cpus"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["parallel_origin"] == "blk_suite.yaml compile.parallel"
    # The basename, so the line stays readable and still says which file to open.
    assert "size blk_suite.yaml compile.parallel" in caplog.text
    assert "cfg-dispatch.compile.parallel" not in caplog.text
    # The withheld `time` row still carries the attribution for the footer.
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.parallel_origin == "blk_suite.yaml compile.parallel"


def test_a_single_build_is_advised_even_at_a_wide_parallel():
    """One build cannot have a tail, so its ratio is still its own.

    The gate is on the builds that ran, not on the `parallel` flag.
    """
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 200,  # 0.25 efficiency
        },
        parallel=4,
        cpus=8,
        compile_work={"records": 1, "compiled": 1, "compiled_sec": 90.0},
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    # The head submitted 8 x 4 = 32: ratio 200 / (100 x 32), ceil(32 x 0.0625 x 1.5) =
    # 3, not divided again by idle slots.
    assert cpus_a.suggested == "3"
    # 3 > the 2 already configured, so the note explains the lever that is oversized.
    assert "compile.parallel 4" in cpus_a.edit_hint["note"]
    # Nothing said the suite owns the key, so the lever is cfg-dispatch's.
    assert "lower cfg-dispatch.compile.parallel" in cpus_a.edit_hint["note"]


def test_the_parallel_lever_names_the_suite_when_the_suite_owns_it():
    """A suite's own `compile.parallel` is what governs this job.

    Naming `cfg-dispatch.compile.parallel` when tests.yaml sets the key would give
    advice that moves nothing.
    """
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 200,
        },
        parallel=4,
        cpus=8,
        compile_work={"records": 1, "compiled": 1, "compiled_sec": 90.0},
        compile_origins={"parallel": "suite"},
        suite_config_hint="verif/blk/tests.yaml",
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    note = cpus_a.edit_hint["note"]
    # Named by its file, the spelling every line that mentions the key uses.
    assert "lower tests.yaml compile.parallel" in note
    assert "cfg-dispatch.compile.parallel" not in note
    # The cpus hint is unaffected: the suite set `parallel`, not `cpus`.
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.compile.cpus"
    # The row carries the same spelling up to the rendered table.
    assert cpus_a.parallel_origin == "tests.yaml compile.parallel"


def test_the_cpus_decomposition_comes_from_the_configured_per_build_value():
    """AllocCPUS is what the site gave, not what the YAML asked for.

    Under CR_CPU with threads-per-core, core-granularity rounding or a site
    `sbatch-args` override, sacct reports more cpus than the head requested. The
    decomposition is stated from the resolved `cfg-dispatch.compile.cpus`, which is
    also the ratio's denominator and the reported `reserved`. The allocated figure
    is named separately so `sacct` still reconciles.
    """
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,  # the site rounded 3 up to a whole node's core count
            "total_cpu_s": 100,  # 0.33 efficiency against the 3 requested
        },
        parallel=1,
        cpus=3,
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    note = cpus_a.edit_hint["note"]
    # One build slot: the request is the per-build figure, and the site's rounding is
    # named, not adopted.
    assert "the build job reserved 3" in note
    assert "the scheduler reported 8 allocated" in note
    # 8 would have been the sacct-derived per-build figure.
    assert "= 8 x" not in note
    # `reserved` is the number `cfg-dispatch.compile.cpus` holds; the allocation rides
    # along as an additive field.
    assert cpus_a.reserved == "3"
    assert cpus_a.allocated == "8"
    assert cpus_a.utilization == 100 / 300
    assert cpus_a.suggested == "2"  # ceil(3 x 0.333 x 1.5)


def test_a_saturated_build_job_gets_no_cpus_advice(caplog):
    """Efficiency only argues for fewer cpus, and only when it is low."""
    import logging

    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 200,
                "alloc_cpus": 8,
                "total_cpu_s": 760,  # 0.95 efficiency
            },
            parallel=1,
        )
    assert [f for f in findings if f.resource == "cpus"] == []
    # "No advice" and "advice withheld" are different answers.
    assert "build_advice_withheld" not in caplog.text


def test_a_cpus_reduction_that_cannot_retire_is_dropped():
    """Suggesting the reservation it already has is churn, not advice."""
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 2,
            "total_cpu_s": 60,  # 0.3 efficiency, but 1 cpu per build already
        },
        parallel=1,
        cpus=1,
    )
    assert [f for f in findings if f.resource == "cpus"] == []


def test_build_cpus_fall_back_to_what_the_head_asked_for(caplog):
    """A backend that reports usage but not the allocation still ratios."""
    import logging

    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "total_cpu_s": 100,
    }
    findings = _build_advice(telemetry, parallel=1, cpus=4)
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    assert cpus_a.reserved == "4"

    # The fallback is the scaled reservation: at parallel 2 the head asked for 4 x 2, so
    # the ratio is 100 / (100 x 8), not 100 / (100 x 4).
    with caplog.at_level(logging.INFO):
        scaled = _build_advice(telemetry, parallel=2, cpus=4)
    assert [f for f in scaled if f.resource == "cpus"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["efficiency"] == 0.125


def test_a_build_job_never_gets_memory_advice():
    """MaxRSS is sampled, and a too-small compile mem is an OOM kill."""
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 60,
            "timelimit_s": 7200,
            "req_mem_bytes": 8 * 2**30,
            "max_rss_bytes": 2**20,
        }
    )
    assert [f for f in findings if f.resource == "mem"] == []


def test_build_advice_without_a_root_config_path_still_names_the_field():
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200}, root=None
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {"path": "cfg-dispatch.compile.time"}


def test_build_advice_points_at_the_root_config_when_no_suite_block_won():
    """cfg-dispatch owns every field."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "path": "cfg-dispatch.compile.time",
        "file": "root_config.yaml",
    }


def test_build_advice_points_at_the_suite_for_a_field_it_overrode():
    """Editing cfg-dispatch.compile.time would move nothing here."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": "suite"},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "file": "/abs/verif/blk/tests.yaml",
        "path": "compile.time",
    }


def test_build_advice_attribution_is_per_field():
    """A suite that overrides only `mem` still gets root-level time advice.

    An origin for one field never leaks onto another's hint.
    """
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 200,
        },
        parallel=1,
        cpus=8,
        compile_origins={"mem": "suite", "cpus": "suite"},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    assert time_a.edit_hint["path"] == "cfg-dispatch.compile.time"
    assert time_a.edit_hint["file"] == "root_config.yaml"
    assert cpus_a.edit_hint["file"] == "/abs/verif/blk/tests.yaml"
    assert cpus_a.edit_hint["path"] == "compile.cpus"
    # The note that explains the decomposition survives the branch.
    assert "the build job reserved 8." in cpus_a.edit_hint["note"]


def test_build_advice_falls_back_to_the_root_config_with_no_suite_path():
    """With no suite path to name, the cfg-dispatch hint is kept.

    Advice runs after every job finished, so a missing path degrades, never aborts.
    """
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": "suite"},
        suite_config_hint=None,
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "path": "cfg-dispatch.compile.time",
        "file": "root_config.yaml",
    }


def test_build_advice_names_the_testbench_whose_block_won_the_field():
    """The build job's reservation is a max, so the winner is named."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": {"origin": "testbench", "testbench": "tb_chip_t1"}},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "file": "/abs/verif/blk/tests.yaml",
        "path": "testbenches[name=tb_chip_t1].compile.time",
    }


def test_build_advice_mixes_testbench_and_suite_attribution_in_one_run():
    """`time` from the big geometry, `cpus` from the suite block."""
    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 200,
        },
        parallel=1,
        cpus=8,
        compile_origins={
            "time": {"origin": "testbench", "testbench": "tb_chip_t1"},
            "cpus": {"origin": "suite", "testbench": None},
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    assert time_a.edit_hint["path"] == "testbenches[name=tb_chip_t1].compile.time"
    assert cpus_a.edit_hint["path"] == "compile.cpus"
    assert cpus_a.edit_hint["file"] == "/abs/verif/blk/tests.yaml"


def test_build_advice_keeps_cfg_dispatch_for_a_field_no_testbench_won():
    """The nested map names cfg-dispatch explicitly and still routes."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": {"origin": "cfg-dispatch", "testbench": None}},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "path": "cfg-dispatch.compile.time",
        "file": "root_config.yaml",
    }


def _tb(name):
    return {"origin": "testbench", "testbench": name}


_SUITE_SOURCE = {"origin": "suite", "testbench": None}


def test_build_time_reduce_is_withheld_when_two_sources_tie(caplog):
    """Two testbenches at the same time: no single edit lowers the max."""
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            compile_origins={
                "time": {
                    "origin": "testbench",
                    "testbench": "tb_a",
                    "sources": [_tb("tb_a"), _tb("tb_b")],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "time"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "compile-origin-tied"
    assert record.rtl_fields["resource"] == "time"
    # Both tied paths are named, so a reader sees what would have to move.
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_a].compile.time",
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_b].compile.time",
    ]


def test_a_testbench_tied_with_the_whole_job_value_also_withholds(caplog):
    """A block that merely reaches the suite's figure moves nothing alone."""
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            compile_origins={
                "time": {
                    "origin": "testbench",
                    "testbench": "tb_a",
                    "sources": [_tb("tb_a"), _SUITE_SOURCE],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "time"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_a].compile.time",
        "/abs/verif/blk/tests.yaml:compile.time",
    ]


def test_one_source_still_gets_its_reduce_advice():
    """The untied case is untouched and still names the testbench."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_a",
                "sources": [_tb("tb_a")],
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "reduce"
    assert time_a.edit_hint["path"] == "testbenches[name=tb_a].compile.time"


def test_a_tie_never_withholds_raise_advice():
    """Raising any one source raises a maximum, and a sum with it."""
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 7200, "timelimit_s": 7200},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_a",
                "sources": [_tb("tb_a"), _tb("tb_b")],
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"


def test_build_cpus_reduce_is_withheld_when_two_sources_tie(caplog):
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 7200,
                "alloc_cpus": 8,
                "total_cpu_s": 200,
            },
            parallel=1,
            cpus=8,
            compile_origins={
                "cpus": {
                    "origin": "testbench",
                    "testbench": "tb_a",
                    "sources": [_tb("tb_a"), _tb("tb_b")],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "cpus"] == []
    reasons = [
        r.rtl_fields["reason"]
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert "compile-origin-tied" in reasons


def test_a_tie_that_straddles_two_files_names_both(caplog):
    """A testbench tied with cfg-dispatch: two files, two keys, no lever."""
    with caplog.at_level(logging.INFO):
        _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            compile_origins={
                "time": {
                    "origin": "testbench",
                    "testbench": "tb_a",
                    "sources": [
                        _tb("tb_a"),
                        {"origin": "cfg-dispatch", "testbench": None},
                    ],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_a].compile.time",
        "root_config.yaml:cfg-dispatch.compile.time",
    ]


def test_build_time_reduce_is_withheld_when_the_value_is_a_sum(caplog):
    """A whole-job suggestion cannot be written into one contributor.

    Telemetry says 30 minutes was enough; the reservation is 30 + 60 over one
    worker. Writing 30 into the 60-minute testbench leaves the aggregate at 60.
    """
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            compile_origins={
                "time": {
                    "origin": "testbench",
                    "testbench": "tb_big",
                    # One source produces it, so this is not a tie...
                    "sources": [_tb("tb_big")],
                    # ...but it is a sum of two builds.
                    "aggregated": True,
                    "contributors": [_tb("tb_big"), _tb("tb_small")],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "time"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "compile-aggregate"
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_big].compile.time",
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_small].compile.time",
    ]


def test_a_single_contributor_is_still_attributable():
    """One build decides the wall clock, so the suggestion is appliable."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_big",
                "sources": [_tb("tb_big")],
                "aggregated": False,
                "contributors": [_tb("tb_big")],
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "reduce"
    assert time_a.edit_hint["path"] == "testbenches[name=tb_big].compile.time"


def test_an_aggregate_never_withholds_raise_advice():
    """A sum still moves when any contributor is raised."""
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 7200, "timelimit_s": 7200},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_big",
                "sources": [_tb("tb_big")],
                "aggregated": True,
                "contributors": [_tb("tb_big"), _tb("tb_small")],
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"


def test_two_contributors_on_one_key_still_withhold(caplog):
    """The count comes from the contributors, not from the rendered paths.

    Two planned builds on one testbench point at the same YAML key; counting
    deduplicated paths would read that as a single lever.
    """
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            compile_origins={
                "time": {
                    "origin": "testbench",
                    "testbench": "tb_a",
                    "sources": [_tb("tb_a")],
                    "aggregated": True,
                    "contributors": [_tb("tb_a"), _tb("tb_a")],
                }
            },
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "time"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "compile-aggregate"
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_a].compile.time"
    ]


def _aggregated_time(primary_seconds, contributors=("tb_big", "tb_small")):
    return {
        "time": {
            "origin": "testbench",
            "testbench": contributors[0],
            "sources": [_tb(contributors[0])],
            "aggregated": True,
            "contributors": [_tb(name) for name in contributors],
            "contributor_value": primary_seconds,
        }
    }


def test_an_aggregated_raise_names_the_contributors_own_new_value():
    """30 + 60 reserved, a 135 target: write 105, not 135.

    The hint points at the 60-minute testbench, where 135 would re-aggregate to
    165. The key has to become its own 60 plus the 45-minute shortfall.
    """
    findings = _build_advice(
        # Reserved 90 minutes, used all of it: the near-limit raise suggests 90 x 1.5 =
        # 135.
        {"state": "COMPLETED", "elapsed_s": 5400, "timelimit_s": 5400},
        compile_origins=_aggregated_time(3600),
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"
    assert time_a.suggested == "01:45:00"  # 60 + (135 - 90)
    # The whole-job figure survives in the machine event...
    assert time_a.suggested_total == "02:15:00"
    assert time_a.aggregate_delta == "+00:45:00"
    event = time_a.as_event()
    assert event["suggested"] == "01:45:00"
    assert event["suggested_total"] == "02:15:00"
    # ...and the hint says what the number means.
    assert "02:15:00 in total" in time_a.edit_hint["note"]
    assert time_a.edit_hint["path"] == "testbenches[name=tb_big].compile.time"


def test_an_aggregated_timeout_raise_is_translated_too():
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 5400, "timelimit_s": 5400},
        compile_origins=_aggregated_time(3600),
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"
    assert time_a.suggested == "01:45:00"
    assert time_a.suggested_total == "02:15:00"


def test_a_raise_shares_its_delta_across_repeated_contributors():
    """One key, two builds: raise it by half the shortfall each.

    Two 30-minute builds of one testbench run back to back and reserve 60. A
    90-minute target written as `+30` on that key would give 120, since each
    occurrence carries 15.
    """
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 3600, "timelimit_s": 3600},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_a",
                "sources": [_tb("tb_a")],
                "aggregated": True,
                # The same key twice: two planned builds, one YAML field.
                "contributors": [_tb("tb_a"), _tb("tb_a")],
                "contributor_value": 1800,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "00:45:00"  # 30 + 30/2, not 30 + 30
    assert time_a.suggested_total == "01:30:00"
    assert time_a.aggregate_delta == "+00:15:00"
    # The two occurrences re-aggregate to exactly the target.
    assert 2 * time_to_seconds(time_a.suggested) == time_to_seconds(
        time_a.suggested_total
    )


def _reaggregate(finding, schedule, parallel, governing):
    """The makespan the suggested edit actually produces."""
    value = time_to_seconds(finding.suggested)
    makespan, _, _ = greedy_schedule(
        [value if name == governing else seconds for name, seconds in schedule],
        parallel,
    )
    return makespan


def test_a_raise_reschedules_before_trusting_the_arithmetic():
    """Raising a repeated build can push a later copy behind a neighbour.

    Plan order A=60m, B=66m, A=60m over two workers has a 120-minute makespan.
    Splitting a 180-minute target's shortfall over A's two occurrences suggests
    90m, but at 90m the second A lands after B and the job reserves 156m. The
    proposal is scheduled, not predicted.
    """
    schedule = [("A", 3600), ("B", 3960), ("A", 3600)]
    tb_a = _tb("A")
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 7200, "timelimit_s": 7200},
        parallel=2,
        compile_origins={
            "time": {
                **tb_a,
                "sources": [tb_a],
                "aggregated": True,
                "contributors": [tb_a, tb_a],
                "contributor_value": 3600,
                "schedule": schedule,
                "parallel": 2,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"
    target = time_to_seconds(time_a.suggested_total)
    assert target == 10800
    # The naive arithmetic would have said 01:30:00 and delivered 156m.
    assert time_to_seconds(time_a.suggested) > 5400
    # Applying the number reaches the target.
    assert _reaggregate(time_a, schedule, 2, "A") >= target


def test_a_serial_queue_raise_survives_rescheduling():
    """parallel 1: the order cannot change, so the share stands."""
    schedule = [("A", 1800), ("A", 1800)]
    tb_a = _tb("A")
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 3600, "timelimit_s": 3600},
        compile_origins={
            "time": {
                **tb_a,
                "sources": [tb_a],
                "aggregated": True,
                "contributors": [tb_a, tb_a],
                "contributor_value": 1800,
                "schedule": schedule,
                "parallel": 1,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "00:45:00"  # unchanged by the reschedule
    assert _reaggregate(time_a, schedule, 1, "A") == time_to_seconds(
        time_a.suggested_total
    )


def test_a_single_occurrence_raise_survives_rescheduling():
    """One build in the queue: its own value is the makespan."""
    schedule = [("A", 3600)]
    tb_a = _tb("A")
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 3600, "timelimit_s": 3600},
        compile_origins={
            "time": {
                **tb_a,
                "sources": [tb_a],
                "aggregated": True,
                # Two contributors recorded but only one is this key: the share is the
                # whole delta.
                "contributors": [tb_a, _tb("B")],
                "contributor_value": 3600,
                "schedule": schedule,
                "parallel": 1,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "01:30:00"
    assert _reaggregate(time_a, schedule, 1, "A") >= time_to_seconds(
        time_a.suggested_total
    )


def test_an_unreachable_target_falls_back_to_the_whole_job_figure():
    """The bounded search gives up safely, never short.

    The provenance names a key the schedule does not contain, so no edit to it
    moves the makespan. The fallback is the whole-job figure, which over-reserves.
    """
    tb_a = _tb("A")
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 3600, "timelimit_s": 3600},
        compile_origins={
            "time": {
                **tb_a,
                "sources": [tb_a],
                "aggregated": True,
                "contributors": [tb_a, tb_a],
                "contributor_value": 1800,
                # No "A" in the queue: nothing this key does reaches it.
                "schedule": [("Z", 1800), ("Z", 1800)],
                "parallel": 1,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"
    assert time_a.suggested == "01:30:00"  # the whole-job target itself
    assert time_a.suggested_total is None
    assert time_a.aggregate_delta is None


def test_a_raise_on_distinct_contributors_keeps_the_whole_delta():
    """Two different keys: only the named one moves, so it takes it all."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 3600, "timelimit_s": 3600},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_a",
                "sources": [_tb("tb_a")],
                "aggregated": True,
                "contributors": [_tb("tb_a"), _tb("tb_b")],
                "contributor_value": 1800,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "01:00:00"  # 30 + the full 30
    assert time_a.aggregate_delta == "+00:30:00"


def test_an_unaggregated_raise_keeps_the_whole_job_figure():
    """One contributor: the suggestion is the value to write."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 5400, "timelimit_s": 5400},
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_big",
                "sources": [_tb("tb_big")],
                "aggregated": False,
                "contributors": [_tb("tb_big")],
                "contributor_value": 5400,
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "02:15:00"
    assert time_a.suggested_total is None
    assert time_a.aggregate_delta is None
    assert "in total" not in (time_a.edit_hint.get("note") or "")


def test_an_aggregate_without_a_contributor_value_is_not_translated():
    """An older state dict degrades to the whole-job figure, not to silence."""
    origins = _aggregated_time(3600)
    del origins["time"]["contributor_value"]
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 5400, "timelimit_s": 5400},
        compile_origins=origins,
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.suggested == "02:15:00"
    assert time_a.suggested_total is None


def test_a_real_tied_schedule_reaches_the_withhold(caplog):
    """End to end: the aggregation's own map trips the tie rule.

    The two halves live in different modules, so this feeds the real
    `aggregate_compile_resources` output into the advice.
    """
    cfg = DispatchConfigFile(compile=DispatchCompileFile(time="00:01:00")).initialise()
    _, origins = aggregate_compile_resources(
        cfg,
        None,
        [
            ("tb_a", TbCompile(time="01:00:00")),
            ("tb_b", TbCompile(time="01:00:00")),
        ],
        parallel=2,
    )
    with caplog.at_level(logging.INFO):
        findings = _build_advice(
            {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 3600},
            compile_origins=origins,
            suite_config_hint="/abs/verif/blk/tests.yaml",
        )
    assert [f for f in findings if f.resource == "time"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "compile-origin-tied"
    assert record.rtl_fields["paths"] == [
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_a].compile.time",
        "/abs/verif/blk/tests.yaml:testbenches[name=tb_b].compile.time",
    ]


def test_an_origins_map_without_sources_never_withholds():
    """A state dict from before the aggregation degrades, not goes silent."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": "suite"},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint["path"] == "compile.time"


_ALL_REUSED = {"records": 8, "compiled": 0, "compiled_sec": 0.0}
# A job that short-circuited every build: seconds of wall clock, no cpu.
_REUSE_RUN = {
    "state": "COMPLETED",
    "elapsed_s": 60,
    "timelimit_s": 7200,
    "alloc_cpus": 16,
    "total_cpu_s": 4,
}


def test_a_build_job_that_compiled_nothing_gets_no_reduce_advice():
    """Every build reused its stamp, so the job is seconds long against a 2 h limit.

    Reading that as "the compile is fast" would advise a 5-minute limit that the
    next real RTL change TIMEOUTs against.
    """
    assert _build_advice(_REUSE_RUN, compile_work=_ALL_REUSED) == []


def test_an_envelope_that_cannot_say_yields_no_reduce_advice():
    """A build job with no envelope or records is unknown, not "nothing ran"."""
    assert _build_advice(_REUSE_RUN, compile_work=None) == []
    assert _build_advice(_REUSE_RUN, compile_work={"records": 0, "compiled": 0}) == []


def test_a_timed_out_build_job_raises_even_with_nothing_recorded():
    """A kill is a fact about the reservation, not a measurement of work."""
    findings = _build_advice(
        {"state": "TIMEOUT", "elapsed_s": 7200, "timelimit_s": 7200},
        compile_work=_ALL_REUSED,
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.direction == "raise"


def test_a_build_job_shorter_than_one_accounting_interval_gets_no_reduce():
    """TotalCPU comes from usage samples.

    A job that finished inside one interval was measured at most once.
    """
    assert _build_advice(_REUSE_RUN, accounting_interval_s=300) == []
    # Once the interval says it was sampled, the same job advises.
    assert _build_advice(_REUSE_RUN, accounting_interval_s=30) != []


def test_withheld_build_advice_is_logged_rather_than_left_silent(caplog):
    """ "No advice" and "advice withheld" are different answers."""
    import logging

    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work=_ALL_REUSED)

    assert "no reduce advice for the build job" in caplog.text
    assert "reused their stamps" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, accounting_interval_s=300)

    assert "accounting interval" in caplog.text


def test_withheld_build_advice_carries_the_seconds_actually_spent_compiling(caplog):
    """The machine payload says build time next to the job's wall clock.

    It rides the same event as `builds`/`compiled` and is absent, not zero, when
    there was no envelope to measure.
    """
    import logging

    def _withheld():
        return [
            r
            for r in caplog.records
            if getattr(r, "rtl_event", None) == "rightsize.build_advice_withheld"
        ]

    with caplog.at_level(logging.INFO):
        _build_advice(
            _REUSE_RUN,
            compile_work={"records": 4, "compiled": 1, "compiled_sec": 55.0},
            accounting_interval_s=300,
        )
    (record,) = _withheld()
    assert record.rtl_fields["builds"] == 4
    assert record.rtl_fields["compiled_sec"] == 55.0

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work=None)
    (record,) = _withheld()
    assert "compiled_sec" not in record.rtl_fields


def test_an_unknown_build_job_is_not_reported_as_one_that_reused_stamps(caplog):
    """Could not tell is its own reason, and the line says so.

    A build job that was OOM-killed or TIMEOUTed leaves an sacct row but no
    envelope; the line must not print "none of its None build(s) compiled".
    """
    import logging

    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work=None)

    assert "no reduce advice for the build job" in caplog.text
    assert "no record of what it built" in caplog.text
    assert "reused their stamps" not in caplog.text
    assert "None build(s)" not in caplog.text

    # An envelope without records reads the same: records, not compiles, separate
    # "nothing to do" from "nothing recorded".
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work={"records": 0, "compiled": 0})

    assert "no record of what it built" in caplog.text
    assert "0 build(s)" not in caplog.text


def _rendered_metadata(findings, monkeypatch):
    """The metadata lines `_render_reservation_advice` puts under the table."""
    import rtl_buddy.rtl_buddy as rbmod

    captured = {}
    monkeypatch.setattr(rbmod, "render_summary", lambda **kw: captured.update(kw))
    rbmod.RtlBuddy._render_reservation_advice(object(), findings)
    return captured["metadata"]


def _finding(phase, resource="time"):
    return RightsizeFinding(
        suite="verif/blk/tests.yaml",
        test="(build job)" if phase == "compile" else "t_basic",
        resource=resource,
        reserved="02:00:00",
        peak="00:01:00",
        utilization=0.01,
        direction="reduce",
        suggested="00:05:00",
        runs=1,
        reg_level=None,
        phase=phase,
    )


def test_the_field_column_keeps_the_per_test_hint_path(tmp_path, monkeypatch, capsys):
    """`tests[name=alpha]` is data, and Rich does not treat it as markup.

    The per-test hint path in the Field column names the entry to edit; as markup
    it would collapse to `tests.resources.cpus`.
    """
    import logging

    import rtl_buddy.rtl_buddy as rbmod
    from rtl_buddy.logging_utils import setup_logging

    finding = _finding("sim", resource="cpus")
    finding.test = "alpha[0]"
    finding.edit_hint = {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=alpha].resources.cpus",
    }

    monkeypatch.setenv("COLUMNS", "400")  # keep each row on one line
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    try:
        rbmod.RtlBuddy._render_reservation_advice(object(), [finding])
    finally:
        # setup_logging() attached a file handler under tmp_path; remove it so it does
        # not outlive the test.
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    stderr = capsys.readouterr().err
    assert "tests[name=alpha].resources.cpus" in stderr
    assert "alpha[0]" in stderr


def test_the_rendered_rows_carry_the_hint_path_unescaped(monkeypatch):
    """The escape belongs to the renderer, not to the data.

    The same rows reach the `--machine` `summary` event and the plain-text log.
    """
    finding = _finding("sim", resource="cpus")
    finding.edit_hint = {"path": "tests[name=alpha].resources.cpus"}
    rows, _metadata = _rendered_rows([finding], monkeypatch)
    assert rows[0]["field"] == "tests[name=alpha].resources.cpus"


def test_the_compile_sim_note_only_appears_under_a_compile_sim_row(monkeypatch):
    """A note explaining a row the table does not contain is noise.

    The build job's row is `compile`, not `compile+sim`, so the "peak spans both
    phases" line would describe something absent.
    """
    build_only = _rendered_metadata([_finding("compile")], monkeypatch)
    assert not any("spans both phases" in line for line in build_only)
    assert any(
        "the compile row is the suite's build job" in line for line in build_only
    )

    in_job = _rendered_metadata([_finding("compile+sim")], monkeypatch)
    assert any("spans both phases" in line for line in in_job)
    assert not any("build job" in line for line in in_job)

    sim_only = _rendered_metadata([_finding("sim")], monkeypatch)
    assert len(sim_only) == 1


def test_the_build_job_note_names_the_key_that_governs_parallel(monkeypatch):
    """The footnote names the key that sizes the row.

    A suite whose `compile:` block sets `parallel` is not moved by
    `cfg-dispatch.compile.parallel`.
    """
    root_owned = _finding("compile")
    assert root_owned.parallel_origin is None  # an unattributed row
    lines = _rendered_metadata([root_owned], monkeypatch)
    assert any("up to cfg-dispatch.compile.parallel builds" in line for line in lines)

    suite_owned = _finding("compile")
    suite_owned.parallel_origin = "blk_suite.yaml compile.parallel"
    lines = _rendered_metadata([suite_owned], monkeypatch)
    assert any("up to blk_suite.yaml compile.parallel builds" in line for line in lines)
    assert not any("cfg-dispatch.compile.parallel" in line for line in lines)


def test_the_build_job_note_states_the_rule_when_suites_disagree(monkeypatch):
    """One regression can table several suites that do not agree.

    Each row's own file is already in the Field column, so the footnote states the
    layering instead of naming a file.
    """
    suite_owned = _finding("compile")
    suite_owned.parallel_origin = "blk_suite.yaml compile.parallel"
    root_owned = _finding("compile")
    root_owned.suite = "verif/other/tests.yaml"
    root_owned.parallel_origin = "cfg-dispatch.compile.parallel"
    lines = _rendered_metadata([suite_owned, root_owned], monkeypatch)
    assert any(
        "up to the resolved compile.parallel (suite block or cfg-dispatch) builds"
        in line
        for line in lines
    )
    assert not any("blk_suite.yaml" in line for line in lines)


def test_the_per_build_clause_only_appears_under_a_build_cpus_row(monkeypatch):
    """The cpus row is gated independently of the build-job row itself.

    A `reduce` on cpus needs low efficiency and evidence that a compile ran, so a
    build job often has a `time` row and no `cpus` row; the cpus note is then
    omitted.
    """
    time_only = _rendered_metadata([_finding("compile")], monkeypatch)
    assert any("the compile row is the suite's build job" in line for line in time_only)
    assert not any("per-build" in line for line in time_only)

    with_cpus = _rendered_metadata(
        [_finding("compile"), _finding("compile", resource="cpus")], monkeypatch
    )
    assert any("cpus suggestion is per-build" in line for line in with_cpus)


def test_whole_core_rounding_does_not_produce_cpus_advice():
    """A single-threaded test on a whole-core site is not over-reserved.

    `SelectTypeParameters=NONE` with `ThreadsPerCore=2` gives a job that asked for
    one cpu two, so sacct reports `AllocCPUS=2` against `ReqCPUS=1`. The ratio is
    taken against the request, the number the project controls.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 2,  # the scheduler rounded 1 up to a whole core
                "req_cpus": 1,
                "total_cpu_s": 500.0,  # 0.25 eff vs the allocation, 0.5 vs 1 cpu
            },
        )
    ]
    assert [f for f in _analyze(rows) if f.resource == "cpus"] == []


def test_cpu_efficiency_is_measured_against_the_requested_cpus():
    """Rounding is the scheduler's; the reservation is fine.

    4 allocated against 2 requested, 1.2 cpu-seconds per wall second: 0.3 against
    the allocation (under the 0.5 threshold) but 0.6 against the request.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 2,
                "total_cpu_s": 1200.0,
            },
        )
    ]
    assert [f for f in _analyze(rows) if f.resource == "cpus"] == []


def test_a_genuinely_over_reserved_test_still_gets_cpus_advice():
    """Nothing rounded: 4 asked for, 4 given, 25% used."""
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,
                "total_cpu_s": 1000.0,
            },
        )
    ]
    (cpu,) = [f for f in _analyze(rows) if f.resource == "cpus"]
    assert cpu.direction == "reduce"
    assert cpu.reserved == "4"
    assert cpu.suggested == "2"  # ceil(4 x 0.25 x 1.5)
    # Request and allocation agree, so there is nothing extra to reconcile.
    assert cpu.allocated is None
    assert cpu.as_event()["allocated"] is None


def test_a_cpus_finding_names_the_request_and_carries_the_allocation():
    """`reserved` is the number the named Field holds.

    8 allocated against 4 requested, a quarter of the request used: the row says
    `Reserved 4`, matching tests.yaml. The allocated figure is additive so `sacct`
    and `squeue` still reconcile.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 4,
                "total_cpu_s": 1000.0,  # 0.25 eff against the requested 4
            },
        )
    ]
    (cpu,) = [f for f in _analyze(rows) if f.resource == "cpus"]
    assert cpu.reserved == "4"
    assert cpu.allocated == "8"
    assert cpu.suggested == "2"
    assert cpu.utilization == 0.25
    assert cpu.as_event()["allocated"] == "8"


def test_telemetry_without_a_request_still_ratios_against_the_allocation():
    """Older telemetry, and any backend that reports only what it gave."""
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "total_cpu_s": 1000.0,
            },
        )
    ]
    (cpu,) = [f for f in _analyze(rows) if f.resource == "cpus"]
    assert cpu.reserved == "8"
    assert cpu.allocated is None
    assert cpu.suggested == "2"  # ceil(8 x 0.125 x 1.5)


def test_a_build_job_is_also_judged_against_its_request():
    """Same rounding, same fix, for the suite's build job."""
    retired = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 2,
            "req_cpus": 1,
            "total_cpu_s": 50,  # 0.25 eff vs the allocation, 0.5 vs the request
        },
        parallel=1,
        cpus=1,
    )
    assert [f for f in retired if f.resource == "cpus"] == []

    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,  # rounded up to a whole node's cores
            "req_cpus": 4,
            "total_cpu_s": 100,  # 0.25 eff against the requested 4
        },
        parallel=1,
        cpus=4,
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    assert cpus_a.reserved == "4"
    assert cpus_a.allocated == "8"
    assert cpus_a.suggested == "2"  # ceil(4 x 0.25 x 1.5)
    note = cpus_a.edit_hint["note"]
    assert "the build job reserved 4" in note
    assert "the scheduler reported 8 allocated" in note


def _rendered_rows(findings, monkeypatch):
    """The rows `_render_reservation_advice` hands to the table."""
    import rtl_buddy.rtl_buddy as rbmod

    captured = {}
    monkeypatch.setattr(rbmod, "render_summary", lambda **kw: captured.update(kw))
    rbmod.RtlBuddy._render_reservation_advice(object(), findings)
    return captured["rows"], captured["metadata"]


def test_the_table_shows_the_allocation_beside_the_request(monkeypatch):
    plain = _finding("sim", resource="cpus")
    rows, metadata = _rendered_rows([plain], monkeypatch)
    assert rows[0]["reserved"] == "02:00:00"
    assert not any("allocated" in line for line in metadata)

    rounded = _finding("sim", resource="cpus")
    rounded.reserved = "4"
    rounded.allocated = "8"
    rows, metadata = _rendered_rows([rounded], monkeypatch)
    assert rows[0]["reserved"] == "4 (8 allocated)"
    assert any("whole cores" in line for line in metadata)


def test_the_configured_request_beats_what_the_scheduler_reports():
    """`--cpus-per-task` is the request; ReqCPUS is only a report of it.

    The head knows what it submitted, so the advice is site-independent: a
    `cpus: 1` test on a whole-core node is never advised down to 1.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 2,
                "req_cpus": 2,  # the site rounded this one too
                "total_cpu_s": 500.0,
            },
            requested_cpus=1,
        )
    ]
    assert [f for f in _analyze(rows) if f.resource == "cpus"] == []


def test_a_test_row_without_req_cpus_still_uses_the_configured_request():
    """No `ReqCPUS` in telemetry: the head's own number carries it."""
    retired = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 2,  # whole-core rounding, no request reported
                "total_cpu_s": 500.0,
            },
            requested_cpus=1,
        )
    ]
    assert [f for f in _analyze(retired) if f.resource == "cpus"] == []

    over = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "total_cpu_s": 1000.0,  # 0.25 eff against the requested 4
            },
            requested_cpus=4,
        )
    ]
    (cpu,) = [f for f in _analyze(over) if f.resource == "cpus"]
    assert cpu.reserved == "4"
    assert cpu.allocated == "8"
    assert cpu.utilization == 0.25
    assert cpu.suggested == "2"


def test_a_build_row_without_req_cpus_still_uses_the_configured_request():
    """Same for the build job: `compile.cpus x parallel` is what it asked."""
    retired = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 2,  # whole-core rounding, no request reported
            "total_cpu_s": 50,
        },
        parallel=1,
        cpus=1,
    )
    assert [f for f in retired if f.resource == "cpus"] == []

    findings = _build_advice(
        {
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "req_cpus": 8,  # a site that normalizes ReqCPUS as well
            "total_cpu_s": 100,  # 0.25 eff against the 4 the head submitted
        },
        parallel=1,
        cpus=4,
    )
    (cpus_a,) = [f for f in findings if f.resource == "cpus"]
    assert cpus_a.reserved == "4"
    assert cpus_a.allocated == "8"
    assert cpus_a.utilization == 0.25
    assert cpus_a.suggested == "2"


def test_a_cpus_override_in_sbatch_args_withdraws_the_configured_request():
    """`cfg-dispatch.sbatch-args` is appended last, so it wins.

    A `--cpus-per-task` there means the resolved reservation was never submitted,
    so the ratio and the decomposition fall back to what the scheduler reports.
    """
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "alloc_cpus": 8,
        "req_cpus": 8,  # what the override actually asked for
        "total_cpu_s": 200,
    }
    # Without the override the configured 2 would be the denominator: 200 / (100 x 2) =
    # 1.0, nothing to advise.
    assert [
        f for f in _build_advice(telemetry, parallel=1, cpus=2) if f.resource == "cpus"
    ] == []

    (cpus_a,) = [
        f
        for f in _build_advice(
            telemetry, parallel=1, cpus=2, cpus_override=["--cpus-per-task=8"]
        )
        if f.resource == "cpus"
    ]
    # 200 / (100 x 8) = 0.25 against the 8 the override submitted.
    assert cpus_a.reserved == "8"
    assert cpus_a.allocated is None
    assert cpus_a.utilization == 0.25
    assert cpus_a.suggested == "3"  # ceil(8 x 0.25 x 1.5)
    # The hint names the argument, not the field it masks: editing `compile.cpus` would
    # not move the next reservation.
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpus_a.edit_hint["file"] == "root_config.yaml"
    note = cpus_a.edit_hint["note"]
    assert "sbatch-args `--cpus-per-task=8` sets this job's cpu request" in note
    assert "cfg-dispatch.compile.cpus" in note
    # The superseded decomposition is gone with it.
    assert "the build job reserved" not in note

    # time advice is unaffected: `--cpus-per-task` masks nothing there.
    (time_a,) = [
        f
        for f in _build_advice(
            telemetry, parallel=1, cpus=2, cpus_override=["--cpus-per-task=8"]
        )
        if f.resource == "time"
    ]
    assert time_a.edit_hint["path"] == "cfg-dispatch.compile.time"


def test_a_test_row_falls_back_when_the_head_records_no_request():
    """The head's half of the same guard: it records nothing.

    A row whose `requested_cpus` is absent is the "ask the scheduler" case.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,
                "total_cpu_s": 1000.0,
            },
            requested_cpus=None,
        )
    ]
    (cpu,) = [f for f in _analyze(rows) if f.resource == "cpus"]
    assert cpu.reserved == "4"
    assert cpu.suggested == "2"


def test_a_cpus_override_retargets_the_per_test_edit_hint():
    """Naming a masked field is advice that cannot be applied.

    `sbatch-args` wins over the generated `--cpus-per-task`, so the hint names the
    argument and says which field it supersedes.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,
                "req_mem_bytes": 8 * 2**30,
                "max_rss_bytes": 2**30,
                "total_cpu_s": 1000.0,  # 0.25 efficiency against the 4
            },
            requested_cpus=None,  # withdrawn by the override
            cpus_override=["--cpus-per-task=4"],
        )
    ]
    findings = _analyze(rows, root_config_path="root_config.yaml")
    (cpu,) = [f for f in findings if f.resource == "cpus"]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpu.edit_hint["file"] == "root_config.yaml"
    note = cpu.edit_hint["note"]
    assert "sbatch-args `--cpus-per-task=4` sets this job's cpu request" in note
    assert "tests[name=t].resources.cpus" in note

    # Only cpus is masked: `--cpus-per-task` supersedes neither mem nor time.
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.edit_hint["path"] == "tests[name=t].resources.mem"
    assert "note" not in mem.edit_hint
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.edit_hint["path"] == "tests[name=t].resources.time"


def test_an_overridden_in_job_compile_row_names_the_field_it_masks():
    """The note names whichever cpus field the layering would have chosen.

    For a job that compiles inside itself, that can be `cfg-dispatch.compile.cpus`
    or the suite's own `compile.cpus` rather than the test's `resources`.
    """
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 1000,
        "timelimit_s": 3600,
        "alloc_cpus": 4,
        "req_cpus": 4,
        "total_cpu_s": 1000.0,
    }
    rows = [
        _row(
            "t",
            telemetry,
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            cpus_override=["-c 4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert "superseding cfg-dispatch.compile.cpus" in cpu.edit_hint["note"]

    # The suite's own compile block, when that is the layer that won.
    rows = [
        _row(
            "t",
            telemetry,
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            cpus_override=["-c 4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(
            rows,
            root_config_path="root_config.yaml",
            compile_origins={"cpus": "suite"},
        )
        if f.resource == "cpus"
    ]
    assert "superseding compile.cpus" in cpu.edit_hint["note"]


def test_two_orthogonal_cpu_options_withhold_the_per_argument_suggestion():
    """`--ntasks` x `--cpus-per-task` is a product, not a winner.

    With one argument the suggestion goes straight into it. With two the request
    is their product, so the hint still names `sbatch-args`, states the product
    and hands the decomposition back.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,  # 4 tasks x 2 cpus-per-task
                "total_cpu_s": 2000.0,  # 0.25 efficiency against those 8
            },
            requested_cpus=None,
            cpus_override=["--ntasks=4", "--cpus-per-task=2"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "8"
    assert cpu.suggested == "3"  # ceil(8 x 0.25 x 1.5), the whole-job figure
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    note = cpu.edit_hint["note"]
    assert (
        "`--ntasks=4` and `--cpus-per-task=2` set this job's cpu request together"
        in note
    )
    # No arithmetic claim: sbatch's own precedence decides how they combine.
    assert "product" not in note
    assert "decompose it across them per sbatch's own precedence" in note
    # No single argument is claimed to take the number.
    assert "sets this job's cpu request" not in note
    assert "change it there" not in note
    # The masked field is still named, so the reader knows what was lost.
    assert "tests[name=t].resources.cpus" in note


def test_one_cpu_option_still_takes_the_suggestion_directly():
    """The single-argument note is unchanged: there is nothing to split."""
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 2000.0,
            },
            requested_cpus=None,
            cpus_override=["--cpus-per-task=8"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    note = cpu.edit_hint["note"]
    assert "sbatch-args `--cpus-per-task=8` sets this job's cpu request" in note
    assert "Suggested value is the whole-job cpu count." in note
    assert "product" not in note


def test_the_build_row_withholds_it_too_under_orthogonal_options():
    """Same rule for the suite's build job."""
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "alloc_cpus": 8,
        "req_cpus": 8,  # 4 tasks x 2 cpus-per-task
        "total_cpu_s": 200,  # 0.25 efficiency against those 8
    }
    (cpus_a,) = [
        f
        for f in _build_advice(
            telemetry,
            parallel=1,
            cpus=2,
            cpus_override=["--ntasks=4", "--cpus-per-task=2"],
        )
        if f.resource == "cpus"
    ]
    assert cpus_a.reserved == "8"
    assert cpus_a.suggested == "3"
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    note = cpus_a.edit_hint["note"]
    assert (
        "`--ntasks=4` and `--cpus-per-task=2` set this job's cpu request together"
        in note
    )
    # No arithmetic claim: sbatch's own precedence decides how they combine.
    assert "product" not in note
    assert "decompose it across them per sbatch's own precedence" in note
    assert "cfg-dispatch.compile.cpus" in note
    # The superseded per-build decomposition stays gone.
    assert "the build job reserved" not in note


_MODIFIER_TELEMETRY = {
    "state": "COMPLETED",
    "elapsed_s": 1000,
    "timelimit_s": 3600,
    "alloc_cpus": 8,
    "req_cpus": 8,
    "total_cpu_s": 2000.0,  # 0.25 efficiency against those 8
}


@pytest.mark.parametrize(
    "arg",
    [
        "--ntasks=4",
        "-n 4",
        "--ntasks-per-node=2",
        "--nodes=2",
        "-N 2",
    ],
)
def test_a_lone_task_or_node_count_does_not_take_the_suggestion(arg):
    """Writing 3 into `--ntasks` asks for three tasks, not three cpus.

    Only `--cpus-per-task` and `--cpus-per-gpu` state a cpu count outright. A task
    count or topology modifier scales the request, so "change it there" would be
    unappliable.
    """
    rows = [
        _row(
            "t",
            _MODIFIER_TELEMETRY,
            requested_cpus=None,
            cpus_override=[arg],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.suggested == "3"  # ceil(8 x 0.25 x 1.5), the whole-job figure
    note = cpu.edit_hint["note"]
    assert f"`{arg}` multiplies this job's cpu request" in note
    # The generated `--cpus-per-task` is untouched, so the per-task field is still named
    # as a lever.
    assert "the generated --cpus-per-task from tests[name=t].resources.cpus" in note
    assert "no single one of them takes it" in note
    # It is not the "write the number in here" wording.
    assert "sets this job's cpu request" not in note
    assert "change it there" not in note


@pytest.mark.parametrize("arg", ["--cpus-per-task=8", "-c 8", "-c8", "-c=8"])
def test_a_lone_direct_cpu_count_still_takes_the_suggestion(arg):
    """`--cpus-per-task` names a cpu count, so the number goes straight in."""
    rows = [_row("t", _MODIFIER_TELEMETRY, requested_cpus=None, cpus_override=[arg])]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    note = cpu.edit_hint["note"]
    assert f"sbatch-args `{arg}` sets this job's cpu request" in note
    assert "change it there" in note
    assert "raises this job's cpu request" not in note


def test_the_build_row_also_declines_a_lone_modifier():
    """Same rule for the suite's build job."""
    (cpus_a,) = [
        f
        for f in _build_advice(
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 7200,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 200,  # 0.25 efficiency
            },
            parallel=1,
            cpus=2,
            cpus_override=["--ntasks-per-node=4"],
        )
        if f.resource == "cpus"
    ]
    assert cpus_a.suggested == "3"
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    note = cpus_a.edit_hint["note"]
    assert "`--ntasks-per-node=4` multiplies this job's cpu request" in note
    assert "cfg-dispatch.compile.cpus" in note
    assert "change it there" not in note


def test_an_override_disables_the_compile_cpus_floor():
    """The floor bounds the generated reservation, which sbatch never saw.

    An in-job compile's allocation is max(sim, compile), so no reduce goes below the
    compile side unless a `sbatch-args` cpu argument supersedes the whole
    reservation. Clamping to a floor of 8 under a request of 4 would push every
    suggestion to 8 and drop it, so a genuinely over-reserved run would report
    nothing.
    """
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 1000,
        "timelimit_s": 3600,
        "alloc_cpus": 4,
        "req_cpus": 4,  # the override's request
        "total_cpu_s": 1000.0,  # 0.25 efficiency against those 4
    }
    floor = {"cpus": 8, "mem": "16G", "time": "02:00:00"}

    # Without an override the floor is real: the allocation cannot go below 8, so there
    # is nothing to advise.
    unoverridden = [
        _row(
            "t",
            telemetry,
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor=floor,
            requested_cpus=4,
        )
    ]
    assert [f for f in _analyze(unoverridden) if f.resource == "cpus"] == []

    # With one, the floor bounds a reservation that was never submitted.
    overridden = [
        _row(
            "t",
            telemetry,
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor=floor,
            requested_cpus=None,
            cpus_override=["--cpus-per-task=4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(overridden, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "4"
    assert cpu.suggested == "2"  # ceil(4 x 0.25 x 1.5), not clamped up to 8
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"

    # The mem and time floors are untouched: `--cpus-per-task` supersedes neither.
    assert [f for f in _analyze(overridden) if f.resource == "mem"] == []


def test_the_note_makes_no_arithmetic_claim_about_four_arguments():
    """`--ntasks=8 --nodes=2 --ntasks-per-node=4 --cpus-per-task=2` is 16.

    Not the product of all four: sbatch's own precedence decides, and `--ntasks`
    wins. The note names the arguments and leaves the combining rule to sbatch.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 16,
                "req_cpus": 16,  # 8 tasks x 2 cpus, not 8 x 2 x 4 x 2
                "total_cpu_s": 4000.0,  # 0.25 efficiency against those 16
            },
            requested_cpus=None,
            cpus_override=[
                "--ntasks=8",
                "--nodes=2",
                "--ntasks-per-node=4",
                "--cpus-per-task=2",
            ],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "16"
    assert cpu.suggested == "6"  # ceil(16 x 0.25 x 1.5), the whole-job figure
    note = cpu.edit_hint["note"]
    # Every argument is named, in the order the project wrote them.
    assert (
        "`--ntasks=8`, `--nodes=2`, `--ntasks-per-node=4` and "
        "`--cpus-per-task=2` set this job's cpu request together" in note
    )
    # No arithmetic is claimed about how they combine.
    assert "product" not in note
    assert " x " not in note
    assert "decompose it across them per sbatch's own precedence" in note


def test_an_env_override_names_the_variable_and_no_file():
    """There is no YAML to edit, so the hint does not name a file.

    `SBATCH_NTASKS=4` beside a generated `--cpus-per-task=2` requests eight cpus. It
    supersedes the test's `resources.cpus` like a `sbatch-args` entry, but lives in
    the environment.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,  # 4 tasks x the generated 2 cpus
                "total_cpu_s": 2000.0,  # 0.25 efficiency against those 8
            },
            requested_cpus=None,
            cpus_override=["SBATCH_NTASKS=4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "8"
    assert cpu.suggested == "3"  # ceil(8 x 0.25 x 1.5), the whole-job figure
    assert cpu.edit_hint["path"] == "env"
    assert "file" not in cpu.edit_hint
    note = cpu.edit_hint["note"]
    # A variable is not a cpu count: it multiplies the generated one.
    assert "`SBATCH_NTASKS=4` multiplies this job's cpu request" in note
    assert "the generated --cpus-per-task from tests[name=t].resources.cpus" in note
    assert "the task count in the environment" in note
    assert "sbatch-args" not in note


def test_env_and_args_together_name_both_and_keep_the_file():
    """`sbatch-args` is the actionable half, so the hint still points there.

    The command line beats the environment, so an edit in `sbatch-args` can defeat
    the variable; the note names both so the leftover factor is not a surprise.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 2000.0,
            },
            requested_cpus=None,
            cpus_override=["--cpus-per-task=2", "SBATCH_NTASKS=4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpu.edit_hint["file"] == "root_config.yaml"
    note = cpu.edit_hint["note"]
    assert "sbatch-args and the environment supersedes" in note
    assert (
        "`--cpus-per-task=2` and `SBATCH_NTASKS=4` set this job's cpu request" in note
    )
    assert "product" not in note


def test_an_args_override_hint_names_the_backends_own_config():
    """The `file` is where the arguments really live.

    The overrides are read off the backend, instantiated from the orchestration
    `root_config.yaml`, which can differ from the root this suite resolved. The
    hint names the backend's file.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 2000.0,  # 0.25 efficiency
            },
            requested_cpus=None,
            cpus_override=["--cpus-per-task=8"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(
            rows,
            root_config_path="verif/blk/root_config.yaml",
            sbatch_args_config_path="/proj/orchestration/root_config.yaml",
        )
        if f.resource == "cpus"
    ]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpu.edit_hint["file"] == "/proj/orchestration/root_config.yaml"


def test_only_the_args_hint_moves_to_the_backends_config():
    """The suite-resolved fields keep naming the suite's own root.

    `cfg-dispatch.compile.*` advice is about the reservation this suite resolved;
    only the verbatim `sbatch-args` passthrough belongs to the backend.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 24 * 2**30,
                "max_rss_bytes": 2 * 2**30,
                "alloc_cpus": 1,
                "req_cpus": 1,
                "total_cpu_s": 100.0,
            },
            governed_by={"mem": "compile"},
            compile_floor={"mem": "1G"},
        )
    ]
    (mem,) = [
        f
        for f in _analyze(
            rows,
            root_config_path="verif/blk/root_config.yaml",
            sbatch_args_config_path="/proj/orchestration/root_config.yaml",
        )
        if f.resource == "mem"
    ]
    assert mem.edit_hint["path"] == "cfg-dispatch.compile.mem"
    assert mem.edit_hint["file"] == "verif/blk/root_config.yaml"


def test_without_a_backend_config_the_args_hint_falls_back_to_the_root():
    """A caller with no better answer gets the root_config_path it passed."""
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 2000.0,
            },
            requested_cpus=None,
            cpus_override=["--cpus-per-task=8"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.edit_hint["file"] == "root_config.yaml"


def test_the_build_rows_args_hint_names_the_backends_config_too():
    """The `(build job)` row carries the same override, so the same file."""
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "alloc_cpus": 8,
        "req_cpus": 8,
        "total_cpu_s": 200,  # 0.25 efficiency against the 8 submitted
    }
    (cpus_a,) = [
        f
        for f in _build_advice(
            telemetry,
            parallel=1,
            cpus=2,
            root="verif/blk/root_config.yaml",
            cpus_override=["--cpus-per-task=8"],
            sbatch_args_config_path="/proj/orchestration/root_config.yaml",
        )
        if f.resource == "cpus"
    ]
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpus_a.edit_hint["file"] == "/proj/orchestration/root_config.yaml"
    # A field with no override still points at the suite's own root.
    (time_a,) = [
        f
        for f in _build_advice(
            telemetry,
            parallel=1,
            cpus=2,
            root="verif/blk/root_config.yaml",
            sbatch_args_config_path="/proj/orchestration/root_config.yaml",
        )
        if f.resource == "time"
    ]
    assert time_a.edit_hint["file"] == "verif/blk/root_config.yaml"


def test_the_build_row_takes_an_env_override_too():
    """Same rule for the suite's build job."""
    (cpus_a,) = [
        f
        for f in _build_advice(
            {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 7200,
                "alloc_cpus": 8,
                "req_cpus": 8,
                "total_cpu_s": 200,  # 0.25 efficiency
            },
            parallel=1,
            cpus=2,
            cpus_override=["SBATCH_NODES=4"],
        )
        if f.resource == "cpus"
    ]
    assert cpus_a.reserved == "8"
    assert cpus_a.edit_hint["path"] == "env"
    assert "file" not in cpus_a.edit_hint
    note = cpus_a.edit_hint["note"]
    assert "`SBATCH_NODES=4` multiplies this job's cpu request" in note
    assert "the generated --cpus-per-task from cfg-dispatch.compile.cpus" in note


def test_a_task_count_override_keeps_the_per_task_compile_floor():
    """`--ntasks=2` leaves `--cpus-per-task=8` alone and asks for two of it.

    The compile floor of 8 still holds: even one task costs 8 cpus, so a suggestion
    below 8 could never be reached. The floor is not multiplied by the tasks
    observed; 8 is reachable by dropping to a single task. Clearing the floor is
    right only for a direct `-c` override, which replaces the generated flag.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 16,
                "req_cpus": 16,  # 2 tasks x the generated 8 cpus
                "total_cpu_s": 1600.0,  # 0.10 efficiency against those 16
            },
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor={"cpus": 8, "mem": "16G", "time": "02:00:00"},
            requested_cpus=None,  # withdrawn: ReqCPUS is not the per-task value
            submitted_cpus_per_task=8,
            cpus_override=["--ntasks=2"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    # ceil(16 x 0.10 x 1.5) = 3 is below the per-task floor and is clamped up to 8,
    # which is reachable at one task.
    assert cpu.reserved == "16"
    assert cpu.suggested == "8"
    # The note states the decomposition as an observation and names both levers.
    note = cpu.edit_hint["note"]
    assert "the request is 8 per task x 2 tasks" in note
    assert "lower cfg-dispatch.compile.cpus, the task count in sbatch-args" in note


def test_a_direct_override_still_clears_the_floor():
    """`-c` replaces the generated flag, so the floor it bounded is gone."""
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,
                "total_cpu_s": 1000.0,  # 0.25 efficiency
            },
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor={"cpus": 8, "mem": "16G", "time": "02:00:00"},
            requested_cpus=None,
            submitted_cpus_per_task=8,
            cpus_override=["--cpus-per-task=4"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.suggested == "2"  # ceil(4 x 0.25 x 1.5), not clamped up to 8


def test_a_task_count_override_still_advises_what_it_can_reach():
    """The floor scales; it does not silence everything above it.

    4 cpus per task over 4 tasks is 16 requested against a floor of 8 whole-job, so
    a suggestion of 12 is reachable by halving the tasks or the per-task field.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 16,
                "req_cpus": 16,  # 4 tasks x the generated 4 cpus
                "total_cpu_s": 8000.0,  # 0.50 efficiency, just under
            },
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor={"cpus": 2, "mem": "16G", "time": "02:00:00"},
            requested_cpus=None,
            submitted_cpus_per_task=4,
            cpus_override=["--ntasks=4"],
        )
    ]
    cfg = RightsizeConfigFile(over_threshold=0.6)
    (cpu,) = [
        f
        for f in _analyze(rows, cfg=cfg, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "16"
    assert cpu.suggested == "12"  # ceil(16 x 0.5 x 1.5), above the 2 x 4 floor
    # The note decomposes the request from observation, not sbatch arithmetic.
    assert "the request is 4 per task x 4 tasks" in cpu.edit_hint["note"]


def test_an_unknowable_task_count_still_states_only_what_it_knows():
    """A request that is not a whole multiple of the per-task cpus.

    The per-task floor is unaffected; the "N per task x M tasks" clause is an
    observation, so it is omitted rather than guessed.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 10,
                "req_cpus": 10,  # not a multiple of the generated 4
                "total_cpu_s": 1000.0,  # 0.10 efficiency
            },
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor={"cpus": 4, "mem": "16G", "time": "02:00:00"},
            requested_cpus=None,
            submitted_cpus_per_task=4,
            cpus_override=["--ntasks=2"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    # ceil(10 x 0.1 x 1.5) = 2, clamped up to the per-task floor of 4.
    assert cpu.suggested == "4"
    # With no task count to state, the note says only what it knows.
    assert "per task x" not in cpu.edit_hint["note"]


def _mixed_seed_rows(second_override):
    """Two seeds of one test, the second submitted under a changed request."""
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 1000,
        "timelimit_s": 3600,
        "alloc_cpus": 4,
        "req_cpus": 4,
        "total_cpu_s": 1000.0,  # 0.25 efficiency
    }
    return [
        _row("t", telemetry, run_id=1, requested_cpus=4, submitted_cpus_per_task=4),
        _row(
            "t",
            telemetry,
            run_id=2,
            requested_cpus=None,
            submitted_cpus_per_task=4,
            cpus_override=second_override,
        ),
    ]


def test_cpus_advice_is_withheld_when_a_tests_runs_were_not_submitted_alike(caplog):
    """One `reserved` and one `edit_hint` cannot describe two reservations.

    A retry is submitted into the environment the process holds by then. When
    some seeds retried after the ambient `SBATCH_*` changed, their rows describe a
    different request, so the cpus row is withheld, as
    `parallel-utilization-ambiguous` does for the build job.
    """
    import logging

    with caplog.at_level(logging.INFO):
        findings = _analyze(
            _mixed_seed_rows(["SBATCH_NTASKS=4"]), root_config_path="root_config.yaml"
        )

    assert [f for f in findings if f.resource == "cpus"] == []
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "rightsize.cpus_advice_withheld"
    ]
    assert record.rtl_fields["reason"] == "mixed-cpu-requests"
    assert record.rtl_fields["test"] == "t"
    assert record.rtl_fields["runs"] == 2
    # The omission is legible, not a silent gap.
    assert "no cpus advice for t" in caplog.text
    assert "not all submitted with the same cpu request" in caplog.text


def test_runs_submitted_alike_still_get_their_cpus_advice(caplog):
    """The guard is about disagreement, not about having several runs."""
    import logging

    rows = _mixed_seed_rows(None)
    # Making the second row agree with the first again removes the omission.
    rows[1]["requested_cpus"] = 4
    with caplog.at_level(logging.INFO):
        (cpu,) = [
            f
            for f in _analyze(rows, root_config_path="root_config.yaml")
            if f.resource == "cpus"
        ]
    assert cpu.reserved == "4"
    assert cpu.suggested == "2"
    assert "cpus_advice_withheld" not in caplog.text


def test_a_differing_per_task_value_alone_also_withholds():
    """`submitted_cpus_per_task` is part of the request, so it counts too."""
    rows = _mixed_seed_rows(None)
    rows[1]["requested_cpus"] = 4
    rows[1]["submitted_cpus_per_task"] = 2
    assert [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ] == []


def test_mem_and_time_advice_survive_a_mixed_cpu_request():
    """Only the cpus row depends on the reservation that differed."""
    rows = _mixed_seed_rows(["SBATCH_NTASKS=4"])
    for row in rows:
        row["results"].results["telemetry"] = {
            **row["results"].results["telemetry"],
            "req_mem_bytes": 8 * 2**30,
            "max_rss_bytes": 2**30,
        }
    findings = _analyze(rows, root_config_path="root_config.yaml")
    assert {f.resource for f in findings} == {"mem", "time"}


def test_a_gpu_derived_task_count_reads_as_a_task_count_override():
    """The pair multiplies the generated per-task cpus, like `--ntasks`.

    The compile floor survives, the per-task field stays a lever, and the note
    names both halves.
    """
    rows = [
        _row(
            "t",
            {
                "state": "COMPLETED",
                "elapsed_s": 1000,
                "timelimit_s": 3600,
                "alloc_cpus": 8,
                "req_cpus": 8,  # 2 gpus x 2 tasks-per-gpu x the generated 2
                "total_cpu_s": 2000.0,  # 0.25 efficiency against those 8
            },
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            compile_floor={"cpus": 2, "mem": "16G", "time": "02:00:00"},
            requested_cpus=None,
            submitted_cpus_per_task=2,
            cpus_override=["--gpus=2", "--ntasks-per-gpu=2"],
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.reserved == "8"
    assert cpu.suggested == "3"  # ceil(8 x 0.25 x 1.5), above the floor of 2
    note = cpu.edit_hint["note"]
    assert "`--gpus=2` and `--ntasks-per-gpu=2` multiply this job's cpu request" in note
    assert "the generated --cpus-per-task from cfg-dispatch.compile.cpus" in note
    # It multiplies, so it must not read as a direct replacement.
    assert "change it there" not in note


def test_the_documented_note_examples_are_the_notes_actually_emitted():
    """docs/concepts/dispatch.md quotes these verbatim; keep them true."""
    from pathlib import Path

    import rtl_buddy

    docs = (
        Path(rtl_buddy.__file__).resolve().parents[2]
        / "docs"
        / "concepts"
        / "dispatch.md"
    )
    if not docs.is_file():  # pragma: no cover - installed without the docs tree
        pytest.skip("docs tree not present next to the package")
    text = docs.read_text()
    masked = "tests[name=wr_single].resources.cpus"

    for args, per_task, tasks in [
        (["--cpus-per-task=4"], None, None),
        (["--ntasks=4"], 8, 4),
        (["--ntasks=4", "--cpus-per-task=2"], None, None),
    ]:
        note = _override_note(args, masked, per_task=per_task, tasks=tasks)
        # The docs wrap these into a fenced block, so compare word streams.
        assert " ".join(note.split()) in " ".join(text.split()), note


def test_the_verilate_job_gets_its_own_row_and_phase():
    """Two jobs, two reservations, two rows a reader can tell apart."""
    telemetry = {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200}
    (build,) = [f for f in _build_advice(telemetry) if f.resource == "time"]
    (verilate,) = [
        f for f in _build_advice(telemetry, phase="verilate") if f.resource == "time"
    ]
    assert (build.test, build.phase) == (BUILD_JOB_ROW, "compile")
    assert (verilate.test, verilate.phase) == (VERILATE_JOB_ROW, "verilate")
    # Parenthesised, so neither can collide with a real test name.
    assert VERILATE_JOB_ROW == "(verilate job)"


def test_verilate_advice_names_the_verilate_key_it_is_written_at():
    """`cfg-dispatch.compile.time` would not move this job.

    The verilate sub-block beats every `compile:` layer.
    """
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        phase="verilate",
        compile_origins={
            "time": {
                "origin": "cfg-dispatch",
                "testbench": None,
                "key": "verilate.time",
            }
        },
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "path": "cfg-dispatch.compile.verilate.time",
        "file": "root_config.yaml",
    }


def test_verilate_advice_names_the_layer_and_the_key_together():
    """Suite and testbench spellings, each with the key that really holds it."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        phase="verilate",
        compile_origins={
            "time": {
                "origin": "testbench",
                "testbench": "tb_chip_t1",
                "key": "verilate.time",
            }
        },
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint == {
        "file": "/abs/verif/blk/tests.yaml",
        "path": "testbenches[name=tb_chip_t1].compile.verilate.time",
    }

    # A field the verilate reservation inherited from `compile:` is named at that key.
    inherited = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        phase="verilate",
        compile_origins={"time": {"origin": "suite", "testbench": None, "key": "time"}},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (inherited_time,) = [f for f in inherited if f.resource == "time"]
    assert inherited_time.edit_hint["path"] == "compile.time"


def test_the_build_job_row_is_unchanged_by_the_verilate_keys():
    """A provenance map with no `key` applies to every compile field."""
    findings = _build_advice(
        {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
        compile_origins={"time": {"origin": "suite", "testbench": None}},
        suite_config_hint="/abs/verif/blk/tests.yaml",
    )
    (time_a,) = [f for f in findings if f.resource == "time"]
    assert time_a.edit_hint["path"] == "compile.time"


def test_mode_governed_advice_names_the_mode_key():
    """A `-M cov` field is edited at `resources.modes.cov.<field>`.

    Naming the base key would be advice that cannot retire, since the mode block
    overrides it.
    """
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 1700,
        "timelimit_s": 3600,
        "req_mem_bytes": 24 * 2**30,
        "max_rss_bytes": 3 * 2**30,
    }
    rows = [_row("t", telemetry, resource_modes={"mem": "cov"})]
    findings = {f.resource: f for f in _analyze(rows)}
    assert findings["mem"].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "tests[name=t].resources.modes.cov.mem",
    }
    # A field the mode block did not state keeps the base key.
    assert findings["time"].edit_hint["path"] == "tests[name=t].resources.time"
    # A run with no mode block in play is unchanged.
    plain = {f.resource: f for f in _analyze([_row("t", telemetry)])}
    assert plain["mem"].edit_hint["path"] == "tests[name=t].resources.mem"
