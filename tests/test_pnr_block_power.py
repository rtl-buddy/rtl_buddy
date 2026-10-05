"""Tests for the power hookup of `blocks:` instances (rtl_buddy#713).

`block_power.tcl` ties each block supply pin the pdn-config leaves floating to a parent
supply net, and joins a block's supply pins on the parent's top strap layer to the
parent's straps, which `pdngen` stops short of the block. Its geometry is pure Tcl and is
driven here without OpenROAD; the rendering and the failure report go through `rb pnr`.
"""

import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config.pdk import PdkConfig
from rtl_buddy.tools import pnr_openroad

from test_pnr_blocks import _BLOCK_YAML, _block_suite, _top_backend, _write
from test_pnr_macro_pack import _tcl_eval


def _tcl(body: str) -> str:
    source = files("rtl_buddy.pnr").joinpath("block_power.tcl").read_text()
    return _tcl_eval(source + "\n" + body + "\n")


def _plan(pin, own, others=(), horizontal=True):
    def rect(r):
        return "{" + " ".join(str(v) for v in r) + "}"

    own_tcl = " ".join(rect(r) for r in own)
    others_tcl = " ".join("{" + f"{name} {rect(r)}" + "}" for name, r in others)
    return _tcl(
        f"set result [rb::block_power::plan_join {rect(pin)} "
        f"{1 if horizontal else 0} {{{own_tcl}}} {{{others_tcl}}}]"
    )


# The issue's geometry in DBU: a 1.6 um met5 pin across the block, and the parent's
# straps on its track ending 3.2 um short of it on both sides.
_PIN = (35880, 64480, 92000, 66080)
_LEFT = (10120, 64480, 32680, 66080)
_RIGHT = (95200, 64480, 230460, 66080)


def test_a_pin_between_two_strap_ends_is_joined_overlapping_each_by_a_width():
    assert _plan(_PIN, [_LEFT, _RIGHT]) == "join {31080 64480 96800 66080}"


def test_the_join_comes_from_the_strap_gap_not_a_fixed_overlap():
    left = (10120, 64480, 30000, 66080)
    right = (99000, 64480, 230460, 66080)
    assert _plan(_PIN, [left, right]) == "join {28400 64480 100600 66080}"


def test_a_pin_with_a_strap_on_one_side_only_is_joined_to_that_side():
    assert _plan(_PIN, [_LEFT]) == "join {31080 64480 92000 66080}"


def test_straps_on_other_tracks_are_ignored():
    other_track = (95200, 37280, 230460, 38880)
    assert _plan(_PIN, [_LEFT, other_track]) == "join {31080 64480 92000 66080}"


def test_a_pin_a_strap_already_overlaps_needs_no_join():
    through = (10120, 64480, 230460, 66080)
    assert _plan(_PIN, [through]) == "covered"


def test_a_pin_on_the_other_nets_track_fails_the_phase_check():
    """A mirrored (MX) copy puts each supply pin where the other net's strap runs."""
    own_elsewhere = (10120, 50880, 32680, 52480)
    result = _plan(_PIN, [own_elsewhere], others=[("VSS", _LEFT)])
    assert result == "misaligned VSS 51680"


def test_a_pin_on_no_track_fails_the_phase_check():
    assert _plan(_PIN, [], others=[]) == "misaligned {} {}"


def test_a_join_that_would_touch_another_nets_strap_is_refused():
    """A rotated block's pin runs across the straps; joining it to the VDD strap end
    would short it to the VSS strap end beside it.
    """
    result = _tcl(
        "set result [rb::block_power::plan_join {40000 30000 41600 90000} 1 "
        "{{0 59200 36800 60800}} {{VSS {0 45600 36800 47200}}}]"
    )
    assert result == "short VSS"


def test_a_vertical_layer_joins_along_y():
    pin = (64480, 35880, 66080, 92000)
    below = (64480, 10120, 66080, 32680)
    above = (64480, 95200, 66080, 230460)
    assert (
        _plan(pin, [below, above], horizontal=False) == "join {64480 31080 66080 96800}"
    )


def test_a_pin_ties_to_its_namesake_else_the_only_net_of_its_kind():
    assert _tcl("set result [rb::block_power::pick_net VDD {VDD VDDA}]") == "VDD"
    assert _tcl("set result [rb::block_power::pick_net VDDB {VDD}]") == "VDD"
    assert _tcl("set result [rb::block_power::pick_net VDDB {VDD VDDA}]") == ""
    assert _tcl("set result [rb::block_power::pick_net VDDB {}]") == ""


def test_instance_names_are_quoted_for_the_global_connection_pattern():
    assert (
        _tcl(r"set result [rb::block_power::regex_quote {u_top.blk[0]$x}]")
        == r"u_top\.blk\[0\]\$x"
    )


def _with_pdn_config(tmp_path, monkeypatch):
    pdn = _write(tmp_path / "top/pdn.tcl", "# grid\n")
    monkeypatch.setattr(PdkConfig, "get_pdn_config", lambda self: str(pdn))
    return pdn


def test_a_run_with_blocks_ties_before_pdngen_and_joins_after(tmp_path, monkeypatch):
    _block_suite(tmp_path)
    pdn = _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    assert backend.run().is_pass()

    script = Path(backend._script_path()).read_text()
    source = script.index(f"source {pdn}\n")
    tie = script.index("rb::block_power::tie_supplies {blk_top}\n")
    pdngen = script.index("\npdngen\n")
    join = script.index("rb::block_power::join_straps {blk_top}\n")
    assert source < tie < pdngen < join
    assert script.count("proc rb::block_power::plan_join") == 1


def test_a_run_without_blocks_keeps_the_plain_power_grid(tmp_path, monkeypatch):
    pdn = _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, "")

    assert backend.run().is_pass()

    script = Path(backend._script_path()).read_text()
    assert f"source {pdn}\npdngen\n" in script
    assert "block_power" not in script


def test_a_block_power_problem_fails_the_run_naming_the_pin(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    _block_suite(tmp_path)
    _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    problems = [
        "u_blk/VDDB (VDD, met5 y=68.000 um, R0) is not on a parent VDD strap",
        "u_blk/VSSB (VSS, met5 y=54.400 um, R0) is not on a parent VSS strap",
    ]

    def _run(cmd, **_kw):
        Path(cmd[cmd.index("-log") + 1]).write_text(
            "".join(f"RB-BLOCK-POWER-ERROR: {p}\n" for p in problems)
        )
        return MagicMock(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == f"block power: {problems[0]} (+1 more)"


def test_a_stray_off_track_shape_is_a_warning_not_a_failure(
    tmp_path, monkeypatch, caplog
):
    from unittest.mock import MagicMock

    _block_suite(tmp_path)
    _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    stray = "u_blk/VDD (VDD, met5 y=44.540 um, R0) is not on a parent VDD strap"

    def _run(cmd, **_kw):
        Path(cmd[cmd.index("-log") + 1]).write_text(
            f"RB-BLOCK-POWER-WARNING: {stray}; the pin's other shapes are joined\n"
        )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    with caplog.at_level("WARNING"):
        assert backend.run().is_pass()
    assert any(stray in r.getMessage() for r in caplog.records)


# A self-contained OpenROAD case: a five-metal technology, and a 60 um block whose
# supply pins are on met5 at the parent's met5 pitch and offset, named apart from the
# parent's nets so that only `tie_supplies` connects them.
_TECH_LEF = (
    dedent("""\
    VERSION 5.8 ;
    BUSBITCHARS "[]" ;
    DIVIDERCHAR "/" ;
    UNITS
      DATABASE MICRONS 1000 ;
    END UNITS
    MANUFACTURINGGRID 0.005 ;
    SITE core
      CLASS CORE ;
      SYMMETRY Y ;
      SIZE 0.46 BY 2.72 ;
    END core
    """)
    + "".join(
        f"LAYER met{n}\n  TYPE ROUTING ; DIRECTION {d} ; PITCH {p} ; WIDTH {w} ; "
        f"SPACING {w} ;\nEND met{n}\n"
        + (
            f"LAYER via{n}\n  TYPE CUT ; SPACING 0.8 ; WIDTH 0.8 ;\nEND via{n}\n"
            if n < 5
            else ""
        )
        for n, d, p, w in (
            (1, "HORIZONTAL", 0.34, 0.14),
            (2, "VERTICAL", 0.46, 0.14),
            (3, "HORIZONTAL", 0.68, 0.3),
            (4, "VERTICAL", 0.92, 0.3),
            (5, "HORIZONTAL", 3.4, 1.6),
        )
    )
    + dedent("""\
    VIARULE M3M4 GENERATE
      LAYER met3 ; ENCLOSURE 0.19 0.19 ;
      LAYER met4 ; ENCLOSURE 0.19 0.19 ;
      LAYER via3 ; RECT -0.4 -0.4 0.4 0.4 ; SPACING 1.6 BY 1.6 ;
    END M3M4
    VIARULE M4M5 GENERATE
      LAYER met4 ; ENCLOSURE 0.19 0.19 ;
      LAYER met5 ; ENCLOSURE 0.31 0.31 ;
      LAYER via4 ; RECT -0.4 -0.4 0.4 0.4 ; SPACING 1.6 BY 1.6 ;
    END M4M5
    END LIBRARY
    """)
)

_BLOCK_LEF = dedent("""\
    VERSION 5.8 ;
    MACRO blk
      CLASS BLOCK ;
      ORIGIN 0 0 ;
      SIZE 60 BY 60 ;
      PIN VDDB
        DIRECTION INOUT ;
        USE POWER ;
        PORT
          LAYER met5 ;
            RECT 0 31.84 60 33.44 ;
        END
      END VDDB
      PIN VSSB
        DIRECTION INOUT ;
        USE GROUND ;
        PORT
          LAYER met5 ;
            RECT 0 18.24 60 19.84 ;
        END
      END VSSB
      OBS
        LAYER met1 ; RECT 0 0 60 60 ;
        LAYER met2 ; RECT 0 0 60 60 ;
        LAYER met3 ; RECT 0 0 60 60 ;
        LAYER met4 ; RECT 0 0 60 60 ;
        LAYER met5 ; RECT 0 0 60 60 ;
      END
    END blk
    END LIBRARY
    """)

_PARENT_PDN = dedent("""\
    add_global_connection -net {VDD} -inst_pattern {.*} -pin_pattern {^VDD$} -power
    add_global_connection -net {VSS} -inst_pattern {.*} -pin_pattern {^VSS$} -ground
    global_connect
    set_voltage_domain -name {CORE} -power {VDD} -ground {VSS}
    define_pdn_grid -name {grid} -voltage_domains {CORE}
    add_pdn_stripe -grid {grid} -layer {met4} -width {1.600} -pitch {27.200} -offset {13.600}
    """)
_MET5_STRAPS = dedent("""\
    add_pdn_stripe -grid {grid} -layer {met5} -width {1.600} -pitch {27.200} -offset {13.600}
    add_pdn_connect -grid {grid} -layers {met4 met5}
    """)

# A parent whose top strap layer is met4, below the block's met5 pins.
_MET3_STRAPS = dedent("""\
    add_pdn_stripe -grid {grid} -layer {met3} -width {1.600} -pitch {27.200} -offset {13.600}
    add_pdn_connect -grid {grid} -layers {met3 met4}
    """)

_OPENROAD = shutil.which("openroad")


def _openroad_case(tmp_path, *, y_um, orient, met5=True, join=True):
    """Place the block at (60, y_um), build the grid, run both steps and check the grid."""
    _write(tmp_path / "tech.lef", _TECH_LEF)
    _write(tmp_path / "blk.lef", _BLOCK_LEF)
    _write(tmp_path / "top.v", "module top ();\n  blk u_blk ();\nendmodule\n")
    _write(tmp_path / "pdn.tcl", _PARENT_PDN + (_MET5_STRAPS if met5 else _MET3_STRAPS))
    procs = files("rtl_buddy.pnr").joinpath("block_power.tcl").read_text()
    _write(tmp_path / "block_power.tcl", procs)
    script = _write(
        tmp_path / "run.tcl",
        dedent(f"""\
            read_lef tech.lef
            read_lef blk.lef
            read_verilog top.v
            link_design top
            initialize_floorplan -die_area {{0 0 200 200}} -core_area {{10 10 190 190}} -site core
            place_inst -name u_blk -location {{60 {y_um}}} -orientation {orient} -status FIRM
            source pdn.tcl
            source block_power.tcl
            rb::block_power::tie_supplies {{blk}}
            pdngen
            {"rb::block_power::join_straps {blk}" if join else ""}
            foreach net {{VDD VSS}} {{
              if {{[catch {{check_power_grid -net $net -dont_require_terminals}}]}} {{
                puts "CPG $net FAIL"
              }} else {{
                puts "CPG $net ok"
              }}
            }}
            """),
    )
    proc = subprocess.run(
        [_OPENROAD, "-exit", "-no_init", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    return proc.stdout + proc.stderr


needs_openroad = pytest.mark.skipif(_OPENROAD is None, reason="openroad not installed")


@needs_openroad
def test_openroad_ties_and_joins_a_met5_block_and_the_grid_checks_clean(tmp_path):
    out = _openroad_case(tmp_path, y_um=32.64, orient="R0")

    assert "rb: tied u_blk/VDDB to VDD" in out
    assert "rb: tied u_blk/VSSB to VSS" in out
    assert "rb: joined 2 block supply pin(s) on met5" in out
    assert "CPG VDD ok" in out and "CPG VSS ok" in out
    assert "RB-BLOCK-POWER-ERROR" not in out


@needs_openroad
def test_openroad_without_the_join_the_block_pins_are_unconnected(tmp_path):
    out = _openroad_case(tmp_path, y_um=32.64, orient="R0", join=False)

    assert "PSM-0039" in out
    assert "CPG VDD FAIL" in out and "CPG VSS FAIL" in out


@needs_openroad
@pytest.mark.parametrize("y_um,orient", [(32.64, "MX"), (35.36, "R0")])
def test_openroad_fails_a_mirrored_or_shifted_block(tmp_path, y_um, orient):
    out = _openroad_case(tmp_path, y_um=y_um, orient=orient)

    assert (
        "RB-BLOCK-POWER-ERROR: u_blk/VDDB (VDD, met5 y=" in out
        and f", {orient}) is not on a parent VDD strap" in out
    )
    assert "CPG" not in out


@needs_openroad
def test_openroad_fails_a_block_pin_above_the_parents_top_strap_layer(tmp_path):
    out = _openroad_case(tmp_path, y_um=32.64, orient="R0", met5=False)

    assert (
        "RB-BLOCK-POWER-ERROR: u_blk/VDDB has a shape on met5, above the parent's "
        "top strap layer met4" in out
    )
