"""Tests for the optimisation steps the flow runs as ORFS does, each behind a key that turns it off.

The steps are the floorplan's setup repair, `improve_placement` and `optimize_mirroring` after the placement stage's legalization, `repair_clock_inverters` before CTS, the repair after global route, and antenna repair after global route and after detailed route. With every step off, every script a run writes renders as the flow did without them.
"""

import os
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import _backend, _pdk, _platform, _pnr_cfg

from rtl_buddy.config.pdk import PlacementFile
from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.config.pnr_platform import read_run_overrides
from rtl_buddy.config.pnr_repair import GlobalRouteRepair, RepairDesign
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

#: Platform fields that turn every step off, the global-route repair included.
STEPS_OFF = dict(
    floorplan_setup_repair=False,
    cts_repair_clock_inverters=False,
    global_route_antenna_repair=False,
    detailed_route_antenna_repair=False,
    global_route_repair=None,
    placement=PlacementFile(improve_placement=False, optimize_mirroring=False),
)

#: The template placeholders the steps fill; `repair_clock_inverters` goes into the `before-cts` block.
_STEP_PLACEHOLDERS = (
    "{{ floorplan_setup_repair }}",
    "{{ detailed_placement_optimization }}",
    "{{ global_route_antenna_repair }}",
    "{{ detailed_route_antenna_repair }}",
)

_FLOORPLAN_REPAIR = (
    'puts ">>> Floorplan setup repair"\n'
    'repair_timing -setup -sequence "unbuffer,sizeup,swap,vt_swap" -skip_last_gasp\n'
)


def _scripts(tmp_path, platform, *, kind="one session", template=None, mode=None):
    """Every script a run on `platform` writes, by file name; `template` replaces the flow template."""
    pnr_cfg = _pnr_cfg(
        tmp_path,
        CHECKPOINT_STAGES if kind == "checkpoints" else None,
        stages=kind == "stages",
    )
    if mode is not None:
        pnr_cfg.mode = mode
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    if kind != "one session":
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
        os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    if template is not None:
        backend._load_template = lambda: template
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
    written = {"pnr.tcl": script.read_text()}
    if backend._ckpt_run_dir:
        for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
            written[path.name] = path.read_text()
    return written


def _flow(tmp_path, **platform) -> str:
    return _scripts(tmp_path, _platform(_pdk(tmp_path), **platform))["pnr.tcl"]


def test_the_steps_run_by_default_in_orfs_order(tmp_path):
    text = _flow(tmp_path)
    assert _FLOORPLAN_REPAIR in text
    assert (
        'puts ">>> Detail placement (legalize)"\n'
        "detailed_placement\n"
        'puts ">>> Detail placement optimization"\n'
        "improve_placement\n"
        "optimize_mirroring\n"
        "rb::global_connect\n"
    ) in text
    assert ("\nrepair_clock_inverters\nclock_tree_synthesis") in text
    assert (
        'puts ">>> Global-route antenna repair"\n'
        "repair_antennas -iterations 5\n"
        "check_placement -verbose\n"
        "check_antennas -report_file $OUT_DIR/grt_antennas.rpt\n"
        "rb::global_connect\n"
    ) in text
    assert (
        "    -verbose 1\n"
        'puts ">>> Detailed-route antenna repair"\n'
        "set rb_antenna_rounds 1\n"
        "if {[repair_antennas]} {\n"
        "  detailed_route \\\n"
    ) in text
    assert (
        "while {[check_antennas] && $rb_antenna_rounds < 5} {\n"
        "  repair_antennas\n"
        "  detailed_route \\\n"
        "      -output_drc  $OUT_DIR/route.drc.rpt \\\n"
        "      -output_maze $OUT_DIR/route.maze.log \\\n"
        "      -verbose 1\n"
        "  incr rb_antenna_rounds\n"
        "}\n"
        "check_antennas -report_file $OUT_DIR/drt_antennas.rpt\n"
        "rb::global_connect\n"
    ) in text
    order = [
        ">>> Floorplan setup repair",
        ">>> Macro placement",
        ">>> Global placement",
        "improve_placement",
        "repair_clock_inverters",
        "clock_tree_synthesis",
        ">>> Global-route repair",
        ">>> Global-route antenna repair",
        ">>> Detail route",
        ">>> Detailed-route antenna repair",
        ">>> Fill insertion",
    ]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions), order


def test_the_repair_after_global_route_is_orfs_by_default(tmp_path):
    platform = _platform(_pdk(tmp_path))
    assert platform.get_global_route_repair() == GlobalRouteRepair(
        repair_design=RepairDesign(), setup_margin=0.0, hold_margin=0.0
    )
    text = _flow(tmp_path)
    assert (
        'puts ">>> Global-route repair (reroute incremental)"\n'
        "estimate_parasitics -global_routing\n"
        'puts ">>> Global-route repair: repair_design"\n'
        "repair_design\n"
    ) in text
    assert "repair_timing -setup\n" in text[text.index(">>> Global-route repair") :]


@pytest.mark.parametrize(
    "field, value, gone",
    [
        ("floorplan_setup_repair", False, "repair_timing -setup -sequence"),
        ("cts_repair_clock_inverters", False, "repair_clock_inverters"),
        ("global_route_antenna_repair", False, "repair_antennas -iterations"),
        ("detailed_route_antenna_repair", False, "rb_antenna_rounds"),
        ("global_route_repair", None, ">>> Global-route repair"),
        ("placement", PlacementFile(improve_placement=False), "improve_placement"),
        ("placement", PlacementFile(optimize_mirroring=False), "optimize_mirroring"),
    ],
)
def test_each_key_turns_its_step_off_alone(tmp_path, field, value, gone):
    default = _flow(tmp_path)
    text = _flow(tmp_path, **{field: value})
    assert gone in default
    assert gone not in text
    others = [m for m in ("rb_antenna_rounds", "repair_clock_inverters") if m != gone]
    for marker in others:
        assert marker in text


def test_with_both_placement_steps_off_the_placement_stage_is_as_before(tmp_path):
    text = _flow(
        tmp_path,
        placement=PlacementFile(improve_placement=False, optimize_mirroring=False),
    )
    assert ">>> Detail placement optimization" not in text
    assert (
        'puts ">>> Detail placement (legalize)"\ndetailed_placement\nrb::global_connect\n'
        in text
    )


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_with_every_step_off_every_script_renders_as_the_flow_without_them(
    tmp_path, kind
):
    platform = _platform(_pdk(tmp_path), **STEPS_OFF)
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    for placeholder in _STEP_PLACEHOLDERS:
        assert template.count(placeholder) == 1
        template = template.replace(placeholder, "")

    now = _scripts(tmp_path, platform, kind=kind)
    before = _scripts(tmp_path, platform, kind=kind, template=template)

    assert now == before
    assert len(now) == (7 if kind == "stages" else 1)


def test_a_run_turns_the_steps_off_over_its_platform(tmp_path):
    platform = _platform(_pdk(tmp_path))
    overrides = read_run_overrides(
        "pnr run 'r'",
        routing_layers=None,
        cts_buffer=None,
        cts_clock_nets=None,
        cts_repair_clock_nets=None,
        cts_latency_balancing=None,
        post_cts_setup_repair=None,
        routing_layer_adjustment=None,
        port_buffer=None,
        tapcell_tcl=None,
        config_dir=str(tmp_path),
        floorplan_setup_repair=False,
        cts_repair_clock_inverters=False,
        global_route_antenna_repair=False,
        detailed_route_antenna_repair=False,
    )
    run = platform.with_run_overrides(overrides).with_run_placement(
        PlacementFile(improve_placement=False, optimize_mirroring=False), "r"
    )
    assert run.get_floorplan_setup_repair() is False
    assert run.get_cts_repair_clock_inverters() is False
    assert run.get_global_route_antenna_repair() is False
    assert run.get_detailed_route_antenna_repair() is False
    assert run.get_placement_improve_placement() is False
    assert run.get_placement_optimize_mirroring() is False
    # The platform keeps its own.
    assert platform.get_floorplan_setup_repair() is True
    assert platform.get_placement_improve_placement() is True


def test_a_probe_skips_the_floorplan_repair_and_the_antenna_repair_after_global_route(
    tmp_path,
):
    from rtl_buddy.config.pnr_repair import PnrMode

    scripts = _scripts(
        tmp_path, _platform(_pdk(tmp_path)), kind="checkpoints", mode=PnrMode.PROBE
    )
    text = scripts["pnr.tcl"]
    assert 'puts ">>> Floorplan setup repair: skipped (mode: probe)"\n' in text
    assert 'puts ">>> Global-route antenna repair: skipped (mode: probe)"\n' in text
    assert "\nrepair_timing" not in text
    assert "\nrepair_antennas" not in text


def test_allow_congestion_reaches_the_antenna_repair_after_global_route(tmp_path):
    from rtl_buddy.config.pnr_global_route import PnrGlobalRouteFile

    text = _flow(tmp_path, global_route=PnrGlobalRouteFile(allow_congestion=True))
    assert "repair_antennas -iterations 5 -allow_congestion\n" in text


@pytest.mark.parametrize("antenna_repair", [False, True])
def test_the_flow_ends_the_detailed_route_stage_after_the_antenna_repair(
    tmp_path, antenna_repair
):
    """The antenna repair routes again, so the stage does not end on leaving the first detailed route."""
    platform = _platform(_pdk(tmp_path), detailed_route_antenna_repair=antenna_repair)
    text = _scripts(tmp_path, platform, kind="checkpoints")["pnr.tcl"]
    assert ("detailed_route {detailed_route leave}" in text) is not antenna_repair
    ends = "rb::ckpt::stage_end detailed_route\n"
    assert (ends in text) is antenna_repair
    if antenna_repair:
        assert text.index("check_antennas -report_file $OUT_DIR/drt_antennas.rpt") < (
            text.index(ends)
        )
        assert text.index(ends) < text.index(">>> Fill insertion")


def test_with_checkpoints_the_place_stage_ends_before_the_clock_inverters(tmp_path):
    text = _scripts(tmp_path, _platform(_pdk(tmp_path)), kind="checkpoints")["pnr.tcl"]
    assert (
        "rb::ckpt::checkpoint_for clock_tree_synthesis enter\nrepair_clock_inverters\n"
    ) in text


@pytest.mark.parametrize("stages", [False, True])
def test_antenna_repair_routes_again_while_violations_remain(
    tmp_path, monkeypatch, stages
):
    platform = _platform(_pdk(tmp_path), global_route_antenna_repair=False)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_ANTENNAS": "1"}, stages=stages
    )
    backend.root_cfg.get_pnr_platform_cfg.return_value = platform

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    log = (artefacts / "pnr.log").read_text()
    drt = log.split(">>> Detailed-route antenna repair")[1].split(">>> Fill insertion")[
        0
    ]
    # The first repair clears the violation, so one detailed route follows and the loop ends.
    assert drt.count("stub: repair_antennas") == 1
    assert drt.count("stub: detailed_route") == 1


@pytest.mark.parametrize("stages", [False, True])
def test_antenna_repair_after_detailed_route_stops_after_five_routes(
    tmp_path, monkeypatch, stages
):
    platform = _platform(_pdk(tmp_path), global_route_antenna_repair=False)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_ANTENNAS": "100"}, stages=stages
    )
    backend.root_cfg.get_pnr_platform_cfg.return_value = platform

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    log = (artefacts / "pnr.log").read_text()
    drt = log.split(">>> Detailed-route antenna repair")[1].split(">>> Fill insertion")[
        0
    ]
    assert drt.count("stub: detailed_route") == 5
    assert drt.count("stub: repair_antennas") == 5


def test_a_run_that_fails_rsz_0089_names_the_wire_rc_and_the_fix(tmp_path, monkeypatch):
    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=None,
        env={"RB_STUB_FAIL": "repair_timing", "RB_STUB_FAIL_CODE": "RSZ-0089"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    desc = res.results["desc"]
    assert "setup repair found no wire RC (RSZ-0089)" in desc
    assert "layer-rc-tcl (see rb docs show concepts/pnr#set-wire-rc)" in desc
    assert (
        "floorplan-setup-repair: false, post-cts-setup-repair: false and a "
        "global-route-repair without setup-margin, such as {hold-margin: 0}"
    ) in desc


def test_another_failure_has_no_wire_rc_hint(tmp_path, monkeypatch):
    backend, _ = _backend(
        tmp_path, monkeypatch, checkpoints=None, env={"RB_STUB_FAIL": "repair_timing"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert "wire RC" not in res.results["desc"]


def test_a_step_turned_off_enters_the_block_digest(tmp_path):
    from rtl_buddy.tools.pnr_abstract import _cts_controls_digest, _flow_steps_digest

    default = _platform(_pdk(tmp_path))
    off = _platform(_pdk(tmp_path), **STEPS_OFF)
    assert _flow_steps_digest(default) == {}
    assert _flow_steps_digest(off) == {
        "floorplan_setup_repair": False,
        "global_route_antenna_repair": False,
        "detailed_route_antenna_repair": False,
    }
    assert "cts_repair_clock_inverters" not in _cts_controls_digest(default)
    assert _cts_controls_digest(off)["cts_repair_clock_inverters"] is False
