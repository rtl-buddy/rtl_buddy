"""Tests for `floorplan.pins`: side, range, group and exact IO pin constraints (rtl_buddy#105)."""

import textwrap
from importlib.resources import files

import pytest
from test_pnr_floorplan_controls import _floorplan, _load, _render
from test_pnr_macro_pack import _tcl_eval

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import (
    PinSide,
    PnrPin,
    PnrPinFile,
    PnrSuiteConfig,
)
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract


def _pins(tmp_path, *entries) -> list[PnrPin]:
    return _load(tmp_path, pins=[PnrPinFile(**e) for e in entries]).get_floorplan().pins


def test_no_pins_by_default(tmp_path):
    assert _load(tmp_path).get_floorplan().pins == []


def test_side_range_group_and_exact_entries_load(tmp_path):
    pins = _pins(
        tmp_path,
        {"names": "req_*", "side": "left"},
        {"names": ["rsp_*", "irq"], "side": "right", "start": 10, "end": 80.5},
        {"names": "data*", "side": "top", "start": 5, "group": True, "order": True},
        {"names": "dbg*", "group": True},
        {"names": "clk", "location": [0, 42.5], "layer": "met3", "size": [0.3, 1]},
        {"names": "rst_n", "location": [12, 0]},
    )

    assert pins == [
        PnrPin(names=("req_*",), side=PinSide.LEFT),
        PnrPin(names=("rsp_*", "irq"), side=PinSide.RIGHT, start=10.0, end=80.5),
        PnrPin(names=("data*",), side=PinSide.TOP, start=5.0, group=True, order=True),
        PnrPin(names=("dbg*",), group=True),
        PnrPin(names=("clk",), location=(0.0, 42.5), layer="met3", size=(0.3, 1.0)),
        PnrPin(names=("rst_n",), location=(12.0, 0.0)),
    ]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"names": []}, "names must name at least one name"),
        ({"names": "a b", "side": "left"}, "without whitespace"),
        ({"names": ["ok", "x{y"], "side": "left"}, "'x{y'"),
        ({"names": "a", "side": "north"}, "unknown side 'north'"),
        ({"names": "a"}, "constrains nothing"),
        ({"names": "a", "start": 3}, "start needs a side"),
        ({"names": "a", "side": "left", "end": -1}, "end must be a non-negative"),
        ({"names": "a", "side": "left", "start": 9, "end": 9}, "start must be below"),
        ({"names": "a", "side": "left", "order": True}, "order needs group: true"),
        ({"names": "a", "side": "left", "layer": "m3"}, "layer applies to a pin"),
        ({"names": "a", "group": True, "size": [1, 1]}, "size applies to a pin"),
        (
            {"names": "a", "side": "left", "location": [0, 1]},
            "cannot be combined with side",
        ),
        (
            {"names": "a", "group": True, "location": [0, 1]},
            "cannot be combined with group",
        ),
        ({"names": ["a", "b"], "location": [0, 1]}, "names lists 2"),
        ({"names": "a", "location": [0]}, r"location must be \[x, y\]"),
        ({"names": "a", "location": [-1, 0]}, "die coordinates, which start at 0"),
        ({"names": "a", "location": [0, 1], "size": [0, 1]}, "size must be positive"),
        ({"names": "a", "location": [0, 1], "layer": "m 3"}, "is not a layer name"),
    ],
)
def test_a_malformed_pin_entry_fails_at_load_and_names_its_index(
    tmp_path, entry, message
):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _pins(tmp_path, {"names": "ok", "side": "left"}, entry)
    assert "pnr run 'fp': floorplan.pins[1]" in str(err.value)


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
                  pins:
                    - {names: [paddr*, psel], side: left}
                    - {names: prdata, side: right, start: 10, end: 60}
                    - {names: pwdata, side: top, group: true, order: true}
                    - {names: apb_clk, location: [0, 30], layer: metal3}
            """
        )
    )
    pins = PnrSuiteConfig(str(path)).get_runs("r")[0].get_floorplan().pins

    assert [p.names for p in pins] == [
        ("paddr*", "psel"),
        ("prdata",),
        ("pwdata",),
        ("apb_clk",),
    ]
    assert pins[1].start == 10.0 and pins[1].end == 60.0
    assert pins[3].location == (0.0, 30.0) and pins[3].layer == "metal3"


def test_unset_pins_render_nothing(tmp_path):
    text = _render(tmp_path)

    assert "rb::pins" not in text
    assert "floorplan.pins" not in text


def test_pin_entries_render_before_place_pins_with_exact_pins_first(tmp_path):
    pins_file = tmp_path / "pins.tcl"
    pins_file.write_text("# pins\n")
    text = _render(
        tmp_path,
        _floorplan(
            pins=[
                PnrPin(names=("req_*", "clk"), side=PinSide.LEFT),
                PnrPin(names=("rsp_*",), side=PinSide.RIGHT, start=10.0, end=80.25),
                PnrPin(names=("a*",), side=PinSide.TOP, start=5.0),
                PnrPin(names=("b*",), side=PinSide.BOTTOM, end=7.0),
                PnrPin(names=("data*",), group=True, order=True),
                PnrPin(names=("irq[3]",), location=(0.0, 42.5)),
                PnrPin(
                    names=("rst_n",), location=(12.0, 0.0), layer="M2", size=(0.1, 0.2)
                ),
            ]
        ),
        pin_constraints=str(pins_file),
    )

    calls = [
        "rb::pins::place {floorplan.pins[5]} {irq[3]} 0 42.5 {} {}",
        "rb::pins::place {floorplan.pins[6]} {rst_n} 12 0 {M2} {0.1 0.2}",
        "rb::pins::constrain {floorplan.pins[0]} {req_* clk} {left:*} 0 0",
        "rb::pins::constrain {floorplan.pins[1]} {rsp_*} {right:10-80.25} 0 0",
        "rb::pins::constrain {floorplan.pins[2]} {a*} {top:5-*} 0 0",
        "rb::pins::constrain {floorplan.pins[3]} {b*} {bottom:*-7} 0 0",
        "rb::pins::constrain {floorplan.pins[4]} {data*} {} 1 1",
    ]
    assert "\n".join(calls) + "\n" in text
    assert text.count("proc rb::pins::place") == 1
    assert (
        text.index('puts ">>> Macro placement"')
        < text.index('puts ">>> IO pin placement"')
        < text.index("proc rb::pins::ports")
        < text.index(calls[0])
        < text.index(f'source "{pins_file}"')
        < text.index("place_pins -hor_layers")
        < text.index("global_placement -density")
    )


def test_nearest_edge_picks_the_closest_die_edge():
    source = files("rtl_buddy.pnr").joinpath("pins.tcl").read_text()
    die = "{0 0 1000 400}"
    results = [
        _tcl_eval(source + f"\nset result [rb::pins::nearest_edge {die} {x} {y}]")
        for x, y in ((0, 200), (990, 100), (500, 3), (500, 399), (30, 20))
    ]
    assert results == ["left", "right", "bottom", "top", "bottom"]


def _platform(tmp_path):
    pdk = PdkConfig(
        PdkConfigFile(
            name="p",
            site="core",
            corners={"typ": "pdk/typ.lib"},
            tech_lef="pdk/tech.lef",
            macro_lef="pdk/cells.lef",
        ),
        str(tmp_path / "root_config.yaml"),
    )
    return PnrPlatformConfig(
        PnrPlatformConfigFile(name="p", pdk="p", cts_buffer="B"), lambda _n: pdk
    )


def test_pins_enter_the_config_digest_only_when_set(tmp_path):
    default = pnr_abstract.abstract_config(_load(tmp_path), _platform(tmp_path))
    assert "pins" not in default["floorplan"]

    constrained = pnr_abstract.abstract_config(
        _load(
            tmp_path,
            pins=[
                PnrPinFile(names="req_*", side="left", start=1),
                PnrPinFile(names="clk", location=[0, 4]),
            ],
        ),
        _platform(tmp_path),
    )
    assert constrained["floorplan"]["pins"] == [
        {"names": ["req_*"], "side": "left", "start": 1.0},
        {"names": ["clk"], "location": [0.0, 4.0]},
    ]
    assert pnr_abstract.config_digest(constrained) != pnr_abstract.config_digest(
        default
    )
