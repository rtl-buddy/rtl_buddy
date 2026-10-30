"""`match-cell-footprint`: `-match_cell_footprint` on every `repair_design` and `repair_timing` of `rb pnr`, as ORFS's `MATCH_CELL_FOOTPRINT` (rtl_buddy#1003).

Sessions run under the stub OpenROAD harness of `test_pnr_checkpoints`.
"""

import re

import pytest
from serde.compat import SerdeError
from test_pnr_checkpoints import _pdk
from test_pnr_hook_points import _ROUNDS, _ROUNDS_ENV, _run_backend, _scripts
from test_pnr_repair import _render
from test_pnr_run_overrides import _digest_cfg, _platform

from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools import pnr_abstract, pnr_openroad

_FLAG = " -match_cell_footprint"

#: A Tcl command line that runs a repair, not one that logs it.
_REPAIR_RE = re.compile(r"^\s*(?:repair_design|repair_timing)\b.*$", re.MULTILINE)


def _repairs(text: str) -> list[str]:
    return _REPAIR_RE.findall(text)


# -- configuration -----------------------------------------------------------------


def test_it_defaults_to_off(tmp_path):
    assert _platform(_pdk(tmp_path)).get_match_cell_footprint() is False


@pytest.mark.parametrize(
    ("pdk", "platform", "expected"),
    [
        (True, None, True),
        (None, True, True),
        (True, False, False),
        (False, True, True),
        (False, None, False),
    ],
)
def test_the_platform_overrides_the_pdk(tmp_path, pdk, platform, expected):
    resolved = _platform(
        _pdk(tmp_path, match_cell_footprint=pdk), match_cell_footprint=platform
    )
    assert resolved.get_match_cell_footprint() is expected


def test_a_non_boolean_is_refused(tmp_path):
    with pytest.raises(SerdeError, match="match_cell_footprint"):
        _pdk(tmp_path, match_cell_footprint="yes")
    with pytest.raises(SerdeError, match="match_cell_footprint"):
        _platform(_pdk(tmp_path), match_cell_footprint="yes")


# -- the flow ------------------------------------------------------------------------


def test_every_repair_of_the_flow_matches_footprints(tmp_path):
    """Placement, the floorplan setup repair, post-CTS setup and hold, and the default global-route repair."""
    text = _render(tmp_path, _platform(_pdk(tmp_path), match_cell_footprint=True))

    repairs = _repairs(text)
    assert repairs == [
        'repair_timing -setup -sequence "unbuffer,sizeup,swap,vt_swap" '
        "-skip_last_gasp -match_cell_footprint",
        "repair_design -match_cell_footprint",
        "repair_timing -setup -match_cell_footprint",
        "repair_timing -hold -match_cell_footprint",
        "repair_design -match_cell_footprint",
        "repair_timing -setup -match_cell_footprint",
        "repair_timing -hold -match_cell_footprint",
    ]


def test_the_place_stage_options_come_before_the_flag(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    platform = _platform(
        _pdk(tmp_path),
        match_cell_footprint=True,
        placement=PlacementFile(repair_design={"slew-margin": 20, "cap-margin": 10}),
    )

    repairs = _repairs(_render(tmp_path, platform))

    assert repairs[1] == (
        "repair_design -slew_margin 20 -cap_margin 10 -match_cell_footprint"
    )


def test_the_hold_only_global_route_repair_matches_footprints(tmp_path):
    text = _render(
        tmp_path,
        _platform(
            _pdk(tmp_path), match_cell_footprint=True, global_route_hold_repair=True
        ),
    )
    assert _repairs(text)[-1] == "repair_timing -hold -match_cell_footprint"


def test_repair_steps_put_the_flag_on_every_repair():
    """`after-timing` and `wire-rc` repairs included; the log lines name the command run."""
    from rtl_buddy.config.pnr_repair import RepairDesign, WireRc

    design = RepairDesign(
        slew_margin=20.0, wire_rc=WireRc(layer="metal5"), after_timing=True
    )
    steps = pnr_openroad._repair_steps_tcl("T", design, 0.1, 0.05, 1.0, None, _FLAG)

    repairs = _repairs(steps)
    assert repairs == [
        "repair_design -slew_margin 20 -match_cell_footprint",
        "repair_timing -setup -setup_margin 100 -match_cell_footprint",
        "repair_timing -hold -hold_margin 50 -setup_margin 100 -match_cell_footprint",
        "repair_design -slew_margin 20 -match_cell_footprint",
    ]
    for command in repairs:
        assert f">>> T: {command}" in steps


@pytest.mark.parametrize("reroute", ["eco", "full"])
@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_every_script_differs_only_by_the_flag_on_its_repairs(
    tmp_path, monkeypatch, stages, reroute
):
    """Every session script, the post-route rounds' and the electrical round's included: unset renders as before, set adds the flag to each repair and nothing else."""
    post_route = {
        **_ROUNDS,
        "reroute": reroute,
        "repair-design": {"slew-margin": 20, "after-timing": True},
        "electrical-repair": {"slew-margin": 10},
    }
    scripts = {}
    for name, platform in (("unset", {}), ("set", {"match_cell_footprint": True})):
        root = tmp_path / name
        root.mkdir()
        backend, artefacts = _run_backend(
            root,
            monkeypatch,
            {},
            stages=stages,
            checkpoints=None,
            post_route=post_route,
            corners=["ss", "ff"],
            env={**_ROUNDS_ENV, "RB_STUB_SLEW": "3 2 1 0", "RB_STUB_CAP": "0 0 0 0"},
            platform=platform,
        )
        assert isinstance(backend.run(), PnrPassResults)
        scripts[name] = _scripts(artefacts)

    electrical = "06_post_route_repair.r3.repair.tcl"
    assert electrical in scripts["set"]
    assert scripts["set"].keys() == scripts["unset"].keys()
    for name, text in scripts["set"].items():
        assert text.replace(_FLAG, "") == scripts["unset"][name], name
        assert _FLAG not in scripts["unset"][name], name
        for command in _repairs(text):
            assert command.endswith(_FLAG), (name, command)
    assert _repairs(scripts["set"][electrical]) == [
        "repair_design -slew_margin 10 -match_cell_footprint"
    ]
    assert len(_repairs(scripts["set"]["06_post_route_repair.r1.repair.tcl"])) == 3


# -- configuration digest ------------------------------------------------------------


def test_it_joins_the_digest_only_when_set(tmp_path):
    plain = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _platform(_pdk(tmp_path))
    )
    off = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _platform(_pdk(tmp_path), match_cell_footprint=False)
    )
    on = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _platform(_pdk(tmp_path), match_cell_footprint=True)
    )

    assert "match_cell_footprint" not in plain
    assert off == plain
    assert on["match_cell_footprint"] is True
    assert pnr_abstract.config_digest(on) != pnr_abstract.config_digest(plain)
