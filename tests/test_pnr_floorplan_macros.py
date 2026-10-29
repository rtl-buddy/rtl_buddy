"""Tests for `floorplan.macros`: fixed location, orientation and per-macro halo (rtl_buddy#105)."""

import textwrap
from importlib.resources import files

import pytest
from test_pnr_floorplan_controls import _floorplan, _load, _render
from test_pnr_floorplan_pins import _platform
from tcl_eval import _tcl_eval

from rtl_buddy.config.pnr import (
    PnrMacro,
    PnrMacroFile,
    PnrSuiteConfig,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract


def _macro_hook(tmp_path) -> dict:
    """A `macro-placement` hook, which lets an entry orient a macro without locating it."""
    (tmp_path / "place_macros.tcl").write_text("# places the macros\n")
    return {"macro-placement": "place_macros.tcl"}


def _macros(tmp_path, *entries, hook=False, **floorplan) -> list[PnrMacro]:
    return (
        _load(
            tmp_path,
            hooks=_macro_hook(tmp_path) if hook else None,
            macros=[PnrMacroFile(**e) for e in entries],
            **floorplan,
        )
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
        hook=True,
    )

    assert macros == [
        PnrMacro("u_sram", location=(30.0, 200.5), orientation="MX"),
        PnrMacro("u_blk*", orientation="R180", halo=(2.0, 3.5)),
        PnrMacro(r"gen\[0\].u_mem", halo=(0.0, 0.0)),
    ]


@pytest.mark.parametrize(
    "orientation", ["R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"]
)
def test_every_orientation_loads(tmp_path, orientation):
    (macro,) = _macros(
        tmp_path, {"instance": "m", "orientation": orientation}, hook=True
    )
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
        ({"instance": "m", "orientation": "r90"}, "unknown orientation 'r90'"),
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


def test_an_orientation_without_a_location_needs_a_macro_placement_hook(tmp_path):
    with pytest.raises(
        FatalRtlBuddyError,
        match="floorplan.macros\\[0\\]: RTL-MP chooses the orientation of every "
        "macro it places; give this macro a location too, or place the macros "
        "with a 'macro-placement' hook",
    ):
        _macros(tmp_path, {"instance": "m", "orientation": "MX"})
    (macro,) = _macros(
        tmp_path, {"instance": "m", "orientation": "MX", "location": [5, 5]}
    )
    assert macro.location == (5.0, 5.0)
    (macro,) = _macros(tmp_path, {"instance": "m", "orientation": "MX"}, hook=True)
    assert macro.orientation == "MX"


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
                  utilization: 0.55
                  macros:
                    - {instance: u_sram, location: [30, 200], orientation: MX, halo: [3, 3]}
                    - {instance: "u_c*", halo: [1, 1]}
            """
        )
    )
    fp = PnrSuiteConfig(str(path)).get_runs("r")[0].get_floorplan()

    assert fp.macros == [
        PnrMacro("u_sram", (30.0, 200.0), "MX", (3.0, 3.0)),
        PnrMacro("u_c*", halo=(1.0, 1.0)),
    ]


_APPLY = (
    "  set rb_movable_macros [rb::macros::apply $block $macros $MACRO_DIRECTIVES "
    "$site_grid_dbu $dbu_per_micron]\n"
)


def test_unset_directives_render_the_plain_macro_branch(tmp_path):
    text = _render(tmp_path)

    assert "rb::macros" not in text
    assert "rb_movable_macros" not in text
    assert "set MACRO_CELL_HALOS {}\n" in text
    assert (
        "if {[llength $macros] > 0} {\n"
        "  file mkdir $OUT_DIR/rtlmp\n"
        "  rtl_macro_placer -report_directory $OUT_DIR/rtlmp\n"
    ) in text


def test_directives_fix_macros_before_rtl_mp_places_the_rest(tmp_path):
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
    # RTL-MP leaves the LOCKED macros where they are, and does not run when every macro is located.
    assert (
        _APPLY + "  if {[llength $rb_movable_macros] > 0} {\n"
        "    file mkdir $OUT_DIR/rtlmp\n"
        "    rtl_macro_placer -report_directory $OUT_DIR/rtlmp\n"
        "    foreach inst $rb_movable_macros {\n"
    ) in text
    assert (
        text.index('puts ">>> Macro placement"')
        < text.index("proc rb::macros::apply")
        < text.index(_APPLY)
        < text.index("rtl_macro_placer -")
        < text.index("dict exists $MACRO_CELL_HALOS [$inst getName]")
    )


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
