"""OpenROAD thread count: validation, allocation cap, emitted Tcl (#654)."""

import logging
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config import openroad_threads
from rtl_buddy.config.openroad_threads import (
    AFFINITY_SOURCE,
    AUTO,
    detect_allocation,
    parse_reported_threads,
    plan_threads,
    validate_threads,
)
from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PnrConfig, PnrFloorplan, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError

_NO_ALLOCATION = (None, None)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 1, 8, 128, AUTO])
def test_validate_accepts_positive_ints_auto_and_unset(value):
    assert validate_threads(value, where="pnr run 'x'") == value


@pytest.mark.parametrize(
    "value",
    # 0 is OpenROAD's spelling of "every host core"; a bool is an int to
    # Python; "8" is a quoted string, "max" OpenROAD's own keyword.
    [0, -1, -8, True, False, "8", "max", "AUTO", "", 2.0],
)
def test_validate_rejects_everything_else(value):
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'x': 'threads' must be"):
        validate_threads(value, where="pnr run 'x'")


def _pnr_yaml(tmp_path, threads_line):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent(
            f"""\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: demo_pnr
                desc: demo
                synth: demo_synth
                synth-path: synth.yaml
                constraints: c.sdc
                platform: nangate45_typ
            {threads_line}
            """
        )
    )
    return path


def test_pnr_yaml_threads_defaults_to_unset(tmp_path):
    run = PnrSuiteConfig(str(_pnr_yaml(tmp_path, ""))).get_runs("demo_pnr")[0]
    assert run.get_threads() is None


@pytest.mark.parametrize("raw, expected", [("4", 4), ("auto", AUTO)])
def test_pnr_yaml_threads_round_trips(tmp_path, raw, expected):
    path = _pnr_yaml(tmp_path, f"    threads: {raw}")
    run = PnrSuiteConfig(str(path)).get_runs("demo_pnr")[0]
    assert run.get_threads() == expected


@pytest.mark.parametrize("raw", ["0", "-2", "true", '"4"', "max", "1.5"])
def test_pnr_yaml_invalid_threads_fail_loading(tmp_path, raw):
    path = _pnr_yaml(tmp_path, f"    threads: {raw}")
    with pytest.raises(FatalRtlBuddyError):
        PnrSuiteConfig(str(path))


def test_power_and_synth_entries_validate_threads_too(tmp_path):
    from rtl_buddy.config.power import PowerConfigFile
    from rtl_buddy.config.synth import SynthConfigFile

    power = PowerConfigFile(
        name="p",
        desc="d",
        synth="s",
        synth_path="synth.yaml",
        platform="nangate45_typ",
        threads=0,
    )
    with pytest.raises(FatalRtlBuddyError, match="power run 'p'"):
        power.initialise(str(tmp_path))
    power.threads = 2
    assert power.initialise(str(tmp_path)).get_threads() == 2

    synth = SynthConfigFile(
        name="s",
        desc="d",
        model="m",
        model_path="models.yaml",
        tool="openroad",
        threads=True,
    )
    # Rejected before the model is loaded, so no models.yaml is needed.
    with pytest.raises(FatalRtlBuddyError, match="synthesis 's'"):
        synth.initialise(str(tmp_path))


# ---------------------------------------------------------------------------
# Allocation detection
# ---------------------------------------------------------------------------


def test_no_scheduler_and_full_affinity_is_no_allocation():
    assert detect_allocation({}, affinity=12, cpu_count=12) == _NO_ALLOCATION
    assert detect_allocation({}, affinity=None, cpu_count=12) == _NO_ALLOCATION


def test_slurm_cpus_per_task_is_the_allocation():
    env = {"SLURM_JOB_ID": "42", "SLURM_CPUS_PER_TASK": "4", "SLURM_CPUS_ON_NODE": "8"}
    assert detect_allocation(env) == (4, "SLURM_CPUS_PER_TASK")


def test_slurm_cpus_on_node_without_a_per_task_count():
    env = {"SLURM_JOB_ID": "42", "SLURM_CPUS_ON_NODE": "8"}
    assert detect_allocation(env) == (8, "SLURM_CPUS_ON_NODE")


def test_malformed_slurm_value_falls_through_to_the_next():
    env = {
        "SLURM_JOB_ID": "42",
        "SLURM_CPUS_PER_TASK": "4(x2)",
        "SLURM_CPUS_ON_NODE": "6",
    }
    assert detect_allocation(env) == (6, "SLURM_CPUS_ON_NODE")


def test_slurm_variables_outside_a_job_are_ignored():
    """A leaked `SLURM_CPUS_PER_TASK` with no job is not an allocation."""
    assert detect_allocation({"SLURM_CPUS_PER_TASK": "4"}) == _NO_ALLOCATION


def test_a_restricted_affinity_mask_is_an_allocation():
    assert detect_allocation({}, affinity=3, cpu_count=12) == (3, AFFINITY_SOURCE)


def test_the_smaller_of_slurm_and_affinity_wins():
    env = {"SLURM_JOB_ID": "42", "SLURM_CPUS_PER_TASK": "8"}
    assert detect_allocation(env, affinity=2, cpu_count=64) == (2, AFFINITY_SOURCE)
    env["SLURM_CPUS_PER_TASK"] = "2"
    assert detect_allocation(env, affinity=6, cpu_count=64) == (
        2,
        "SLURM_CPUS_PER_TASK",
    )


def test_live_detection_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "7")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "5")
    monkeypatch.setattr(openroad_threads, "_affinity_count", lambda: None)
    assert detect_allocation() == (5, "SLURM_CPUS_PER_TASK")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _capture_events(monkeypatch):
    events: list[tuple[int, str, dict]] = []
    real = openroad_threads.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(openroad_threads, "log_event", _record)
    return events


def test_unset_emits_nothing_and_records_one_thread(monkeypatch):
    events = _capture_events(monkeypatch)
    plan = plan_threads(None, flow="pnr", run="r", allocation=(8, "SLURM_CPUS_ON_NODE"))
    assert plan.tcl() == ""
    assert plan.count == 1
    # An allocation alone never raises the count: unset stays deterministic.
    assert plan.fields() == {
        "requested": None,
        "effective": 1,
        "allocation": 8,
        "allocation_source": "SLURM_CPUS_ON_NODE",
    }
    assert [e[1] for e in events] == ["openroad.threads"]


def test_explicit_count_without_an_allocation_is_honoured():
    plan = plan_threads(16, flow="pnr", run="r", allocation=_NO_ALLOCATION)
    assert plan.tcl() == "set_thread_count 16"
    assert not plan.capped


def test_explicit_count_within_the_allocation_is_honoured(monkeypatch):
    events = _capture_events(monkeypatch)
    plan = plan_threads(4, flow="pnr", run="r", allocation=(4, "SLURM_CPUS_PER_TASK"))
    assert plan.tcl() == "set_thread_count 4"
    assert not [e for e in events if e[1] == "openroad.threads_capped"]


def test_explicit_count_above_the_allocation_is_clamped_with_a_warning(monkeypatch):
    events = _capture_events(monkeypatch)
    plan = plan_threads(8, flow="pnr", run="r", allocation=(4, "SLURM_CPUS_PER_TASK"))
    assert plan.tcl() == "set_thread_count 4"
    assert plan.capped
    assert plan.fields()["requested"] == 8
    assert plan.fields()["effective"] == 4
    capped = [(lvl, f) for lvl, e, f in events if e == "openroad.threads_capped"]
    assert capped == [
        (
            logging.WARNING,
            {
                "flow": "pnr",
                "run": "r",
                "requested": 8,
                "allocation": 4,
                "allocation_source": "SLURM_CPUS_PER_TASK",
            },
        )
    ]


def test_auto_follows_the_allocation():
    plan = plan_threads(AUTO, flow="pnr", run="r", allocation=(6, AFFINITY_SOURCE))
    assert plan.tcl() == "set_thread_count 6"
    assert plan.fields()["requested"] == AUTO


def test_auto_without_an_allocation_is_one_thread_not_every_core():
    plan = plan_threads(AUTO, flow="pnr", run="r", allocation=_NO_ALLOCATION)
    assert plan.tcl() == "set_thread_count 1"


def test_openroads_own_report_wins_as_the_effective_count():
    """OpenROAD clamps to hardware concurrency by itself (ORD-0030)."""
    plan = plan_threads(64, flow="pnr", run="r", allocation=_NO_ALLOCATION)
    assert plan.fields(reported=12)["effective"] == 12


def test_parse_reported_threads_takes_the_last_report():
    log = (
        "OpenROAD 26Q2\n"
        "[INFO ORD-0030] Using 8 thread(s).\n"
        "[INFO ORD-0030] Using 4 thread(s).\n"
    )
    assert parse_reported_threads(log) == 4
    assert parse_reported_threads("no such line\n") is None


# ---------------------------------------------------------------------------
# rb pnr: emitted Tcl and recorded provenance
# ---------------------------------------------------------------------------


def _platform(tmp_path):
    pdk = PdkConfig(
        PdkConfigFile(
            name="nangate45",
            site="FreePDK45_38x28_10R_NP_162NW_34O",
            corners={"typ": "pdk/lib/typ.lib"},
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
            tie_hi="LOGIC1_X1/Z",
            tie_lo="LOGIC0_X1/Z",
            fill_cells=["FILLCELL_X1"],
        ),
        str(tmp_path / "root_config.yaml"),
    )
    return PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ", pdk="nangate45", cts_buffer="BUF_X4"
        ),
        lambda _name: pdk,
    )


def _backend(tmp_path, threads, root_cfg=None):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    cfg = PnrConfig(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(tmp_path / "constraints.sdc"),
        platform="nangate45_typ",
        floorplan=PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=1000,
        tool_overrides=None,
        threads=threads,
    )
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    synth.get_name.return_value = "demo_synth"
    cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    return OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=root_cfg or MagicMock(),
    )


@pytest.fixture
def no_allocation(monkeypatch):
    """A process in no allocation, whatever host the suite runs on."""
    for var in ("SLURM_JOB_ID", "SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(openroad_threads, "_affinity_count", lambda: None)


def _script(tmp_path, threads):
    backend = _backend(tmp_path, threads)
    platform = _platform(tmp_path)
    return Path(
        backend._write_script(platform, backend.pnr_cfg.get_floorplan())
    ).read_text()


def test_unset_threads_leaves_the_pnr_script_unchanged(tmp_path, no_allocation):
    text = _script(tmp_path, None)
    assert "set_thread_count" not in text
    # The placeholder line collapses to the blank line that was always
    # there, so the script is byte-identical to the pre-#654 one.
    assert 'file mkdir $OUT_DIR\n\nputs ">>> Reading Liberty + LEF"' in text


def test_configured_threads_precede_the_first_liberty_read(tmp_path, no_allocation):
    text = _script(tmp_path, 4)
    assert (
        'file mkdir $OUT_DIR\n\nputs ">>> Threads"\nset_thread_count 4\n\n'
        'puts ">>> Reading Liberty + LEF"' in text
    )
    assert text.index("set_thread_count 4") < text.index("read_liberty")
    assert "{{" not in text


def test_pnr_script_is_clamped_to_a_slurm_allocation(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "2")
    monkeypatch.setattr(openroad_threads, "_affinity_count", lambda: None)
    assert "set_thread_count 2\n" in _script(tmp_path, 8)


def _run(tmp_path, monkeypatch, threads, log):
    from rtl_buddy.tools import pnr_openroad

    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _platform(tmp_path)
    backend = _backend(tmp_path, threads, root_cfg=root_cfg)
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **k: nullcontext())
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)

    def _fake_run(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text(log)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)
    return backend.run()


def test_pnr_pass_records_requested_and_reported_threads(
    tmp_path, monkeypatch, no_allocation
):
    res = _run(tmp_path, monkeypatch, 4, "[INFO ORD-0030] Using 4 thread(s).\n")
    assert res.results["result"] == "PASS"
    assert res.results["openroad_threads"] == {
        "requested": 4,
        "effective": 4,
        "allocation": None,
        "allocation_source": None,
    }


def test_pnr_pass_without_threads_records_the_default(
    tmp_path, monkeypatch, no_allocation
):
    res = _run(tmp_path, monkeypatch, None, "")
    assert res.results["openroad_threads"]["requested"] is None
    assert res.results["openroad_threads"]["effective"] == 1


def test_pnr_failure_after_openroad_still_records_threads(
    tmp_path, monkeypatch, no_allocation
):
    res = _run(
        tmp_path,
        monkeypatch,
        2,
        "[INFO ORD-0030] Using 2 thread(s).\n[ERROR DRT-0073] no access point\n",
    )
    assert res.results["result"] == "FAIL"
    assert res.results["openroad_threads"]["effective"] == 2


def test_pnr_machine_row_carries_openroad_threads():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    threads = {
        "requested": 8,
        "effective": 4,
        "allocation": 4,
        "allocation_source": "SLURM_CPUS_PER_TASK",
    }
    res = PnrPassResults(name="demo/results", fields={"openroad_threads": threads})
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": res})
    assert row["openroad_threads"] == threads


def test_a_count_in_the_log_is_ignored_when_none_was_set():
    """With nothing emitted OpenROAD logs no count: one in the log is stale."""
    plan = plan_threads(None, flow="pnr", run="r", allocation=_NO_ALLOCATION)
    assert plan.fields(reported=8)["effective"] == 1
