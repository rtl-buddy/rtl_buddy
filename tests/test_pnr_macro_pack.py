"""Unit tests for the P&R flow's macro packer (`rtl_buddy/pnr/macro_pack.tcl`).

The packer is Tcl because the only place a macro's footprint is known is the
OpenROAD database, after the LEFs are read and the netlist linked. It is a
separate file, free of OpenROAD commands, so these tests can drive it with a
plain Tcl interpreter — `tclsh`, or the one CPython's `tkinter` embeds —
instead of a full P&R run.
"""

import math
import shutil
import subprocess
from importlib.resources import files

import pytest

# The issue's three macros, in microns: an OpenRAM 1 kB SRAM and two
# hardened partitions (#626).
SRAM = ("sram", 479.78, 397.50)
PART_A = ("part_a", 98.94, 98.94)
PART_B = ("part_b", 89.47, 89.47)

DBU_PER_MICRON = 1000
# What `flow.tcl.template` passes: the 0.005 um manufacturing grid.
GRID = 5


def _embedded_interpreter():
    """CPython's embedded Tcl, or `None` when this build has none.

    `tkinter.Tcl()` needs no display, but a CPython built without the
    `_tkinter` extension — or with one whose Tcl library is missing — has to
    fall back to the `tclsh` binary.
    """
    try:
        import tkinter
    except ImportError:  # pragma: no cover - depends on the build of CPython
        return None
    try:
        return tkinter.Tcl()
    except Exception:  # pragma: no cover - depends on the build of CPython
        return None


def _tcl_eval(script: str) -> str:
    """Run `script`, which must leave its answer in `result`, and return it.

    Prefers the interpreter `tkinter` embeds (no external binary needed),
    falls back to `tclsh`, and skips when neither is available. The answer
    travels in a variable rather than on stdout because only the `tclsh`
    path has a stdout to read.
    """
    interp = _embedded_interpreter()
    if interp is not None:
        return interp.eval(script + "\nset result")
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


def _place(macros, core_w, core_h, halo_um, origin_um=0.0):
    """Run the packer and return {name: (x_um, y_um)}, or None for no fit."""
    body = (
        f"set p [rb::macro_pack::place {_core(core_w, core_h, origin_um)} "
        f"{{{_macro_list(macros)}}} {_um(halo_um)} {GRID}]\n"
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


def test_origins_are_snapped_to_the_placement_grid():
    macros = [("odd", 100.003, 100.007), PART_A]
    placement = _place(macros, 400.0, 400.0, 3.3331, origin_um=0.1237)

    assert placement is not None
    for name, (x, y) in placement.items():
        assert math.isclose(round(x * 1000) % GRID, 0.0, abs_tol=1e-9), name
        assert math.isclose(round(y * 1000) % GRID, 0.0, abs_tol=1e-9), name


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
        "core macros halo grid dbu_per_micron"
    )
    assert source.strip().endswith("}")
