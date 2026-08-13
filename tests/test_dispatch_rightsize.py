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
    compile_floor=None,
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
    }


def _analyze(
    rows,
    cfg=_CFG,
    families=None,
    reg_level=0,
    root_config_path=None,
    accounting_interval_s=None,
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


# ------------- #358: reduce advice must be reachable under the compile floor


def _over_reserved_row(**kwargs):
    """10s of 1h, 1G of 8G — both resources look comfortably over-reserved."""
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
    # Unclamped these would be the 5-minute / 128M floors; the compile needs
    # more, so that is what the allocation can actually be reduced to.
    assert findings["time"].suggested == "00:30:00"
    assert findings["time"].edit_hint["path"] == "cfg-dispatch.compile.time"
    assert findings["mem"].suggested == "4G"
    assert findings["mem"].edit_hint["path"] == "cfg-dispatch.compile.mem"
    assert findings["mem"].direction == "reduce"


def test_reduce_is_dropped_when_the_floor_equals_the_reservation():
    """A suggestion the clamp returns to the current value saves nothing.

    Emitting it would make the agent loop rerun, see the advice fail to
    retire, and have no way to tell that from a wrong suggestion.
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
    """The floor only clamps reductions; a kill must still raise."""
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


# ------------------------------- memory advice needs a sampled peak (#365)


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
    """MaxRSS on a job never sampled is not a small peak, it is no peak —
    and advising a reservation from it points at the OOM floor (#365)."""
    rows = [_row("fast", _short_job())]

    assert "mem" not in [f.resource for f in _analyze(rows, accounting_interval_s=30)]
    # Only memory: elapsed time is measured directly, not sampled, so time
    # advice for the same short job stands.
    assert "time" in [f.resource for f in _analyze(rows, accounting_interval_s=30)]
    # And the same numbers with adequate sampling are advice, not noise.
    assert "mem" in [f.resource for f in _analyze(rows, accounting_interval_s=1)]


def test_mem_advice_survives_when_the_interval_is_unknown():
    """No interval means no evidence the peak is untrustworthy."""
    rows = [_row("fast", _short_job())]
    assert "mem" in [f.resource for f in _analyze(rows)]


def test_an_oom_kill_still_raises_however_coarse_the_sampling(caplog):
    """A kill is a fact about the reservation, not a measurement of it —
    the same rule the TIMEOUT case follows for time."""
    import logging

    rows = [_row("oom", _short_job(state="OUT_OF_MEMORY"), passing=False)]

    with caplog.at_level(logging.WARNING):
        findings = [
            f for f in _analyze(rows, accounting_interval_s=30) if f.resource == "mem"
        ]

    assert [f.direction for f in findings] == ["raise"]
    # ...and it must not also be reported as an omission: advice was given.
    assert "memory advice omitted" not in caplog.text


def test_nothing_is_reported_omitted_when_there_was_nothing_to_advise(caplog):
    """No reservation and no peak are unrelated reasons for silence; naming
    those tests in "memory advice omitted" would blame the wrong cause."""
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
    """Utilization is judged per test across its seeds, and so is this: one
    run long enough to be sampled makes the test's peak meaningful."""
    rows = [
        _row("mixed", _short_job(elapsed_s=2), run_id=1),
        _row("mixed", _short_job(elapsed_s=90, max_rss_bytes=3900 * 2**20), run_id=2),
    ]

    findings = [
        f for f in _analyze(rows, accounting_interval_s=30) if f.resource == "mem"
    ]
    assert [f.direction for f in findings] == ["raise"]


def test_the_omission_is_logged_rather_than_left_silent(caplog):
    """Empty advice reads as "nothing to say"; it must not be able to mean
    "the numbers were unusable" without saying so."""
    import logging

    rows = [_row("fast", _short_job()), _row("slow", _short_job(elapsed_s=600))]

    with caplog.at_level(logging.WARNING):
        _analyze(rows, accounting_interval_s=30)

    assert "memory advice omitted" in caplog.text
    assert "fast" in caplog.text
    assert "slow" not in caplog.text
