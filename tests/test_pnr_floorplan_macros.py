"""Tests for `floorplan.macros`: fixed location, orientation and per-macro halo (rtl_buddy#105)."""

import textwrap
from importlib.resources import files

import pytest
from test_pnr_floorplan_controls import _floorplan, _load, _render
from test_pnr_floorplan_pins import _platform
from test_pnr_macro_pack import _tcl_eval

from rtl_buddy.config.pnr import (
    BlockageType,
    MacroPlacement,
    PnrBlockage,
    PnrMacro,
    PnrMacroFile,
    PnrSuiteConfig,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract


def _macros(tmp_path, *entries, **floorplan) -> list[PnrMacro]:
    return (
        _load(tmp_path, macros=[PnrMacroFile(**e) for e in entries], **floorplan)
        .get_floorplan()
        .macros
    )


def test_no_macro_directives_by_default(tmp_path):
    assert _load(tmp_path).get_floorplan().macros == []


def test_directives_load(tmp_path):
    macros = _macros(
        tmp_path,
        {"instance": "u_sram", "location": [30, 200.5], "orientation": "MX"},
        {"instance": "u_blk*", "orientation": "R180", "halo": [2, 3.5]},
        {"instance": r"gen\[0\].u_mem", "halo": [0, 0]},
    )

    assert macros == [
        PnrMacro("u_sram", location=(30.0, 200.5), orientation="MX"),
        PnrMacro("u_blk*", orientation="R180", halo=(2.0, 3.5)),
        PnrMacro(r"gen\[0\].u_mem", halo=(0.0, 0.0)),
    ]


@pytest.mark.parametrize("orientation", ["R0", "R180", "MX", "MY"])
def test_every_row_compatible_orientation_loads(tmp_path, orientation):
    (macro,) = _macros(tmp_path, {"instance": "m", "orientation": orientation})
    assert macro.orientation == orientation


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"instance": ""}, "non-empty name"),
        ({"instance": "u m", "halo": [1, 1]}, "without whitespace"),
        ({"instance": "u_m"}, "changes nothing"),
        ({"instance": "m", "location": [1]}, r"location must be \[x, y\]"),
        ({"instance": "m", "location": [-1, 2]}, "die coordinates, which start at 0"),
        ({"instance": "m", "orientation": "N"}, "unknown orientation 'N'"),
        ({"instance": "m", "orientation": "R90"}, "rotates the macro by 90 degrees"),
        ({"instance": "m", "orientation": "MXR90"}, "rotates the macro by 90"),
        ({"instance": "m", "halo": [1, -1]}, "halo must be non-negative"),
        ({"instance": "m", "halo": [1, 2, 3]}, r"halo must be \[x, y\]"),
    ],
)
def test_a_malformed_directive_fails_at_load_and_names_its_index(
    tmp_path, entry, message
):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _macros(tmp_path, {"instance": "ok", "halo": [1, 1]}, entry)
    assert "pnr run 'fp': floorplan.macros[1]" in str(err.value)


def test_rtl_mp_refuses_an_orientation_without_a_location(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="chooses the orientation"):
        _macros(
            tmp_path,
            {"instance": "m", "orientation": "MX"},
            macro_placement="rtl-mp",
        )
    (macro,) = _macros(
        tmp_path,
        {"instance": "m", "orientation": "MX", "location": [5, 5]},
        macro_placement="rtl-mp",
    )
    assert macro.location == (5.0, 5.0)


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
                  macros:
                    - {instance: u_sram, location: [30, 200], orientation: MX, halo: [3, 3]}
                    - {instance: "u_c*", orientation: R180}
            """
        )
    )
    fp = PnrSuiteConfig(str(path)).get_runs("r")[0].get_floorplan()

    assert fp.macros == [
        PnrMacro("u_sram", (30.0, 200.0), "MX", (3.0, 3.0)),
        PnrMacro("u_c*", orientation="R180"),
    ]


_APPLY = (
    "  set rb_movable_macros [rb::macros::apply $block $macros $MACRO_DIRECTIVES "
    "$site_grid_dbu [expr {int(round([ord::microns_to_dbu $MACRO_HALO]))}] "
    "$dbu_per_micron]\n"
)


def test_unset_directives_render_the_plain_macro_branch(tmp_path):
    text = _render(tmp_path)

    assert "rb::macros" not in text
    assert "rb_movable_macros" not in text
    assert "set MACRO_CELL_HALOS {}\n" in text
    assert "  foreach inst $macros {\n    set master [$inst getMaster]" in text


def test_directives_fix_macros_before_the_packer_and_feed_it_keepouts(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            macros=[
                PnrMacro("u_sram", (30.25, 200.0), "MX", (3.0, 4.0)),
                PnrMacro("u_c*", orientation="R180"),
            ]
        ),
    )

    entries = (
        "  set MACRO_DIRECTIVES {}\n"
        "  lappend MACRO_DIRECTIVES [list {floorplan.macros[0]} {u_sram} {30.25 200} MX {3 4}]\n"
        "  lappend MACRO_DIRECTIVES [list {floorplan.macros[1]} {u_c*} {} R180 {}]\n"
    )
    assert entries + _APPLY in text
    assert (
        "  if {[llength $rb_movable_macros] > 0} {\n  set core [$block getCoreArea]\n"
    ) in text
    assert "  foreach inst $rb_movable_macros {\n    set master" in text
    assert (
        "      $footprints $halo_dbu $site_grid_dbu $dbu_per_micron \\\n"
        "      lower-left $MACRO_KEEPOUTS]\n"
    ) in text
    assert (
        text.index('puts ">>> Macro placement"')
        < text.index("proc rb::macros::apply")
        < text.index(_APPLY)
        < text.index("rb::macro_pack::solve \\")
        < text.index("dict exists $MACRO_CELL_HALOS [$inst getName]")
    )


def test_directives_with_an_anchor_and_hard_blockages_keep_both(tmp_path):
    from rtl_buddy.config.pnr import MacroAnchor

    text = _render(
        tmp_path,
        _floorplan(
            macro_anchor=MacroAnchor.UPPER_RIGHT,
            blockages=[PnrBlockage((5.0, 5.0, 50.0, 50.0), BlockageType.HARD)],
            macros=[PnrMacro("u_sram", halo=(1.0, 1.0))],
        ),
    )

    assert text.count("      upper-right $MACRO_KEEPOUTS]\n") == 1
    assert text.index("set MACRO_KEEPOUTS {}") < text.index(_APPLY)


def test_rtl_mp_runs_after_the_directives(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            macro_placement=MacroPlacement.RTL_MP,
            macros=[PnrMacro("u_sram", (30.0, 20.0))],
        ),
    )

    assert text.index(_APPLY) < text.index("rtl_macro_placer -")
    assert "rb::macro_pack::solve \\" not in text


def _tcl(script: str) -> str:
    source = files("rtl_buddy.pnr").joinpath("macros.tcl").read_text()
    return _tcl_eval(source + "\n" + script)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # Nearest grid point from the origin.
        ("1240 100 1000 1000 9000 500", "1200"),
        ("1260 100 1000 1000 9000 500", "1300"),
        # Rounding that would push a fitting box past the high edge steps back.
        ("8460 100 1000 1000 9000 520", "8400"),
        # Rounding below the low edge steps forward.
        ("1000 300 950 1000 9000 500", "1250"),
        # A box already outside stays outside; the caller reports it.
        ("8800 100 1000 1000 9000 500", "8800"),
        # No grid: unchanged.
        ("1234 1 0 0 9000 10", "1234"),
    ],
)
def test_snap_keeps_a_fitting_macro_inside_the_core(args, expected):
    assert _tcl(f"set result [rb::macros::snap {args}]") == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("{0 0 10 10}", "{5 5 15 15}", "1"),
        ("{0 0 10 10}", "{10 0 20 10}", "0"),
        ("{0 0 10 10}", "{0 10 10 20}", "0"),
        ("{0 0 10 10}", "{2 2 3 3}", "1"),
    ],
)
def test_overlaps_ignores_touching_edges(a, b, expected):
    assert _tcl(f"set result [rb::macros::overlaps {a} {b}]") == expected


def test_directives_enter_the_config_digest_only_when_set(tmp_path):
    default = pnr_abstract.abstract_config(_load(tmp_path), _platform(tmp_path))
    assert "macros" not in default["floorplan"]

    directed = pnr_abstract.abstract_config(
        _load(
            tmp_path,
            macros=[
                PnrMacroFile(instance="u_sram", location=[1, 2], orientation="MX"),
                PnrMacroFile(instance="u_c*", halo=[2, 2]),
            ],
        ),
        _platform(tmp_path),
    )
    assert directed["floorplan"]["macros"] == [
        {"instance": "u_sram", "location": [1.0, 2.0], "orientation": "MX"},
        {"instance": "u_c*", "halo": [2.0, 2.0]},
    ]
    assert pnr_abstract.config_digest(directed) != pnr_abstract.config_digest(default)
