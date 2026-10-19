"""Tests for `global-route-repair`, `post-route-repair` and `mode: probe`.

Configuration is read through the platform and run readers; flows are rendered by the
OpenROAD backend; post-route rounds and probes run under the stub OpenROAD harness of
`test_pnr_checkpoints`, extended to report a slack per round.
"""

import json
import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import (
    _HARNESS,
    _backend,
    _events,
    _pdk,
    _pnr_cfg,
    _runs,
)

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PNR_STAGES, PnrConfigFile
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
)
from rtl_buddy.config.pnr_repair import (
    UNSET,
    GlobalRouteRepair,
    PnrMode,
    PostRouteRepair,
    RepairDesign,
    Reroute,
    read_global_route_repair,
    read_post_route_repair,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_openroad
from rtl_buddy.tools.pnr_openroad import (
    _DETAILED_ROUTE_CMD,
    _END_INCREMENTAL_CMD,
    _GLOBAL_ROUTE_CMD,
    OpenRoadPnr,
)

# -- configuration ---------------------------------------------------------------


def _platform(pdk=None, **kw):
    base = dict(
        name="pl",
        pdk="p",
        cts_buffer="BUF",
        routing_layers=PnrRoutingLayersFile(signal="metal2-metal6", clock="metal4"),
    )
    base.update(kw)
    pdk = pdk or _pdk(Path("/project"))
    return PnrPlatformConfig(PnrPlatformConfigFile(**base), lambda _name: pdk)


def test_no_repair_block_leaves_both_repairs_off():
    platform = _platform()
    assert platform.get_global_route_repair() is None
    assert platform.get_post_route_repair() is None


def test_a_platform_block_resolves_with_its_defaults():
    platform = _platform(
        global_route_repair={"setup-margin": 0.1},
        post_route_repair={"repair-design": {}, "hold-margin": 0.02},
    )
    assert platform.get_global_route_repair() == GlobalRouteRepair(
        setup_margin=0.1, reroute=Reroute.INCREMENTAL
    )
    assert platform.get_post_route_repair() == PostRouteRepair(
        repair_design=RepairDesign(), hold_margin=0.02, rounds=1, margin_step=0.0
    )


def test_a_run_overrides_the_platform_field_by_field():
    platform = _platform(
        global_route_repair={
            "repair-design": {"slew-margin": 20, "cap-margin": 10},
            "setup-margin": 0.1,
            "hold-margin": 0.05,
        },
        post_route_repair={"setup-margin": 0.1, "rounds": 2, "margin-step": 0.05},
    )
    run_gr = read_global_route_repair(
        "run",
        {"repair-design": {"cap-margin": 30}, "hold-margin": None, "reroute": "full"},
    )
    run_pr = read_post_route_repair("run", {"rounds": 4})

    resolved = platform.with_run_repairs(run_gr, run_pr, "r")

    assert resolved.get_global_route_repair() == GlobalRouteRepair(
        repair_design=RepairDesign(slew_margin=20.0, cap_margin=30.0),
        setup_margin=0.1,
        hold_margin=None,
        reroute=Reroute.FULL,
    )
    assert resolved.get_post_route_repair() == PostRouteRepair(
        setup_margin=0.1, rounds=4, margin_step=0.05
    )
    # The platform keeps its own.
    assert platform.get_global_route_repair().hold_margin == 0.05
    assert platform.get_post_route_repair().rounds == 2


def test_a_run_turns_a_platform_repair_off_with_null_and_on_without_one():
    platform = _platform(global_route_repair={"hold-margin": 0.0})
    off = platform.with_run_repairs(
        None, read_post_route_repair("run", {"setup-margin": 0.0}), "r"
    )
    assert off.get_global_route_repair() is None
    assert off.get_post_route_repair() == PostRouteRepair(setup_margin=0.0)
    assert platform.with_run_repairs(UNSET, UNSET, "r") is platform


def test_the_hold_repair_shorthand_is_a_hold_only_global_route_repair():
    platform = _platform(global_route_hold_repair=True)
    assert platform.get_global_route_repair() == GlobalRouteRepair(hold_margin=0.0)


def test_the_shorthand_and_the_block_together_are_refused():
    with pytest.raises(FatalRtlBuddyError) as exc:
        _platform(global_route_hold_repair=True, global_route_repair={"hold-margin": 0})
    assert "global-route-hold-repair" in str(exc.value)
    assert "global-route-repair" in str(exc.value)
    assert "'pl'" in str(exc.value)


def test_a_block_that_repairs_nothing_is_refused():
    with pytest.raises(FatalRtlBuddyError, match="post-route-repair repairs nothing"):
        _platform(post_route_repair={"rounds": 3})
    platform = _platform(global_route_repair={"setup-margin": 0.1})
    with pytest.raises(FatalRtlBuddyError, match="global-route-repair repairs nothing"):
        platform.with_run_repairs(
            read_global_route_repair("run", {"setup-margin": None}), UNSET, "r"
        )


@pytest.mark.parametrize(
    "block, words",
    [
        ({"setup-margin": -0.1}, "setup-margin must be a number >= 0"),
        ({"hold-margin": "x"}, "hold-margin must be a number"),
        ({"hold-margin": True}, "hold-margin must be a number"),
        (
            {"repair-design": {"slew-margin": 150}},
            "slew-margin must be a number from 0",
        ),
        (
            {"repair-design": {"max-wire-length": 0}},
            "max-wire-length must be a positive",
        ),
        ({"repair-design": 3}, "repair-design must be a mapping"),
        ({"setup-margin": 0, "rounds": 0}, "rounds must be a positive integer"),
        ({"setup-margin": 0, "rounds": 1.5}, "rounds must be a positive integer"),
        ({"setup-margin": 0, "margin-step": None}, "margin-step must be a number"),
    ],
)
def test_a_bad_post_route_value_is_refused_naming_the_key(block, words):
    with pytest.raises(FatalRtlBuddyError, match=words):
        _platform(post_route_repair=block)


def test_a_bad_reroute_is_refused():
    with pytest.raises(
        FatalRtlBuddyError, match="reroute must be one of incremental, full"
    ):
        _platform(global_route_repair={"hold-margin": 0, "reroute": "partial"})


def test_post_route_margins_step_each_round_after_the_first():
    repair = PostRouteRepair(
        setup_margin=0.1, hold_margin=None, rounds=3, margin_step=0.05
    )
    assert [repair.margins(r) for r in (1, 2, 3)] == [
        (0.1, None),
        (0.15, None),
        (0.2, None),
    ]


def test_mode_defaults_to_closure_and_rejects_others(tmp_path):
    def _run(**kw):
        return PnrConfigFile(
            name="r", desc="r", synth="s", synth_path="synth.yaml", platform="p", **kw
        ).initialise(str(tmp_path))

    assert _run().get_mode() is PnrMode.CLOSURE
    assert _run(mode="probe").get_mode() is PnrMode.PROBE
    with pytest.raises(FatalRtlBuddyError, match="mode must be one of closure, probe"):
        _run(mode="explore")


# -- rendering -------------------------------------------------------------------


def _render(tmp_path, platform, *, ps_per_unit=1.0, **overrides) -> str:
    """The one-session `pnr.tcl` `platform` and the run's `overrides` render."""
    pnr_cfg = replace(_pnr_cfg(tmp_path, ()), **overrides)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = ps_per_unit
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


def _real_platform(tmp_path, **kw):
    return _platform(_pdk(tmp_path), **kw)


def test_a_flow_without_repair_knobs_renders_as_before(tmp_path):
    """The template with its placeholders filled as the knobs' absence fills them is the flow."""
    text = _render(tmp_path, _real_platform(tmp_path))
    assert (
        '\nputs ">>> Post-CTS timing repair"\n'
        "estimate_parasitics -placement\n"
        "repair_timing -hold\n"
    ) in text
    assert "Global-route repair" not in text
    assert "post_route_repair" not in text


def test_the_template_runs_the_commands_a_repair_runs_again():
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    assert _GLOBAL_ROUTE_CMD in template
    assert _DETAILED_ROUTE_CMD in template


@pytest.mark.parametrize(
    "ps_per_unit, setup, hold, unit",
    [(1.0, "200", "50", "1ps"), (1000.0, "0.2", "0.05", "1ns")],
)
def test_global_route_repair_passes_margins_in_the_liberty_unit(
    tmp_path, ps_per_unit, setup, hold, unit
):
    platform = _real_platform(
        tmp_path,
        global_route_repair={
            "repair-design": {"slew-margin": 20, "max-wire-length": 500},
            "setup-margin": 0.2,
            "hold-margin": 0.05,
        },
    )
    text = _render(tmp_path, platform, ps_per_unit=ps_per_unit)
    assert (
        _GLOBAL_ROUTE_CMD + 'puts ">>> Global-route repair (reroute incremental)"\n'
        "estimate_parasitics -global_routing\n"
        'puts ">>> Global-route repair: repair_design -slew_margin 20 '
        '-max_wire_length 500"\n'
        "repair_design -slew_margin 20 -max_wire_length 500\n"
        f'puts ">>> Global-route repair: repair_timing -setup -setup_margin {setup} '
        f'(setup margin 0.2 ns in Liberty time unit {unit})"\n'
        f"repair_timing -setup -setup_margin {setup}\n"
        f'puts ">>> Global-route repair: repair_timing -hold -hold_margin {hold} '
        f'(hold margin 0.05 ns in Liberty time unit {unit})"\n'
        f"repair_timing -hold -hold_margin {hold}\n"
        "global_route -start_incremental\n"
        "detailed_placement\n" + _END_INCREMENTAL_CMD + "check_placement -verbose\n"
    ) in text
    # The global-route checkpoint is taken after the repair.
    assert text.index(
        "check_placement -verbose\n", text.index("Global-route repair")
    ) < (text.index(">>> Detail route"))


def test_a_full_reroute_routes_again_after_legalizing(tmp_path):
    platform = _real_platform(
        tmp_path, global_route_repair={"setup-margin": 0, "reroute": "full"}
    )
    text = _render(tmp_path, platform)
    assert (
        'puts ">>> Global-route repair: repair_timing -setup (setup margin 0 ns)"\n'
        "repair_timing -setup\n"
        "detailed_placement\n"
        "check_placement -verbose\n"
        'puts ">>> Global-route repair: global route"\n' + _GLOBAL_ROUTE_CMD
    ) in text
    assert "-start_incremental" not in text


def test_a_run_repair_overrides_the_platform_in_the_flow(tmp_path):
    platform = _real_platform(tmp_path, global_route_hold_repair=True)
    gr = read_global_route_repair("run", {"setup-margin": 0.1})
    text = _render(tmp_path, platform.with_run_repairs(gr, UNSET, "r"))
    setup = text.index("repair_timing -setup -setup_margin 100\n")
    assert setup < text.index("repair_timing -hold\n", setup)


def test_a_probe_skips_timing_repair_and_stops_after_global_route(tmp_path):
    platform = _real_platform(
        tmp_path,
        post_cts_setup_repair=True,
        global_route_repair={"hold-margin": 0.1},
        post_route_repair={"setup-margin": 0.1},
    )
    text = _render(tmp_path, platform, mode=PnrMode.PROBE)
    assert not re.search(r"^repair_timing", text, re.MULTILINE)
    assert 'puts ">>> Post-CTS timing repair: skipped (mode: probe)"' in text
    assert 'puts ">>> Global-route repair: skipped (mode: probe)"' in text
    # Electrical repair and legalization still run.
    assert "\nrepair_design\n" in text
    assert "repair_tie_fanout" in text
    assert text.count("detailed_placement") >= 2
    assert "rb::ckpt::stage_end global_route stop" in text
    assert (
        "detailed_route" not in text.split("rb::ckpt::stage_end global_route stop")[1]
    )
    assert pnr_openroad.STOPPED_MARKER + "global_route" in text


# -- runs under the stub harness -------------------------------------------------

# Each round's extraction session reports the round's setup slack from
# `RB_STUB_SETUP` (one value per round, in the Liberty unit) and a hold slack
# of 0; `RB_STUB_DRC=<round>` makes that round's detailed route report a violation.
_ROUND_STUBS = r"""
proc rb_round {} {
    global argv
    if {[regexp {\.r(\d+)\.(extract|repair)\.tcl$} [lindex $argv 0] -> round]} {
        return $round
    }
    return 0
}
proc report_worst_slack {args} {
    global env
    set round [rb_round]
    if {$round == 0 || ![info exists env(RB_STUB_SETUP)]} { return }
    if {[lsearch $args -max] >= 0} {
        puts "worst slack max [lindex $env(RB_STUB_SETUP) [expr {$round - 1}]]"
    } else {
        puts "worst slack min 0.0"
    }
}
proc report_tns {args} {
    if {[rb_round] == 0} { return }
    if {[lsearch $args -min] >= 0} { puts "tns min 0.0" } else { puts "tns max -1.5" }
}
proc remove_fillers {args} { rb_stub remove_fillers }
proc detailed_route {args} {
    global env
    rb_stub detailed_route
    set i [lsearch $args -output_drc]
    if {$i >= 0 && [info exists env(RB_STUB_DRC)] && $env(RB_STUB_DRC) == [rb_round]} {
        rb_write [lindex $args [expr {$i + 1}]] "violation"
    }
}
"""


def _rcx_pdk(tmp_path) -> PdkConfig:
    (tmp_path / "pdk").mkdir(exist_ok=True)
    (tmp_path / "pdk" / "rcx.rules").write_text("rules\n")
    base = _pdk(tmp_path)
    return PdkConfig(
        PdkConfigFile(
            name="nangate45",
            site=base.get_site(),
            corners={"typ": "pdk/lib/typ.lib"},
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
            tie_hi="LOGIC1_X1/Z",
            tie_lo="LOGIC0_X1/Z",
            fill_cells=["FILLCELL_X1"],
            klayout_tech="pdk/klayout/tech.lyt",
            cell_gds="pdk/gds/cells.gds",
            rcx_rules="pdk/rcx.rules",
        ),
        str(tmp_path / "root_config.yaml"),
    )


def _with_repair(backend, tmp_path, *, post_route=None, rcx=True) -> None:
    """Give a stub-harness backend a platform whose `post-route-repair` is `post_route`, and the round stubs."""
    pdk = _rcx_pdk(tmp_path) if rcx else _pdk(tmp_path)
    extra = {"post_route_repair": post_route} if post_route is not None else {}
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk, **extra)
    (tmp_path / "harness.tcl").write_text(
        _HARNESS.replace(
            "source [lindex $argv 0]", _ROUND_STUBS + "source [lindex $argv 0]"
        )
    )


def _repair_backend(
    tmp_path,
    monkeypatch,
    *,
    post_route=None,
    mode=PnrMode.CLOSURE,
    rcx=True,
    stages=False,
    env=None,
    **options,
):
    """A stub-harness backend whose platform sets `post_route` as its `post-route-repair`."""
    pnr_cfg = replace(_pnr_cfg(tmp_path, stages=stages), mode=mode)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=pnr_cfg, env=env, stages=stages, **options
    )
    _with_repair(backend, tmp_path, post_route=post_route, rcx=rcx)
    return backend, artefacts


def _rounds(run_dir) -> list[dict]:
    return [e for e in _events(run_dir) if e["event"] == "post_route_round"]


def _done(run_dir) -> dict:
    (event,) = [e for e in _events(run_dir) if e["event"] == "post_route_repair_done"]
    return event


_PR = {"setup-margin": 0.1, "rounds": 3, "margin-step": 0.05}


@pytest.mark.parametrize("stages", [False, True])
def test_rounds_repair_until_timing_is_met(tmp_path, monkeypatch, stages):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=stages,
        env={"RB_STUB_SETUP": "-0.2 -0.1 0.01"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is True
    (run_dir,) = _runs(artefacts)
    rounds = _rounds(run_dir)
    assert [r["round"] for r in rounds] == [1, 2, 3]
    assert [r["repair"] for r in rounds] == [True, True, False]
    # The default Liberty unit is 1ns; the margin steps each round.
    assert [r["setup_margin_ns"] for r in rounds] == [0.1, 0.15, None]
    assert [r["wns_setup_ps"] for r in rounds] == [-200.0, -100.0, 10.0]
    assert _done(run_dir)["repaired_rounds"] == 2
    assert _done(run_dir)["reason"] == "timing met"
    assert (run_dir / "06_post_route_repair.r2.repair.log").is_file()
    assert not (run_dir / "06_post_route_repair.r3.repair.log").exists()
    r2 = (run_dir / "06_post_route_repair.r2.repair.tcl").read_text()
    assert "repair_timing -setup -setup_margin 0.15\n" in r2
    assert "remove_fillers\n" in r2
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.odb]" in r2
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 05_detailed_route.odb]" in r1
    repair = r1.index("\nrepair_timing -setup")
    assert r1.index("global_route -start_incremental") < repair
    assert repair < r1.index("global_route -end_incremental")
    assert r1.index("global_route -end_incremental") < r1.index("\ndetailed_route \\")
    log = (artefacts / "pnr.log").read_text()
    assert (
        ">>> Post-route repair round 2: repair_timing -setup -setup_margin 0.15 " in log
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["checkpoints"]
    assert "post_route_repair" in manifest["resources"]
    ended = [e["stage"] for e in _events(run_dir) if e["event"] == "stage_end"]
    assert ended[-2:] == ["post_route_repair", "finish"]
    # Finish starts from the last repair.
    finish = (run_dir / "07_finish.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 06_post_route_repair.odb]" in finish


def test_a_dispatched_run_runs_every_round_in_the_stage_job(tmp_path, monkeypatch):
    from test_pnr_dispatch import _dispatched

    monkeypatch.setenv("RB_STUB_SETUP", "-0.2 0.01")
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    _with_repair(backend, tmp_path, post_route=_PR)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    stages = [job["stage"] for job in slurm.submitted()]
    assert stages == list(PNR_STAGES)
    (run_dir,) = _runs(artefacts)
    assert [r["repair"] for r in _rounds(run_dir)] == [True, False]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["dispatch"]["stages"]
    assert "post_route_repair" in manifest["resources"]


def test_rounds_stop_after_the_last_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 2},
        env={"RB_STUB_SETUP": "-0.2 -0.1"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert [r["repair"] for r in _rounds(run_dir)] == [True, True]
    assert _done(run_dir)["repaired_rounds"] == 2
    assert _done(run_dir)["reason"] == "2 round(s) repaired"


def test_met_timing_keeps_the_detailed_route_as_the_stage_checkpoint(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, env={"RB_STUB_SETUP": "0.0"}
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert [r["repair"] for r in _rounds(run_dir)] == [False]
    assert _done(run_dir)["repaired_rounds"] == 0
    assert not (run_dir / "06_post_route_repair.r1.repair.log").exists()
    odb = run_dir / "06_post_route_repair.odb"
    assert odb.read_bytes() == (run_dir / "05_detailed_route.odb").read_bytes()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["checkpoints"]


def test_unreadable_timing_stops_the_rounds(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(tmp_path, monkeypatch, post_route=_PR)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (only,) = _rounds(run_dir)
    assert only["wns_setup_ps"] is None and only["repair"] is False
    assert _done(run_dir)["reason"] == "timing was not reported"


def test_a_round_with_drc_violations_stops_the_rounds(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.1", "RB_STUB_DRC": "1"},
    )

    res = backend.run()

    (run_dir,) = _runs(artefacts)
    assert [r["round"] for r in _rounds(run_dir)] == [1]
    assert _done(run_dir)["repaired_rounds"] == 1
    assert "DRC" in _done(run_dir)["reason"]
    assert res.results["drc_count"] == 1


def test_a_failed_repair_session_fails_the_stage(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.1", "RB_STUB_FAIL": "remove_fillers"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert "06_post_route_repair" in res.results["desc"]
    (run_dir,) = _runs(artefacts)
    ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
    assert ended[-1]["stage"] == "post_route_repair"
    assert ended[-1]["status"] == "error"
    assert not [e for e in _events(run_dir) if e["event"] == "post_route_repair_done"]


def test_stop_after_post_route_repair_reports_on_extracted_parasitics(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 0.0"},
        stop_after="post_route_repair",
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["stopped_after"] == "post_route_repair"
    (run_dir,) = _runs(artefacts)
    report = (run_dir / "06_post_route_repair.report.tcl").read_text()
    assert "extract_parasitics" in report
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.odb]" in report
    assert not (run_dir / "07_finish.tcl").exists()


def test_post_route_repair_without_rcx_rules_is_a_setup_error(tmp_path, monkeypatch):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, rcx=False
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "rcx-rules" in res.results["desc"]


def test_stop_after_post_route_repair_needs_the_stage(tmp_path, monkeypatch):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, stop_after="post_route_repair"
    )

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "post_route_repair" in res.results["desc"]


def test_a_probe_passes_unrouted_with_its_global_route_checkpoint(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, mode=PnrMode.PROBE
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["mode"] == "probe"
    assert res.results["stopped_after"] == "global_route"
    assert res.results["desc"].startswith("P&R probe stopped after global_route")
    assert not (artefacts / "demo_top.def").exists()
    assert not (artefacts / "demo_top.routed.odb").exists()
    (run_dir,) = _runs(artefacts)
    assert (run_dir / "04_global_route.odb").is_file()
    assert not (run_dir / "05_detailed_route.odb").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["mode"] == "probe"
    flow = Path(backend._script_path()).read_text()
    assert not re.search(r"^repair_timing", flow, re.MULTILINE)


def test_a_probe_cannot_stop_after_detailed_route(tmp_path, monkeypatch):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, mode=PnrMode.PROBE, stop_after="detailed_route"
    )

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "probe" in res.results["desc"]


def test_a_probe_cannot_be_resumed_into_a_closure_run(tmp_path, monkeypatch):
    probe, artefacts = _repair_backend(
        tmp_path, monkeypatch, mode=PnrMode.PROBE, stages=True
    )
    assert probe.run().results["result"] == "PASS"
    (probe_dir,) = _runs(artefacts)

    closure, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=str(probe_dir),
        resume_from="detailed_route",
    )
    res = closure.run()

    assert res.results["result"] == "FAIL"
    assert "probe" in res.results["desc"]
    assert "closure" in res.results["desc"]
    assert len(_runs(artefacts)) == 1


def test_the_abstract_digest_keeps_the_shorthand_and_names_other_repairs(tmp_path):
    from rtl_buddy.tools import pnr_abstract

    pnr_cfg = _pnr_cfg(tmp_path, ())
    shorthand = pnr_abstract.abstract_config(
        pnr_cfg, _real_platform(tmp_path, global_route_hold_repair=True)
    )
    assert shorthand["global_route_hold_repair"] is True
    assert "global_route_repair" not in shorthand
    plain = pnr_abstract.abstract_config(pnr_cfg, _real_platform(tmp_path))
    assert "global_route_hold_repair" not in plain
    assert "post_route_repair" not in plain
    both = pnr_abstract.abstract_config(
        pnr_cfg,
        _real_platform(
            tmp_path,
            global_route_repair={"setup-margin": 0.1, "reroute": "full"},
            post_route_repair={"hold-margin": 0},
        ),
    )
    assert both["global_route_repair"]["reroute"] == "full"
    assert both["post_route_repair"]["hold_margin"] == 0.0
    json.dumps(both)
