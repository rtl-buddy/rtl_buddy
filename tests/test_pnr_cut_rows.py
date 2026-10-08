"""Unit tests for row cutting under hard placement blockages (`rtl_buddy/pnr/cut_rows.tcl`, rtl_buddy#773).

`rb::rows::segments` is plain arithmetic, and `rb::rows::cut_under` runs here against
stub row objects and a stub `odb::dbRow_create` / `odb::dbRow_destroy`, so both run
under a bare Tcl interpreter.
"""

from importlib.resources import files

import pytest
from test_pnr_macro_pack import _tcl_eval


def _source() -> str:
    return files("rtl_buddy.pnr").joinpath("cut_rows.tcl").read_text()


def _segments(x0, num_sites, pitch, intervals, min_sites=1) -> str:
    spans = " ".join(f"{{{lo} {hi}}}" for lo, hi in intervals)
    return _tcl_eval(
        _source()
        + f"\nset result [rb::rows::segments {x0} {num_sites} {pitch} {{{spans}}} {min_sites}]"
    )


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        # Site-aligned: the cut is exactly the blockage.
        ([(200, 400)], "{0 20} {40 60}"),
        # Off-grid edges round outward, so the remnants stay on the site grid.
        ([(205, 395)], "{0 20} {40 60}"),
        # Overlapping blockages cut once.
        ([(250, 500), (100, 300)], "{0 10} {50 50}"),
        # A blockage past either row end leaves only the other side.
        ([(-50, 300)], "{30 70}"),
        ([(700, 1500)], "{0 70}"),
        ([(-1, 1001)], ""),
    ],
)
def test_segments_cut_whole_sites_around_each_blockage(intervals, expected):
    assert _segments(0, 100, 10, intervals) == expected


def test_segments_drop_remnants_under_the_minimum():
    assert _segments(0, 100, 10, [(30, 400)], min_sites=5) == "{40 60}"
    assert _segments(0, 100, 10, [(30, 960)], min_sites=5) == ""


def test_segments_count_sites_from_the_row_origin():
    # sky130hd unithd (0.46 um pitch) from a 5.06 um core edge: a 20-40 um blockage.
    assert _segments(5060, 172, 460, [(20000, 40000)]) == "{0 32} {76 96}"


# Stub ODB: each row is a command answering the getters `cut_under` calls, a box is a
# command answering xMin/yMin/xMax/yMax, and create/destroy log what they were asked.
_STUB_ODB = r"""
namespace eval odb {}
set created {}
set destroyed {}
proc odb::dbRow_destroy {row} { lappend ::destroyed $row }
proc odb::dbRow_create {block name site x y orient dir count pitch} {
    lappend ::created [list $name $x $y $orient $dir $count $pitch]
}
proc make_box {name x0 y0 x1 y1} {
    proc $name {method} "return \[dict get {xMin $x0 yMin $y0 xMax $x1 yMax $y1} \$method\]"
}
proc make_row {name y orient} {
    make_box ${name}_box 0 $y 1000 [expr {$y + 100}]
    proc $name {method} "switch -- \$method {
        getBBox {return ${name}_box} getName {return $name} getSite {return site}
        getOrient {return $orient} getDirection {return HORIZONTAL}
        getSpacing {return 10} getOrigin {return {0 $y}} getSiteCount {return 100}
    }"
}
proc site {method} { return 100 }
make_row ROW_0 0 R0
make_row ROW_1 100 MX
make_row ROW_2 200 R0
proc block {method} { return {ROW_0 ROW_1 ROW_2} }
"""


def _cut(boxes) -> str:
    """Return `count {destroyed rows} {created rows}` after cutting the stub rows."""
    spans = " ".join("{" + " ".join(str(v) for v in box) + "}" for box in boxes)
    return _tcl_eval(
        _source()
        + _STUB_ODB
        + f"\nset cut [rb::rows::cut_under block {{{spans}}}]"
        + "\nset result [list $cut $destroyed $created]"
    )


def test_cut_under_splits_each_overlapped_row_and_keeps_its_site_and_orient():
    # Rows 0 and 1 overlap; the box only touches row 2's bottom edge.
    assert _cut([(305, 50, 595, 200)]) == (
        "2 {ROW_0 ROW_1} "
        "{{ROW_0_1 0 0 R0 HORIZONTAL 30 10} {ROW_0_2 600 0 R0 HORIZONTAL 40 10}"
        " {ROW_1_1 0 100 MX HORIZONTAL 30 10} {ROW_1_2 600 100 MX HORIZONTAL 40 10}}"
    )


def test_cut_under_drops_remnants_narrower_than_twice_the_row_height():
    # The site is 100 high, so a remnant needs 200 DBU (20 sites); the 15-site left
    # one goes.
    assert _cut([(150, 210, 700, 290)]) == (
        "1 ROW_2 {{ROW_2_1 700 200 R0 HORIZONTAL 30 10}}"
    )


def test_cut_under_leaves_rows_a_box_misses():
    assert _cut([(0, 300, 1000, 400)]) == "0 {} {}"
