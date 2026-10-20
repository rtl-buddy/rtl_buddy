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
    with pytest.raises(FatalRtlBuddyError, match="cannot be harden: true"):
        _run(mode="probe", harden=True)


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

# Each round's extraction session reports the round's setup and hold worst
# slack from `RB_STUB_SETUP` and `RB_STUB_HOLD` (one value per round, in the
# Liberty unit; hold defaults to 0), and each corner's from
# `RB_STUB_SETUP_<corner>` and `RB_STUB_HOLD_<corner>`; `RB_STUB_DRC=<round>`
# makes that round's detailed route report a violation; `RB_STUB_RSZ0074=<round>`
# makes that round's `repair_timing` fail as OpenROAD's does without usable routes.
# `repair_timing` and `detailed_route` log what `design_is_routed` answers them.
_ROUND_STUBS = r"""
proc design_is_routed {args} { return 1 }
proc rb_round {} {
    global argv
    if {[regexp {\.r(\d+)\.(extract|repair)\.tcl$} [lindex $argv 0] -> round]} {
        return $round
    }
    return 0
}
proc rb_slack {var {default ""}} {
    global env
    if {![info exists env($var)]} { return $default }
    return [lindex $env($var) [expr {[rb_round] - 1}]]
}
proc report_worst_slack {args} {
    global env
    set round [rb_round]
    if {$round == 0 || ![info exists env(RB_STUB_SETUP)]} { return }
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
    global env
    if {[rb_round] == 0} { return }
    if {[info exists env(RB_STUB_ERROR)] && $env(RB_STUB_ERROR) == [rb_round]} {
        puts "\[ERROR STA-0001\] stub"
    }
    if {[lsearch $args -min] >= 0} { puts "tns min 0.0" } else { puts "tns max -1.5" }
}
proc remove_fillers {args} { rb_stub remove_fillers }
proc repair_timing {args} {
    global env
    puts "stub: repair_timing routed [design_is_routed]"
    if {[info exists env(RB_STUB_RSZ0074)] && $env(RB_STUB_RSZ0074) == [rb_round]} {
        puts "\[ERROR RSZ-0074\] Failed to build tree from global routes"
        error "RSZ-0074"
    }
    rb_stub repair_timing
}
proc detailed_route {args} {
    global env
    puts "stub: detailed_route routed [design_is_routed]"
    rb_stub detailed_route
    set i [lsearch $args -output_drc]
    if {$i >= 0 && [info exists env(RB_STUB_DRC)] && $env(RB_STUB_DRC) == [rb_round]} {
        rb_write [lindex $args [expr {$i + 1}]] "violation type: Short\n\tsrcs: net:a net:b\n\tbbox = (1.0, 2.0) - (1.1, 2.1) on Layer met1"
    }
    set i [lsearch $args -output_maze]
    if {$i >= 0} { rb_write [lindex $args [expr {$i + 1}]] "maze r[rb_round]" }
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

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert "06_post_route_repair" in res.results["desc"]
    assert ("set post-route-repair.reroute: full" in res.results["desc"]) is named


def test_a_dispatched_eco_round_that_cannot_rebuffer_names_reroute_full(
    tmp_path, monkeypatch
):
    from test_pnr_dispatch import _dispatched

    monkeypatch.setenv("RB_STUB_SETUP", "-0.2")
    monkeypatch.setenv("RB_STUB_RSZ0074", "1")
    backend, _artefacts, _slurm = _dispatched(tmp_path, monkeypatch)
    _with_repair(backend, tmp_path, post_route={**_PR, "rounds": 1})

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert "06_post_route_repair" in res.results["desc"]
    assert "set post-route-repair.reroute: full" in res.results["desc"]


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

    assert res.results["result"] == "FAIL"
    assert "before the end of round 1" in res.results["desc"]
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
        ("pnr.post_route_round", 1),
        ("pnr.post_route_round", 2),
        ("pnr.post_route_repair_done", None),
    ]
    # The fake Slurm runs the stage job in this process, then the head logs the rounds again from the run's progress events.
    assert logged == once + once

    assert res.results["result"] == "PASS", res.results
    stages = [job["stage"] for job in slurm.submitted()]
    assert stages == list(PNR_STAGES)
    (run_dir,) = _runs(artefacts)
    assert [r["repair"] for r in _rounds(run_dir)] == [True, False]
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
    assert len(sessions) == 1 + 2 * _PR["rounds"]
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
    assert only["wns_setup_ps"] is None and only["repair"] is False
    assert _done(run_dir)["reason"] == "timing was not reported"


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
    # Setup is not repaired, so its violation does not keep the rounds going.
    assert [r["repair"] for r in rounds] == [True, True, False]
    assert [r["wns_hold_ps"] for r in rounds] == [-20.0, -10.0, 0.0]
    assert [r["hold_margin_ns"] for r in rounds] == [0.01, 0.02, None]
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
    assert only["repair"] is False and only["wns_hold_ps"] == -200.0
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
    assert [r["repair"] for r in rounds] == [True, False]
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
    (only,) = _rounds(run_dir)
    assert only["repair"] is True
    assert _done(run_dir)["repaired_rounds"] == 1
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
    # Round 2 meets setup on every corner, but hold fails at ff.
    assert [r["repair"] for r in rounds] == [True, True, False]
    assert rounds[0]["corners"]["ss"]["wns_setup_ps"] == -300.0
    assert rounds[1]["corners"]["ff"]["wns_hold_ps"] == -20.0
    for script in ("r1.extract", "r1.repair"):
        text = (run_dir / f"06_post_route_repair.{script}.tcl").read_text()
        assert re.search(r"^read_spef -corner ss .*\.r1\.spef\]$", text, re.MULTILINE)
        assert re.search(r"^read_spef -corner ff .*\.r1\.spef\]$", text, re.MULTILINE)
        assert not re.search(r"^read_spef \[", text, re.MULTILINE)


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
    assert "1 DRC violation(s)" in _done(run_dir)["reason"]
    assert res.results["drc_count"] == 1
    # The stage's checkpoint keeps the last round's router reports.
    kept = run_dir / "06_post_route_repair.route.drc.rpt"
    assert kept.read_text().startswith("violation type: Short")
    maze = run_dir / "06_post_route_repair.route.maze.log"
    assert maze.read_text().strip() == "maze r1"


def test_an_error_line_in_a_round_ends_the_stage(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        stages=True,
        env={"RB_STUB_SETUP": "-0.2 -0.1", "RB_STUB_ERROR": "1"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert "06_post_route_repair" in res.results["desc"]
    assert "ERROR" in res.results["desc"]
    (run_dir,) = _runs(artefacts)
    assert _rounds(run_dir) == []
    assert not (run_dir / "06_post_route_repair.r1.repair.log").exists()
    assert not (run_dir / "07_finish.log").exists()


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


@pytest.mark.parametrize("rcx", [True, False])
def test_a_hardened_block_records_the_rcx_rules_it_was_extracted_with(
    tmp_path, monkeypatch, rcx
):
    backend, _artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR if rcx else None, rcx=rcx
    )
    monkeypatch.setattr(
        backend.pnr_cfg,
        "resolve_synth_cfg",
        lambda: MagicMock(get_top=lambda: "t", get_name=lambda: "s"),
    )
    platform = backend.root_cfg.get_pnr_platform_cfg.return_value

    inputs = backend.abstract_inputs(platform)

    if rcx:
        assert inputs["rcx_rules"].endswith("rcx.rules")
    else:
        assert "rcx_rules" not in inputs


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


def test_a_staged_run_without_repair_resumes_into_post_route_repair(
    tmp_path, monkeypatch
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
        env={"RB_STUB_SETUP": "-0.2 0.01"},
    )
    res = repair.run()

    assert res.results["result"] == "PASS", res.results
    resumed = next(p for p in _runs(artefacts) if p != plain_dir)
    assert [r["round"] for r in _rounds(resumed)] == [1, 2]
    r1 = (resumed / "06_post_route_repair.r1.repair.tcl").read_text()
    assert f'set RB_PRED_DIR "{plain_dir}"' in r1


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
