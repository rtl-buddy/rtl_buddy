"""Unit tests for the P&R flow's macro packer (`rtl_buddy/pnr/macro_pack.tcl`).

The packer is Tcl because the only place a macro's footprint is known is the
OpenROAD database, after the LEFs are read and the netlist linked. It is a
separate file, free of OpenROAD commands, so these tests can drive it with a
plain Tcl interpreter — `tclsh`, or the one CPython's `tkinter` embeds —
instead of a full P&R run.

Both run in a subprocess, never in this one: importing `_tkinter` starts a
Tcl notifier thread that never exits, and on macOS a later fork+exec from
such a process can wedge its child in `close()` forever (#641). That is
also why the constraint reader has a worker process.
"""

import math
import shutil
import subprocess
import sys
from importlib.resources import files

import pytest

# The issue's three macros, in microns: an OpenRAM 1 kB SRAM and two
# hardened partitions (#626).
SRAM = ("sram", 479.78, 397.50)
PART_A = ("part_a", 98.94, 98.94)
PART_B = ("part_b", 89.47, 89.47)

DBU_PER_MICRON = 1000
# What `flow.tcl.template` passes: the standard-cell site grid as
# {site_width row_height}. The packing tests use a fine one so their
# expected coordinates read straight off the halo arithmetic; the row-grid
# tests below use sky130hd's real site.
GRID = "{5 5}"
SKY130HD_SITE = (0.46, 2.72)


#: Runs the script `tkinter` embeds a Tcl interpreter for, in a child
#: process, and writes the answer on stdout. A CPython built without the
#: `_tkinter` extension — or with one whose Tcl library is missing — fails
#: here and the `tclsh` binary is used instead.
_EMBEDDED_DRIVER = """
import sys
try:
    import tkinter
    interp = tkinter.Tcl()
except Exception:
    raise SystemExit(9)  # no usable embedded Tcl; the caller tries tclsh
sys.stdout.write(str(interp.eval(sys.stdin.read())))
"""


def _tcl_eval(script: str) -> str:
    """Run `script`, which must leave its answer in `result`, and return it.

    Prefers the interpreter `tkinter` embeds (no external binary needed),
    falls back to `tclsh`, and skips when neither is available. Both run
    out of process, so the answer travels on stdout either way.
    """
    embedded = subprocess.run(
        [sys.executable, "-c", _EMBEDDED_DRIVER],
        input=script + "\nset result\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if embedded.returncode == 0:
        return embedded.stdout
    if embedded.returncode != 9:  # pragma: no cover - a test bug, not a skip
        raise AssertionError(embedded.stderr.strip())
    tclsh = shutil.which("tclsh")
    if tclsh is None:  # pragma: no cover - depends on the machine
        pytest.skip("no Tcl interpreter (tkinter or tclsh) available")
    proc = subprocess.run(
        [tclsh],
        input=script + "\nputs -nonewline $result\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover - a test bug, not a skip
        raise AssertionError(proc.stderr.strip())
    return proc.stdout


def _um(value: float) -> int:
    return int(round(value * DBU_PER_MICRON))


def _macro_list(macros) -> str:
    return " ".join(f"{{{name} {_um(w)} {_um(h)}}}" for name, w, h in macros)


def _core(width_um: float, height_um: float, origin_um: float = 0.0) -> str:
    x0 = _um(origin_um)
    y0 = _um(origin_um)
    return f"{{{x0} {y0} {x0 + _um(width_um)} {y0 + _um(height_um)}}}"


def _run(body: str) -> str:
    source = files("rtl_buddy.pnr").joinpath("macro_pack.tcl").read_text()
    return _tcl_eval(source + "\n" + body + "\n")


def _keepout_list(keepouts) -> str:
    return " ".join(
        "{" + " ".join(str(_um(v)) for v in rect) + "}" for rect in keepouts
    )


def _place(
    macros,
    core_w,
    core_h,
    halo_um,
    origin_um=0.0,
    grid=GRID,
    anchor=None,
    keepouts=None,
):
    """Run the packer and return {name: (x_um, y_um)}, or None for no fit.

    `anchor` and `keepouts` (rectangles in microns) are passed only when
    given, so the default call is the one the flow has always made."""
    extra = ""
    if anchor is not None or keepouts is not None:
        extra = f" {anchor or 'lower-left'} {{{_keepout_list(keepouts or [])}}}"
    body = (
        f"set p [rb::macro_pack::place {_core(core_w, core_h, origin_um)} "
        f"{{{_macro_list(macros)}}} {_um(halo_um)} {grid}{extra}]\n"
        "set result {}\n"
        "dict for {name xy} $p { lappend result [list $name {*}$xy] }"
    )
    result = _run(body).strip()
    if not result:
        return None
    placement = {}
    for entry in _tcl_list(result):
        name, x, y = entry.split()
        placement[name] = (int(x) / DBU_PER_MICRON, int(y) / DBU_PER_MICRON)
    return placement


def _tcl_list(text: str) -> list[str]:
    """Split a Tcl list of `{name x y}` triples into its elements."""
    elements = []
    depth = 0
    current = ""
    for char in text:
        if char == "{":
            depth += 1
            if depth == 1:
                current = ""
                continue
        elif char == "}":
            depth -= 1
            if depth == 0:
                elements.append(current)
                continue
        if depth >= 1:
            current += char
    return elements


def _no_fit_message(macros, core_w, core_h, halo_um) -> str:
    body = (
        f"set result [rb::macro_pack::no_fit_message {_core(core_w, core_h)} "
        f"{{{_macro_list(macros)}}} {_um(halo_um)} {GRID} {DBU_PER_MICRON}]"
    )
    return _run(body)


def _boxes(placement, macros):
    """(x0, y0, x1, y1) per macro, in microns."""
    sizes = {name: (w, h) for name, w, h in macros}
    return {
        name: (x, y, x + sizes[name][0], y + sizes[name][1])
        for name, (x, y) in placement.items()
    }


def _gap(box_a, box_b) -> float:
    """Manhattan channel between two boxes; 0.0 when they overlap."""
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return max(dx, dy)


# ----------------------------------------------------------------------
# Packing
# ----------------------------------------------------------------------


def test_mixed_size_macros_fit_the_design_sized_core():
    """The issue's reproduction: one big SRAM and two small partitions in a
    core sized for the design, which the equal-slot rule could not do (#626)."""
    macros = [SRAM, PART_A, PART_B]
    placement = _place(macros, 695.0, 695.0, 12.0)

    assert placement is not None, "the issue's three macros must fit 695 x 695"
    # Tallest first: the SRAM opens the bottom row, the first partition fits
    # beside it, the second starts the row above — rather than each macro
    # taking a slot sized for the SRAM, which needed ~2x this core.
    assert placement == {
        "sram": (12.0, 12.0),
        "part_a": (503.78, 12.0),
        "part_b": (12.0, 421.5),
    }
    for name, (x0, y0, x1, y1) in _boxes(placement, macros).items():
        assert x0 >= 0.0 and y0 >= 0.0, name
        assert x1 <= 695.0 and y1 <= 695.0, name


def test_no_two_macros_overlap_and_every_channel_holds_the_halo():
    macros = [SRAM, PART_A, PART_B, ("part_c", 150.0, 60.0)]
    placement = _place(macros, 695.0, 695.0, 12.0)

    assert placement is not None
    boxes = _boxes(placement, macros)
    names = sorted(boxes)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            assert _gap(boxes[first], boxes[second]) >= 12.0, (
                f"{first} and {second} are closer than the halo"
            )


def test_the_halo_is_kept_to_every_core_edge():
    macros = [SRAM, PART_A]
    halo = 20.0
    placement = _place(macros, 695.0, 695.0, halo)

    assert placement is not None
    for name, (x0, y0, x1, y1) in _boxes(placement, macros).items():
        assert x0 >= halo, name
        assert y0 >= halo, name
        assert x1 <= 695.0 - halo, name
        assert y1 <= 695.0 - halo, name


def test_a_larger_halo_can_push_a_macro_into_the_next_row():
    """Same core, same macros: the halo is what decides how many fit a row."""
    macros = [SRAM, PART_A, PART_B]
    tight = _place(macros, 695.0, 695.0, 2.0)
    loose = _place(macros, 695.0, 695.0, 12.0)

    assert tight is not None and loose is not None
    # 479.78 + 98.94 + 89.47 = 668.19 um of macro leaves 26.81 um for four
    # 2 um channels but not for four 12 um ones.
    assert len({y for _, y in tight.values()}) == 1
    assert len({y for _, y in loose.values()}) == 2


def _site_grid(site=SKY130HD_SITE) -> str:
    width, height = site
    return f"{{{_um(width)} {_um(height)}}}"


def _on_grid(value_um: float, pitch_um: float, origin_um: float) -> bool:
    steps = (value_um - origin_um) / pitch_um
    return math.isclose(steps, round(steps), abs_tol=1e-6)


def test_origins_are_snapped_to_the_site_grid_from_the_core_corner():
    """Rows start at the core's lower-left corner, so that — not zero — is
    where the site grid is counted from; the y pitch is the row height and
    the x pitch the site width."""
    macros = [("odd", 100.003, 100.007), PART_A]
    origin = 20.0
    placement = _place(
        macros, 400.0, 400.0, 3.3331, origin_um=origin, grid=_site_grid()
    )

    assert placement is not None
    site_w, row_h = SKY130HD_SITE
    for name, (x, y) in placement.items():
        assert _on_grid(x, site_w, origin), name
        assert _on_grid(y, row_h, origin), name
        assert x >= origin + 3.3331 and y >= origin + 3.3331, name


def test_issue_639_second_shelf_lands_on_a_row_boundary():
    """The regression in #639: two OpenRAM 1 kB SRAMs on sky130hd at a 20 um
    core margin and halo. The second shelf used to start at 457.5 um, 0.42 um
    under the row boundary at 457.92 um; the detailed placer's padding check
    rounded the macro up to that row and scanned the row above the macro's
    top edge as the macro's own, failing DPL-0011 on one macro.
    """
    macros = [("sram_a", *SRAM[1:]), ("sram_b", *SRAM[1:])]
    origin, halo = 20.0, 20.0
    # Wide enough for one SRAM per shelf, not for two side by side.
    placement = _place(macros, 960.0, 960.0, halo, origin_um=origin, grid=_site_grid())

    assert placement is not None
    site_w, row_h = SKY130HD_SITE
    ys = sorted(y for _, y in placement.values())
    assert len(ys) == 2, "the two SRAMs must stack, one per shelf"
    # Row 8 (20 + 8 * 2.72 = 41.76 um) is the first row that clears the
    # 20 um halo; the shelf above it, 41.76 + 397.5 + 20 = 459.26 um, snaps
    # up to row 162.
    assert math.isclose(ys[0], origin + 8 * row_h, abs_tol=1e-9)
    assert math.isclose(ys[1], origin + 162 * row_h, abs_tol=1e-9)
    for name, (x, y) in placement.items():
        assert _on_grid(x, site_w, origin), name
        assert _on_grid(y, row_h, origin), name
    # Snapping up only ever widens a channel.
    assert ys[1] - (ys[0] + SRAM[2]) >= halo


def test_a_unit_grid_leaves_origins_alone():
    placement = _place([("odd", 100.003, 100.007)], 400.0, 400.0, 3.333, grid="{1 1}")

    assert placement == {"odd": (3.333, 3.333)}


def test_equal_size_macros_are_placed_in_rows_left_to_right():
    macros = [(f"m{i}", 100.0, 100.0) for i in range(4)]
    placement = _place(macros, 460.0, 460.0, 10.0)

    assert placement is not None
    # 4 x 100 um plus five 10 um channels is 450 um, so all four share the
    # bottom row of a 460 um core, in name order, and none is centred in a
    # slot of its own.
    assert sorted(placement.values()) == [
        (10.0, 10.0),
        (120.0, 10.0),
        (230.0, 10.0),
        (340.0, 10.0),
    ]
    assert {y for _, y in placement.values()} == {10.0}


def test_a_single_macro_lands_in_the_core_corner_inside_the_halo():
    placement = _place([SRAM], 695.0, 695.0, 12.0)

    assert placement == {"sram": (12.0, 12.0)}


def test_a_single_macro_that_exactly_fills_the_core_with_its_halo_fits():
    placement = _place([("snug", 100.0, 50.0)], 120.0, 70.0, 10.0)

    assert placement == {"snug": (10.0, 10.0)}


def test_placement_is_deterministic_and_independent_of_input_order():
    macros = [SRAM, PART_A, PART_B, ("part_c", 98.94, 98.94)]
    first = _place(macros, 695.0, 695.0, 12.0)
    again = _place(macros, 695.0, 695.0, 12.0)
    reversed_input = _place(list(reversed(macros)), 695.0, 695.0, 12.0)

    assert first == again
    assert first == reversed_input
    # `part_a` and `part_c` have identical footprints, so only the name
    # breaks the tie; the tie must break the same way every time, and the
    # earlier name is packed first — here into the row below.
    assert first["part_a"][1] < first["part_c"][1]


# ----------------------------------------------------------------------
# No fit
# ----------------------------------------------------------------------


def test_a_macro_wider_than_the_core_does_not_fit():
    assert _place([SRAM], 400.0, 695.0, 12.0) is None


def test_macros_that_do_not_fit_report_what_was_tried_and_what_would():
    macros = [SRAM, PART_A, PART_B]
    message = _no_fit_message(macros, 500.0, 500.0, 12.0)

    assert "3 macros do not fit the 500.000 x 500.000 um floorplan core" in message
    assert "shelf packing, tallest macro first" in message
    assert "12.000 um halo (placement.macro-halo)" in message
    assert "largest footprint 479.780 x 397.500 um" in message
    assert "smallest core at this aspect ratio that would fit:" in message
    assert "placement.macro-halo" in message.splitlines()[-1]


def test_the_reported_minimum_core_actually_fits_and_a_hair_less_does_not():
    macros = [SRAM, PART_A, PART_B]
    message = _no_fit_message(macros, 500.0, 500.0, 12.0)
    quoted = [
        line for line in message.splitlines() if "smallest core at this aspect" in line
    ][0]
    width, height = (
        float(v) for v in quoted.split(":")[1].replace("um", "").split("x")
    )

    assert _place(macros, width, height, 12.0) is not None
    assert _place(macros, width * 0.98, height * 0.98, 12.0) is None


def test_no_fit_reports_the_impossible_case_rather_than_a_number():
    # One macro 100x taller than a core no scaling inside the search range
    # can reach.
    message = _no_fit_message([("huge", 10.0, 100000.0)], 10.0, 10.0, 1.0)

    assert "no core up to 64x this one fits these macros at this halo" in message


# ----------------------------------------------------------------------
# Rendered flow
# ----------------------------------------------------------------------


def test_the_rendered_flow_sources_the_packer_and_is_valid_tcl():
    """The packer file is substituted into pnr.tcl verbatim, so the flow's
    own call has to agree with its procedure names and arity."""
    source = files("rtl_buddy.pnr").joinpath("macro_pack.tcl").read_text()
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()

    assert "{{ macro_pack_procs }}" in template
    assert "rb::macro_pack::solve \\" in template
    assert _run("set result [info args rb::macro_pack::solve]") == (
        "core macros halo grid dbu_per_micron anchor keepouts"
    )
    # The two trailing arguments are optional, and default to the packing
    # the flow did before they existed (#105).
    assert (
        _run(
            "set result [list [info default rb::macro_pack::solve anchor a] $a "
            "[info default rb::macro_pack::solve keepouts k] $k]"
        )
        == "1 lower-left 1 {}"
    )
    assert source.strip().endswith("}")


# ----------------------------------------------------------------------
# Anchor corner (#105)
# ----------------------------------------------------------------------

ANCHORS = ("lower-left", "lower-right", "upper-left", "upper-right")


def _mirror(placement, macros, core_w, core_h, anchor, origin_um=0.0):
    """Reflect a lower-left packing into `anchor`'s corner, in microns."""
    sizes = {name: (w, h) for name, w, h in macros}
    flip_x = anchor.endswith("right")
    flip_y = anchor.startswith("upper")
    lo_hi_x = 2 * origin_um + core_w
    lo_hi_y = 2 * origin_um + core_h
    return {
        name: (
            round(lo_hi_x - x - sizes[name][0], 6) if flip_x else x,
            round(lo_hi_y - y - sizes[name][1], 6) if flip_y else y,
        )
        for name, (x, y) in placement.items()
    }


def _rounded(placement):
    return {name: (round(x, 6), round(y, 6)) for name, (x, y) in placement.items()}


def test_an_explicit_lower_left_anchor_is_the_default_packing():
    macros = [SRAM, PART_A, PART_B, ("part_c", 150.0, 60.0)]
    default = _place(macros, 695.0, 695.0, 12.0)
    explicit = _place(macros, 695.0, 695.0, 12.0, anchor="lower-left", keepouts=[])

    assert default == explicit


@pytest.mark.parametrize("anchor", ANCHORS)
def test_each_anchor_is_the_default_packing_reflected_into_its_corner(anchor):
    """On a core the grid divides evenly, an anchor is an exact reflection:
    the same rows, the same order, the same channels, from another corner."""
    macros = [SRAM, PART_A, PART_B, ("part_c", 150.0, 60.0)]
    base = _place(macros, 695.0, 695.0, 12.0)
    anchored = _place(macros, 695.0, 695.0, 12.0, anchor=anchor)

    assert base is not None and anchored is not None
    assert _rounded(anchored) == _mirror(base, macros, 695.0, 695.0, anchor)


@pytest.mark.parametrize("anchor", ANCHORS)
def test_a_single_macro_lands_in_the_anchor_corner_inside_the_halo(anchor):
    placement = _place([("m", 100.0, 50.0)], 400.0, 300.0, 10.0, anchor=anchor)

    expected_x = 290.0 if anchor.endswith("right") else 10.0
    expected_y = 240.0 if anchor.startswith("upper") else 10.0
    assert placement == {"m": (expected_x, expected_y)}


@pytest.mark.parametrize("anchor", ANCHORS)
def test_anchored_origins_stay_on_the_site_grid_counted_from_the_lower_left(anchor):
    """The rows belong to the real core, so a mirrored packing still snaps
    every *origin* (the macro's lower-left corner) to the grid counted from
    the core's lower-left corner — on a core the grid does not divide, and
    with footprints that are no multiple of it either."""
    macros = [("odd", 100.003, 100.007), PART_A, ("slab", 180.01, 30.3)]
    origin, halo = 20.0, 3.3331
    core_w, core_h = 400.123, 401.777
    placement = _place(
        macros,
        core_w,
        core_h,
        halo,
        origin_um=origin,
        grid=_site_grid(),
        anchor=anchor,
    )

    assert placement is not None
    site_w, row_h = SKY130HD_SITE
    for name, (x, y) in placement.items():
        assert _on_grid(x, site_w, origin), name
        assert _on_grid(y, row_h, origin), name
    boxes = _boxes(placement, macros)
    for name, (x0, y0, x1, y1) in boxes.items():
        assert x0 >= origin + halo - 1e-9, name
        assert y0 >= origin + halo - 1e-9, name
        assert x1 <= origin + core_w - halo + 1e-9, name
        assert y1 <= origin + core_h - halo + 1e-9, name
    names = sorted(boxes)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            assert _gap(boxes[first], boxes[second]) >= halo - 1e-9, (first, second)


@pytest.mark.parametrize("anchor", ANCHORS)
def test_the_first_macro_hugs_the_anchor_corner_within_one_grid_step(anchor):
    """Snapping away from the anchor edge widens that channel by less than
    one site (x) or one row (y), never more."""
    origin, halo = 20.0, 20.0
    core_w, core_h = 960.123, 960.777
    placement = _place(
        [SRAM], core_w, core_h, halo, origin_um=origin, grid=_site_grid(), anchor=anchor
    )

    assert placement is not None
    site_w, row_h = SKY130HD_SITE
    (x0, y0, x1, y1) = _boxes(placement, [SRAM])["sram"]
    channel_x = (origin + core_w - x1) if anchor.endswith("right") else (x0 - origin)
    channel_y = (origin + core_h - y1) if anchor.startswith("upper") else (y0 - origin)
    assert halo - 1e-9 <= channel_x < halo + site_w
    assert halo - 1e-9 <= channel_y < halo + row_h


@pytest.mark.parametrize("anchor", ANCHORS)
def test_issue_639_rows_stay_row_aligned_from_every_corner(anchor):
    """The #639 pair of SRAMs, stacked one per shelf, from each corner: both
    bottoms on row boundaries and the channel between them at least the halo."""
    macros = [("sram_a", *SRAM[1:]), ("sram_b", *SRAM[1:])]
    origin, halo = 20.0, 20.0
    placement = _place(
        macros, 960.0, 960.0, halo, origin_um=origin, grid=_site_grid(), anchor=anchor
    )

    assert placement is not None
    site_w, row_h = SKY130HD_SITE
    ys = sorted(y for _, y in placement.values())
    assert len(ys) == 2
    for name, (x, y) in placement.items():
        assert _on_grid(x, site_w, origin), name
        assert _on_grid(y, row_h, origin), name
    assert ys[1] - (ys[0] + SRAM[2]) >= halo


def test_an_unknown_anchor_is_an_error():
    with pytest.raises(AssertionError, match="unknown macro anchor"):
        _place([SRAM], 695.0, 695.0, 12.0, anchor="centre")


def test_the_no_fit_message_names_a_non_default_anchor():
    macros = [SRAM, PART_A, PART_B]
    body = (
        f"set result [rb::macro_pack::no_fit_message {_core(500.0, 500.0)} "
        f"{{{_macro_list(macros)}}} {_um(12.0)} {GRID} {DBU_PER_MICRON} upper-right]"
    )
    message = _run(body)

    assert (
        "packing from the upper-right core corner (floorplan.macro-anchor)" in message
    )
    # The minimum is still one this packer, from this corner, would fit.
    quoted = [
        line for line in message.splitlines() if "smallest core at this aspect" in line
    ][0]
    width, height = (
        float(v) for v in quoted.split(":")[1].replace("um", "").split("x")
    )
    assert _place(macros, width, height, 12.0, anchor="upper-right") is not None


# ----------------------------------------------------------------------
# Hard-blockage keep-outs (#105)
# ----------------------------------------------------------------------


def _overlaps(box, rect) -> bool:
    x0, y0, x1, y1 = box
    kx0, ky0, kx1, ky1 = rect
    return x0 < kx1 and kx0 < x1 and y0 < ky1 and ky0 < y1


def test_a_keepout_in_the_row_pushes_the_macro_past_it():
    keepout = (0.0, 0.0, 50.0, 200.0)
    placement = _place([("m", 100.0, 100.0)], 400.0, 400.0, 10.0, keepouts=[keepout])

    # Pushed to the keep-out's right edge; no halo is owed to a keep-out.
    assert placement == {"m": (50.0, 10.0)}


def test_a_keepout_spanning_the_row_lifts_the_macro_above_it():
    keepout = (0.0, 0.0, 400.0, 60.0)
    placement = _place([("m", 100.0, 100.0)], 400.0, 400.0, 10.0, keepouts=[keepout])

    assert placement == {"m": (10.0, 60.0)}


def test_the_next_macro_in_a_row_also_steps_over_a_keepout():
    macros = [("a", 100.0, 100.0), ("b", 100.0, 100.0)]
    keepout = (115.0, 50.0, 140.0, 70.0)
    placement = _place(macros, 400.0, 400.0, 10.0, keepouts=[keepout])

    assert placement == {"a": (10.0, 10.0), "b": (140.0, 10.0)}


@pytest.mark.parametrize("anchor", ANCHORS)
def test_no_macro_overlaps_a_keepout_from_any_corner(anchor):
    macros = [SRAM, PART_A, PART_B, ("part_c", 150.0, 60.0)]
    # One keep-out in each corner, and one off to the side.
    keepouts = [
        (0.0, 0.0, 120.0, 90.0),
        (575.0, 0.0, 695.0, 90.0),
        (0.0, 605.0, 120.0, 695.0),
        (575.0, 605.0, 695.0, 695.0),
        (600.0, 330.0, 650.0, 380.0),
    ]
    placement = _place(
        macros,
        695.0,
        695.0,
        12.0,
        grid=_site_grid(),
        anchor=anchor,
        keepouts=keepouts,
    )

    assert placement is not None
    boxes = _boxes(placement, macros)
    for name, box in boxes.items():
        for rect in keepouts:
            assert not _overlaps(box, rect), (name, rect)
        assert _on_grid(box[0], SKY130HD_SITE[0], 0.0), name
        assert _on_grid(box[1], SKY130HD_SITE[1], 0.0), name
    names = sorted(boxes)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            assert _gap(boxes[first], boxes[second]) >= 12.0, (first, second)


def test_a_keepout_that_leaves_no_room_is_a_no_fit_that_says_so():
    keepouts = [(0.0, 0.0, 400.0, 350.0)]
    assert _place([("m", 100.0, 100.0)], 400.0, 400.0, 10.0, keepouts=keepouts) is None
    body = (
        f"set result [rb::macro_pack::no_fit_message {_core(400.0, 400.0)} "
        f"{{{_macro_list([('m', 100.0, 100.0)])}}} {_um(10.0)} {GRID} "
        f"{DBU_PER_MICRON} lower-left {{{_keepout_list(keepouts)}}}]"
    )
    message = _run(body)

    assert "keeping every macro out of 1 hard placement blockage(s)" in message
    assert "floorplan.blockages" in message.splitlines()[-1]
