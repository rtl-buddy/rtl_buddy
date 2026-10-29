"""Tests for the check that no macro overlaps a hard blockage or core cut-out after macro placement (`rtl_buddy/pnr/keepouts.tcl`).

`rb::keepouts::check` runs here against stub instances under a bare Tcl interpreter; the render tests read the generated flow.
"""

from importlib.resources import files

import pytest
from tcl_eval import _tcl_eval
from test_pnr_floorplan_controls import _floorplan, _render

from rtl_buddy.config.pnr import BlockageLifetime, BlockageType, PnrBlockage


def _source() -> str:
    return files("rtl_buddy.pnr").joinpath("keepouts.tcl").read_text()


# A stub instance is a command answering `getName` and `getBBox`; a box answers xMin/yMin/xMax/yMax.
_STUBS = r"""
proc stub_box {x0 y0 x1 y1} {
  set name stub_box[incr ::stub_boxes]
  set ::$name [dict create xMin $x0 yMin $y0 xMax $x1 yMax $y1]
  proc $name {method} "dict get \$::$name \$method"
  return $name
}
proc stub_inst {name x0 y0 x1 y1} {
  set box [stub_box $x0 $y0 $x1 $y1]
  proc $name {method} "switch \$method { getName { return $name } getBBox { return $box } }"
  return $name
}
"""


def _check(macros, boxes, labels="{}") -> str:
    insts = " ".join(
        f"[stub_inst {n} {x0} {y0} {x1} {y1}]" for n, x0, y0, x1, y1 in macros
    )
    spans = " ".join(f"{{{x0} {y0} {x1} {y1}}}" for x0, y0, x1, y1 in boxes)
    return _tcl_eval(
        _STUBS
        + _source()
        + f"\nset result [catch {{rb::keepouts::check [list {insts}] {{{spans}}} {{{labels}}} 1000}} message]"
        + '\nset result "$result $message"'
    )


def test_a_macro_clear_of_every_keepout_passes():
    result = _check(
        [("sram", 0, 0, 100, 100)],
        [(100, 0, 200, 50), (0, 100, 100, 200)],
        "floorplan.blockages[0] floorplan.core-cutouts[0]",
    )
    assert result == "0 "


def test_a_macro_over_a_keepout_fails_naming_macro_region_and_fix():
    result = _check(
        [("far", 500, 500, 600, 600), ("sram", 0, 0, 100, 100)],
        [(300, 300, 400, 400), (50, 20, 150, 80)],
        "floorplan.core-cutouts[0] floorplan.blockages[2]",
    )
    assert result == (
        "1 macro sram at (0, 0)-(0.1, 0.1) um overlaps floorplan.blockages[2] "
        "(0.05, 0.02)-(0.15, 0.08) um after macro placement; fix the macro with "
        "a floorplan.macros location, widen placement.macro-channel, or move the "
        "blockage"
    )


def test_a_cutout_label_is_named():
    result = _check(
        [("sram", 0, 0, 100, 100)],
        [(90, 90, 300, 300)],
        "floorplan.core-cutouts[1]",
    )
    assert "overlaps floorplan.core-cutouts[1]" in result


@pytest.mark.parametrize(
    "box", [(100, 0, 200, 100), (-50, 100, 50, 200), (0, -50, 100, 0)]
)
def test_touching_edges_do_not_overlap(box):
    assert _check([("sram", 0, 0, 100, 100)], [box], "k") == "0 "


def _calls(text: str) -> int:
    return text.count("  rb::keepouts::check $macros $HARD_BLOCKAGES")


def test_the_check_follows_macro_placement_inside_the_macro_branch(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(blockages=[PnrBlockage((5.0, 5.0, 50.0, 50.0), BlockageType.HARD)]),
    )

    call = "  rb::keepouts::check $macros $HARD_BLOCKAGES $HARD_BLOCKAGE_LABELS $dbu_per_micron\n"
    assert _calls(text) == 1
    assert text.count("proc rb::keepouts::check") == 1
    assert (
        text.index("proc rb::keepouts::check")
        < text.index("rtl_macro_placer -report_directory")
        < text.index(call)
        < text.index("odb::dbBlockage_create")
    )
    assert "lappend HARD_BLOCKAGE_LABELS floorplan.blockages[0]\n" in text


def test_blockage_labels_count_every_blockage_and_cutouts_have_their_own(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            core_cutouts=[(80.0, 80.0, 140.0, 140.0)],
            blockages=[
                PnrBlockage((10.0, 10.0, 20.0, 20.0), BlockageType.SOFT),
                PnrBlockage((5.0, 5.0, 50.0, 50.0), BlockageType.HARD),
            ],
        ),
    )

    assert "lappend HARD_BLOCKAGE_LABELS floorplan.core-cutouts[0]\n" in text
    assert "lappend HARD_BLOCKAGE_LABELS floorplan.blockages[1]\n" in text
    assert "floorplan.blockages[0]" not in text


@pytest.mark.parametrize(
    "blockages",
    [
        [],
        [PnrBlockage((10.0, 10.0, 20.0, 20.0), BlockageType.SOFT)],
        [PnrBlockage((1.0, 2.0, 3.0, 4.0), BlockageType.PARTIAL, 0.5)],
    ],
)
def test_without_hard_blockages_or_cutouts_nothing_is_rendered(tmp_path, blockages):
    text = _render(tmp_path, _floorplan(blockages=blockages))

    assert "keepouts" not in text
    assert "HARD_BLOCKAGE" not in text


def test_the_check_reads_only_flow_blockages(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(
            blockages=[
                PnrBlockage(
                    (5.0, 5.0, 50.0, 50.0),
                    BlockageType.HARD,
                    lifetime=BlockageLifetime.GLOBAL_PLACEMENT,
                )
            ]
        ),
    )

    assert "keepouts" not in text
