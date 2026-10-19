"""Tests for planner files, rotated macros and global-placement-only blockages (rtl_buddy#850).

Covers `floorplan.macros-file`, `pins-file`, `macro-snap`, `pin-snap`, `pins-complete`, blockage `lifetime`, `placement.rotated-macro-footprint`, the run-level `placement:` block and the routed-DEF check.
"""

import json
import textwrap
from contextlib import nullcontext
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr import _capture_pnr_events, _make_pdk_cfg, _make_pnr_cfg, _one_event
from test_pnr_floorplan_controls import _floorplan
from test_pnr_macro_pack import _tcl_eval

from rtl_buddy.config.pdk import PlacementFile
from rtl_buddy.config.pnr import (
    BlockageLifetime,
    BlockageType,
    MacroPlacement,
    PinSide,
    PnrBlockage,
    PnrBlockageFile,
    PnrConfig,
    PnrConfigFile,
    PnrFloorplan,
    PnrFloorplanFile,
    PnrMacro,
    PnrMacroFile,
    PnrPin,
    PnrSuiteConfig,
    with_planner_files,
)
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract, pnr_placement_check
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return str(path)


def _load(tmp_path, placement=None, **floorplan) -> PnrConfig:
    return PnrConfigFile(
        name="fp",
        desc="fp",
        synth="demo",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(**floorplan),
        placement=placement,
    ).initialise(str(tmp_path))


def _expanded(tmp_path, **floorplan) -> PnrFloorplan:
    return with_planner_files("fp", _load(tmp_path, **floorplan).get_floorplan())


def _platform(tmp_path, pdk_placement=None, placement=None, **overrides):
    pdk = _make_pdk_cfg(
        tmp_path, **({"placement": pdk_placement} if pdk_placement else {})
    )
    if placement is not None:
        overrides["placement"] = placement
    return PnrPlatformConfig(
        PnrPlatformConfigFile(name="nangate45_typ", pdk="nangate45", **overrides),
        lambda _name: pdk,
    )


def _render(
    tmp_path, fp: PnrFloorplan, platform=None, *, checkpoints=False, **overrides
) -> str:
    pnr_cfg = _make_pnr_cfg(tmp_path, floorplan=fp, **overrides)
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    if checkpoints:
        backend._ckpt_run_dir = str(tmp_path / "ckpt")
    platform = platform or _platform(tmp_path)
    return Path(backend._write_script(platform, fp)).read_text()


# ── planner files ───────────────────────────────────────────────────────────


def test_planner_file_paths_are_relative_to_pnr_yaml_and_not_read_at_load(tmp_path):
    fp = _load(
        tmp_path, macros_file="plan/macros.yaml", pins_file="plan/pins.json"
    ).get_floorplan()

    assert fp.macros_file == str(tmp_path / "plan" / "macros.yaml")
    assert fp.pins_file == str(tmp_path / "plan" / "pins.json")
    assert fp.macros == [] and fp.pins == []


def test_a_suite_with_a_missing_planner_file_still_loads(tmp_path):
    """`rb pnr --list` loads the suite; the file is read only when the run starts."""
    path = _write(
        tmp_path / "pnr.yaml",
        """\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: r
            desc: r
            synth: s
            synth-path: synth.yaml
            platform: p
            floorplan:
              macros-file: plan/missing.yaml
              pins-file: plan/missing.json
        """,
    )
    assert PnrSuiteConfig(path).get_run_names() == ["r"]


def test_macros_file_entries_follow_the_inline_ones_with_their_own_labels(tmp_path):
    _write(
        tmp_path / "plan" / "macros.yaml",
        """\
        - {instance: u_a, location: [10.123456, 20.5], orientation: R90}
        - {instance: "u_b*", orientation: MYR90, halo: [1, 2]}
        """,
    )
    fp = _expanded(
        tmp_path,
        macros=[PnrMacroFile(instance="u_c", halo=[3, 3])],
        macros_file="plan/macros.yaml",
    )

    assert fp.macros == [
        PnrMacro("u_c", halo=(3.0, 3.0)),
        PnrMacro(
            "u_a",
            location=(10.123456, 20.5),
            orientation="R90",
            label="floorplan.macros-file[0]",
            exact=True,
        ),
        PnrMacro(
            "u_b*",
            orientation="MYR90",
            halo=(1.0, 2.0),
            label="floorplan.macros-file[1]",
            exact=True,
        ),
    ]


def test_pins_file_reads_exact_pins_and_region_entries_from_json(tmp_path):
    entries = [
        {
            "name": "din[0]",
            "location": [0.5, 12.3456789],
            "layer": "M3",
            "size": [1, 0.2],
        },
        {"names": ["din[1]"], "location": [0.5, 14]},
        {"names": "dout*", "side": "right", "start": 5, "end": 50},
    ]
    (tmp_path / "pins.json").write_text(json.dumps(entries))

    fp = _expanded(tmp_path, pins_file="pins.json")

    assert fp.pins == [
        PnrPin(
            names=("din[0]",),
            location=(0.5, 12.3456789),
            layer="M3",
            size=(1.0, 0.2),
            label="floorplan.pins-file[0]",
            exact=True,
        ),
        PnrPin(
            names=("din[1]",),
            location=(0.5, 14.0),
            label="floorplan.pins-file[1]",
            exact=True,
        ),
        replace(fp.pins[2], label="floorplan.pins-file[2]", exact=True),
    ]
    assert fp.pins[2].side == "right" and fp.pins[2].end == 50.0


@pytest.mark.parametrize(
    ("kind", "text", "message"),
    [
        ("macros", "{instance: a}\n", "must hold a list of entries, got dict"),
        ("macros", "- [a, b]\n", r"\[0\]: an entry must be a mapping"),
        ("macros", "- {location: [1, 2]}\n", r"\[0\]: instance is required"),
        (
            "macros",
            "- {instance: a, halo: [1, 1]}\n- {instance: b, rotate: 1}\n",
            r"\[1\]: unknown key 'rotate'",
        ),
        (
            "macros",
            "- {instance: a, orientation: N}\n",
            r"\[0\]: unknown orientation 'N'",
        ),
        ("macros", "- {instance: a, instance: b}\n", "duplicate key 'instance'"),
        ("macros", "- {instance: a\n", "is not valid YAML"),
        (
            "pins",
            "- {name: a, names: b, location: [0, 1]}\n",
            "give name or names, not both",
        ),
        ("pins", "- {name: a, side: left}\n", "needs a location"),
        ("pins", "- {side: left}\n", "name or names is required"),
        ("pins", "- {names: [a, b], location: [0, 1]}\n", "names lists 2"),
        (
            "pins",
            "- {names: a, side: left, group: yes please}\n",
            "group has the wrong type",
        ),
        (
            "macros",
            "- {instance: a, location: here}\n",
            "location has the wrong type, got 'here'",
        ),
    ],
)
def test_a_bad_planner_file_entry_names_the_file_and_the_entry(
    tmp_path, kind, text, message
):
    path = _write(tmp_path / f"{kind}.yaml", text)
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _expanded(tmp_path, **{f"{kind}_file": f"{kind}.yaml"})
    assert f"pnr run 'fp': floorplan.{kind}-file" in str(err.value)
    assert path in str(err.value) or "must hold a list" in str(err.value)


def test_a_missing_planner_file_names_it(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="cannot read .*nope.yaml"):
        _expanded(tmp_path, macros_file="nope.yaml")


def test_an_empty_planner_file_adds_nothing(tmp_path):
    _write(tmp_path / "macros.yaml", "# nothing yet\n")
    assert _expanded(tmp_path, macros_file="macros.yaml").macros == []


def test_rtl_mp_still_refuses_a_file_orientation_without_a_location(tmp_path):
    _write(tmp_path / "macros.yaml", "- {instance: a, orientation: R90}\n")
    with pytest.raises(FatalRtlBuddyError, match="chooses the orientation"):
        _expanded(tmp_path, macros_file="macros.yaml", macro_placement="rtl-mp")


def test_a_large_pins_file_loads_quickly(tmp_path):
    entries = "".join(
        f"- {{name: 'p[{i}]', location: [0.5, {i * 0.68:.2f}], layer: M3}}\n"
        for i in range(5000)
    )
    _write(tmp_path / "pins.yaml", entries)
    assert len(_expanded(tmp_path, pins_file="pins.yaml").pins) == 5000


# ── rendering ──────────────────────────────────────────────────────────────


def test_file_coordinates_render_at_full_precision_and_inline_ones_to_the_nm(tmp_path):
    fp = _floorplan(
        macros=[
            PnrMacro("u_a", location=(1.23456, 2.0)),
            PnrMacro(
                "u_b",
                (10.1234567, 0.00001),
                "R90",
                (0.5, 0.25),
                "floorplan.macros-file[0]",
                True,
            ),
        ],
        pins=[
            PnrPin(names=("a",), location=(0.123456, 5.0)),
            PnrPin(
                names=("b",),
                location=(0.5, 7.0000001),
                size=(1.0, 0.123456789),
                label="floorplan.pins-file[3]",
                exact=True,
            ),
        ],
    )
    text = _render(tmp_path, fp)

    assert "[list {floorplan.macros[0]} {u_a} {1.235 2} {} {}]" in text
    assert (
        "[list {floorplan.macros-file[0]} {u_b} {10.1234567 0.00001} R90 {0.5 0.25}]"
        in text
    )
    assert "rb::pins::place {floorplan.pins[0]} {a} 0.123 5 {} {}\n" in text
    assert (
        "rb::pins::place {floorplan.pins-file[3]} {b} 0.5 7.0000001 {} {1 0.123456789}\n"
        in text
    )


def test_snap_off_passes_the_flag_to_the_helpers(tmp_path):
    fp = _floorplan(
        macros=[PnrMacro("u_a", location=(1.0, 2.0))],
        pins=[PnrPin(names=("a",), location=(0.0, 5.0))],
        macro_snap=False,
        pin_snap=False,
    )
    text = _render(tmp_path, fp)

    assert "$dbu_per_micron 0]\n" in text
    assert "rb::pins::place {floorplan.pins[0]} {a} 0 5 {} {} 0\n" in text


def test_pin_entries_run_exact_inline_then_file_then_regions_then_the_check(tmp_path):
    pin_script = tmp_path / "pins.tcl"
    pin_script.write_text("# user pins\n")
    fp = _floorplan(
        pins=[
            PnrPin(names=("r0",), side=PinSide.LEFT),
            PnrPin(names=("e0",), location=(0.0, 1.0)),
            PnrPin(
                names=("r1",),
                side=PinSide.TOP,
                label="floorplan.pins-file[0]",
                exact=True,
            ),
            PnrPin(
                names=("e1",),
                location=(0.0, 2.0),
                label="floorplan.pins-file[1]",
                exact=True,
            ),
        ],
        pins_complete=True,
    )
    text = _render(tmp_path, fp, pin_constraints=str(pin_script))
    order = [
        "set rb::pins::track 1",
        "rb::pins::place {floorplan.pins[1]} {e0}",
        "rb::pins::place {floorplan.pins-file[1]} {e1}",
        "rb::pins::constrain {floorplan.pins[0]} {r0}",
        "rb::pins::constrain {floorplan.pins-file[0]} {r1}",
        "rb::pins::check_complete\n",
        f'source "{pin_script}"',
        "place_pins -hor_layers",
    ]
    positions = [text.index(line) for line in order]
    assert positions == sorted(positions)


def test_pins_complete_without_entries_still_checks(tmp_path):
    text = _render(tmp_path, _floorplan(pins_complete=True))
    assert "rb::pins::check_complete\n" in text


def test_global_placement_blockages_live_only_around_global_placement(tmp_path):
    fp = _floorplan(
        blockages=[
            PnrBlockage((1.0, 2.0, 3.0, 4.0), BlockageType.HARD),
            PnrBlockage(
                (5.0, 5.0, 9.0, 9.0),
                BlockageType.PARTIAL,
                0.35,
                BlockageLifetime.GLOBAL_PLACEMENT,
            ),
            PnrBlockage(
                (6.0, 6.0, 8.0, 8.0),
                BlockageType.HARD,
                None,
                BlockageLifetime.GLOBAL_PLACEMENT,
            ),
        ]
    )
    text = _render(tmp_path, fp, checkpoints=True)

    flow = "set blockage_box [[create_blockage -region {1 2 3 4}] getBBox]"
    partial = (
        "lappend RB_GP_BLOCKAGES [create_blockage -region {5 5 9 9} -max_density 35]\n"
    )
    hard = "lappend RB_GP_BLOCKAGES [create_blockage -region {6 6 8 8}]\n"
    # Only the flow blockage is a packer keep-out with its rows cut.
    assert text.count("create_blockage -region") == 3
    assert "{6 6 8 8}] getBBox" not in text
    ordered = [
        flow,
        "rb::ckpt::checkpoint_for global_placement enter\n",
        partial,
        hard,
        "global_placement -density",
        "foreach rb_blockage $RB_GP_BLOCKAGES { odb::dbBlockage_destroy $rb_blockage }",
        'puts ">>> Repair tie fanout"',
    ]
    positions = [text.index(line) for line in ordered]
    assert positions == sorted(positions)
    # The destroy follows global placement directly.
    after = text[text.index("global_placement -density") :]
    assert after.splitlines()[1].startswith("foreach rb_blockage $RB_GP_BLOCKAGES")


def test_without_checkpoints_no_checkpoint_is_forced(tmp_path):
    fp = _floorplan(
        blockages=[
            PnrBlockage(
                (5.0, 5.0, 9.0, 9.0),
                BlockageType.SOFT,
                None,
                BlockageLifetime.GLOBAL_PLACEMENT,
            )
        ]
    )
    text = _render(tmp_path, fp)
    assert "checkpoint_for" not in text
    assert "lappend RB_GP_BLOCKAGES [create_blockage -region {5 5 9 9} -soft]\n" in text


def test_unset_keys_add_nothing_around_global_placement(tmp_path):
    for fp in (
        _floorplan(),
        _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="MX")]),
        # RTL-MP only flips macros, so it alone turns none.
        _floorplan(macro_placement=MacroPlacement.RTL_MP),
    ):
        text = _render(tmp_path, fp)
        assert "RB_GP_BLOCKAGES" not in text
        assert "rb::rotated" not in text
        assert "RB_DPL_OPTIONS" not in text
        assert "global_placement -density 0.7 -pad_left 1 -pad_right 1\n\n" in text
        assert text.count("detailed_placement\n") == 3


@pytest.mark.parametrize(
    "fp",
    [
        _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="R270")]),
        _floorplan(
            macro_placement=MacroPlacement.RTL_MP,
            macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="R90")],
        ),
    ],
    ids=["pack", "rtl-mp"],
)
def test_rotated_footprint_block_blocks_it_for_global_placement_only(tmp_path, fp):
    text = _render(tmp_path, fp)

    assert "set rb_rotated_macros [rb::rotated::macros $macros]" in text
    assert (
        "lappend RB_GP_BLOCKAGES "
        "{*}[rb::rotated::block_footprints $block $rb_rotated_macros $site_grid_dbu]\n"
        "global_placement -density"
    ) in text
    assert "RB_DPL_OPTIONS" not in text


@pytest.mark.parametrize(
    "point",
    ["session", "after-floorplan", "macro-placement", "after-pdn", "after-pins"],
)
def test_a_hook_before_global_placement_arms_the_rotated_footprint(tmp_path, point):
    """A hook may turn a macro, so the screen is set up; it blocks only turned ones."""
    text = _render(tmp_path, _floorplan(), hooks={point: (str(tmp_path / "h.tcl"),)})

    assert "set rb_rotated_macros [rb::rotated::macros $macros]" in text


@pytest.mark.parametrize(
    "point", ["after-global-placement", "after-cts", "before-reports"]
)
def test_a_hook_after_global_placement_does_not_arm_it(tmp_path, point):
    text = _render(tmp_path, _floorplan(), hooks={point: (str(tmp_path / "h.tcl"),)})

    assert "rb::rotated" not in text


def test_rotated_footprint_diamond_legalizer_reaches_every_detailed_placement(tmp_path):
    fp = _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="MXR90")])
    platform = _platform(
        tmp_path,
        placement=PlacementFile(rotated_macro_footprint="diamond-legalizer"),
        global_route_hold_repair=True,
    )
    text = _render(tmp_path, fp, platform)

    assert "  set RB_DPL_OPTIONS -use_diamond_legalizer\n" in text
    assert "RB_GP_BLOCKAGES" not in text
    assert text.count("detailed_placement {*}$RB_DPL_OPTIONS\n") == 4
    assert "detailed_placement\n" not in text


def test_rotated_footprint_none_leaves_the_flow_alone(tmp_path):
    fp = _floorplan(macros=[PnrMacro("u_a", location=(1.0, 2.0), orientation="R90")])
    platform = _platform(
        tmp_path, placement=PlacementFile(rotated_macro_footprint="none")
    )
    text = _render(tmp_path, fp, platform)

    assert "rb::rotated" not in text and "RB_DPL_OPTIONS" not in text


def test_located_macros_fail_at_macro_placement_on_a_netlist_without_any(tmp_path):
    fp = _floorplan(
        macros=[
            PnrMacro("u_a", location=(1.0, 2.0)),
            PnrMacro("u_b*", orientation="MX"),
            PnrMacro(
                "u_c",
                location=(3.0, 4.0),
                label="floorplan.macros-file[0]",
                exact=True,
            ),
        ]
    )
    text = _render(tmp_path, fp)

    branch_end = (
        "        $inst\n"
        "  }\n"
        "} else {\n"
        "  error {floorplan.macros: the netlist has no hard macros, but 2 entries give "
        "a macro location: floorplan.macros[0] (u_a), floorplan.macros-file[0] (u_c)}\n"
        "}\n"
    )
    assert branch_end in text
    assert text.index(branch_end) < text.index('puts ">>> IO pin placement"')


def test_the_no_macros_failure_names_three_located_entries_and_counts_the_rest(
    tmp_path,
):
    fp = _floorplan(
        macros=[PnrMacro(f"u_{i}", location=(float(i), 1.0)) for i in range(5)]
    )
    assert (
        "  error {floorplan.macros: the netlist has no hard macros, but 5 entries give "
        "a macro location: floorplan.macros[0] (u_0), floorplan.macros[1] (u_1), "
        "floorplan.macros[2] (u_2), +2 more}\n"
    ) in _render(tmp_path, fp)


def test_entries_without_a_location_leave_the_macro_branch_alone(tmp_path):
    for fp in (
        _floorplan(),
        _floorplan(macros=[PnrMacro("u_a", orientation="MX", halo=(1.0, 1.0))]),
    ):
        text = _render(tmp_path, fp)
        assert "the netlist has no hard macros" not in text
        assert "        $inst\n  }\n}\n" in text


# ── placement: on the PDK, platform and run ─────────────────────────────────


def test_run_placement_overrides_the_platform_and_pdk_field_by_field(tmp_path):
    platform = _platform(
        tmp_path,
        pdk_placement=PlacementFile(
            density=0.5, padding=2, rotated_macro_footprint="none"
        ),
        placement=PlacementFile(density=0.6, macro_halo=15.0),
    )
    run = PlacementFile(density=0.45, rotated_macro_footprint="diamond-legalizer")
    merged = platform.with_run_placement(run, "r")

    assert merged.get_placement_density() == 0.45
    assert merged.get_placement_padding() == 2
    assert merged.get_placement_macro_halo() == 15.0
    assert merged.get_placement_rotated_macro_footprint() == "diamond-legalizer"
    assert platform.get_placement_density() == 0.6
    assert platform.get_placement_rotated_macro_footprint() == "none"
    assert platform.with_run_placement(None, "r") is platform
    # Applying it again changes nothing, as `abstract_config` does on a merged platform.
    again = merged.with_run_placement(run, "r")
    assert again.get_placement_density() == 0.45


def test_rotated_macro_footprint_defaults_to_block(tmp_path):
    assert _platform(tmp_path).get_placement_rotated_macro_footprint() == "block"


def test_an_unknown_rotated_macro_footprint_fails(tmp_path):
    with pytest.raises(
        FatalRtlBuddyError, match="rotated-macro-footprint must be one of"
    ):
        _platform(tmp_path, placement=PlacementFile(rotated_macro_footprint="rotate"))
    with pytest.raises(
        FatalRtlBuddyError, match="pnr run 'fp'.*rotated-macro-footprint"
    ):
        _load(tmp_path, placement=PlacementFile(rotated_macro_footprint="rotate"))


def test_run_placement_is_validated_on_the_resolved_values(tmp_path):
    platform = _platform(tmp_path, placement=PlacementFile(routability_driven=True))
    platform.with_run_placement(PlacementFile(routability_use_grt=True), "r")
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'r' on platform"):
        _platform(tmp_path).with_run_placement(
            PlacementFile(routability_use_grt=True), "r"
        )
    with pytest.raises(FatalRtlBuddyError, match="placement.density"):
        _load(tmp_path, placement=PlacementFile(density=1.5))


# ── digests and inputs ─────────────────────────────────────────────────────


def _abstract(tmp_path, platform=None, **floorplan):
    cfg = _load(tmp_path, **floorplan)
    return pnr_abstract.abstract_config(cfg, platform or _platform(tmp_path))


def test_unset_keys_leave_the_abstract_record_as_it_was(tmp_path):
    record = _abstract(tmp_path, blockages=[PnrBlockageFile(rect=[1.0, 2.0, 3.0, 4.0])])
    for key in ("macros_file", "pins_file", "macro_snap", "pin_snap", "pins_complete"):
        assert key not in record["floorplan"]
    assert "lifetime" not in record["floorplan"]["blockages"][0]
    assert "rotated_macro_footprint" not in record["placement"]


def test_set_keys_enter_the_abstract_record(tmp_path):
    cfg = PnrConfigFile(
        name="fp",
        desc="fp",
        synth="demo",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(
            macros_file="plan/m.yaml",
            pins_file="plan/p.json",
            macro_snap=False,
            pin_snap=False,
            pins_complete=True,
            blockages=[
                PnrBlockageFile(rect=[1.0, 2.0, 3.0, 4.0], lifetime="global-placement")
            ],
        ),
        placement=PlacementFile(density=0.4, rotated_macro_footprint="none"),
    ).initialise(str(tmp_path))
    record = pnr_abstract.abstract_config(cfg, _platform(tmp_path))

    fp = record["floorplan"]
    assert fp["macros_file"].endswith("plan/m.yaml")
    assert fp["pins_file"].endswith("plan/p.json")
    assert (fp["macro_snap"], fp["pin_snap"], fp["pins_complete"]) == (
        False,
        False,
        True,
    )
    assert fp["blockages"][0]["lifetime"] == "global-placement"
    assert record["placement"]["density"] == 0.4
    assert record["placement"]["rotated_macro_footprint"] == "none"


def test_planner_files_are_inputs_only_when_set(tmp_path):
    plain = OpenRoadPnr("x", _make_pnr_cfg(tmp_path), str(tmp_path), MagicMock())
    assert plain._planner_files(lambda p: p) == {}
    fp = replace(
        _floorplan(),
        macros_file=str(tmp_path / "m.yaml"),
        pins_file=str(tmp_path / "p.json"),
    )
    backend = OpenRoadPnr(
        "x", _make_pnr_cfg(tmp_path, floorplan=fp), str(tmp_path), MagicMock()
    )
    assert backend._planner_files(lambda p: p) == {
        "macros_file": str(tmp_path / "m.yaml"),
        "pins_file": str(tmp_path / "p.json"),
    }


# ── Tcl helpers under a bare interpreter ───────────────────────────────────


def _helper(name: str) -> str:
    return files("rtl_buddy.pnr").joinpath(name).read_text()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # A 400 x 480 R90 macro at (100, 200): its unrotated footprint is 480 x 400.
        ("100 200 480 400 0 0 {1 1} {0 0 1000 1000}", "100 200 580 600"),
        ("100 200 480 400 5 7 {1 1} {0 0 1000 1000}", "95 193 585 607"),
        # Outward to whole 10-DBU sites and 100-DBU rows from the core corner (3, 50).
        ("100 200 480 400 5 7 {10 100} {3 50 1000 1000}", "93 150 593 650"),
        ("100 200 480 400 5 7 {1 1} {98 0 560 1000}", "98 193 560 607"),
        ("100 200 480 400 0 0 {1 1} {600 0 1000 1000}", ""),
    ],
)
def test_rotated_footprint_box(args, expected):
    result = _tcl_eval(
        _helper("rotated_macros.tcl") + f"\nset result [rb::rotated::footprint {args}]"
    )
    assert result == expected


# A stub database for `rb::rotated::block_footprints`: one macro `m`, whose master is
# 4100 x 6050 DBU, at (`::llx`, `::lly`) in orientation `::orient`, in a core from
# (500, 700) with 100-DBU sites and 1000-DBU rows.
_FAKE_ROTATED = """
rename puts rb_real_puts
set ::printed {}
proc puts {args} { lappend ::printed [lindex $args end] }
namespace eval ord {}
namespace eval odb {}
proc ord::microns_to_dbu {um} { return [expr {$um * 1000}] }
set ::created {}
proc odb::dbBlockage_create {block args} {
    lappend ::created [list $block {*}$args]
    return "blockage[llength $::created]"
}
set MACRO_CELL_HALO 0.5
set MACRO_CELL_HALOS {}
proc fake_block {cmd} { return [dict get {getCoreArea fake_core} $cmd] }
proc fake_core {cmd} {
    return [dict get {xMin 500 yMin 700 xMax 100500 yMax 100700} $cmd]
}
proc fake_master {cmd} { return [dict get {getWidth 4100 getHeight 6050} $cmd] }
proc fake_bbox {cmd} { return [dict get [list xMin $::llx yMin $::lly] $cmd] }
proc m {cmd} {
    return [dict get [list getName m getOrient $::orient getMaster fake_master \
        getBBox fake_bbox] $cmd]
}
"""


def _block_footprints(orient: str, llx: int, lly: int, halos: str = "") -> list[str]:
    script = (
        _helper("rotated_macros.tcl")
        + _FAKE_ROTATED
        + f"""
set ::orient {orient}
set ::llx {llx}
set ::lly {lly}
set MACRO_CELL_HALOS [dict create {halos}]
set handles [rb::rotated::block_footprints fake_block [rb::rotated::macros {{m}}] {{100 1000}}]
set result [join [concat $handles $::created [lsearch -all -inline $::printed {{>>>   *}}]] |]
"""
    )
    result = _tcl_eval(script)
    return result.split("|") if result else []


@pytest.mark.parametrize("orient", ["R90", "R270", "MXR90", "MYR90"])
def test_block_footprints_screens_a_turned_macro_on_whole_rows_and_sites(orient):
    """The unrotated 4100 x 6050 footprint from (10500, 20700), grown by the macro's
    1.23 x 2.34 um halo to (9270, 18360)-(15830, 29090), then outward to the grid.
    """
    assert _block_footprints(orient, 10500, 20700, "m {1.23 2.34}") == [
        "blockage1",
        "fake_block 9200 17700 15900 29700",
        f">>>   m {orient}: blocked {{9200 17700 15900 29700}} DBU",
    ]


@pytest.mark.parametrize("orient", ["R0", "R180", "MX", "MY"])
def test_block_footprints_leaves_an_unturned_macro_alone(orient):
    assert _block_footprints(orient, 10500, 20700, "m {1.23 2.34}") == []


def test_block_footprints_takes_the_default_halo_and_clips_to_the_core():
    """At the core corner with the 0.5 um default halo: (0, 200)-(5100, 7250) grows to
    (0, -300)-(5100, 7700) and is clipped to the core at (500, 700).
    """
    assert _block_footprints("R90", 500, 700) == [
        "blockage1",
        "fake_block 500 700 5100 7700",
        ">>>   m R90: blocked {500 700 5100 7700} DBU",
    ]


_FAKE_DB = """
rename puts rb_real_puts
set ::printed {}
proc puts {args} { lappend ::printed [lindex $args end] }
namespace eval ord {}
namespace eval sta {}
proc ord::get_db_block {} { return fake_block }
proc fake_block {cmd} { return $::bterms }
proc sta::sta_to_db_port {port} { return $port }
set ::constraints {}
proc set_io_pin_constraint {args} { lappend ::constraints $args }
proc make_bterm {name type} {
    proc $name {cmd} "if {\\$cmd eq {getName}} { return $name } else { return $type }"
    lappend ::bterms $name
}
"""


def test_pins_complete_names_each_class_of_uncovered_port():
    script = (
        _helper("pins.tcl")
        + _FAKE_DB
        + """
set ::bterms {}
foreach {name type} {a SIGNAL b SIGNAL c SIGNAL d SIGNAL e CLOCK VDD POWER VSS GROUND} {
    make_bterm $name $type
}
proc get_ports {args} {
    set pattern [lindex $args end]
    return [lsearch -all -inline -glob {a b c d e VDD VSS} $pattern]
}
set rb::pins::track 1
dict set rb::pins::exact_entries a 2
dict set rb::pins::exact_entries b 1
rb::pins::constrain {floorplan.pins[0]} {b c} {left:*} 0 0
rb::pins::constrain {floorplan.pins[1]} {c c*} {right:*} 0 0
rb::pins::constrain {floorplan.pins[2]} {d} {top:*} 0 0
set err ""
catch {rb::pins::check_complete} err
set result [join [concat [lsearch -all -inline $::printed RB-PINS*] [list $err]] |]
"""
    )
    lines = _tcl_eval(script).split("|")
    assert lines == [
        "RB-PINS-COMPLETE: duplicate-exact 1 a",
        "RB-PINS-COMPLETE: several-regions 1 c",
        "RB-PINS-COMPLETE: uncovered 1 e",
        "floorplan.pins-complete: 1 duplicate-exact, 1 several-regions, 1 uncovered port(s)",
    ]
    # c reaches OpenROAD once, which refuses a port in two regions (ODB-0239).
    issued = _tcl_eval(script + "\nset result [join $::constraints |]")
    assert issued.split("|") == [
        "-pin_names c -region left:*",
        "-pin_names d -region top:*",
    ]


def test_side_and_group_rules_leave_out_exactly_placed_ports():
    script = (
        _helper("pins.tcl")
        + _FAKE_DB
        + """
set ::bterms {}
foreach name {p0 p1 p2 q0} { make_bterm $name SIGNAL }
proc get_ports {args} { return [lsearch -all -inline -glob {p0 p1 p2 q0} [lindex $args end]] }
dict set rb::pins::exact_entries p1 1
dict set rb::pins::exact_entries q0 1
rb::pins::constrain {floorplan.pins[0]} {p* q*} {left:*} 1 1
rb::pins::constrain {floorplan.pins[1]} {p*} {left:10-20} 0 0
rb::pins::constrain {floorplan.pins[2]} {q0} {top:*} 0 0
set result [join [concat $::constraints [lsearch -all -inline $::printed {>>>*}]] |]
"""
    )
    assert _tcl_eval(script).split("|") == [
        "-pin_names {p0 p2} -region left:* -group -order",
        "-pin_names {p0 p2} -region left:10-20",
        ">>>   floorplan.pins[0]: set_io_pin_constraint -pin_names {p* q*} -region left:* -group -order (2 port(s) with an exact pin or an earlier rule left out)",
        ">>>   floorplan.pins[1]: set_io_pin_constraint -pin_names p* -region left:10-20 (1 port(s) with an exact pin or an earlier rule left out)",
        ">>>   floorplan.pins[2]: every matched port has an exact pin or an earlier rule; nothing to constrain",
    ]


def test_pins_complete_passes_when_every_signal_port_is_covered():
    script = (
        _helper("pins.tcl")
        + _FAKE_DB
        + """
set ::bterms {}
foreach {name type} {a SIGNAL b SIGNAL VDD POWER} { make_bterm $name $type }
proc get_ports {args} { return [lsearch -all -inline -glob {a b VDD} [lindex $args end]] }
set rb::pins::track 1
dict set rb::pins::exact_entries a 1
rb::pins::constrain {floorplan.pins[0]} {a b} {left:*} 0 0
rb::pins::check_complete
set result [lindex $::printed end]
"""
    )
    assert (
        _tcl_eval(script)
        == ">>>   floorplan.pins-complete: every signal port is covered"
    )


def test_pins_complete_message_parses_into_a_description():
    log = (
        "RB-PINS-COMPLETE: duplicate-exact 1 a\n"
        "RB-PINS-COMPLETE: uncovered 12 p0 p1 p2 p3 p4 p5 p6 p7 p8 p9\n"
    )
    desc = pnr_placement_check.describe_pins_complete(
        pnr_placement_check.parse_pins_complete(log)
    )
    assert desc == (
        "floorplan.pins-complete: 1 port(s) with more than one exact entry (a); "
        "12 port(s) matched by no entry (p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, +2 more)"
    )


_FAKE_MACROS = """
rename puts rb_real_puts
set ::printed {}
proc puts {args} { lappend ::printed [lindex $args end] }
namespace eval ord {}
proc ord::microns_to_dbu {um} { return [expr {round($um * 1000)}] }
# A 400 x 600 DBU macro `m`; turned by 90 degrees its box is 600 x 400.
array set ::m {orient R0 x 0 y 0 status PLACED}
proc fake_core {cmd} { return [dict get {xMin 0 yMin 0 xMax 10000 yMax 10000} $cmd] }
proc fake_master {cmd} { return [dict get {isBlock 1 getName M} $cmd] }
proc fake_bbox {cmd} {
    set rotated [expr {$::m(orient) in {R90 R270 MXR90 MYR90}}]
    set w [expr {$rotated ? 600 : 400}]
    set h [expr {$rotated ? 400 : 600}]
    return [dict get [list getDX $w getDY $h xMin $::m(x) yMin $::m(y)] $cmd]
}
proc m {cmd args} {
    switch $cmd {
        getName { return m }
        getMaster { return fake_master }
        setOrient { set ::m(orient) [lindex $args 0] }
        getOrient { return $::m(orient) }
        getBBox { return fake_bbox }
        setLocation { lassign $args ::m(x) ::m(y) }
        setPlacementStatus { set ::m(status) [lindex $args 0] }
        isFixed { return [expr {$::m(status) eq "LOCKED"}] }
    }
}
proc fake_block {cmd args} {
    switch $cmd {
        findInst { return [expr {[lindex $args 0] eq "m" ? "m" : "NULL"}] }
        getInsts { return {m} }
        getCoreArea { return fake_core }
    }
}
"""


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        ("0", "RB-FLOORPLAN-MACRO: 1235 2001 R90 m"),
        ("1", "RB-FLOORPLAN-MACRO: 1200 2000 R90 m"),
    ],
)
def test_a_later_orientation_applies_before_any_location(snap, expected):
    """Entry 0 locates `m`, entry 1 turns it: the location is the oriented box's corner."""
    script = (
        _helper("macros.tcl")
        + _FAKE_MACROS
        + f"""
set entries [list [list {{floorplan.macros[0]}} m {{1.2345 2.0005}} {{}} {{}}] \\
                  [list {{floorplan.macros-file[0]}} m* {{}} R90 {{}}]]
rb::macros::apply fake_block {{m}} $entries {{100 1000}} 10 1000 {snap}
set result [lindex $::printed end]
"""
    )
    assert _tcl_eval(script) == expected


def test_a_key_set_inline_and_in_the_file_fails_naming_both():
    script = (
        _helper("macros.tcl")
        + _FAKE_MACROS
        + """
set entries [list [list {floorplan.macros[0]} m {} R180 {}] \\
                  [list {floorplan.macros-file[2]} m {} R90 {}]]
catch {rb::macros::apply fake_block {m} $entries {1 1} 10 1000} result
"""
    )
    assert _tcl_eval(script) == (
        "floorplan.macros-file[2]: macro m already has a orientation from "
        "floorplan.macros[0] (floorplan.macros)"
    )


# ── the routed-DEF check ───────────────────────────────────────────────────

_DEF = """\
VERSION 5.8 ;
DESIGN demo_top ;
UNITS DISTANCE MICRONS {units} ;
COMPONENTS 3 ;
    - u_a\\/u_ram fakeram + FIXED ( {mx} 100003 ) W ;
    - u_b fakeram
      + SOURCE DIST + PLACED ( 5 6 ) N ;
    - u_c BUF_X1 + PLACED ( 7 8 ) FS ;
END COMPONENTS
PINS 2 ;
    - din[1] + NET din[1] + DIRECTION INPUT + USE SIGNAL
      + PORT
        + LAYER metal3 ( -140 -280 ) ( 140 280 )
        + FIXED ( {px} 66667 ) N ;
    - dout + NET dout + DIRECTION OUTPUT + USE SIGNAL
      + LAYER metal2 ( -70 -140 ) ( 70 140 )
      + PLACED ( 10 20 ) E ;
END PINS
NETS 0 ;
END NETS
END DESIGN
"""


def _tags(dbu=2000):
    return pnr_placement_check.FloorplanTags(
        dbu=dbu,
        macros={"u_a\\/u_ram": (100005, 100003, "R90")},
        pins={"din[1]": ("metal3", (261, 66387, 541, 66947))},
    )


def _def(tmp_path, units=2000, mx=100005, px=401) -> str:
    path = tmp_path / "demo_top.def"
    path.write_text(_DEF.format(units=units, mx=mx, px=px))
    return str(path)


def test_routed_check_parses_the_tags_the_helpers_print():
    log = (
        "RB-FLOORPLAN-DBU: 2000\n"
        "RB-FLOORPLAN-MACRO: 100005 100003 R90 u_a\\/u_ram\n"
        "RB-FLOORPLAN-PIN: 1 2 3 4 metal3 din[0]\n"
        "RB-FLOORPLAN-PIN: 261 66387 541 66947 metal3 din[1]\n"
        "RB-FLOORPLAN-PIN: 0 0 1 1 metal3 din[0]\n"
    )
    tags = pnr_placement_check.parse_tags(log)
    assert tags.dbu == 2000
    assert tags.macros == {"u_a\\/u_ram": (100005, 100003, "R90")}
    # A later placement of the same pin wins.
    assert tags.pins["din[0]"] == ("metal3", (0, 0, 1, 1))


def test_routed_check_passes_when_the_def_agrees(tmp_path):
    assert pnr_placement_check.check_routed_def(_tags(), _def(tmp_path)) == []


def test_routed_check_scales_def_units(tmp_path):
    tags = pnr_placement_check.FloorplanTags(
        dbu=2000,
        macros={"u_b": (10, 12, "R0")},
        pins={"dout": ("metal2", (-260, -100, 300, 180))},
    )
    path = _def(tmp_path, units=1000)
    assert pnr_placement_check.check_routed_def(tags, path) == []


def test_routed_check_truncates_to_coarser_def_units_as_write_def_does(tmp_path):
    """11 and 13 DBU at 2000 per micron are written as 5 and 6 at 1000; a pin shape may be one unit off."""
    tags = pnr_placement_check.FloorplanTags(
        dbu=2000,
        macros={"u_b": (11, 13, "R0")},
        pins={"dout": ("metal2", (-261, -101, 301, 181))},
    )
    path = _def(tmp_path, units=1000)
    assert pnr_placement_check.check_routed_def(tags, path) == []


def test_routed_check_names_a_moved_macro_and_a_moved_pin(tmp_path):
    problems = pnr_placement_check.check_routed_def(
        _tags(), _def(tmp_path, mx=100010, px=402)
    )
    assert problems == [
        "macro u_a\\/u_ram is at (100010, 100003) W in the routed DEF, not "
        "(100005, 100003) W as the floorplan placed it (DEF units)",
        "pin din[1] is metal3 [262, 66387, 542, 66947] in the routed DEF, not "
        "metal3 [261, 66387, 541, 66947] as the floorplan placed it (DEF units)",
    ]


def test_routed_check_compares_the_orientation(tmp_path):
    tags = replace(_tags(), macros={"u_a/u_ram": (100005, 100003, "R270")}, pins={})
    (problem,) = pnr_placement_check.check_routed_def(tags, _def(tmp_path))
    assert "W in the routed DEF, not (100005, 100003) E" in problem


def test_routed_check_reports_a_missing_macro(tmp_path):
    tags = replace(_tags(), macros={"u_gone": (1, 2, "R0")}, pins={})
    assert pnr_placement_check.check_routed_def(tags, _def(tmp_path)) == [
        "macro u_gone is not placed in the routed DEF"
    ]


def _routed_backend(tmp_path, monkeypatch, fp, log, def_text=None, returncode=0):
    from rtl_buddy.tools import pnr_openroad

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    platform = MagicMock()
    platform.get_pdk.return_value = _make_pdk_cfg(tmp_path)
    platform.get_dont_use_cells.return_value = []
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform
    pnr_cfg = _make_pnr_cfg(tmp_path, floorplan=fp)
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), root_cfg)
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    monkeypatch.setattr(backend, "_has_tcl_command", lambda _c: True)
    monkeypatch.setattr(backend, "_ps_per_unit", 1.0, raising=False)
    monkeypatch.setattr(pnr_openroad, "liberty_time_unit_ps", lambda _libs: 1.0)
    launched = []

    def _fake_openroad(cmd, **kwargs):
        launched.append(cmd)
        kwargs["stdout"].write(log + ">>> DONE\n")
        if def_text is not None:
            (Path(backend.artefact_dir) / "demo_top.def").write_text(def_text)
        result = MagicMock()
        result.returncode = returncode
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_openroad)
    return backend, launched


_ROUTED_LOG = (
    "RB-FLOORPLAN-DBU: 2000\n"
    "RB-FLOORPLAN-MACRO: 100005 100003 R90 u_a\\/u_ram\n"
    "RB-FLOORPLAN-PIN: 261 66387 541 66947 metal3 din[1]\n"
)


def test_a_routed_def_that_moved_a_fixed_macro_fails_the_run(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    fp = _floorplan(
        macros=[PnrMacro("u_a/u_ram", location=(50.0, 50.0), orientation="R90")],
        pins=[PnrPin(names=("din[1]",), location=(0.2, 33.3))],
    )
    backend, _ = _routed_backend(
        tmp_path,
        monkeypatch,
        fp,
        _ROUTED_LOG,
        _DEF.format(units=2000, mx=100006, px=401),
    )

    res = backend.run()

    assert not res.is_pass()
    assert res.results["fail_stage"] == "routed_check"
    assert res.results["desc"].startswith(
        "routed DEF differs from the floorplan: macro u_a\\/u_ram is at (100006, 100003) W"
    )
    level, fields = _one_event(events, "pnr.routed_check_failed")
    assert fields["count"] == 1


def test_a_routed_def_that_agrees_passes(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    fp = _floorplan(
        macros=[PnrMacro("u_a/u_ram", location=(50.0, 50.0), orientation="R90")],
        pins=[PnrPin(names=("din[1]",), location=(0.2, 33.3))],
    )
    backend, _ = _routed_backend(
        tmp_path,
        monkeypatch,
        fp,
        _ROUTED_LOG,
        _DEF.format(units=2000, mx=100005, px=401),
    )

    assert backend.run().is_pass()
    _level, fields = _one_event(events, "pnr.routed_check_passed")
    assert (fields["macros"], fields["pins"]) == (1, 1)


def test_pins_complete_failure_fails_the_run_with_each_class(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    log = "RB-PINS-COMPLETE: uncovered 2 a b\n"
    backend, _ = _routed_backend(
        tmp_path, monkeypatch, _floorplan(pins_complete=True), log, returncode=1
    )

    res = backend.run()

    assert res.results["desc"] == (
        "floorplan.pins-complete: 2 port(s) matched by no entry (a, b)"
    )
    _level, fields = _one_event(events, "pnr.pins_incomplete")
    assert fields["classes"] == {"uncovered": 2}


def test_xfail_does_not_excuse_a_pins_complete_failure(tmp_path, monkeypatch):
    from rtl_buddy.runner.xfail import apply_xfail

    log = "RB-PINS-COMPLETE: uncovered 2 a b\n"
    backend, _ = _routed_backend(
        tmp_path, monkeypatch, _floorplan(pins_complete=True), log, returncode=1
    )

    res = backend.run()
    assert res.results["fail_stage"] == "pins_complete"
    # Even an `xfail_match` that names the failure leaves it a FAIL.
    apply_xfail(res, match="pins-complete")

    assert res.results["result"] == "FAIL"
    assert not res.is_pass()
    assert res.results["desc"] == (
        "xfail not applied (pins_complete): "
        "floorplan.pins-complete: 2 port(s) matched by no entry (a, b)"
    )


def test_a_located_macro_on_a_netlist_without_hard_macros_stops_at_macro_placement(
    tmp_path, monkeypatch
):
    """Under the stub OpenROAD the netlist has no instances, so no hard macros."""
    from test_pnr_checkpoints import _backend

    backend, artefacts = _backend(tmp_path, monkeypatch, checkpoints=None)
    backend.pnr_cfg.floorplan = _floorplan(
        macros=[PnrMacro("u_inner/u_sram", location=(10.0, 20.0), orientation="R90")]
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == (
        "OpenROAD exited with code 1: floorplan.macros: the netlist has no hard "
        "macros, but 1 entry gives a macro location: floorplan.macros[0] "
        "(u_inner/u_sram)"
    )
    log = (artefacts / "pnr.log").read_text()
    assert ">>> Macro placement" in log
    assert ">>> IO pin placement" not in log


def test_a_bad_planner_file_fails_at_setup_before_openroad(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    fp = replace(_floorplan(), pins_file=str(tmp_path / "missing.json"))
    backend, launched = _routed_backend(tmp_path, monkeypatch, fp, "")

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "missing.json" in res.results["desc"]
    assert launched == []
    _one_event(events, "pnr.setup_invalid")


def test_new_error_events_have_readable_messages():
    from rtl_buddy.logging_utils import _human_message

    assert (
        _human_message("pnr.setup_invalid", {"pnr": "r", "error": "bad"})
        == 'P&R "r": bad'
    )
    for event in ("pnr.pins_incomplete", "pnr.routed_check_failed"):
        assert _human_message(event, {"pnr": "r", "desc": "d"}) == 'P&R "r": d'
