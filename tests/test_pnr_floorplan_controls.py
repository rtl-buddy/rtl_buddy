"""Floorplan controls for `rb pnr` (#105): the macro-packer anchor corner,
standard-cell placement blockages, and IO pin placement after the macros.

The packing geometry itself is tested in test_pnr_macro_pack.py; this file
covers the `pnr.yaml` schema and the Tcl the flow renders from it.
"""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import (
    BlockageType,
    MacroAnchor,
    PnrBlockage,
    PnrBlockageFile,
    PnrConfig,
    PnrConfigFile,
    PnrFloorplan,
    PnrFloorplanFile,
    PnrSuiteConfig,
)
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError

# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------


def _load(tmp_path, **floorplan) -> PnrConfig:
    return PnrConfigFile(
        name="fp",
        desc="fp",
        synth="demo",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(**floorplan),
    ).initialise(str(tmp_path))


def _blockage(**fields) -> PnrBlockageFile:
    return PnrBlockageFile(**fields)


def test_floorplan_defaults_keep_the_flow_as_it_was(tmp_path):
    fp = _load(tmp_path).get_floorplan()

    assert fp.macro_anchor is MacroAnchor.LOWER_LEFT
    assert fp.blockages == []


@pytest.mark.parametrize("anchor", [a.value for a in MacroAnchor])
def test_every_anchor_corner_loads(tmp_path, anchor):
    assert _load(tmp_path, macro_anchor=anchor).get_floorplan().macro_anchor == anchor


def test_an_unknown_anchor_fails_at_load(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="unknown 'floorplan.macro-anchor'"):
        _load(tmp_path, macro_anchor="centre")


def test_blockages_load_with_hard_as_the_default_type(tmp_path):
    fp = _load(
        tmp_path,
        blockages=[
            _blockage(rect=[10.0, 20.0, 30.0, 40.0]),
            _blockage(rect=[0.0, 0.0, 5.5, 5.5], type="soft"),
            _blockage(rect=[1.0, 2.0, 3.0, 4.0], type="partial", max_density=0.4),
        ],
    ).get_floorplan()

    assert fp.blockages == [
        PnrBlockage((10.0, 20.0, 30.0, 40.0), BlockageType.HARD),
        PnrBlockage((0.0, 0.0, 5.5, 5.5), BlockageType.SOFT),
        PnrBlockage((1.0, 2.0, 3.0, 4.0), BlockageType.PARTIAL, 0.4),
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"rect": [0.0, 0.0, 10.0]}, r"rect must be \[x0, y0, x1, y1\]"),
        ({"rect": [10.0, 0.0, 10.0, 5.0]}, "x0 < x1 and y0 < y1"),
        ({"rect": [0.0, 8.0, 10.0, 5.0]}, "x0 < x1 and y0 < y1"),
        ({"rect": [-1.0, 0.0, 10.0, 5.0]}, "die coordinates, which start at 0"),
        ({"rect": [0.0, 0.0, 1.0, 1.0], "type": "firm"}, "unknown type 'firm'"),
        ({"rect": [0.0, 0.0, 1.0, 1.0], "type": "partial"}, "needs max-density"),
        (
            {"rect": [0.0, 0.0, 1.0, 1.0], "type": "partial", "max_density": 0.0},
            "between 0 and 1, exclusive",
        ),
        (
            {"rect": [0.0, 0.0, 1.0, 1.0], "type": "partial", "max_density": 1.0},
            "between 0 and 1, exclusive",
        ),
        (
            {"rect": [0.0, 0.0, 1.0, 1.0], "type": "hard", "max_density": 0.5},
            "partial blockages only, not hard",
        ),
        (
            {"rect": [0.0, 0.0, 1.0, 1.0], "type": "soft", "max_density": 0.5},
            "partial blockages only, not soft",
        ),
    ],
)
def test_a_malformed_blockage_fails_at_load_and_names_its_index(
    tmp_path, fields, message
):
    blockages = [_blockage(rect=[0.0, 0.0, 1.0, 1.0]), _blockage(**fields)]
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _load(tmp_path, blockages=blockages)
    assert "floorplan.blockages[1]" in str(err.value)


def test_the_yaml_spelling_loads(tmp_path):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: r
                desc: r
                synth: s
                synth-path: synth.yaml
                platform: p
                floorplan:
                  utilization: 0.4
                  macro-anchor: upper-right
                  blockages:
                    - rect: [0, 0, 120, 90]
                    - {rect: [10, 10, 20.5, 20], type: partial, max-density: 0.3}
            """
        )
    )
    fp = PnrSuiteConfig(str(path)).get_runs("r")[0].get_floorplan()

    assert fp.macro_anchor is MacroAnchor.UPPER_RIGHT
    assert fp.blockages == [
        PnrBlockage((0.0, 0.0, 120.0, 90.0), BlockageType.HARD),
        PnrBlockage((10.0, 10.0, 20.5, 20.0), BlockageType.PARTIAL, 0.3),
    ]


def test_a_bad_blockage_in_yaml_fails_the_suite_load(tmp_path):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: r
                desc: r
                synth: s
                synth-path: synth.yaml
                platform: p
                floorplan:
                  blockages:
                    - rect: [50, 0, 10, 90]
            """
        )
    )
    with pytest.raises(FatalRtlBuddyError, match="x0 < x1"):
        PnrSuiteConfig(str(path))


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _render(tmp_path, floorplan: PnrFloorplan | None = None, **overrides) -> str:
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

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
    platform = PnrPlatformConfig(
        PnrPlatformConfigFile(name="nangate45_typ", pdk="nangate45"), lambda _: pdk
    )
    pnr_cfg = PnrConfig(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(tmp_path / "constraints.sdc"),
        platform="nangate45_typ",
        floorplan=floorplan
        or PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=1000,
        tool_overrides=None,
        **overrides,
    )
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=pnr_cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


def _floorplan(**fields) -> PnrFloorplan:
    return PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0, **fields)


_DEFAULT_SOLVE = (
    "  set placement [rb::macro_pack::solve \\\n"
    "      [list [$core xMin] [$core yMin] [$core xMax] [$core yMax]] \\\n"
    "      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron]\n"
)


def test_unset_controls_render_no_blockages_and_the_plain_packer_call(tmp_path):
    text = _render(tmp_path)

    assert "create_blockage" not in text
    assert "MACRO_KEEPOUTS" not in text
    assert _DEFAULT_SOLVE in text
    assert 'make_tracks\n\nputs ">>> Tie cells"' in text
    assert "{{" not in text


def test_io_pins_are_placed_after_the_macros_and_the_power_grid(tmp_path):
    """`place_pins` warns about every unplaced macro (PPL-0015) and sees it
    at the origin, so the pins go in once the macros are FIRM and, as in
    OpenROAD's reference flow, after the PDN — and before any standard cell
    is placed (#105)."""
    pins = tmp_path / "pins.tcl"
    pins.write_text("# pins\n")
    text = _render(tmp_path, pin_constraints=str(pins))

    macros = text.index('puts ">>> Macro placement"')
    source = text.index(f'source "{pins}"')
    place_pins = text.index("place_pins -hor_layers")
    global_place = text.index("global_placement -density")
    assert text.index("make_tracks") < macros < source < place_pins
    assert place_pins < global_place
    # Exactly one pin-placement stage, and none before the macros.
    assert text.count('puts ">>> IO pin placement"') == 1
    assert text.index('puts ">>> IO pin placement"') > macros


def test_io_pins_follow_a_configured_pdn(tmp_path, monkeypatch):
    pdn = tmp_path / "pdn.tcl"
    pdn.write_text("# grid\n")
    monkeypatch.setattr(PdkConfig, "get_pdn_config", lambda self: str(pdn))
    text = _render(tmp_path)

    assert (
        text.index('puts ">>> Macro placement"')
        < text.index("pdngen")
        < text.index("place_pins -hor_layers")
        < text.index("global_placement -density")
    )


@pytest.mark.parametrize(
    "anchor", [MacroAnchor.LOWER_RIGHT, MacroAnchor.UPPER_LEFT, MacroAnchor.UPPER_RIGHT]
)
def test_a_non_default_anchor_is_passed_to_the_packer(tmp_path, anchor):
    text = _render(tmp_path, _floorplan(macro_anchor=anchor))

    assert (
        "      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron \\\n"
        f"      {anchor.value}]\n"
    ) in text
    assert "create_blockage" not in text


def test_the_lower_left_anchor_renders_the_plain_call(tmp_path):
    text = _render(tmp_path, _floorplan(macro_anchor=MacroAnchor.LOWER_LEFT))

    assert _DEFAULT_SOLVE in text


def test_blockages_render_after_the_floorplan_and_feed_hard_ones_to_the_packer(
    tmp_path,
):
    text = _render(
        tmp_path,
        _floorplan(
            blockages=[
                PnrBlockage((0.0, 0.0, 120.0, 90.5), BlockageType.HARD),
                PnrBlockage((10.0, 10.0, 20.123456, 20.0), BlockageType.SOFT),
                PnrBlockage((1.0, 2.0, 3.0, 4.0), BlockageType.PARTIAL, 0.35),
            ]
        ),
    )

    block = (
        "make_tracks\n"
        "\n"
        'puts ">>> Placement blockages"\n'
        "set MACRO_KEEPOUTS {}\n"
        "set blockage_box [[create_blockage -region {0 0 120 90.5}] getBBox]\n"
        "lappend MACRO_KEEPOUTS [list [$blockage_box xMin] [$blockage_box yMin]"
        " [$blockage_box xMax] [$blockage_box yMax]]\n"
        "create_blockage -region {10 10 20.123 20} -soft\n"
        "create_blockage -region {1 2 3 4} -max_density 35\n"
        "\n"
        'puts ">>> Tie cells"\n'
    )
    assert block in text
    assert (
        "      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron \\\n"
        "      lower-left $MACRO_KEEPOUTS]\n"
    ) in text
    assert text.index("create_blockage") < text.index("global_placement -density")


def test_soft_and_partial_blockages_alone_leave_the_packer_call_alone(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            blockages=[
                PnrBlockage((10.0, 10.0, 20.0, 20.0), BlockageType.SOFT),
                PnrBlockage((1.0, 2.0, 3.0, 4.0), BlockageType.PARTIAL, 0.5),
            ]
        ),
    )

    assert "create_blockage -region {10 10 20 20} -soft\n" in text
    assert "create_blockage -region {1 2 3 4} -max_density 50\n" in text
    assert "MACRO_KEEPOUTS" not in text
    assert _DEFAULT_SOLVE in text


def test_an_anchor_and_hard_blockages_render_together(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            macro_anchor=MacroAnchor.UPPER_RIGHT,
            blockages=[PnrBlockage((5.0, 5.0, 50.0, 50.0), BlockageType.HARD)],
        ),
    )

    assert "      upper-right $MACRO_KEEPOUTS]\n" in text
