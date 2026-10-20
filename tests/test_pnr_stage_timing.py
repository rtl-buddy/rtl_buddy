"""Tests for the per-corner timing event at each P&R stage end.

Runs use the stub OpenROAD harness of `test_pnr_checkpoints` plus the round stubs of
`test_pnr_repair` and `_TIMING_STUBS`: a slack is a function of the parasitics the
session set last (`none`, `placement`, `global_routing`, `extracted`) and of the
corner, so an event's numbers show which parasitics it was timed on, and the stub
reports print the same numbers for a comparison with the event.

Pinned behaviour:

- every stage of a one-session or staged run ends with one `stage_end` timing event,
  after its checkpoint, on the parasitics the stage's own report uses;
- global-route repair adds a `before_repair` event, each post-route round a `round`
  event, and the stage keeps the kept round's;
- a side with no constrained path is null with its TNS, a query that fails gives null
  with the reason, and neither fails the run or changes its result;
- the manifest lists each stage's events.
"""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from test_pnr_checkpoints import (
    _HARNESS,
    _backend,
    _events,
    _pdk,
    _platform,
    _pnr_cfg,
    _render,
    _runs,
)
from test_pnr_repair import _ROUND_STUBS, _rcx_pdk

from rtl_buddy.tools import pnr_checkpoints, pnr_openroad
from rtl_buddy.tools.pnr_stages import TEMPLATE_STAGES

# Slack in seconds by parasitics (k = 1..4) and corner index c: setup -100k - 10c ps,
# hold 20k + c ps, setup TNS three times the setup slack, hold TNS 0. Each post-route
# round improves k by 0.5.
# `RB_STUB_UNCONSTRAINED=setup|hold|both` gives that side INF, as OpenSTA does for a
# side with no constrained path; `RB_STUB_TIMING_FAIL=1` makes the queries rb's timing
# events make fail, not the flow's reports; `RB_STUB_ESTIMATE_FAIL=<n>` fails the n-th
# `estimate_parasitics -placement` of a session, which is the `place` stage end's (the
# flow's own is the first).
_TIMING_STUBS = r"""
namespace eval sta {}
set rb_parasitics none
set rb_placement_estimates 0
proc estimate_parasitics {args} {
    global rb_parasitics rb_placement_estimates env
    if {[lindex $args 0] eq "-placement"} { incr rb_placement_estimates }
    if {[info exists env(RB_STUB_ESTIMATE_FAIL)]
        && $rb_placement_estimates == $env(RB_STUB_ESTIMATE_FAIL)} {
        error "EST-0001 no layer RC"
    }
    set rb_parasitics [string trimleft [lindex $args 0] -]
    puts "stub: estimate_parasitics $args"
}
proc read_spef {args} {
    global rb_parasitics
    set rb_parasitics extracted
}
proc rb_in_ckpt {} {
    for {set i [expr {[info level] - 1}]} {$i > 0} {incr i -1} {
        if {[string match "rb::ckpt::*" [lindex [info level $i] 0]]} { return 1 }
    }
    return 0
}
proc rb_slacks {corner} {
    global rb_parasitics env
    if {[info exists env(RB_STUB_TIMING_FAIL)] && [rb_in_ckpt]} {
        error "STA-9999 timing failed"
    }
    set k [dict get {none 1 placement 2 global_routing 3 extracted 4} $rb_parasitics]
    if {$rb_parasitics eq "extracted" && [rb_round] ne ""} {
        set k [expr {4 - 0.5 * [rb_round]}]
    }
    set c [expr {$corner eq "" ? 0 : [lsearch -exact {tt ss ff} $corner]}]
    set setup [expr {-1e-10 * $k - 1e-11 * $c}]
    set hold [expr {2e-11 * $k + 1e-12 * $c}]
    set tns [expr {$setup * 3}]
    set tns_hold 0.0
    set open [expr {[info exists env(RB_STUB_UNCONSTRAINED)] ? $env(RB_STUB_UNCONSTRAINED) : ""}]
    if {$open in {setup both}} { set setup 1e30; set tns 0.0 }
    if {$open in {hold both}} { set hold 1e30 }
    return [list $setup $hold $tns $tns_hold]
}
proc rb_ns {value} {
    if {$value >= 1e20} { return INF }
    return [format %.3f [expr {$value * 1e9}]]
}
proc sta::find_scene {name} { return $name }
proc sta::format_time {value digits} { return [rb_ns $value] }
proc sta::worst_slack_cmd {kind} { return [lindex [rb_slacks ""] [expr {$kind eq "max" ? 0 : 1}]] }
proc sta::total_negative_slack_cmd {kind} { return [lindex [rb_slacks ""] [expr {$kind eq "max" ? 2 : 3}]] }
proc sta::worst_slack_scene {scene kind} { return [lindex [rb_slacks $scene] [expr {$kind eq "max" ? 0 : 1}]] }
proc sta::total_negative_slack_scene_cmd {scene kind} { return [lindex [rb_slacks $scene] [expr {$kind eq "max" ? 2 : 3}]] }
proc report_worst_slack {args} {
    set side [expr {[lsearch $args -min] >= 0 ? 1 : 0}]
    puts "worst slack [expr {$side ? "min" : "max"}] [rb_ns [lindex [rb_slacks ""] $side]]"
}
proc report_tns {args} {
    if {[lsearch $args -min] >= 0} {
        puts "tns min [rb_ns [lindex [rb_slacks ""] 3]]"
    } else {
        puts "tns max [rb_ns [lindex [rb_slacks ""] 2]]"
    }
}
"""

_NS = {"none": 1, "placement": 2, "global_route": 3, "extracted": 4}

_STAGES = list(TEMPLATE_STAGES)


def _expected(parasitics: str, corner: int = 0) -> dict:
    k = _NS[parasitics]
    return {
        "wns_setup_ps": pytest.approx(-100.0 * k - 10.0 * corner),
        "wns_hold_ps": pytest.approx(20.0 * k + corner),
        "tns_ps": pytest.approx(-300.0 * k - 30.0 * corner),
        "tns_hold_ps": pytest.approx(0.0),
    }


def _configure(
    backend, tmp_path, *, gr_repair=None, post_route=None, corners=None, rcx=False
):
    """Give a stub-harness backend the timing stubs and a platform with the repair knobs and corners."""
    pdk_corners = {c: "pdk/lib/typ.lib" for c in corners} if corners else None
    if rcx:
        pdk = _rcx_pdk(tmp_path, pdk_corners)
    else:
        pdk = _pdk(tmp_path, **({"corners": pdk_corners} if corners else {}))
    extra = {}
    if gr_repair is not None:
        extra["global_route_repair"] = gr_repair
    if post_route is not None:
        extra["post_route_repair"] = post_route
    if corners:
        extra["sta_corners"] = list(corners)
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk, **extra)
    (tmp_path / "harness.tcl").write_text(
        _HARNESS.replace(
            "source [lindex $argv 0]",
            _ROUND_STUBS + _TIMING_STUBS + "source [lindex $argv 0]",
        )
    )


def _run(tmp_path, monkeypatch, *, stages=False, env=None, configure=None, **options):
    pnr_cfg = replace(_pnr_cfg(tmp_path, stages=stages))
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        pnr_cfg=pnr_cfg,
        stages=stages,
        env=env,
        **options,
    )
    _configure(backend, tmp_path, **(configure or {}))
    res = backend.run()
    return res, artefacts


def _timing(run_dir, stage=None) -> list[dict]:
    return [
        e
        for e in _events(run_dir)
        if e["event"] == "stage_timing" and stage in (None, e["stage"])
    ]


def _by_stage(run_dir) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in _timing(run_dir):
        out.setdefault(e["stage"], []).append(e)
    return out


_SOURCE = {
    "floorplan": "none",
    "place": "placement",
    "cts": "placement",
    "global_route": "global_route",
    "detailed_route": "global_route",
    "finish": "global_route",
}


@pytest.mark.parametrize("stages", [False, True])
def test_each_stage_end_is_timed_on_the_parasitics_of_its_own_report(
    tmp_path, monkeypatch, stages
):
    res, artefacts = _run(tmp_path, monkeypatch, stages=stages)

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    timed = _by_stage(run_dir)
    assert list(timed) == _STAGES
    for stage, (event,) in timed.items():
        assert event["point"] == "stage_end"
        assert event["parasitics"] == _SOURCE[stage]
        assert event["index"] == pnr_checkpoints.STAGE_INDEX[stage]
        assert event["corners"] == {
            "typ": _expected("global_route" if stage == "finish" else _SOURCE[stage])
        }
    # `typ` is the platform's one corner; no multi-corner entry is invented.
    # A stage is timed after its checkpoint and before its end is recorded.
    kinds = [(e["event"], e.get("stage")) for e in _events(run_dir)]
    for stage in _STAGES[:-1]:
        assert (
            kinds.index(("checkpoint", stage))
            < kinds.index(("stage_timing", stage))
            < kinds.index(("stage_end", stage))
        )
    assert kinds.index(("stage_timing", "finish")) < kinds.index(
        ("stage_end", "finish")
    )


def test_the_estimate_a_stage_end_sets_is_the_one_its_report_sets(
    tmp_path, monkeypatch
):
    res, artefacts = _run(tmp_path, monkeypatch, stages=True)

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    commands = {
        "place": "estimate_parasitics -placement",
        "cts": "estimate_parasitics -placement",
        "global_route": "estimate_parasitics -global_routing",
        "detailed_route": "estimate_parasitics -global_routing",
    }
    for stage, command in commands.items():
        log = (
            run_dir / f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}.log"
        ).read_text()
        assert f"stub: {command}" in log, stage
        assert command in pnr_openroad._STAGE_PARASITICS[stage]
    floorplan = (run_dir / "01_floorplan.log").read_text()
    assert "estimate_parasitics" not in floorplan.split(">>> Initializing")[-1]
    assert pnr_openroad._STAGE_PARASITICS["floorplan"] == ""


@pytest.mark.parametrize("stage", ["place", "cts", "global_route", "detailed_route"])
@pytest.mark.parametrize("stages", [False, True])
def test_a_stage_event_equals_the_numbers_of_its_stage_report(
    tmp_path, monkeypatch, stage, stages
):
    res, artefacts = _run(tmp_path, monkeypatch, stages=stages, stop_after=stage)

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (event,) = _timing(run_dir, stage)
    log = (artefacts / "pnr.log").read_text()
    report = log[log.rindex(f">>> Stage report {stage}") :]
    values = {
        key: float(m.group(1))
        for key, m in (
            (k, re.search(rf"^{pat}\s+(\S+)$", report, re.MULTILINE))
            for k, pat in {
                "wns_setup_ps": "worst slack max",
                "wns_hold_ps": "worst slack min",
                "tns_ps": "tns max",
                "tns_hold_ps": "tns min",
            }.items()
        )
    }
    (corner,) = event["corners"].values()
    assert {k: v * 1000.0 for k, v in values.items()} == {
        k: pytest.approx(corner[k]) for k in values
    }


@pytest.mark.parametrize("stages", [False, True])
def test_global_route_repair_is_timed_before_it_runs_and_at_the_stage_end(
    tmp_path, monkeypatch, stages
):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        stages=stages,
        configure={"gr_repair": {"setup-margin": 0.1}},
    )

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    before, end = _timing(run_dir, "global_route")
    assert (before["point"], end["point"]) == ("before_repair", "stage_end")
    assert before["parasitics"] == end["parasitics"] == "global_route"
    kinds = [(e["event"], e.get("stage") or e.get("step")) for e in _events(run_dir)]
    routed = kinds.index(("step_end", "global_route"))
    repair = kinds.index(("step_begin", "repair_timing"), routed)
    assert (
        routed
        < kinds.index(("stage_timing", "global_route"))
        < repair
        < kinds.index(("checkpoint", "global_route"))
    )
    # One event per other stage.
    assert [len(v) for v in _by_stage(run_dir).values()] == [1, 1, 1, 2, 1, 1]


def test_a_stage_without_global_route_repair_has_no_before_repair_event(
    tmp_path, monkeypatch
):
    res, artefacts = _run(tmp_path, monkeypatch)

    (run_dir,) = _runs(artefacts)
    assert {e["point"] for e in _timing(run_dir)} == {"stage_end"}


@pytest.mark.parametrize("stages", [False, True])
def test_post_route_rounds_are_timed_on_extracted_parasitics(
    tmp_path, monkeypatch, stages
):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        stages=stages,
        configure={
            "post_route": {"setup-margin": 0.1, "rounds": 2, "margin-step": 0.05},
            "rcx": True,
        },
        env={"RB_STUB_UNCONSTRAINED": "hold"},
    )

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    events = _timing(run_dir, "post_route_repair")
    rounds = [e for e in events if e["point"] == "round"]
    assert [e["round"] for e in rounds] == [0, 1, 2]
    assert {e["parasitics"] for e in events} == {"extracted"}
    (end,) = [e for e in events if e["point"] == "stage_end"]
    # The stage reports the round it keeps.
    kept = [e for e in _events(run_dir) if e["event"] == "post_route_repair_done"][0][
        "kept_round"
    ]
    assert end["round"] == kept
    assert end["corners"] == [e for e in rounds if e["round"] == kept][0]["corners"]
    # Every stage still ends with one event, the finish's on the routed SPEF.
    ended = {
        e["stage"]: e
        for e in _events(run_dir)
        if e["event"] == "stage_timing" and e["point"] == "stage_end"
    }
    assert list(ended) == list(pnr_checkpoints.STAGE_INDEX)
    assert ended["finish"]["parasitics"] == "extracted"


def test_round_events_follow_the_extraction_they_time(tmp_path, monkeypatch):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        configure={"post_route": {"setup-margin": 0.1, "rounds": 1}, "rcx": True},
    )

    (run_dir,) = _runs(artefacts)
    round0 = [e for e in _timing(run_dir, "post_route_repair") if e["round"] == 0][0]
    assert round0["corners"] == {"typ": _expected("extracted")}
    # Round 0 times the detailed route's database, before any repair.
    kinds = [(e["event"], e.get("round")) for e in _events(run_dir)]
    assert kinds.index(("stage_timing", 0)) < kinds.index(("post_route_round", 0))


def test_a_multi_corner_platform_is_timed_per_corner(tmp_path, monkeypatch):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        configure={"corners": ["tt", "ss", "ff"]},
    )

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    for stage, (event,) in _by_stage(run_dir).items():
        assert list(event["corners"]) == ["tt", "ss", "ff"], stage
        for index, corner in enumerate(["tt", "ss", "ff"]):
            source = "global_route" if stage == "finish" else _SOURCE[stage]
            assert event["corners"][corner] == _expected(source, index)


@pytest.mark.parametrize("stages", [False, True])
@pytest.mark.parametrize(
    "unconstrained, setup_null, hold_null",
    [("setup", True, False), ("hold", False, True), ("both", True, True)],
)
def test_a_side_without_constrained_paths_is_null_not_an_error(
    tmp_path, monkeypatch, stages, unconstrained, setup_null, hold_null
):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        stages=stages,
        env={"RB_STUB_UNCONSTRAINED": unconstrained},
    )

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert list(_by_stage(run_dir)) == _STAGES
    for event in _timing(run_dir):
        (corner,) = event["corners"].values()
        assert "error" not in corner
        assert (corner["wns_setup_ps"] is None) is setup_null
        assert (corner["tns_ps"] is None) is setup_null
        assert (corner["wns_hold_ps"] is None) is hold_null
        assert (corner["tns_hold_ps"] is None) is hold_null


@pytest.mark.parametrize("stages", [False, True])
def test_a_failed_timing_query_is_recorded_and_never_fails_the_run(
    tmp_path, monkeypatch, stages
):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        stages=stages,
        env={"RB_STUB_TIMING_FAIL": "1"},
        configure={"corners": ["tt", "ss"]},
    )

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert list(_by_stage(run_dir)) == _STAGES
    for event in _timing(run_dir):
        assert list(event["corners"]) == ["tt", "ss"]
        for corner in event["corners"].values():
            assert corner["wns_setup_ps"] is None and corner["tns_hold_ps"] is None
            assert "timing failed" in corner["error"]
    log = (artefacts / "pnr.log").read_text()
    assert "timing of place (stage_end) unavailable" in log
    assert "[ERROR" not in log


def test_a_failed_parasitics_estimate_is_recorded_as_the_reason(tmp_path, monkeypatch):
    res, artefacts = _run(tmp_path, monkeypatch, env={"RB_STUB_ESTIMATE_FAIL": "2"})

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    (place,) = _timing(run_dir, "place")
    assert place["parasitics"] == "placement"
    assert "no layer RC" in place["corners"]["typ"]["error"]
    assert place["corners"]["typ"]["wns_setup_ps"] is None
    # A stage with no estimate to set is timed regardless.
    (floorplan,) = _timing(run_dir, "floorplan")
    assert "error" not in floorplan["corners"]["typ"]


def test_timing_changes_neither_the_result_nor_its_fields(tmp_path, monkeypatch):
    ok = tmp_path / "ok"
    failing = tmp_path / "failing"
    ok.mkdir()
    failing.mkdir()
    res_ok, _ = _run(ok, monkeypatch)
    res_failing, _ = _run(failing, monkeypatch, env={"RB_STUB_TIMING_FAIL": "1"})

    assert res_ok.results["result"] == res_failing.results["result"] == "PASS"
    assert set(res_ok.results) == set(res_failing.results)
    assert not [k for k in res_ok.results if "timing" in k]
    for key in ("routed", "desc", "wns_setup_ps", "tns_ps", "drc_count"):
        assert res_ok.results.get(key) == res_failing.results.get(key), key


def test_the_manifest_lists_each_stages_timing_events(tmp_path, monkeypatch):
    res, artefacts = _run(
        tmp_path,
        monkeypatch,
        configure={"gr_repair": {"setup-margin": 0.1}},
    )

    (run_dir,) = _runs(artefacts)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert list(manifest["timing"]) == _STAGES
    place = manifest["timing"]["place"]
    assert place == [
        {
            "stage": "place",
            "index": "02",
            "point": "stage_end",
            "parasitics": "placement",
            "corners": {"typ": _expected("placement")},
        }
    ]
    assert [e["point"] for e in manifest["timing"]["global_route"]] == [
        "before_repair",
        "stage_end",
    ]


def test_stage_timings_keeps_flow_order_and_drops_bookkeeping():
    events = [
        {"event": "stage_timing", "stage": "cts", "point": "stage_end", "seq": 3},
        {"event": "stage_end", "stage": "cts"},
        {
            "event": "stage_timing",
            "stage": "place",
            "point": "stage_end",
            "t": "x",
            "epoch_ms": 1,
        },
    ]

    assert pnr_checkpoints.stage_timings(events) == {
        "place": [{"stage": "place", "point": "stage_end"}],
        "cts": [{"stage": "cts", "point": "stage_end"}],
    }


def test_an_armed_flow_lists_what_each_stage_is_timed_on(tmp_path):
    text = _render(tmp_path, ("cts",)).read_text()

    arm = next(line for line in text.splitlines() if line.startswith("rb::ckpt::arm"))
    assert (
        "timed {floorplan {none {}} place {placement {estimate_parasitics -placement}}"
        in arm
    )
    assert "detailed_route {global_route {estimate_parasitics -global_routing}}" in arm
    assert 'corners {"typ"}' in arm and "scenes 0" in arm
    final = "rb::ckpt::timing_event finish stage_end global_route\n"
    assert text.count(final) == 1
    assert text.index("report_tns -min") < text.index(final)
    assert text.index(final) < text.index("report_checks -path_delay max")


def test_a_flow_without_checkpoints_has_no_timing_calls(tmp_path):
    text = _render(tmp_path, None).read_text()

    assert "timing_event" not in text and "stage_timing" not in text


def test_a_rcx_platform_times_finish_on_the_extracted_spef(tmp_path):
    from unittest.mock import MagicMock

    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    from test_pnr_checkpoints import _write_synth_config

    _write_synth_config(tmp_path)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    text = Path(
        backend._write_script(
            _platform(_rcx_pdk(tmp_path)), backend.pnr_cfg.get_floorplan()
        )
    ).read_text()

    assert "rb::ckpt::timing_event finish stage_end extracted\n" in text


def test_the_extraction_session_times_its_round_on_the_extracted_spef(tmp_path):
    from unittest.mock import MagicMock

    from test_pnr_checkpoints import _write_synth_config

    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write_synth_config(tmp_path)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_pnr_cfg(tmp_path, stages=True),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    backend._ps_per_unit = 1.0
    backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    Path(backend._ckpt_run_dir).mkdir(parents=True)
    platform = _platform(
        _rcx_pdk(tmp_path), post_route_repair={"setup-margin": 0.1, "rounds": 1}
    )
    backend._write_script(platform, backend.pnr_cfg.get_floorplan())
    script = backend._post_route_extract_script(1, backend._ckpt_run_dir)

    call = "rb::ckpt::timing_event post_route_repair round extracted 1\n"
    assert script.count(call) == 1
    assert script.index("read_spef") < script.index(call)
    assert script.index(call) < script.index("report_worst_slack -max")
