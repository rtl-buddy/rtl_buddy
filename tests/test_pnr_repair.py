"""Tests for `global-route-repair`, `post-route-repair` and `mode: probe`.

Configuration is read through the platform and run readers; flows are rendered by the
OpenROAD backend; post-route rounds and probes run under the stub OpenROAD harness of
`test_pnr_checkpoints`, extended to report a slack per round.
"""

import json
import logging
import os
import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_macro_pack import _tcl_eval
from test_pnr_checkpoints import (
    _HARNESS,
    _backend,
    _events,
    _pdk,
    _pnr_cfg,
    _runs,
)

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PNR_STAGES, PnrConfigFile
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
    PostRouteReroute,
    RepairDesign,
    Reroute,
    read_global_route_repair,
    read_post_route_repair,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.rtl_buddy import RtlBuddy
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
        repair_design=RepairDesign(),
        hold_margin=0.02,
        rounds=1,
        margin_step=0.0,
        reroute=PostRouteReroute.ECO,
    )


def test_a_run_overrides_the_platform_field_by_field():
    platform = _platform(
        global_route_repair={
            "repair-design": {"slew-margin": 20, "cap-margin": 10},
            "setup-margin": 0.1,
            "hold-margin": 0.05,
        },
        post_route_repair={
            "setup-margin": 0.1,
            "rounds": 2,
            "margin-step": 0.05,
            "reroute": "full",
        },
    )
    run_gr = read_global_route_repair(
        "run",
        {"repair-design": {"cap-margin": 30}, "hold-margin": None, "reroute": "full"},
    )
    run_pr = read_post_route_repair("run", {"rounds": 4, "reroute": "eco"})

    resolved = platform.with_run_repairs(run_gr, run_pr, "r")

    assert resolved.get_global_route_repair() == GlobalRouteRepair(
        repair_design=RepairDesign(slew_margin=20.0, cap_margin=30.0),
        setup_margin=0.1,
        hold_margin=None,
        reroute=Reroute.FULL,
    )
    assert resolved.get_post_route_repair() == PostRouteRepair(
        setup_margin=0.1, rounds=4, margin_step=0.05, reroute=PostRouteReroute.ECO
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
    alone = platform.with_run_repairs(UNSET, UNSET, "r")
    assert alone.get_global_route_repair() == platform.get_global_route_repair()


def test_the_hold_repair_shorthand_is_a_hold_only_global_route_repair():
    platform = _platform(global_route_hold_repair=True)
    assert platform.get_global_route_repair() == GlobalRouteRepair(hold_margin=0.0)


def test_the_shorthand_and_the_block_together_are_refused():
    with pytest.raises(FatalRtlBuddyError) as exc:
        _platform(global_route_hold_repair=True, global_route_repair={"hold-margin": 0})
    assert "global-route-hold-repair" in str(exc.value)
    assert "global-route-repair" in str(exc.value)
    assert "'pl'" in str(exc.value)


def test_a_block_that_repairs_nothing_is_refused_where_a_run_uses_it():
    platform = _platform(post_route_repair={"rounds": 1})
    with pytest.raises(
        FatalRtlBuddyError,
        match="pnr run 'r' on platform 'pl': post-route-repair repairs nothing",
    ):
        platform.with_run_repairs(UNSET, UNSET, "r")
    with pytest.raises(FatalRtlBuddyError, match="pnr platform 'pl': post-route"):
        platform.get_post_route_repair()
    platform = _platform(global_route_repair={"setup-margin": 0.1})
    with pytest.raises(FatalRtlBuddyError, match="global-route-repair repairs nothing"):
        platform.with_run_repairs(
            read_global_route_repair("run", {"setup-margin": None}), UNSET, "r"
        )


def test_a_partial_platform_block_loads_and_a_run_completes_it():
    platform = _platform(
        global_route_repair={"reroute": "full"},
        post_route_repair={"rounds": 3, "margin-step": 0.05},
    )
    resolved = platform.with_run_repairs(
        read_global_route_repair("run", {"hold-margin": 0.01}),
        read_post_route_repair("run", {"setup-margin": 0.1}),
        "r",
    )
    assert resolved.get_global_route_repair() == GlobalRouteRepair(
        hold_margin=0.01, reroute=Reroute.FULL
    )
    assert resolved.get_post_route_repair() == PostRouteRepair(
        setup_margin=0.1, rounds=3, margin_step=0.05
    )


def test_a_root_config_with_a_partial_platform_block_loads(minimal_project):
    from test_config_pnr_unknown_keys import PDK_ALL, _with_root_blocks

    from rtl_buddy.config.root import RootConfig

    _with_root_blocks(
        minimal_project,
        [PDK_ALL],
        [
            {
                "name": "pl1",
                "pdk": "pdk1",
                "corner": "typ",
                "cts-buffer": "b",
                "routing-layers": {"signal": "m2-m6", "clock": "m4"},
                "global-route-repair": {"reroute": "full"},
                "post-route-repair": {"rounds": 3, "margin-step": 0.05},
            }
        ],
    )

    cfg = RootConfig(name="partial")

    assert sorted(cfg.pnr_platform_cfgs) == ["pl1"]


def test_several_rounds_need_a_margin():
    platform = _platform(post_route_repair={"repair-design": {}, "rounds": 2})
    with pytest.raises(
        FatalRtlBuddyError,
        match="post-route-repair.rounds is 2, but the block sets no setup-margin",
    ):
        platform.with_run_repairs(UNSET, UNSET, "r")
    one = _platform(post_route_repair={"repair-design": {}})
    assert one.get_post_route_repair().sides() == ()
    both = _platform(post_route_repair={"hold-margin": 0, "rounds": 2})
    assert both.get_post_route_repair().sides() == ("hold",)


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
        (
            {"setup-margin": 0, "margin-step": -1},
            r"margin-step must be a number >= 0, got -1$",
        ),
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
    with pytest.raises(
        FatalRtlBuddyError,
        match="post-route-repair.reroute must be one of eco, full, got 'incremental'",
    ):
        _platform(post_route_repair={"hold-margin": 0, "reroute": "incremental"})
    with pytest.raises(FatalRtlBuddyError, match="got None"):
        _platform(post_route_repair={"hold-margin": 0, "reroute": None})


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


@pytest.mark.parametrize(
    "checkpoints",
    [
        ["floorplan", "place", "cts", "global_route", "detailed_route"],
        list(CHECKPOINT_STAGES),
        True,
    ],
)
def test_stages_accept_every_checkpoint_with_or_without_post_route_repair(
    tmp_path, checkpoints
):
    run = PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        stages=True,
        checkpoints=checkpoints,
    ).initialise(str(tmp_path))
    assert run.get_checkpoints() == CHECKPOINT_STAGES


def test_stages_refuse_a_partial_checkpoint_list(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="writes every stage checkpoint"):
        PnrConfigFile(
            name="r",
            desc="r",
            synth="s",
            synth_path="synth.yaml",
            platform="p",
            stages=True,
            checkpoints=["cts", "global_route"],
        ).initialise(str(tmp_path))


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


#: The flow template's post-CTS repair before `post_cts_repair` replaced it.
_BASE_POST_CTS = (
    "\n"
    'puts ">>> Post-CTS timing repair"\n'
    "estimate_parasitics -placement\n"
    "{{ setup_repair }}repair_timing -hold\n"
    "\n"
)


def _base_template(template: str) -> str:
    """The flow template as it was before the repair knobs: the post-CTS repair written out, and the global-route hold repair's placeholder."""
    assert template.count("{{ post_cts_repair }}\n") == 1
    assert template.count("{{ global_route_repair }}") == 1
    return template.replace("{{ post_cts_repair }}\n", _BASE_POST_CTS).replace(
        "{{ global_route_repair }}", "{{ global_route_hold_repair }}"
    )


#: What `global-route-hold-repair: true` filled the global-route hold repair's placeholder with.
_BASE_HOLD_REPAIR = (
    'puts ">>> Post-global-route hold repair"\n'
    "estimate_parasitics -global_routing\n"
    "repair_timing -hold\n"
    "global_route -start_incremental\n"
    "detailed_placement\n"
    "global_route -end_incremental -congestion_report_file $CONGESTION_RPT -verbose\n"
    "check_placement -verbose\n"
)


@pytest.mark.parametrize("hold_shorthand", [False, True])
@pytest.mark.parametrize("setup_repair", [False, True])
@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_a_flow_without_repair_knobs_is_the_template_as_before(
    tmp_path, monkeypatch, kind, setup_repair, hold_shorthand
):
    """Without the new knobs, with or without `global-route-hold-repair: true`, every script a run writes is the earlier template's render, whole."""
    platform = _real_platform(
        tmp_path,
        post_cts_setup_repair=setup_repair,
        global_route_hold_repair=hold_shorthand,
    )

    def _write(base: bool) -> dict[str, str]:
        pnr_cfg = _pnr_cfg(
            tmp_path,
            CHECKPOINT_STAGES if kind == "checkpoints" else None,
            stages=kind == "stages",
        )
        pnr_cfg.resolve_synth_cfg = MagicMock(
            return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
        )
        backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
        backend._ps_per_unit = 1.0
        if kind != "one session":
            backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
            os.makedirs(backend._ckpt_run_dir, exist_ok=True)
        if base:
            template = _base_template(backend._load_template())
            monkeypatch.setattr(backend, "_load_template", lambda: template)
            render = backend._render
            filled = {
                "setup_repair": "repair_timing -setup\n" if setup_repair else "",
                "global_route_hold_repair": _BASE_HOLD_REPAIR if hold_shorthand else "",
            }
            monkeypatch.setattr(
                backend, "_render", lambda text, subs: render(text, {**subs, **filled})
            )
        script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
        written = {"pnr.tcl": script.read_text()}
        if backend._ckpt_run_dir:
            for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
                written[path.name] = path.read_text()
        return written

    now = _write(base=False)
    before = _write(base=True)

    assert now == before
    assert len(now) == (7 if kind == "stages" else 1)


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
        'puts ">>> Global-route repair: legalize after repair_design"\n'
        "global_route -start_incremental\n"
        "detailed_placement\n" + _END_INCREMENTAL_CMD + "check_placement -verbose\n"
        f'puts ">>> Global-route repair: repair_timing -setup -setup_margin {setup} '
        f'(setup margin 0.2 ns in Liberty time unit {unit})"\n'
        f"repair_timing -setup -setup_margin {setup}\n"
        f'puts ">>> Global-route repair: repair_timing -hold -hold_margin {hold} '
        f"-setup_margin {setup} (hold margin 0.05 ns, setup margin 0.2 ns in Liberty "
        f'time unit {unit})"\n'
        f"repair_timing -hold -hold_margin {hold} -setup_margin {setup}\n"
        "global_route -start_incremental\n"
        "detailed_placement\n" + _END_INCREMENTAL_CMD + "check_placement -verbose\n"
    ) in text
    # The global-route checkpoint is taken after the repair.
    assert text.index(
        "check_placement -verbose\n", text.index("Global-route repair")
    ) < (text.index(">>> Detail route"))


@pytest.mark.parametrize(
    "block, hold_line",
    [
        (
            {"setup-margin": 0.2, "hold-margin": 0},
            "repair_timing -hold -setup_margin 200 (hold margin 0 ns, setup margin "
            "0.2 ns in Liberty time unit 1ps)",
        ),
        (
            {"setup-margin": 0, "hold-margin": 0.05},
            "repair_timing -hold -hold_margin 50 (hold margin 0.05 ns, setup margin "
            "0 ns in Liberty time unit 1ps)",
        ),
        (
            {"setup-margin": 0, "hold-margin": 0},
            "repair_timing -hold (hold margin 0 ns, setup margin 0 ns)",
        ),
        (
            {"hold-margin": 0.05},
            "repair_timing -hold -hold_margin 50 (hold margin "
            "0.05 ns in Liberty time unit 1ps)",
        ),
    ],
)
def test_the_hold_repair_keeps_a_set_setup_margin(tmp_path, block, hold_line):
    text = _render(tmp_path, _real_platform(tmp_path, global_route_repair=block))
    command = hold_line.split(" (")[0]
    assert f'puts ">>> Global-route repair: {hold_line}"\n{command}\n' in text


def test_a_hold_only_block_without_a_margin_renders_as_the_shorthand(tmp_path):
    shorthand = _render(
        tmp_path, _real_platform(tmp_path, global_route_hold_repair=True)
    )
    block = _render(
        tmp_path, _real_platform(tmp_path, global_route_repair={"hold-margin": 0})
    )
    assert block == shorthand
    assert _BASE_HOLD_REPAIR in shorthand
    assert "Global-route repair" not in shorthand


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
    assert setup < text.index("repair_timing -hold -setup_margin 100\n", setup)


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

# Round k's extraction session reports round k's setup and hold worst slack
# (round 0 is the detailed route) from `RB_STUB_SETUP` and `RB_STUB_HOLD` (one
# value per round from round 0, in the Liberty unit; hold defaults to 0, a
# missing setup value is not reported), and each corner's from
# `RB_STUB_SETUP_<corner>` and `RB_STUB_HOLD_<corner>`. `RB_STUB_DRC` lists the
# rounds whose detailed route reports a violation (0: the flow's detailed
# route); `RB_STUB_RSZ0074=<round>` makes that round's `repair_timing` fail as
# OpenROAD's does without usable routes; `RB_STUB_REPAIR_FAIL=<round>` makes
# that round's `remove_fillers` fail; `RB_STUB_ERROR=<round>` prints an
# `[ERROR` line in that round's extraction. `repair_timing` and
# `detailed_route` log what `design_is_routed` answers them.
_ROUND_STUBS = r"""
proc design_is_routed {args} { return 1 }
proc rb_round {} {
    global argv
    if {[regexp {\.r(\d+)\.(extract|repair)\.tcl$} [lindex $argv 0] -> round]} {
        return $round
    }
    return ""
}
proc rb_slack {var {default ""}} {
    global env
    if {![info exists env($var)]} { return $default }
    return [lindex $env($var) [rb_round]]
}
proc rb_stub_round {var} {
    global env
    return [expr {[info exists env($var)] && $env($var) eq [rb_round]}]
}
proc report_worst_slack {args} {
    global env
    if {[rb_round] eq "" || ![info exists env(RB_STUB_SETUP)]} { return }
    if {[lsearch $args -max] >= 0} {
        puts "worst slack max [rb_slack RB_STUB_SETUP]"
    } else {
        puts "worst slack min [rb_slack RB_STUB_HOLD 0.0]"
    }
}
namespace eval sta {}
set sta_report_default_digits 2
proc sta::find_scene {name} { return $name }
proc sta::format_time {value digits} { return $value }
proc sta::worst_slack_scene {scene min_max} {
    set kind [expr {$min_max eq "max" ? "SETUP" : "HOLD"}]
    return [rb_slack RB_STUB_${kind}_$scene 0.0]
}
proc sta::total_negative_slack_scene_cmd {scene min_max} { return 0.0 }
proc report_tns {args} {
    if {[rb_round] eq ""} { return }
    if {[rb_stub_round RB_STUB_ERROR]} {
        puts "\[ERROR STA-0001\] stub"
    }
    if {[lsearch $args -min] >= 0} { puts "tns min 0.0" } else { puts "tns max -1.5" }
}
proc remove_fillers {args} {
    if {[rb_stub_round RB_STUB_REPAIR_FAIL]} {
        puts "\[ERROR STUB-0003\] remove_fillers failed in round [rb_round]"
        error "STUB-0003"
    }
    rb_stub remove_fillers
}
proc repair_timing {args} {
    puts "stub: repair_timing routed [design_is_routed]"
    if {[rb_stub_round RB_STUB_RSZ0074]} {
        puts "\[ERROR RSZ-0074\] Failed to build tree from global routes"
        error "RSZ-0074"
    }
    rb_stub repair_timing
}
proc detailed_route {args} {
    global env
    puts "stub: detailed_route routed [design_is_routed]"
    rb_stub detailed_route
    set round [expr {[rb_round] eq "" ? 0 : [rb_round]}]
    set i [lsearch $args -output_drc]
    if {$i >= 0} {
        set count 0
        if {[info exists env(RB_STUB_DRC)]} {
            foreach item $env(RB_STUB_DRC) {
                lassign [split $item :] at n
                if {$at == $round} { set count [expr {$n eq "" ? 1 : $n}] }
            }
        }
        set text ""
        for {set v 0} {$v < $count} {incr v} {
            append text "violation type: Short\n\tsrcs: net:a net:b\n\tbbox = ($v.0, 2.0) - ($v.1, 2.1) on Layer met1 r$round\n"
        }
        rb_write [lindex $args [expr {$i + 1}]] [string trimright $text]
    }
    set i [lsearch $args -output_maze]
    if {$i >= 0} { rb_write [lindex $args [expr {$i + 1}]] "maze r$round" }
}
rename write_db rb_harness_write_db
proc write_db {path} {
    if {[rb_stub_round RB_STUB_NO_ODB]} { return }
    rb_harness_write_db $path
}
"""


def _rcx_pdk(tmp_path, corners=None) -> PdkConfig:
    (tmp_path / "pdk").mkdir(exist_ok=True)
    (tmp_path / "pdk" / "rcx.rules").write_text("rules\n")
    base = _pdk(tmp_path)
    return PdkConfig(
        PdkConfigFile(
            name="nangate45",
            site=base.get_site(),
            corners=corners or {"typ": "pdk/lib/typ.lib"},
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


def _with_repair(backend, tmp_path, *, post_route=None, rcx=True, corners=None) -> None:
    """Give a stub-harness backend a platform whose `post-route-repair` is `post_route`, and the round stubs. `corners` makes it a multi-corner platform."""
    pdk_corners = {c: "pdk/lib/typ.lib" for c in corners} if corners else None
    pdk = _rcx_pdk(tmp_path, pdk_corners) if rcx else _pdk(tmp_path)
    extra = {"post_route_repair": post_route} if post_route is not None else {}
    if corners:
        extra["sta_corners"] = list(corners)
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
    corners=None,
    **options,
):
    """A stub-harness backend whose platform sets `post_route` as its `post-route-repair`."""
    pnr_cfg = replace(_pnr_cfg(tmp_path, stages=stages), mode=mode)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=pnr_cfg, env=env, stages=stages, **options
    )
    _with_repair(backend, tmp_path, post_route=post_route, rcx=rcx, corners=corners)
    return backend, artefacts


def _rounds(run_dir) -> list[dict]:
    return [e for e in _events(run_dir) if e["event"] == "post_route_round"]


def _done(run_dir) -> dict:
    (event,) = [e for e in _events(run_dir) if e["event"] == "post_route_repair_done"]
    return event


def _logged(monkeypatch) -> list[tuple[int, str, dict]]:
    """Record every event the backend logs, as (level, event, fields)."""
    logged = []
    real_log_event = pnr_openroad.log_event

    def _log_event(logger, level, event, /, **fields):
        logged.append((level, event, fields))
        return real_log_event(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _log_event)
    return logged


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
    # Round 0 is the detailed route; each later round is timed after its repair.
    assert [r["round"] for r in rounds] == [0, 1, 2]
    # The default Liberty unit is 1ns; the margin steps each round.
    assert [r["setup_margin_ns"] for r in rounds] == [None, 0.1, 0.15]
    assert [r["wns_setup_ps"] for r in rounds] == [-200.0, -100.0, 10.0]
    assert [r["drc_count"] for r in rounds] == [0, 0, 0]
    assert [r["best_round"] for r in rounds] == [0, 1, 2]
    assert _done(run_dir) | {
        "kept_round": 2,
        "repaired_rounds": 2,
        "rounds": 3,
        "stop": "timing_met",
        "reason": "timing met",
    } == _done(run_dir)
    assert res.results["post_route_repair"] == {
        "kept_round": 2,
        "repaired_rounds": 2,
        "rounds": 3,
        "stop": "timing_met",
        "reason": "timing met",
    }
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": res})
    assert row["post_route_repair"] == res.results["post_route_repair"]
    assert res.results["desc"] is None or "post-route" not in res.results["desc"]
    assert (run_dir / "06_post_route_repair.r2.repair.log").is_file()
    assert not (run_dir / "06_post_route_repair.r3.repair.log").exists()
    r2 = (run_dir / "06_post_route_repair.r2.repair.tcl").read_text()
    assert "repair_timing -setup -setup_margin 0.15\n" in r2
    assert "remove_fillers\n" in r2
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.r1.odb]" in r2
    assert re.search(r"^read_spef .*\.r1\.spef\]$", r2, re.MULTILINE)
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 05_detailed_route.odb]" in r1
    assert re.search(r"^read_spef .*\.r0\.spef\]$", r1, re.MULTILINE)
    e2 = (run_dir / "06_post_route_repair.r2.extract.tcl").read_text()
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.r2.odb]" in e2
    assert "write_spef [file join $RB_RUN_DIR 06_post_route_repair.r2.spef]" in e2
    assert "\nset ::sta_report_default_digits 4\nreport_worst_slack -max\n" in e2
    e0 = (run_dir / "06_post_route_repair.r0.extract.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 05_detailed_route.odb]" in e0
    # Each round's checkpoint is set aside; the stage's is the kept round's.
    for round_ in (1, 2):
        assert (run_dir / f"06_post_route_repair.r{round_}.odb").is_file()
    assert (run_dir / "06_post_route_repair.odb").samefile(
        run_dir / "06_post_route_repair.r2.odb"
    )
    log = (artefacts / "pnr.log").read_text()
    assert ">>> rb: begin 06_post_route_repair round 0 extraction\n" in log
    assert ">>> rb: begin 06_post_route_repair round 2 repair\n" in log
    # `reroute: eco` keeps the detailed route: no unrouting, no full global route,
    # and the repairs on placement parasitics under the SPEF, inside one
    # incremental global route that legalization closes.
    assert "(reroute eco)" in r1
    assert "destroyNetWires" not in r1
    assert "global_route -congestion_iterations" not in r1
    assert "\nestimate_parasitics -global_routing" not in r1
    order = [
        "\nestimate_parasitics -placement\n",
        "\nread_spef ",
        "\nremove_fillers\n",
        "\nglobal_route -start_incremental\n",
        "\nrb::eco::repair 1 {\n",
        "\nrepair_timing -setup",
        "\n}\n",
        "\ndetailed_placement",
        "\ncheck_placement -verbose\n",
        "\nglobal_route -end_incremental",
        "\ndetailed_route \\",
    ]
    at = r1.index("(reroute eco)")
    for text in order:
        at = r1.index(text, at)
    assert r1.count("global_route -start_incremental") == 1
    # The repairs saw an unrouted design; the detailed route the original answer.
    r1_log = (run_dir / "06_post_route_repair.r1.repair.log").read_text()
    assert "stub: repair_timing routed 0" in r1_log
    assert "stub: repair_timing routed 1" not in r1_log
    assert "stub: detailed_route routed 1" in r1_log
    assert "stub: detailed_route routed 0" not in r1_log
    assert "post_route_eco_unguarded" not in {e["event"] for e in _events(run_dir)}
    assert (
        ">>> Post-route repair round 2: repair_timing -setup -setup_margin 0.15 " in log
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["checkpoints"]
    assert "post_route_repair" in manifest["resources"]
    ended = [e["stage"] for e in _events(run_dir) if e["event"] == "stage_end"]
    assert ended[-2:] == ["post_route_repair", "finish"]
    # Finish starts from the kept round.
    finish = (run_dir / "07_finish.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 06_post_route_repair.odb]" in finish


def test_a_full_reroute_round_routes_the_unrouted_design_first(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 1, "reroute": "full"},
        env={"RB_STUB_SETUP": "-0.2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    assert "(reroute full)" in r1
    repair = r1.index("\nrepair_timing -setup")
    # The repair's buffer trees come from a global route of the unrouted design.
    unroute = r1.index("\n[ord::get_db_block] destroyNetWires\n")
    route = r1.index("\nglobal_route -congestion_iterations")
    assert r1.index("\nremove_fillers\n") < unroute < route
    assert route < r1.index("\nestimate_parasitics -global_routing\n")
    assert r1.index("\nestimate_parasitics -global_routing\n") < r1.index("read_spef")
    assert r1.index("read_spef") < repair < r1.index("global_route -start_incremental")
    assert r1.index("global_route -end_incremental") < r1.index("\ndetailed_route \\")


def test_a_full_round_legalizes_after_repair_design_before_repair_timing(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={
            "repair-design": {"slew-margin": 50},
            "setup-margin": 0.1,
            "reroute": "full",
        },
        env={"RB_STUB_SETUP": "-0.2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    design = r1.index("\nrepair_design -slew_margin 50\n")
    timing = r1.index("\nrepair_timing -setup")
    between = r1[design:timing]
    assert "legalize after repair_design" in between
    assert between.index("global_route -start_incremental") < between.index(
        "\ndetailed_placement"
    )
    assert "global_route -end_incremental" in between
    # And again after the timing repair, before the detailed route.
    after = r1[timing:]
    assert after.index("global_route -end_incremental") < after.index(
        "\ndetailed_route \\"
    )


@pytest.mark.parametrize("reroute, named", [("eco", True), ("full", False)])
def test_an_eco_round_that_cannot_rebuffer_names_reroute_full(
    tmp_path, monkeypatch, reroute, named
):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 1, "reroute": reroute},
        env={"RB_STUB_SETUP": "-0.2", "RB_STUB_RSZ0074": "1"},
    )

    logged = _logged(monkeypatch)

    res = backend.run()

    # The detailed route stands; the warning and the result name the fix.
    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"]["kept_round"] == 0
    assert res.results["post_route_repair"]["stop"] == "session_failed"
    assert "post-route repair round 1's repair session failed" in res.results["desc"]
    assert ("set post-route-repair.reroute: full" in res.results["desc"]) is named
    (warning,) = [f for _l, e, f in logged if e == "pnr.post_route_round_failed"]
    assert ("set post-route-repair.reroute: full" in warning["error"]) is named


def test_a_dispatched_eco_round_that_cannot_rebuffer_names_reroute_full(
    tmp_path, monkeypatch
):
    from test_pnr_dispatch import _dispatched

    monkeypatch.setenv("RB_STUB_SETUP", "-0.2")
    monkeypatch.setenv("RB_STUB_RSZ0074", "1")
    backend, _artefacts, _slurm = _dispatched(tmp_path, monkeypatch)
    _with_repair(backend, tmp_path, post_route={**_PR, "rounds": 1})

    logged = _logged(monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"]["kept_round"] == 0
    assert "set post-route-repair.reroute: full" in res.results["desc"]
    # The head logs the stage job's warning from the progress events; this job
    # runs in the test's process, so its own warning is logged too.
    warnings = [
        (level, fields)
        for level, event, fields in logged
        if event == "pnr.post_route_round_failed"
    ]
    assert len(warnings) == 2 and warnings[0] == warnings[1]
    warning = warnings[-1]
    assert warning[0] == logging.WARNING
    assert warning[1]["round"] == 1 and warning[1]["kept_round"] == 0
    message = _human_message("pnr.post_route_round_failed", warning[1])
    assert "round 1's repair session failed" in message
    assert "post-route-repair.reroute: full" in message


def test_an_eco_round_that_ends_early_after_rsz_0074_names_reroute_full(
    tmp_path, monkeypatch
):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 1},
        env={"RB_STUB_SETUP": "-0.2", "RB_STUB_RSZ0074": "1"},
    )
    # The round logs the error and exits 0, before its end.
    harness = tmp_path / "harness.tcl"
    text = harness.read_text()
    assert text.count('error "RSZ-0074"') == 1
    harness.write_text(text.replace('error "RSZ-0074"', "exit 0"))

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert (
        "OpenROAD exited with code 0 before the end of round 1 repair"
        in res.results["desc"]
    )
    assert "set post-route-repair.reroute: full" in res.results["desc"]


def test_an_eco_round_overrides_design_is_routed_around_its_repairs_only(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={
            "repair-design": {"slew-margin": 50},
            "setup-margin": 0.1,
            "hold-margin": 0.05,
        },
        env={"RB_STUB_SETUP": "-0.2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    body = r1.split("\nrb::eco::repair 1 {\n", 1)[1].split("\n}\n", 1)[0]
    commands = [
        line.split()[0]
        for line in body.splitlines()
        if not line.startswith('puts ">>> Post-route repair round 1: ')
    ]
    # No legalize between the repairs: they build buffer trees from Steiner trees.
    assert commands == ["repair_design", "repair_timing", "repair_timing"]
    assert r1.count("rb::eco::repair 1 ") == 1


@pytest.mark.parametrize("how", ["one session", "staged", "dispatched"])
def test_an_eco_round_without_the_override_warns_and_repairs_as_routed(
    tmp_path, monkeypatch, how
):
    post_route = {**_PR, "rounds": 2}
    if how == "dispatched":
        from test_pnr_dispatch import _dispatched

        monkeypatch.setenv("RB_STUB_SETUP", "-0.2 -0.1")
        backend, artefacts, _slurm = _dispatched(tmp_path, monkeypatch)
        _with_repair(backend, tmp_path, post_route=post_route)
    else:
        backend, artefacts = _repair_backend(
            tmp_path,
            monkeypatch,
            post_route=post_route,
            stages=how == "staged",
            env={"RB_STUB_SETUP": "-0.2 -0.1"},
        )
    # A `repair_timing` that does not call `design_is_routed` itself.
    harness = tmp_path / "harness.tcl"
    text = harness.read_text()
    stub = 'puts "stub: repair_timing routed [design_is_routed]"'
    assert stub in text
    harness.write_text(
        "proc rb_routed {} { return [design_is_routed] }\n"
        + text.replace(stub, stub.replace("design_is_routed", "rb_routed"))
    )

    logged = []
    real_log_event = pnr_openroad.log_event

    def _log_event(logger, level, event, /, **fields):
        logged.append((level, event, fields))
        return real_log_event(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _log_event)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    r1_log = (run_dir / "06_post_route_repair.r1.repair.log").read_text()
    assert "stub: repair_timing routed 1" in r1_log
    assert "repair_timing does not call design_is_routed" in r1_log
    unguarded = [
        e for e in _events(run_dir) if e["event"] == "post_route_eco_unguarded"
    ]
    assert [e["round"] for e in unguarded] == [1, 2]
    (warning,) = [
        (level, fields)
        for level, event, fields in logged
        if event == "pnr.post_route_eco_unguarded"
    ]
    assert warning == (logging.WARNING, {"pnr": "demo_pnr", "rounds": [1, 2]})
    message = _human_message("pnr.post_route_eco_unguarded", warning[1])
    assert "RSZ-0074" in message
    assert "post-route-repair.reroute: full" in message


# `rb::eco::repair` under a bare interpreter: `design_is_routed` answers 1, and
# `repair_timing` asks it unless `RB_UNGUARDED` is set.
_ECO_STUBS = """
set ::events {}
namespace eval rb::ckpt {}
proc rb::ckpt::emit {event fields} { lappend ::events [list $event {*}$fields] }
proc design_is_routed {args} { return 1 }
if {[info exists ::RB_UNGUARDED]} {
    proc repair_timing {args} { return [rb_routed] }
} else {
    proc repair_timing {args} { return [design_is_routed] }
}
proc rb_routed {} { return [design_is_routed] }
"""


def _eco_tcl(script: str, unguarded: bool = False) -> str:
    return _tcl_eval(
        ("set ::RB_UNGUARDED 1\n" if unguarded else "")
        + _ECO_STUBS
        + files("rtl_buddy.pnr").joinpath("eco_repair.tcl").read_text()
        + script
    )


def test_eco_repair_overrides_design_is_routed_inside_and_restores_it():
    result = _eco_tcl(
        "set inside [rb::eco::repair 1 {list [repair_timing] [design_is_routed]}]\n"
        "set result [list $inside [design_is_routed] [repair_timing] $::events]\n"
    )
    assert result == "{0 0} 1 1 {}"


def test_eco_repair_restores_design_is_routed_and_rethrows_a_failed_repair():
    result = _eco_tcl(
        """
proc failing_repair {} {
    error "RSZ-0074 no tree" "repair_timing traceback" {RSZ 74}
}
set code [catch {rb::eco::repair 1 {failing_repair}} message options]
set result [list $code $message [dict get $options -errorcode] \\
    [string match "repair_timing traceback*" [dict get $options -errorinfo]] \\
    [design_is_routed] [llength [info commands ::rb::eco::design_is_routed]]]
"""
    )
    assert result == "1 {RSZ-0074 no tree} {RSZ 74} 1 1 0"


def test_eco_repair_runs_unchanged_and_records_it_when_repair_timing_does_not_ask():
    result = _eco_tcl(
        "set inside [rb::eco::repair 2 {repair_timing}]\n"
        "set result [list $inside [design_is_routed] $::events]\n",
        unguarded=True,
    )
    printed, answer = result.rstrip("\n").rsplit("\n", 1)
    assert printed == (
        ">>> Post-route repair round 2: repair_timing does not call "
        "design_is_routed; repairing on the routed parasitics source"
    )
    assert answer == "1 1 {{post_route_eco_unguarded round 2}}"


def test_a_one_session_run_s_routed_spef_is_vouched_for_for_rb_power(
    tmp_path, monkeypatch
):
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, env={"RB_STUB_SETUP": "0.1"}
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    script = artefacts / "pnr.tcl"
    text = script.read_text()
    # The first session ends before the record of the later ones.
    first, record = text.split("\nexit 0\n", 1)
    assert "write_spef" not in first
    assert "# ---- 06_post_route_repair.tcl ----" in record
    assert "# ---- 07_finish.tcl ----" in record
    finish = (run_dir / "07_finish.tcl").read_text()
    assert "write_spef $OUT_DIR/${DESIGN}.routed.spef\n" in finish
    assert f"# ---- 07_finish.tcl ----\n{finish}" in record
    spef = artefacts / "demo_top.routed.spef"
    spef.write_text("*SPEF\n")
    assert routed_spef_rejection(str(spef), str(script)) is None


def _hook(tmp_path, point, text) -> tuple[str]:
    path = tmp_path / "hooks" / f"{point}.tcl"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return (str(path),)


def _hooked_backend(tmp_path, monkeypatch, hooks, *, stages=False):
    pnr_cfg = replace(_pnr_cfg(tmp_path, stages=stages), hooks=hooks)
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        pnr_cfg=pnr_cfg,
        env={"RB_STUB_SETUP": "-0.2 -0.1 0.01"},
        stages=stages,
    )
    _with_repair(backend, tmp_path, post_route=_PR)
    return backend, artefacts


@pytest.mark.parametrize("stages", [False, True])
def test_each_round_runs_the_before_detailed_route_hooks(tmp_path, monkeypatch, stages):
    hooks = {
        "after-global-route": _hook(tmp_path, "agr", "puts rb_test_agr\n"),
        "before-detailed-route": _hook(tmp_path, "bdr", "puts rb_test_bdr\n"),
    }
    backend, artefacts = _hooked_backend(tmp_path, monkeypatch, hooks, stages=stages)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    for round_ in (1, 2):
        text = (run_dir / f"06_post_route_repair.r{round_}.repair.tcl").read_text()
        hook = text.index("\nrb::hook::run before-detailed-route ")
        assert text.index("\ncheck_placement -verbose\n") < hook
        assert hook < text.index("\ndetailed_route \\")
        assert "rb::hook::run after-global-route" not in text
    log = (artefacts / "pnr.log").read_text()
    # The detailed route, then the two repairing rounds.
    assert log.count("\nrb_test_bdr\n") == 3
    assert log.count("\nrb_test_agr\n") == 1


def test_a_one_session_run_with_rounds_logs_commands_hooks_leave_behind(
    tmp_path, monkeypatch, caplog
):
    hooks = {
        "after-cts": _hook(
            tmp_path,
            "after-cts",
            "proc rb_test_helper {} {}\nrename check_placement rb_test_check\n",
        ),
        "before-detailed-route": _hook(tmp_path, "bdr", "proc rb_test_route {} {}\n"),
    }
    backend, artefacts = _hooked_backend(tmp_path, monkeypatch, hooks)

    with caplog.at_level("INFO"):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results

    def _logged(event):
        return [
            (r.levelname, r.rtl_fields)
            for r in caplog.records
            if getattr(r, "rtl_event", None) == event
        ]

    def _at(point):
        return {"pnr": "demo_pnr", "point": point, "path": hooks[point][0]}

    assert _logged("pnr.hook_commands_not_kept") == [
        ("WARNING", {**_at("after-cts"), "commands": ["check_placement"]})
    ]
    # The before-detailed-route hook runs in the first session and each round, logged once.
    assert _logged("pnr.hook_commands_added") == [
        ("INFO", {**_at("after-cts"), "commands": ["rb_test_check", "rb_test_helper"]}),
        ("INFO", {**_at("before-detailed-route"), "commands": ["rb_test_route"]}),
    ]


def test_a_dispatched_run_runs_every_round_in_the_stage_job(tmp_path, monkeypatch):
    from test_pnr_dispatch import _dispatched

    monkeypatch.setenv("RB_STUB_SETUP", "-0.2 0.01")
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    _with_repair(backend, tmp_path, post_route=_PR)

    logged = []
    real_log_event = pnr_openroad.log_event

    def _log_event(logger, level, event, /, **fields):
        if event.startswith("pnr.post_route_r"):
            logged.append((event, fields.get("round")))
        return real_log_event(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _log_event)

    res = backend.run()

    once = [
        ("pnr.post_route_round", 0),
        ("pnr.post_route_round", 1),
        ("pnr.post_route_repair_done", None),
    ]
    # The fake Slurm runs the stage job in this process, then the head logs the rounds again from the run's progress events.
    assert logged == once + once

    assert res.results["result"] == "PASS", res.results
    stages = [job["stage"] for job in slurm.submitted()]
    assert stages == list(PNR_STAGES)
    (run_dir,) = _runs(artefacts)
    assert [r["best_round"] for r in _rounds(run_dir)] == [0, 1]
    assert res.results["post_route_repair"]["kept_round"] == 1
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["dispatch"]["stages"]
    assert "post_route_repair" in manifest["resources"]


def test_a_dispatched_round_runs_on_the_cpus_its_job_received(tmp_path, monkeypatch):
    from test_pnr_dispatch import _dispatched, _FakeSlurm

    from rtl_buddy.config.dispatch import DispatchResourcesFile
    from rtl_buddy.config.pnr import PnrResources

    monkeypatch.setenv("RB_STUB_SETUP", "-0.2 0.01")
    # sbatch-args come after the reservation, so the job gets 3 CPUs, not 4.
    slurm = _FakeSlurm(monkeypatch, sbatch_args=["--cpus-per-task=3"])
    backend, artefacts, _ = _dispatched(
        tmp_path,
        monkeypatch,
        slurm=slurm,
        cfg=replace(_pnr_cfg(tmp_path, stages=True), threads="auto"),
        resources=PnrResources(base=DispatchResourcesFile(cpus=4)),
    )
    _with_repair(backend, tmp_path, post_route=_PR)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    sessions = pnr_openroad.stage_scripts(str(run_dir), "post_route_repair")
    # The record, round 0's extraction, then each round's repair and extraction.
    assert len(sessions) == 2 + 2 * _PR["rounds"]
    # The job ran a copy of each session's script; the recorded ones keep the head's count.
    for path in sessions:
        recorded = Path(path).read_text()
        assert "\nset_thread_count 4\n" in recorded, path
        ran = (run_dir / "dispatch" / Path(path).name).read_text()
        assert ran == recorded.replace(
            "\nset_thread_count 4\n", "\nset_thread_count 3\n"
        ), path
    manifest = json.loads((run_dir / "manifest.json").read_text())
    (chained,) = [
        e["script"] for e in manifest["chain"] if e["stage"] == "post_route_repair"
    ]
    fingerprint = pnr_openroad._file_fingerprint(sessions[0])
    assert (fingerprint["sha256"], fingerprint["size"]) == (
        chained["sha256"],
        chained["size"],
    )
    (ended,) = [
        e
        for e in _events(run_dir)
        if e["event"] == "stage_end" and e["stage"] == "post_route_repair"
    ]
    assert ended["threads"] == 3


def test_rounds_stop_after_the_last_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 2},
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.05"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert [r["best_round"] for r in _rounds(run_dir)] == [0, 1, 2]
    assert res.results["post_route_repair"] == {
        "kept_round": 2,
        "repaired_rounds": 2,
        "rounds": 2,
        "stop": "last_round",
        "reason": "all 2 round(s) ran",
    }


# -- the round the stage keeps ----------------------------------------------------

_SETUP = {"setup": ["setup"], "hold": ["hold"], "both": ["setup", "hold"], "none": []}


def _timing(setup=None, hold=None, unconstrained=()):
    return {
        "wns_setup_ps": setup,
        "wns_hold_ps": hold,
        "unconstrained": list(unconstrained),
    }


@pytest.mark.parametrize(
    "new, best, sides, better",
    [
        # Worse, better, tied.
        (_timing(-190.0), _timing(-110.0), "setup", False),
        (_timing(-100.0), _timing(-110.0), "setup", True),
        (_timing(-110.0), _timing(-110.0), "setup", False),
        # Met slack counts as 0: more of it is no better, less of it no worse.
        (_timing(50.0), _timing(10.0), "setup", False),
        (_timing(10.0), _timing(-10.0), "setup", True),
        # Setup better while hold, which met, breaks.
        (_timing(-100.0, -5.0), _timing(-200.0, 10.0), "both", False),
        # Hold that still meets with less slack does not count against it.
        (_timing(-100.0, 1.0), _timing(-200.0, 50.0), "both", True),
        # A side the rounds do not repair does not count.
        (_timing(-100.0, -50.0), _timing(-200.0, 10.0), "setup", True),
        (_timing(-300.0, -5.0), _timing(-200.0, -10.0), "hold", True),
        # Both sides must hold their ground.
        (_timing(-100.0, -20.0), _timing(-200.0, -10.0), "both", False),
        (_timing(-200.0, -5.0), _timing(-200.0, -10.0), "both", True),
        # Unconstrained counts as met.
        (_timing(-100.0, unconstrained=["hold"]), _timing(-200.0, -1.0), "both", True),
        # A repair-design-only block judges no timing.
        (_timing(-300.0), _timing(-100.0), "none", True),
    ],
)
def test_a_round_is_better_only_when_no_repaired_side_loses_slack(
    new, best, sides, better
):
    assert pnr_openroad._improves(new, best, _SETUP[sides]) is better


@pytest.mark.parametrize("reroute", ["eco", "full"])
@pytest.mark.parametrize("rounds", [1, 2])
def test_a_round_that_worsens_timing_keeps_the_detailed_route(
    tmp_path, monkeypatch, reroute, rounds
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": rounds, "reroute": reroute},
        env={"RB_STUB_SETUP": "-0.11 -0.19 -0.05"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    rounds_ = _rounds(run_dir)
    assert [r["wns_setup_ps"] for r in rounds_] == [-110.0, -190.0]
    assert [r["best_round"] for r in rounds_] == [0, 0]
    assert res.results["post_route_repair"] == {
        "kept_round": 0,
        "repaired_rounds": 1,
        "rounds": rounds,
        "stop": "no_improvement",
        "reason": "round 1 did not improve timing",
    }
    # The rounds stop: round 2 never repairs.
    assert not (run_dir / "06_post_route_repair.r2.repair.log").exists()
    assert (run_dir / "06_post_route_repair.odb").samefile(
        run_dir / "05_detailed_route.odb"
    )
    assert (artefacts / "route.maze.log").read_text().strip() == "maze r0"
    (checkpoint,) = [
        e
        for e in _events(run_dir)
        if e["event"] == "checkpoint"
        and e["stage"] == "post_route_repair"
        and e.get("source")
    ]
    assert checkpoint["source"] == "05_detailed_route"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    odb = manifest["checkpoints"]["post_route_repair"]["files"]["odb"]
    assert (
        odb["sha256"]
        == pnr_openroad._file_fingerprint(str(run_dir / "05_detailed_route.odb"))[
            "sha256"
        ]
    )


def test_a_better_round_then_a_worse_one_keeps_the_better(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.15 0.5"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert [r["best_round"] for r in _rounds(run_dir)] == [0, 1, 1]
    assert res.results["post_route_repair"] == {
        "kept_round": 1,
        "repaired_rounds": 2,
        "rounds": 3,
        "stop": "no_improvement",
        "reason": "round 2 did not improve timing",
    }
    assert not (run_dir / "06_post_route_repair.r3.repair.log").exists()
    assert (run_dir / "06_post_route_repair.odb").samefile(
        run_dir / "06_post_route_repair.r1.odb"
    )
    assert (run_dir / "06_post_route_repair.route.maze.log").read_text().strip() == (
        "maze r1"
    )
    assert (artefacts / "route.maze.log").read_text().strip() == "maze r1"
    # Round 2 started from round 1's database and SPEF.
    r2 = (run_dir / "06_post_route_repair.r2.repair.tcl").read_text()
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.r1.odb]" in r2
    assert "read_sdc [file join $RB_RUN_DIR 06_post_route_repair.r1.sdc]" in r2


def test_a_tie_keeps_the_earlier_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, env={"RB_STUB_SETUP": "-0.2 -0.2"}
    )

    res = backend.run()

    assert res.results["post_route_repair"]["kept_round"] == 0
    assert res.results["post_route_repair"]["stop"] == "no_improvement"


@pytest.mark.parametrize(
    "hold, kept",
    [
        # Hold met before and breaks: setup's gain does not count.
        ("0.01 -0.005", 0),
        # Hold still met with less slack: setup's gain counts.
        ("0.05 0.001", 1),
    ],
)
def test_a_round_that_breaks_hold_to_gain_setup_is_not_better(
    tmp_path, monkeypatch, hold, kept
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"setup-margin": 0.1, "hold-margin": 0.0, "rounds": 1},
        env={"RB_STUB_SETUP": "-0.2 -0.1", "RB_STUB_HOLD": hold},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"]["kept_round"] == kept


def test_hold_the_block_does_not_repair_does_not_judge_a_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={**_PR, "rounds": 1},
        env={"RB_STUB_SETUP": "-0.2 -0.1", "RB_STUB_HOLD": "0.01 -0.05"},
    )

    res = backend.run()

    assert res.results["post_route_repair"]["kept_round"] == 1
    assert res.results["post_route_repair"]["stop"] == "last_round"


def test_a_later_round_s_unreported_timing_keeps_the_best_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, env={"RB_STUB_SETUP": "-0.2 -0.1"}
    )

    res = backend.run()

    assert res.results["post_route_repair"] == {
        "kept_round": 1,
        "repaired_rounds": 2,
        "rounds": 3,
        "stop": "timing_not_reported",
        "reason": "round 2's timing was not reported",
    }


def test_a_failed_later_stage_still_names_the_kept_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        env={"RB_STUB_SETUP": "-0.2 -0.3", "RB_STUB_FAIL": "filler_placement"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert "07_finish" in res.results["desc"]
    assert res.results["post_route_repair"]["kept_round"] == 0


def test_round_messages_name_the_round_its_timing_and_the_best_round():
    measured = {
        "pnr": "blk",
        "round": 1,
        "rounds": 3,
        "setup_margin_ns": 0.1,
        "hold_margin_ns": None,
        "drc_count": 0,
        "wns_setup_ps": -190.0,
        "wns_hold_ps": 12.0,
        "tns_ps": -1500.0,
        "tns_hold_ps": 0.0,
        "unconstrained": [],
        "best_round": 0,
    }
    assert _human_message("pnr.post_route_round", measured) == (
        'P&R "blk" post-route repair round 1/3 (repaired with setup margin 0.1 ns): '
        "setup WNS -0.1900 ns TNS -1.5000 ns, hold WNS 0.0120 ns TNS 0.0000 ns, "
        "0 DRC violation(s); best round 0"
    )
    drc = {"pnr": "blk", "round": 0, "rounds": 3, "drc_count": 4, "best_round": 0}
    assert _human_message("pnr.post_route_round", drc) == (
        'P&R "blk" post-route repair round 0/3 (the detailed route): not timed, '
        "4 DRC violation(s); best round 0"
    )
    done = {
        "pnr": "blk",
        "kept_round": 0,
        "repaired_rounds": 1,
        "rounds": 3,
        "stop": "no_improvement",
        "reason": "round 1 did not improve timing",
    }
    assert _human_message("pnr.post_route_repair_done", done) == (
        'P&R "blk" post-route repair: kept round 0 (the detailed route); 1 of 3 '
        "round(s) repaired; stopped: round 1 did not improve timing"
    )


def test_met_timing_keeps_the_detailed_route_as_the_stage_checkpoint(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, env={"RB_STUB_SETUP": "0.0"}
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert [r["round"] for r in _rounds(run_dir)] == [0]
    assert _done(run_dir) | {
        "kept_round": 0,
        "repaired_rounds": 0,
        "stop": "timing_met",
    } == _done(run_dir)
    assert (run_dir / "06_post_route_repair.r0.extract.log").is_file()
    assert not (run_dir / "06_post_route_repair.r1.repair.log").exists()
    odb = run_dir / "06_post_route_repair.odb"
    assert odb.samefile(run_dir / "05_detailed_route.odb")
    # The detailed route's report copies come along.
    maze = run_dir / "06_post_route_repair.route.maze.log"
    assert maze.read_text().strip() == "maze r0"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert "post_route_repair" in manifest["checkpoints"]


def test_unreadable_timing_stops_the_rounds(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(tmp_path, monkeypatch, post_route=_PR)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (only,) = _rounds(run_dir)
    assert only["round"] == 0 and only["wns_setup_ps"] is None
    assert _done(run_dir) | {
        "kept_round": 0,
        "stop": "timing_not_reported",
        "reason": "timing was not reported",
    } == _done(run_dir)
    assert not (run_dir / "06_post_route_repair.r1.repair.log").exists()


def test_negative_hold_repairs_when_the_block_repairs_hold(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"hold-margin": 0.01, "rounds": 3, "margin-step": 0.01},
        env={"RB_STUB_SETUP": "-0.5 -0.5 -0.5", "RB_STUB_HOLD": "-0.02 -0.01 0.0"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    rounds = _rounds(run_dir)
    # Setup is not repaired, so its violation neither keeps the rounds going
    # nor makes a round worse.
    assert [r["best_round"] for r in rounds] == [0, 1, 2]
    assert [r["wns_hold_ps"] for r in rounds] == [-20.0, -10.0, 0.0]
    assert [r["hold_margin_ns"] for r in rounds] == [None, 0.01, 0.02]
    assert [r["setup_margin_ns"] for r in rounds] == [None, None, None]
    assert _done(run_dir)["reason"] == "timing met"
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    assert "\nrepair_timing -hold -hold_margin 0.01\n" in r1
    assert "repair_timing -setup" not in r1


def test_a_side_the_block_does_not_repair_does_not_start_a_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "0.01 0.01 0.01", "RB_STUB_HOLD": "-0.2 -0.2 -0.2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (only,) = _rounds(run_dir)
    assert only["round"] == 0 and only["wns_hold_ps"] == -200.0
    assert _done(run_dir) | {"reason": "timing met", "repaired_rounds": 0} == _done(
        run_dir
    )


def test_an_unconstrained_side_counts_as_met(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"setup-margin": 0, "hold-margin": 0, "rounds": 2},
        env={"RB_STUB_SETUP": "-0.1 0.0", "RB_STUB_HOLD": "INF INF"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    rounds = _rounds(run_dir)
    assert [r["best_round"] for r in rounds] == [0, 1]
    assert [r["unconstrained"] for r in rounds] == [["hold"], ["hold"]]
    assert [r["wns_hold_ps"] for r in rounds] == [None, None]
    assert _done(run_dir)["reason"] == "timing met"


def test_a_repair_design_only_block_repairs_once(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"repair-design": {"slew-margin": 10}},
        env={"RB_STUB_SETUP": "0.5"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    # With no timing side to judge, the clean round is kept.
    assert [r["best_round"] for r in _rounds(run_dir)] == [0, 1]
    assert _done(run_dir) | {
        "kept_round": 1,
        "repaired_rounds": 1,
        "stop": "last_round",
    } == _done(run_dir)
    r1 = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    assert "\nrepair_design -slew_margin 10\n" in r1
    assert not re.search(r"^repair_timing", r1, re.MULTILINE)


def test_a_partial_platform_block_fails_only_the_run_that_leaves_it_partial(
    tmp_path, monkeypatch
):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route={"rounds": 3, "margin-step": 0.05}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert (
        "pnr run 'demo_pnr' on platform 'pl': post-route-repair repairs nothing"
        in res.results["desc"]
    )
    assert "in the run's post-route-repair or the platform's" in res.results["desc"]


def test_multi_corner_rounds_time_each_corner_on_its_own_spef_read(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"setup-margin": 0, "hold-margin": 0, "rounds": 3},
        corners=["ss", "ff"],
        env={
            "RB_STUB_SETUP": "-0.3 0.1",
            "RB_STUB_HOLD": "0.05 -0.02",
            "RB_STUB_SETUP_ss": "-0.3 0.1",
            "RB_STUB_SETUP_ff": "0.4 0.5",
            "RB_STUB_HOLD_ss": "0.2 0.2",
            "RB_STUB_HOLD_ff": "0.05 -0.02",
        },
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    rounds = _rounds(run_dir)
    assert rounds[0]["corners"]["ss"]["wns_setup_ps"] == -300.0
    # Round 1 meets setup on every corner, but breaks hold at ff, which met.
    assert rounds[1]["corners"]["ff"]["wns_hold_ps"] == -20.0
    assert [r["best_round"] for r in rounds] == [0, 0]
    assert _done(run_dir)["stop"] == "no_improvement"
    for script in ("r0.extract", "r1.repair"):
        text = (run_dir / f"06_post_route_repair.{script}.tcl").read_text()
        assert re.search(r"^read_spef -corner ss .*\.r0\.spef\]$", text, re.MULTILINE)
        assert re.search(r"^read_spef -corner ff .*\.r0\.spef\]$", text, re.MULTILINE)
        assert not re.search(r"^read_spef \[", text, re.MULTILINE)


@pytest.mark.parametrize("round_", [1, 2])
def test_a_round_with_drc_violations_is_never_kept(tmp_path, monkeypatch, round_):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.05", "RB_STUB_DRC": str(round_)},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    rounds = _rounds(run_dir)
    # The round's route is not timed; the round before it is kept.
    assert [r["round"] for r in rounds] == list(range(round_ + 1))
    assert [r["drc_count"] for r in rounds] == [0] * round_ + [1]
    assert "wns_setup_ps" not in rounds[-1]
    assert not (run_dir / f"06_post_route_repair.r{round_}.extract.log").exists()
    assert res.results["post_route_repair"] == {
        "kept_round": round_ - 1,
        "repaired_rounds": round_,
        "rounds": 3,
        "stop": "drc",
        "reason": f"round {round_}'s detailed route has 1 DRC violation(s)",
    }
    assert res.results["drc_count"] == 0
    # The stage's checkpoint and the artefact directory hold the kept round's
    # router reports; the set-aside round keeps its own.
    maze = f"maze r{round_ - 1}"
    assert (run_dir / "06_post_route_repair.route.drc.rpt").read_text().strip() == ""
    assert (run_dir / "06_post_route_repair.route.maze.log").read_text().strip() == maze
    assert (artefacts / "route.maze.log").read_text().strip() == maze
    assert (artefacts / "route.drc.rpt").read_text().strip() == ""
    kept = run_dir / f"06_post_route_repair.r{round_}.route.drc.rpt"
    assert kept.read_text().startswith("violation type: Short")
    assert kept.read_text().rstrip().endswith(f"r{round_}")
    finish = (run_dir / "07_finish.tcl").read_text()
    assert "read_db [file join $RB_PRED_DIR 06_post_route_repair.odb]" in finish
    stage = (run_dir / "06_post_route_repair.odb").stat().st_ino
    if round_ == 1:
        assert stage == (run_dir / "05_detailed_route.odb").stat().st_ino
    else:
        assert stage == (run_dir / "06_post_route_repair.r1.odb").stat().st_ino


def test_a_detailed_route_with_drc_violations_gets_no_round(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": "-0.2 -0.1", "RB_STUB_DRC": "0"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (only,) = _rounds(run_dir)
    assert only["round"] == 0 and only["drc_count"] == 1
    assert "wns_setup_ps" not in only
    # No session runs: not even round 0's extraction.
    assert not list(run_dir.glob("06_post_route_repair.r[0-9]*.log"))
    assert res.results["post_route_repair"] == {
        "kept_round": 0,
        "repaired_rounds": 0,
        "rounds": 3,
        "stop": "drc",
        "reason": "the detailed route has 1 DRC violation(s)",
    }
    assert res.results["drc_count"] == 1
    assert (run_dir / "06_post_route_repair.odb").samefile(
        run_dir / "05_detailed_route.odb"
    )


def test_an_error_line_in_round_0_s_extraction_fails_the_stage(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        env={"RB_STUB_SETUP": "-0.2 -0.1", "RB_STUB_ERROR": "0"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert "06_post_route_repair" in res.results["desc"]
    assert "ERROR" in res.results["desc"]
    assert "post_route_repair" not in res.results
    (run_dir,) = _runs(artefacts)
    assert _rounds(run_dir) == []
    assert not (run_dir / "06_post_route_repair.r1.repair.log").exists()
    assert not (run_dir / "07_finish.log").exists()


@pytest.mark.parametrize("stages", [False, True])
def test_an_error_line_in_a_later_extraction_sets_the_round_aside(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=stages,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.05", "RB_STUB_ERROR": "2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"]["kept_round"] == 1
    assert res.results["post_route_repair"]["stop"] == "session_failed"
    assert res.results["post_route_repair"]["reason"].startswith(
        "round 2's extraction session failed: 2 ERROR(s) in its log, the first: "
        "[ERROR STA-0001] stub"
    )
    # The error stays in the logs, after the session's begin line and before
    # the line that sets it aside.
    (run_dir,) = _runs(artefacts)
    log = (artefacts / "pnr.log").read_text()
    begin = log.index(">>> rb: begin 06_post_route_repair round 2 extraction\n")
    error = log.index("[ERROR STA-0001] stub", begin)
    aside = log.index(
        ">>> rb: set aside 06_post_route_repair round 2 extraction: ", error
    )
    assert "the stage keeps round 1\n" in log[aside:]
    stage_log = (run_dir / "06_post_route_repair.log").read_text()
    assert stage_log.index("[ERROR STA-0001]") < stage_log.index(">>> rb: set aside")
    assert (run_dir / "07_finish.log").is_file()


def test_a_resume_from_finish_restores_the_last_round_s_reports(tmp_path, monkeypatch):
    first, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        env={"RB_STUB_SETUP": "-0.2 0.01"},
    )
    assert first.run().results["result"] == "PASS"
    (first_dir,) = _runs(artefacts)
    (artefacts / "route.maze.log").unlink()

    again, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        resume=str(first_dir),
        resume_from="finish",
        env={"RB_STUB_SETUP": "-0.2 0.01"},
    )
    res = again.run()

    assert res.results["result"] == "PASS", res.results
    assert (artefacts / "route.maze.log").read_text().strip() == "maze r1"


@pytest.mark.parametrize("post_route", [_PR, None], ids=["repaired", "unrepaired"])
def test_a_resume_from_finish_refuses_changed_rcx_rules_only_after_repair_rounds(
    tmp_path, monkeypatch, post_route
):
    """The rounds extract with `rcx-rules`; without them only `finish`, which a resume from finish reruns, reads the file."""
    first, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=post_route,
        stages=True,
        env={"RB_STUB_SETUP": "-0.2 0.01"},
    )
    assert first.run().results["result"] == "PASS"
    (first_dir,) = _runs(artefacts)
    again, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=post_route,
        stages=True,
        resume=str(first_dir),
        resume_from="finish",
        env={"RB_STUB_SETUP": "-0.2 0.01"},
    )
    (tmp_path / "pdk" / "rcx.rules").write_text("recalibrated rules\n")

    res = again.run()

    if post_route is None:
        assert res.results["result"] == "PASS", res.results
    else:
        assert res.results["fail_stage"] == "resume", res.results
        assert "input rcx_rules differs" in res.results["desc"]


@pytest.mark.parametrize("how", ["one session", "staged", "dispatched"])
@pytest.mark.parametrize("round_", [1, 2])
def test_a_failed_repair_session_keeps_the_best_round(
    tmp_path, monkeypatch, how, round_
):
    env = {"RB_STUB_SETUP": "-0.2 -0.1 -0.05", "RB_STUB_REPAIR_FAIL": str(round_)}
    if how == "dispatched":
        from test_pnr_dispatch import _dispatched

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        backend, artefacts, _slurm = _dispatched(tmp_path, monkeypatch)
        _with_repair(backend, tmp_path, post_route=_PR)
    else:
        backend, artefacts = _repair_backend(
            tmp_path, monkeypatch, post_route=_PR, stages=how == "staged", env=env
        )
    logged = _logged(monkeypatch)

    res = backend.run()

    # Post-route repair is an optimisation: the routed design stands.
    assert res.results["result"] == "PASS", res.results
    kept = round_ - 1
    assert res.results["post_route_repair"] == {
        "kept_round": kept,
        "repaired_rounds": kept,
        "rounds": 3,
        "stop": "session_failed",
        "reason": f"round {round_}'s repair session failed: OpenROAD exited with "
        "code 1: STUB-0003",
    }
    assert res.results["desc"].endswith(
        f"; post-route repair round {round_}'s repair session failed: "
        f"OpenROAD exited with code 1: STUB-0003; kept round {kept}"
    )
    (run_dir,) = _runs(artefacts)
    assert [r["round"] for r in _rounds(run_dir)] == list(range(round_))
    (failed,) = [e for e in _events(run_dir) if e["event"] == "post_route_round_failed"]
    assert failed | {"round": round_, "session": "repair", "kept_round": kept} == failed
    warnings = [
        (level, fields)
        for level, event, fields in logged
        if event == "pnr.post_route_round_failed"
    ]
    assert warnings and all(level == logging.WARNING for level, _f in warnings)
    ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
    assert [e["stage"] for e in ended][-2:] == ["post_route_repair", "finish"]
    assert all(e["status"] == "ok" for e in ended)
    assert "[ERROR STUB-0003]" in (artefacts / "pnr.log").read_text()
    source = "05_detailed_route" if kept == 0 else "06_post_route_repair.r1"
    assert (run_dir / "06_post_route_repair.odb").samefile(run_dir / f"{source}.odb")
    (checkpoint,) = [
        e
        for e in _events(run_dir)
        if e["event"] == "checkpoint"
        and e["stage"] == "post_route_repair"
        and e.get("source")
    ]
    assert checkpoint["source"] == source


@pytest.mark.parametrize("stages", [False, True])
def test_a_repair_session_that_writes_no_database_is_set_aside(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=stages,
        env={"RB_STUB_SETUP": "-0.2 -0.1 -0.05", "RB_STUB_NO_ODB": "2"},
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"] == {
        "kept_round": 1,
        "repaired_rounds": 1,
        "rounds": 3,
        "stop": "session_failed",
        "reason": "round 2's repair session failed: it wrote no checkpoint",
    }
    (run_dir,) = _runs(artefacts)
    assert [r["round"] for r in _rounds(run_dir)] == [0, 1]
    assert not (run_dir / "06_post_route_repair.r2.extract.log").exists()
    assert (run_dir / "06_post_route_repair.odb").samefile(
        run_dir / "06_post_route_repair.r1.odb"
    )


@pytest.mark.parametrize("setup, kept", [("-0.2 0.0", 1), ("-0.2 -0.3", 0)])
def test_stop_after_post_route_repair_reports_on_extracted_parasitics(
    tmp_path, monkeypatch, setup, kept
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        env={"RB_STUB_SETUP": setup},
        stop_after="post_route_repair",
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["stopped_after"] == "post_route_repair"
    assert res.results["post_route_repair"]["kept_round"] == kept
    (run_dir,) = _runs(artefacts)
    report = (run_dir / "06_post_route_repair.report.tcl").read_text()
    assert "extract_parasitics" in report
    assert "read_db [file join $RB_RUN_DIR 06_post_route_repair.odb]" in report
    # The report times the round the stage kept.
    kept_odb = "05_detailed_route.odb" if kept == 0 else "06_post_route_repair.r1.odb"
    assert (run_dir / "06_post_route_repair.odb").samefile(run_dir / kept_odb)
    assert (run_dir / "06_post_route_repair.report.log").is_file()
    assert not (run_dir / "07_finish.tcl").exists()


def test_post_route_repair_without_rcx_rules_is_a_setup_error(tmp_path, monkeypatch):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, rcx=False
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "rcx-rules" in res.results["desc"]


@pytest.mark.parametrize("stages", [False, True])
def test_a_run_that_stops_before_the_rounds_needs_no_rcx_rules(
    tmp_path, monkeypatch, stages
):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        rcx=False,
        stages=stages,
        stop_after="detailed_route",
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["stopped_after"] == "detailed_route"


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


@pytest.mark.parametrize("stage", ["detailed_route", "post_route_repair"])
def test_a_probe_cannot_stop_after_a_stage_past_global_route(
    tmp_path, monkeypatch, stage
):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        mode=PnrMode.PROBE,
        stop_after=stage,
        post_route={"setup-margin": 0.1},
    )

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"] == (
        f"--stop-after {stage}: a probe (mode: probe) ends after global_route"
    )


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


@pytest.mark.parametrize("setup, kept", [("-0.2 0.01", 1), ("-0.2 -0.3", 0)])
def test_a_staged_run_without_repair_resumes_into_post_route_repair(
    tmp_path, monkeypatch, setup, kept
):
    plain, artefacts = _repair_backend(tmp_path, monkeypatch, stages=True)
    assert plain.run().results["result"] == "PASS"
    (plain_dir,) = _runs(artefacts)

    repair, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        resume=str(plain_dir),
        resume_from="post_route_repair",
        env={"RB_STUB_SETUP": setup},
    )
    res = repair.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["post_route_repair"]["kept_round"] == kept
    resumed = next(p for p in _runs(artefacts) if p != plain_dir)
    assert [r["round"] for r in _rounds(resumed)] == [0, 1]
    r1 = (resumed / "06_post_route_repair.r1.repair.tcl").read_text()
    assert f'set RB_PRED_DIR "{plain_dir}"' in r1
    # Round 0 is the inherited detailed route, which the stage then keeps.
    source = plain_dir / "05_detailed_route.odb" if kept == 0 else None
    source = source or resumed / "06_post_route_repair.r1.odb"
    assert (resumed / "06_post_route_repair.odb").samefile(source)
    assert (artefacts / "route.maze.log").read_text().strip() == f"maze r{kept}"


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
