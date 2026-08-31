"""Reservation right-sizing analysis tests (#351 P3).

Pure-function coverage of ``dispatch.rightsize``: per-test aggregation
across seeds, over/under classification, TIMEOUT/OOM pairing, formatting,
guardrails, and the Verilator-only gate on time advice.
"""

from __future__ import annotations

from rtl_buddy.config.dispatch import RightsizeConfigFile
from rtl_buddy.dispatch.rightsize import (
    RightsizeFinding,
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
        # What the head resolved and submitted as `--cpus-per-task` (#505).
        "requested_cpus": requested_cpus,
        # ...and the `sbatch-args` entry that superseded it, if any.
        "cpus_override": cpus_override,
    }


def _analyze(
    rows,
    cfg=_CFG,
    families=None,
    reg_level=0,
    root_config_path=None,
    accounting_interval_s=None,
    compile_origins=None,
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


def test_compile_governed_field_the_suite_won_hints_at_the_suite_config():
    """cfg-dispatch is the layer a suite `compile:` block overrides (#497).

    The in-job-compile case has no build job at all, so the compile
    reservation only ever surfaces here, inside the field-wise maximum. A
    suite that sets `compile.mem: 48G` and OOMs anyway must be sent to its
    own tests.yaml: raising `cfg-dispatch.compile.mem` leaves the 48G in
    place, the allocation unmoved, and the advice repeating every run.
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
    """Only the fields the suite block set move to its tests.yaml (#497)."""
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
    # The suite set mem, so its own file is what holds the binding floor...
    assert findings["mem"].edit_hint == {
        "file": "verif/blk/tests.yaml",
        "path": "compile.mem",
    }
    # ...while time still comes from cfg-dispatch, in the same run.
    assert findings["time"].edit_hint == {
        "file": "/p/root_config.yaml",
        "path": "cfg-dispatch.compile.time",
    }


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


# ------------------------------------ the build job's own row (#495)


class _Res:
    """Stand-in for the resolved per-build JobResources."""

    def __init__(self, cpus):
        self.cpus = cpus


# What the build envelope says the job did. The default is one real
# compile, because that is the case every reduce/raise assertion below is
# about; the "nothing compiled" cases pass their own.
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
    """One slot, so the whole-job ratio IS the per-build one."""
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
    # ceil(8 x 0.25 x 1.5) = 3, and with one build in flight that is the
    # per-build figure — no division, nothing to decompose.
    assert cpus_a.reserved == "8"
    assert cpus_a.suggested == "3"
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.compile.cpus"
    assert "the build job reserved 8." in cpus_a.edit_hint["note"]
    # The `parallel` lever has nothing to say to a job that is already at 1.
    assert "compile.parallel" not in cpus_a.edit_hint["note"]


def test_a_parallel_build_job_withholds_its_cpus_advice(caplog):
    """Whole-job utilization does not decompose into per-build cpus (#496).

    With N slots the ratio also carries the tail — builds of unequal length,
    and a plan with fewer distinct compile keys than slots, both leave
    reserved cpus idle while the longest compile saturates the ones it has.
    Dividing by `parallel` would then advise shrinking exactly the cpus that
    compile needed. sacct accounts the job, not the thread group, so nothing
    here can tell the causes apart: the row is withheld, and time advice —
    wall clock, which N concurrent builds do not inflate — is untouched.
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


def test_a_single_build_is_advised_even_at_a_wide_parallel():
    """One build cannot have a tail, so its ratio is still its own.

    `parallel` caps at the planned config count, so the head cannot really
    produce this pair — but the gate is on the builds that ran, not on the
    flag, and it is the builds that decide whether the ratio decomposes.
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
    # The head submitted 8 x 4 = 32, so the ratio is 200 / (100 x 32) and
    # ceil(32 x 0.0625 x 1.5) = 3 -- not divided again by the idle slots.
    assert cpus_a.suggested == "3"
    # ...but 3 > the 2 already configured, so the note explains the lever
    # that is actually oversized here.
    assert "compile.parallel 4" in cpus_a.edit_hint["note"]


def test_the_cpus_decomposition_comes_from_the_configured_per_build_value():
    """AllocCPUS is what the site gave, not what the YAML asked for.

    Under CR_CPU with threads-per-core, or core-granularity rounding, or a
    site `sbatch-args` override, sacct reports more cpus than the head
    requested. Dividing that by `parallel` names a per-build figure the
    project never wrote — and it can round the current value up far enough
    that the suggestion looks like a reduction from a number that is
    already there, which is advice that never retires. The decomposition
    is therefore stated from the resolved `cfg-dispatch.compile.cpus`, with
    the allocated figure named separately so `sacct` still reconciles.

    The same resolved value is the ratio's denominator and the reported
    `reserved` (#505): this row carries no `req_cpus` at all, so without it
    the whole finding would be stated in the site's rounded numbers.
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
    # One build slot, so the request is the per-build figure with nothing to
    # decompose — and the site's rounding is named, not adopted.
    assert "the build job reserved 3" in note
    assert "the scheduler reported 8 allocated" in note
    # 8 would have been the sacct-derived per-build figure.
    assert "= 8 x" not in note
    # `reserved` is the number `cfg-dispatch.compile.cpus` holds; the
    # allocation rides along as an additive field (#505).
    assert cpus_a.reserved == "3"
    assert cpus_a.allocated == "8"
    assert cpus_a.utilization == 100 / 300
    assert cpus_a.suggested == "2"  # ceil(3 x 0.333 x 1.5)


def test_a_saturated_build_job_gets_no_cpus_advice(caplog):
    """Efficiency only ever argues for fewer cpus, and only when it is low."""
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
    # Nothing was withheld: there was nothing to withhold. "No advice" and
    # "advice withheld" stay different answers.
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

    # The fallback is the *scaled* reservation, which the withheld row is
    # still measured against: at parallel 2 the head asked for 4 x 2, so the
    # ratio is 100 / (100 x 8) and not 100 / (100 x 4). Drop the scaling and
    # a job's efficiency doubles on paper.
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


# ------------- build-advice attribution when a suite overrides a field (#497)


def test_build_advice_points_at_the_root_config_when_no_suite_block_won():
    """The pre-#497 shape, unchanged: cfg-dispatch owns every field."""
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
    """Editing cfg-dispatch.compile.time would move nothing here (#497)."""
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

    `mem` gets no build-job advice at all, so the point is that an origin
    for one field never leaks onto another's hint.
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
    """No honest suite path to name: keep the cfg-dispatch hint (#497).

    Advice is advisory and runs after every job finished — a missing path
    must degrade, never abort.
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


# --------------------- "nothing to compile" is not "compiled fast" (#495)

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
    """The re-run trap: every build reused its stamp, so the job is seconds
    long against a 2 h limit. Reading that as "the compile is fast" advises
    a 5-minute limit, and the next real RTL change TIMEOUTs against it —
    which afterok turns into a cancelled sim fan-out."""
    assert _build_advice(_REUSE_RUN, compile_work=_ALL_REUSED) == []


def test_an_envelope_that_cannot_say_yields_no_reduce_advice():
    """A build job that left no envelope, or one written before the records
    existed (a mixed-version fleet), is *unknown*, not "nothing ran"."""
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
    """TotalCPU is accumulated from usage samples, so a job that finished
    inside one interval was measured at most once — the same reason memory
    advice is withheld for a short test (#365)."""
    assert _build_advice(_REUSE_RUN, accounting_interval_s=300) == []
    # ...and the same job, once the interval says it was sampled, advises.
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

    "The job spent 55s of its 2 h reservation actually compiling" is the
    number a reader wants beside the sim durations, and sacct cannot
    produce it — only the envelope's per-build records can. It rides the
    same event as `builds`/`compiled` and keeps their three-state rule:
    absent, not zero, when there was no envelope to measure.
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
    """Could not tell is its own reason, and the line has to say so.

    A build job OOM-killed or TIMEOUTed leaves an sacct row but no
    envelope, which is exactly the diagnostic case worth reading back.
    Rendering it as "none of its None build(s) actually compiled" both
    lies about the cause and prints a null."""
    import logging

    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work=None)

    assert "no reduce advice for the build job" in caplog.text
    assert "no record of what it built" in caplog.text
    assert "reused their stamps" not in caplog.text
    assert "None build(s)" not in caplog.text

    # A pre-#495 envelope reads the same way: records, not compiles, is
    # what separates "nothing to do" from "nothing recorded".
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _build_advice(_REUSE_RUN, compile_work={"records": 0, "compiled": 0})

    assert "no record of what it built" in caplog.text
    assert "0 build(s)" not in caplog.text


# ------------------------------------------ the advice table's own notes


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


def test_the_compile_sim_note_only_appears_under_a_compile_sim_row(monkeypatch):
    """A note explaining a row the table does not contain is noise.

    The build job's row is `compile`, not `compile+sim`: it is a job that
    compiled and nothing else, so the "the peak spans both phases" line
    would be describing something absent.
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


def test_the_per_build_clause_only_appears_under_a_build_cpus_row(monkeypatch):
    """The cpus row is gated independently of the build-job row itself.

    A `reduce` on cpus needs low efficiency *and* evidence that a compile
    ran, so a build job routinely produces a `time` row and no `cpus` row.
    Explaining that the cpus suggestion is per-build under a table with no
    cpus column sends the reader looking for a number that is not there.
    """
    time_only = _rendered_metadata([_finding("compile")], monkeypatch)
    assert any("the compile row is the suite's build job" in line for line in time_only)
    assert not any("per-build" in line for line in time_only)

    with_cpus = _rendered_metadata(
        [_finding("compile"), _finding("compile", resource="cpus")], monkeypatch
    )
    assert any("cpus suggestion is per-build" in line for line in with_cpus)


# ---------------------------- #505: cpus advice is judged against the REQUEST


def test_whole_core_rounding_does_not_produce_cpus_advice():
    """A single-threaded test on a whole-core site is not over-reserved.

    `SelectTypeParameters=NONE` with `ThreadsPerCore=2` hands a job that
    asked for one cpu two of them, so sacct reports `AllocCPUS=2` against
    `ReqCPUS=1`. Judged against the allocation a single-threaded sim cannot
    beat 0.5 efficiency and every test in the suite is advised down to the
    `cpus: 1` its tests.yaml already says — advice no edit can retire
    (#505). The request is the number the project controls, so it is the
    number the ratio is taken against.
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
    """Rounding is the scheduler's; the reservation is doing fine.

    4 allocated against 2 requested, with 1.2 cpu-seconds per wall second:
    0.3 efficiency against the allocation (under the 0.5 threshold) but 0.6
    against the request. Only the second number is about a reservation
    anyone can edit.
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
    """Nothing rounded, nothing to excuse: 4 asked for, 4 given, 25% used."""
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
    """`reserved` has to be the number the named Field holds.

    8 allocated against 4 requested and a quarter of the request used: the
    advice is real, but a row saying `Reserved 8` sends the reader to a
    tests.yaml that says 4. The allocated figure is additive, so `sacct`
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
    """Older telemetry (and any backend that reports only what it gave)."""
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
    """Same rounding, same fix, for the suite's build job (#495 row)."""
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


# ------------- #505 review: prefer the reservation rtl_buddy itself submitted


def test_the_configured_request_beats_what_the_scheduler_reports():
    """`--cpus-per-task` is the request; ReqCPUS is only a report of it.

    A Slurm that normalizes `ReqCPUS` to the post-rounding figure would put
    the allocation back in the denominator by another route. The head knows
    what it submitted, so it says so, and the advice stays site-independent:
    a `cpus: 1` test on a whole-core node is never advised down to 1.
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
    """No `ReqCPUS` in telemetry at all — the head's own number carries it."""
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


# ------------ #505 review: sbatch-args can supersede the resolved reservation


def test_a_cpus_override_in_sbatch_args_withdraws_the_configured_request():
    """`cfg-dispatch.sbatch-args` is appended last, so it wins.

    A `--cpus-per-task` written there means the reservation the head
    resolved was never submitted, so it may state neither the ratio nor the
    decomposition — both fall back to what the scheduler reports. The head
    detects the override and simply does not offer its number.
    """
    telemetry = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 7200,
        "alloc_cpus": 8,
        "req_cpus": 8,  # what the override actually asked for
        "total_cpu_s": 200,
    }
    # Without the override the configured 2 would be the denominator, and
    # 200 / (100 x 2) = 1.0 is a saturated job with nothing to advise.
    assert [
        f for f in _build_advice(telemetry, parallel=1, cpus=2) if f.resource == "cpus"
    ] == []

    (cpus_a,) = [
        f
        for f in _build_advice(
            telemetry, parallel=1, cpus=2, cpus_override="--cpus-per-task=8"
        )
        if f.resource == "cpus"
    ]
    # 200 / (100 x 8) = 0.25 against the 8 the override submitted.
    assert cpus_a.reserved == "8"
    assert cpus_a.allocated is None
    assert cpus_a.utilization == 0.25
    assert cpus_a.suggested == "3"  # ceil(8 x 0.25 x 1.5)
    # ...and the hint names the argument, not the field it masks: editing
    # `compile.cpus` would not move the next job's reservation, so the
    # finding would come back — the shape #505 is about.
    assert cpus_a.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpus_a.edit_hint["file"] == "root_config.yaml"
    note = cpus_a.edit_hint["note"]
    assert "sbatch-args `--cpus-per-task=8` supersedes" in note
    assert "cfg-dispatch.compile.cpus" in note
    # The superseded decomposition is gone with it.
    assert "the build job reserved" not in note

    # time advice is unaffected: `--cpus-per-task` masks nothing there.
    (time_a,) = [
        f
        for f in _build_advice(
            telemetry, parallel=1, cpus=2, cpus_override="--cpus-per-task=8"
        )
        if f.resource == "time"
    ]
    assert time_a.edit_hint["path"] == "cfg-dispatch.compile.time"


def test_a_test_row_falls_back_when_the_head_records_no_request():
    """The head's half of the same guard: it records nothing (#505 review).

    `analyze_suite_reservations` needs no flag — a row whose
    `requested_cpus` is absent is exactly the "ask the scheduler" case,
    which is also what pre-#505 telemetry looks like.
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


# --------- #505 review: an override retargets the cpus edit hint too


def test_a_cpus_override_retargets_the_per_test_edit_hint():
    """Naming a masked field is advice that cannot be applied.

    `sbatch-args` wins over the generated `--cpus-per-task`, so editing
    `tests[name=t].resources.cpus` leaves the next job's reservation exactly
    where it was and the finding returns on the following run — the
    non-retiring shape #505 exists to stop. The hint names the argument
    instead, and says which field it supersedes.
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
            cpus_override="--cpus-per-task=4",
        )
    ]
    findings = _analyze(rows, root_config_path="root_config.yaml")
    (cpu,) = [f for f in findings if f.resource == "cpus"]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert cpu.edit_hint["file"] == "root_config.yaml"
    note = cpu.edit_hint["note"]
    assert "sbatch-args `--cpus-per-task=4` supersedes" in note
    assert "tests[name=t].resources.cpus" in note

    # Only cpus is masked: mem and time still name the fields that govern
    # them, since `--cpus-per-task` supersedes neither.
    (mem,) = [f for f in findings if f.resource == "mem"]
    assert mem.edit_hint["path"] == "tests[name=t].resources.mem"
    assert "note" not in mem.edit_hint
    (time_f,) = [f for f in findings if f.resource == "time"]
    assert time_f.edit_hint["path"] == "tests[name=t].resources.time"


def test_an_overridden_in_job_compile_row_names_the_field_it_masks():
    """The note names whichever cpus field the layering would have chosen.

    For a job that compiles inside itself the compile reservation can win
    the `cpus` field, so the masked field is `cfg-dispatch.compile.cpus`
    (or the suite's own `compile.cpus`) rather than the test's `resources`.
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
            cpus_override="-c 4",
        )
    ]
    (cpu,) = [
        f
        for f in _analyze(rows, root_config_path="root_config.yaml")
        if f.resource == "cpus"
    ]
    assert cpu.edit_hint["path"] == "cfg-dispatch.sbatch-args"
    assert "supersedes cfg-dispatch.compile.cpus" in cpu.edit_hint["note"]

    # ...and the suite's own compile block when that is the layer that won.
    rows = [
        _row(
            "t",
            telemetry,
            compile_in_job=True,
            governed_by={"cpus": "compile"},
            cpus_override="-c 4",
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
    assert "supersedes compile.cpus" in cpu.edit_hint["note"]
