"""`placement.legalizer`: the engine each stage's `detailed_placement` calls use (rtl_buddy#1019).

Sessions run under the stub OpenROAD harness of `test_pnr_checkpoints`.
"""

import re

import pytest
from test_pnr_checkpoints import _backend, _pdk, _platform, _pnr_cfg, _runs
from test_pnr_hook_points import _ROUNDS, _ROUNDS_ENV, _run_backend, _scripts
from test_pnr_planner_files import _floorplan, _render

from rtl_buddy.config.pdk import MacroPinLimitsFile, PlacementFile, validate_placement
from rtl_buddy.config.pnr import PnrMacro
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.pnr_results import PnrPassResults

_DIAMOND = " -use_diamond_legalizer"

#: The negotiation-then-diamond policy, by stage.
_SPLIT = {
    "place": "negotiation",
    "cts": "negotiation",
    "global_route": "diamond",
    "post_route_repair": "diamond",
}

_DPL_RE = re.compile(r"^detailed_placement\b.*$", re.MULTILINE)


def _dpl(text: str) -> list[str]:
    return _DPL_RE.findall(text)


def _legalizer(tmp_path, pdk=None, platform=None):
    pdk_cfg = _pdk(tmp_path, **({"placement": PlacementFile(legalizer=pdk)}))
    return _platform(pdk_cfg, placement=PlacementFile(legalizer=platform))


# -- configuration -----------------------------------------------------------------


def test_unset_names_no_stage(tmp_path):
    assert _legalizer(tmp_path).get_placement_legalizer() == {}


def test_one_engine_stands_for_every_stage(tmp_path):
    assert _legalizer(tmp_path, platform="diamond").get_placement_legalizer() == {
        "place": "diamond",
        "cts": "diamond",
        "global_route": "diamond",
        "post_route_repair": "diamond",
    }


def test_a_mapping_names_only_its_stages(tmp_path):
    resolved = _legalizer(tmp_path, platform={"post_route_repair": "diamond"})
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
    resolved = _legalizer(tmp_path, pdk, platform)
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
        _legalizer(tmp_path, platform=value)


# -- the flow ------------------------------------------------------------------------


@pytest.mark.parametrize("reroute", ["eco", "full"])
@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_each_stage_legalizes_with_its_engine(tmp_path, monkeypatch, stages, reroute):
    """Unset renders every script as before; set adds the option to the `detailed_placement` calls of the stages it chooses diamond for, and nothing else."""
    scripts = {}
    for name, legalizer in (("unset", None), ("set", _SPLIT)):
        root = tmp_path / name
        root.mkdir()
        backend, artefacts = _run_backend(
            root,
            monkeypatch,
            {},
            stages=stages,
            checkpoints=None,
            post_route={**_ROUNDS, "reroute": reroute},
            corners=["ss", "ff"],
            env=_ROUNDS_ENV,
            platform={"placement": PlacementFile(legalizer=legalizer)},
        )
        asked = []
        monkeypatch.setattr(
            backend,
            "_has_command_option",
            lambda command, option: asked.append((command, option)) or True,
        )
        assert isinstance(backend.run(), PnrPassResults)
        scripts[name] = _scripts(artefacts)
        assert asked == (
            [("detailed_placement", "-use_diamond_legalizer")] if legalizer else []
        )

    assert scripts["set"].keys() == scripts["unset"].keys()
    for name, text in scripts["set"].items():
        assert text.replace(_DIAMOND, "") == scripts["unset"][name], name
        assert _DIAMOND not in scripts["unset"][name], name
    if stages:
        for name, diamond in (
            ("02_place.tcl", False),
            ("03_cts.tcl", False),
            ("04_global_route.tcl", True),
            ("06_post_route_repair.r1.repair.tcl", True),
            ("06_post_route_repair.r2.repair.tcl", True),
        ):
            calls = _dpl(scripts["set"][name])
            assert calls, name
            assert calls == [f"detailed_placement{_DIAMOND if diamond else ''}"] * len(
                calls
            ), name
    else:
        # place 1 and CTS 2, then the global-route repair's and the rounds' in diamond.
        calls = _dpl(scripts["set"]["pnr.tcl"])
        assert calls[:3] == ["detailed_placement"] * 3
        assert len(calls) > 3
        assert calls[3:] == [f"detailed_placement{_DIAMOND}"] * (len(calls) - 3)


def test_the_macro_pin_limits_step_takes_the_place_stages_engine(tmp_path):
    platform = _platform(
        _pdk(tmp_path),
        placement=PlacementFile(
            legalizer={"place": "diamond"},
            macro_pin_limits=MacroPinLimitsFile(buffers=["BUF_X1"]),
        ),
    )
    text = _render(tmp_path, _floorplan(), platform)

    assert f" RB-MACRO-PIN-LIMITS: [list{_DIAMOND}]\n" in text
    # place, then CTS 2 and the global-route repair 2.
    assert _dpl(text) == [f"detailed_placement{_DIAMOND}"] + ["detailed_placement"] * 4


def test_a_stage_the_rotated_footprint_may_turn_diamond_keeps_the_footprint(tmp_path):
    """`rotated-macro-footprint: diamond-legalizer` still decides at run time where `legalizer` leaves negotiation; a stage set to diamond passes it outright."""
    fp = _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="R90")])
    platform = _platform(
        _pdk(tmp_path),
        placement=PlacementFile(
            rotated_macro_footprint="diamond-legalizer",
            legalizer={"place": "negotiation", "global_route": "diamond"},
        ),
    )
    text = _render(tmp_path, fp, platform)

    assert "  set RB_DPL_OPTIONS -use_diamond_legalizer\n" in text
    assert (
        _dpl(text)
        == ["detailed_placement {*}$RB_DPL_OPTIONS"] * 3
        + [f"detailed_placement{_DIAMOND}"] * 2
    )


# -- resume ------------------------------------------------------------------------


def _with_legalizer(backend, legalizer):
    pdk = backend.root_cfg.get_pnr_platform_cfg.return_value.get_pdk()
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        pdk, placement=PlacementFile(legalizer=legalizer)
    )
    backend._has_command_option = lambda *_a: True
    return backend


def test_a_change_for_stages_not_yet_run_resumes(tmp_path, monkeypatch):
    first, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=_pnr_cfg(tmp_path, stages=True), stop_after="cts"
    )
    assert first.run().results["result"] == "PASS"
    (run,) = _runs(artefacts)

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        pnr_cfg=_pnr_cfg(tmp_path, stages=True),
        resume=run.name,
        resume_from="global_route",
    )
    res = _with_legalizer(backend, {"global_route": "diamond"}).run()

    assert res.results["result"] == "PASS", res.results
    assert "accepted_changes" not in res.results
    second = next(r for r in _runs(artefacts) if r != run)
    assert _DIAMOND in (second / "04_global_route.tcl").read_text()


def test_a_change_for_a_stage_already_run_is_refused_for_that_stage_alone(
    tmp_path, monkeypatch
):
    first, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=_pnr_cfg(tmp_path, stages=True), stop_after="cts"
    )
    assert first.run().results["result"] == "PASS"
    (run,) = _runs(artefacts)

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        pnr_cfg=_pnr_cfg(tmp_path, stages=True),
        resume=run.name,
        resume_from="global_route",
    )
    res = _with_legalizer(backend, {"cts": "diamond", "global_route": "diamond"}).run()

    assert res.results["fail_stage"] == "resume", res.results
    desc = res.results["desc"]
    assert "03_cts.tcl" in desc and "(--accept-change script:cts)" in desc
    assert "02_place.tcl" not in desc


# -- what OpenROAD cannot honour ---------------------------------------------------


def _setup(
    tmp_path, monkeypatch, legalizer, *, stages=False, has_option=True, **fields
):
    backend, _ = _backend(
        tmp_path, monkeypatch, pnr_cfg=_pnr_cfg(tmp_path, stages=stages)
    )
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        _pdk(tmp_path), placement=PlacementFile(legalizer=legalizer), **fields
    )
    monkeypatch.setattr(backend, "_has_command_option", lambda *_a: has_option)
    return backend.run().results


@pytest.mark.parametrize(
    ("legalizer", "fields", "later", "earlier"),
    [
        ({"place": "diamond", "cts": "negotiation"}, {}, "cts", "place"),
        ({"cts": "diamond", "global_route": "negotiation"}, {}, "global_route", "cts"),
    ],
    ids=["cts-after-place", "global-route-after-cts"],
)
def test_negotiation_after_diamond_in_one_session_fails_setup(
    tmp_path, monkeypatch, legalizer, fields, later, earlier
):
    results = _setup(tmp_path, monkeypatch, legalizer, **fields)

    assert results["fail_stage"] == "setup", results
    assert results["desc"] == (
        f"placement.legalizer sets {later} to negotiation after {earlier} to "
        "diamond, but they run in one OpenROAD session, where detailed_placement "
        "keeps diamond search once -use_diamond_legalizer is passed; set "
        "stages: true to run each stage in its own session"
    )


@pytest.mark.parametrize(
    ("legalizer", "stages", "fields"),
    [
        ({"place": "diamond", "cts": "negotiation"}, True, {}),
        (
            {"cts": "diamond", "global_route": "negotiation"},
            False,
            {"global_route_repair": None},
        ),
    ],
    ids=["staged", "no-global-route-legalization"],
)
def test_negotiation_after_diamond_in_another_session_runs(
    tmp_path, monkeypatch, legalizer, stages, fields
):
    results = _setup(tmp_path, monkeypatch, legalizer, stages=stages, **fields)

    assert results["result"] == "PASS", results


def test_the_post_route_rounds_are_sessions_of_their_own(tmp_path, monkeypatch):
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        {},
        checkpoints=None,
        post_route=_ROUNDS,
        env=_ROUNDS_ENV,
        platform={
            "placement": PlacementFile(
                legalizer={
                    "global_route": "diamond",
                    "post_route_repair": "negotiation",
                }
            )
        },
    )
    monkeypatch.setattr(backend, "_has_command_option", lambda *_a: True)

    assert isinstance(backend.run(), PnrPassResults)
    rounds = _scripts(artefacts)["06_post_route_repair.r1.repair.tcl"]
    assert _dpl(rounds) == ["detailed_placement"]


def test_an_openroad_without_the_option_fails_setup_naming_the_key(
    tmp_path, monkeypatch
):
    results = _setup(tmp_path, monkeypatch, "negotiation", has_option=False)

    assert results["fail_stage"] == "setup", results
    assert results["desc"] == (
        "placement.legalizer needs detailed_placement -use_diamond_legalizer, with "
        "negotiation the default engine (OpenROAD 26Q4 or newer); OpenROAD "
        "26Q2-stub does not take it"
    )


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
