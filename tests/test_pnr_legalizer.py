"""`placement.legalizer`: the engine each stage's `detailed_placement` calls use (rtl_buddy#1019).

Scripts are rendered without OpenROAD; the setup checks run `OpenRoadPnr.run` up to the point where it would start OpenROAD.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import (
    MacroPinLimitsFile,
    PdkConfig,
    PdkConfigFile,
    PdkPinLayersFile,
    PlacementFile,
    validate_placement,
)
from rtl_buddy.config.pnr import (
    CHECKPOINT_STAGES,
    POST_ROUTE_REPAIR_STAGE,
    PnrConfig,
    PnrFloorplan,
    PnrMacro,
)
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_openroad, pnr_stages
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

_DIAMOND = " -use_diamond_legalizer"

#: The negotiation-then-diamond policy, by stage.
_SPLIT = {
    "place": "negotiation",
    "cts": "negotiation",
    "global_route": "diamond",
    "post_route_repair": "diamond",
}

_ROUNDS = {"setup-margin": 0.1, "rounds": 2}

_DPL_RE = re.compile(r"^detailed_placement\b.*$", re.MULTILINE)


def _dpl(text: str) -> list[str]:
    return _DPL_RE.findall(text)


def _platform(tmp_path, legalizer=None, *, pdk_legalizer=None, **fields):
    """A Nangate45-shaped platform whose PDK has `rcx-rules`; `fields` are further platform fields, `placement` fields included."""
    (tmp_path / "pdk").mkdir(exist_ok=True)
    (tmp_path / "pdk" / "rcx.rules").write_text("rules\n")
    pdk = PdkConfig(
        PdkConfigFile(
            name="nangate45",
            pin_layers=PdkPinLayersFile(horizontal="metal3", vertical="metal2"),
            site="FreePDK45_38x28_10R_NP_162NW_34O",
            corners={"typ": "pdk/lib/typ.lib"},
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
            tie_hi="LOGIC1_X1/Z",
            tie_lo="LOGIC0_X1/Z",
            fill_cells=["FILLCELL_X1"],
            rcx_rules="pdk/rcx.rules",
            placement=PlacementFile(legalizer=pdk_legalizer),
        ),
        str(tmp_path / "root_config.yaml"),
    )
    placement = fields.pop("placement", {})
    return PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            cts_buffer="BUF_X4",
            routing_layers=PnrRoutingLayersFile(signal="metal2-metal6", clock="metal4"),
            placement=PlacementFile(legalizer=legalizer, **placement),
            **fields,
        ),
        lambda _name: pdk,
    )


def _floorplan(**fields) -> PnrFloorplan:
    return PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0, **fields)


def _backend(tmp_path, platform, *, stages=False, floorplan=None) -> OpenRoadPnr:
    sdc = tmp_path / "constraints.sdc"
    sdc.write_text("create_clock -period 1 [get_ports clk]\n")
    pnr_cfg = PnrConfig(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(sdc),
        platform="nangate45_typ",
        floorplan=floorplan or _floorplan(),
        _reglvl=0,
        tool_overrides=None,
        checkpoints=CHECKPOINT_STAGES if stages else None,
        stages=stages,
    )
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), root_cfg)
    # A nanosecond library, for the post-route margins; a fixed netlist, so two renders compare.
    backend._ps_per_unit = 1000.0
    backend._openroad_netlist = str(tmp_path / "demo_top.v")
    return backend


def _one_session(tmp_path, platform, floorplan=None) -> str:
    backend = _backend(tmp_path, platform, floorplan=floorplan)
    fp = backend.pnr_cfg.get_floorplan()
    return Path(backend._write_script(platform, fp)).read_text()


def _staged(tmp_path, platform) -> dict[str, str]:
    """Every session script of a staged run, by stage; the post-route rounds by file name, and the stage itself as its record."""
    backend = _backend(tmp_path, platform, stages=True)
    backend._prepare_flow(platform, backend.pnr_cfg.get_floorplan())
    scripts = {stage: backend._stage_script(stage, "RUN") for stage in backend._stages}
    scripts.update(backend._post_route_scripts("RUN"))
    return scripts


# -- configuration -----------------------------------------------------------------


def test_unset_names_no_stage(tmp_path):
    assert _platform(tmp_path).get_placement_legalizer() == {}


def test_one_engine_stands_for_every_stage(tmp_path):
    assert _platform(tmp_path, "diamond").get_placement_legalizer() == {
        "place": "diamond",
        "cts": "diamond",
        "global_route": "diamond",
        "post_route_repair": "diamond",
    }


def test_a_mapping_names_only_its_stages(tmp_path):
    resolved = _platform(tmp_path, {"post_route_repair": "diamond"})
    assert resolved.get_placement_legalizer() == {"post_route_repair": "diamond"}


@pytest.mark.parametrize(
    ("pdk", "platform", "run", "expected"),
    [
        ({"place": "diamond"}, None, None, {"place": "diamond"}),
        ({"place": "diamond"}, {"cts": "diamond"}, None, {"cts": "diamond"}),
        ({"place": "diamond"}, {"cts": "diamond"}, {}, {}),
        (
            None,
            {"cts": "diamond"},
            "negotiation",
            dict.fromkeys(_SPLIT, "negotiation"),
        ),
    ],
    ids=["pdk", "platform-replaces-pdk", "run-clears", "run-replaces-platform"],
)
def test_each_level_replaces_the_one_below_whole(
    tmp_path, pdk, platform, run, expected
):
    resolved = _platform(tmp_path, platform, pdk_legalizer=pdk)
    if run is not None:
        run_placement = validate_placement(PlacementFile(legalizer=run), "run")
        resolved = resolved.with_run_placement(run_placement, "r")
    assert resolved.get_placement_legalizer() == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("fast", "placement.legalizer.place must be one of negotiation, diamond"),
        (["diamond"], "placement.legalizer must be one of negotiation, diamond, or a"),
        (
            {"finish": "diamond"},
            "placement.legalizer names 'finish', not a stage that runs "
            "detailed_placement: place, cts, global_route, post_route_repair",
        ),
        (
            {"cts": "diamond-legalizer"},
            "placement.legalizer.cts must be one of negotiation, diamond, "
            "got 'diamond-legalizer'",
        ),
    ],
    ids=["engine", "type", "stage", "mapped-engine"],
)
def test_a_bad_value_fails_naming_the_key(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError, match=re.escape(message)):
        _platform(tmp_path, value)


# -- the flow ------------------------------------------------------------------------


@pytest.mark.parametrize("reroute", ["eco", "full"])
def test_each_stage_of_a_staged_run_legalizes_with_its_engine(tmp_path, reroute):
    """Unset renders every session script as before; set adds the option to the `detailed_placement` calls of the stages it chooses diamond for, and nothing else."""
    post_route = {**_ROUNDS, "reroute": reroute}
    unset = _staged(tmp_path, _platform(tmp_path, post_route_repair=post_route))
    chosen = _staged(
        tmp_path, _platform(tmp_path, _SPLIT, post_route_repair=post_route)
    )

    assert chosen.keys() == unset.keys()
    for name, text in chosen.items():
        assert text.replace(_DIAMOND, "") == unset[name], name
        assert _DIAMOND not in unset[name], name
    for name, diamond in (
        ("place", False),
        ("cts", False),
        ("global_route", True),
        (POST_ROUTE_REPAIR_STAGE, True),
        ("06_post_route_repair.r1.repair.tcl", True),
        ("06_post_route_repair.r2.repair.tcl", True),
    ):
        calls = _dpl(chosen[name])
        assert calls, name
        expected = f"detailed_placement{_DIAMOND if diamond else ''}"
        assert calls == [expected] * len(calls), name


def test_a_one_session_run_legalizes_each_stage_with_its_engine(tmp_path):
    unset = _one_session(tmp_path, _platform(tmp_path))
    chosen = _one_session(tmp_path, _platform(tmp_path, _SPLIT))

    assert chosen.replace(_DIAMOND, "") == unset
    # place 1 and CTS 2, then the global-route repair's 2.
    assert (
        _dpl(chosen)
        == ["detailed_placement"] * 3 + [f"detailed_placement{_DIAMOND}"] * 2
    )


def test_the_macro_pin_limits_step_takes_the_place_stages_engine(tmp_path):
    platform = _platform(
        tmp_path,
        {"place": "diamond"},
        placement={"macro_pin_limits": MacroPinLimitsFile(buffers=["BUF_X1"])},
    )
    text = _one_session(tmp_path, platform)

    assert f" RB-MACRO-PIN-LIMITS: [list{_DIAMOND}]\n" in text
    assert _dpl(text) == [f"detailed_placement{_DIAMOND}"] + ["detailed_placement"] * 4


def test_a_stage_the_rotated_footprint_may_turn_diamond_keeps_the_footprint(tmp_path):
    """`rotated-macro-footprint: diamond-legalizer` still decides at run time where `legalizer` leaves negotiation; a stage set to diamond passes it outright."""
    fp = _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="R90")])
    platform = _platform(
        tmp_path,
        {"place": "negotiation", "global_route": "diamond"},
        placement={"rotated_macro_footprint": "diamond-legalizer"},
    )
    text = _one_session(tmp_path, platform, fp)

    assert "  set RB_DPL_OPTIONS -use_diamond_legalizer\n" in text
    assert (
        _dpl(text)
        == ["detailed_placement {*}$RB_DPL_OPTIONS"] * 3
        + [f"detailed_placement{_DIAMOND}"] * 2
    )


# -- resume ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legalizer", "changed"),
    [
        ({"global_route": "diamond"}, {"global_route"}),
        ({"post_route_repair": "diamond"}, {POST_ROUTE_REPAIR_STAGE}),
        ({"cts": "diamond", "global_route": "diamond"}, {"cts", "global_route"}),
    ],
    ids=["global-route", "post-route", "cts-and-global-route"],
)
def test_the_choice_is_part_of_its_own_stages_identity_only(
    tmp_path, legalizer, changed
):
    """A resume compares the identity of each stage before `--from`, so a change for stages not yet run keeps the completed ones."""
    unset = _staged(tmp_path, _platform(tmp_path, post_route_repair=_ROUNDS))
    chosen = _staged(
        tmp_path, _platform(tmp_path, legalizer, post_route_repair=_ROUNDS)
    )

    differs = {
        stage
        for stage in pnr_stages.run_stages(post_route_repair=True)
        if pnr_stages.script_identity(chosen[stage])
        != pnr_stages.script_identity(unset[stage])
    }
    assert differs == changed


# -- what OpenROAD cannot honour ---------------------------------------------------


def _setup(tmp_path, monkeypatch, platform, *, has_option=True):
    """A backend whose `run` stops with `OpenROAD started` where it would start OpenROAD, and the list its option probes go to."""
    backend = _backend(tmp_path, platform)
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: "26Q2-stub")
    monkeypatch.setattr(backend, "_has_tcl_command", lambda _c: True)
    monkeypatch.setattr(backend, "_probe_sta_modes", lambda: True)

    def _no_openroad(*_a, **_k):
        raise AssertionError("OpenROAD started")

    monkeypatch.setattr(pnr_openroad, "_run_session", _no_openroad)
    asked = []
    monkeypatch.setattr(
        backend,
        "_has_command_option",
        lambda command, option: asked.append((command, option)) or has_option,
    )
    return backend, asked


def _sticky(tmp_path, platform, *, stages=False) -> str | None:
    backend = _backend(tmp_path, platform, stages=stages)
    backend._configure_stages(platform)
    return backend._sticky_legalizer_problem(
        platform, platform.get_placement_legalizer()
    )


@pytest.mark.parametrize(
    ("legalizer", "later", "earlier"),
    [
        ({"place": "diamond", "cts": "negotiation"}, "cts", "place"),
        ({"cts": "diamond", "global_route": "negotiation"}, "global_route", "cts"),
    ],
    ids=["cts-after-place", "global-route-after-cts"],
)
def test_negotiation_after_diamond_in_one_session_fails_setup(
    tmp_path, monkeypatch, legalizer, later, earlier
):
    backend, asked = _setup(tmp_path, monkeypatch, _platform(tmp_path, legalizer))
    results = backend.run().results

    assert results["fail_stage"] == "setup", results
    assert results["desc"] == (
        f"placement.legalizer sets {later} to negotiation after {earlier} to "
        "diamond, but they run in one OpenROAD session, where detailed_placement "
        "keeps diamond search once -use_diamond_legalizer is passed; set "
        "stages: true to run each stage in its own session"
    )
    assert asked == []


@pytest.mark.parametrize(
    ("legalizer", "stages", "fields"),
    [
        ({"place": "diamond", "cts": "negotiation"}, True, {}),
        (
            {"cts": "diamond", "global_route": "negotiation"},
            False,
            {"global_route_repair": None},
        ),
        (
            {"global_route": "diamond", "post_route_repair": "negotiation"},
            False,
            {"post_route_repair": _ROUNDS},
        ),
    ],
    ids=["staged", "no-global-route-legalization", "post-route-rounds"],
)
def test_negotiation_after_diamond_in_another_session_is_allowed(
    tmp_path, legalizer, stages, fields
):
    platform = _platform(tmp_path, legalizer, **fields)
    assert _sticky(tmp_path, platform, stages=stages) is None


def test_an_openroad_without_the_option_fails_setup_naming_the_key(
    tmp_path, monkeypatch
):
    backend, asked = _setup(
        tmp_path, monkeypatch, _platform(tmp_path, "negotiation"), has_option=False
    )
    results = backend.run().results

    assert results["fail_stage"] == "setup", results
    assert results["desc"] == (
        "placement.legalizer needs detailed_placement -use_diamond_legalizer, with "
        "negotiation the default engine (OpenROAD 26Q4 or newer); OpenROAD "
        "26Q2-stub does not take it"
    )
    assert asked == [("detailed_placement", "-use_diamond_legalizer")]


def test_an_unset_legalizer_asks_openroad_nothing(tmp_path, monkeypatch):
    backend, asked = _setup(tmp_path, monkeypatch, _platform(tmp_path))

    with pytest.raises(AssertionError, match="OpenROAD started"):
        backend.run()
    assert ("detailed_placement", "-use_diamond_legalizer") not in asked


def test_the_unsupported_message_names_the_key():
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "pnr.legalizer_unsupported", {"pnr": "r", "version": "26Q1"}
    )
    assert message == (
        'P&R "r": placement.legalizer needs detailed_placement '
        "-use_diamond_legalizer, which OpenROAD 26Q1 does not take; unset the key "
        "or use OpenROAD 26Q4 or newer"
    )
