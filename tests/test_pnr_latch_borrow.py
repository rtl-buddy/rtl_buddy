"""The latch borrow report of `rb pnr`'s final reports (rtl_buddy#1090): the report parse, the result fields, the summary cell, and the step in real OpenROAD.

A latch whose data arrives while it is open has a setup slack of exactly 0 in OpenSTA, so the design's worst setup slack reads 0 whatever margin the latch has left. The report texts below are as OpenROAD 26Q2 printed them for ASAP7 latches. The OpenROAD cases need only `openroad` on PATH (the cells are written inline) or, for the issue's reproducer, also `RTL_BUDDY_ASAP7_DIR`, an ORFS `platforms/asap7` directory.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from pnr_events import _capture_pnr_events, _one_event
from pnr_stub_openroad import _pdk, _platform, _pnr_cfg
from tcl_eval import _tcl_eval

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.tools import pnr_latch_borrow, pnr_openroad
from rtl_buddy.tools.pnr_latch_borrow import LatchEndpoint
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

#: The issue's reproducer: `report_checks -path_delay max -to u_lat/D` at the default two digits.
_ISSUE_BLOCK = """\
Startpoint: d (input port clocked by clk)
Endpoint: u_lat (positive level-sensitive latch clocked by clk)
Path Group: clk
Path Type: max

  Delay    Time   Description
---------------------------------------------------------
   0.00    0.00   clock clk (rise edge)
   0.00    0.00   clock network delay (ideal)
 200.00  200.00 ^ input external delay
   0.00  200.00 ^ d (in)
   0.14  200.14 ^ u_lat/D (DHLx1_ASAP7_75t_R)
         200.14   data arrival time

   0.00    0.00   clock clk (rise edge)
   0.00    0.00   clock network delay (ideal)
   0.00    0.00   clock reconvergence pessimism
           0.00 ^ u_lat/CLK (DHLx1_ASAP7_75t_R)
 200.14  200.14   time borrowed from endpoint
         200.14   data required time
---------------------------------------------------------
         200.14   data required time
        -200.14   data arrival time
---------------------------------------------------------
           0.00   slack (MET)

Time Borrowing Information
--------------------------------------------
clk pulse width                       500.00
library setup time                     -0.82
--------------------------------------------
max time borrow                       499.18
actual time borrow                    200.14
--------------------------------------------


"""


def _check(start, end_line, pin, arrival, slack, borrow_lines, corner=""):
    """One latch setup check as `report_checks -digits 6` prints it."""
    return (
        f"Startpoint: {start} (input port clocked by clk)\n"
        f"Endpoint: {end_line}\n"
        "Path Group: clk\nPath Type: max\n"
        + (f"Corner: {corner}\n" if corner else "")
        + "\n    Delay      Time   Description\n"
        "-------------------------------------------------------------\n"
        "   0.000000    0.000000   clock clk (rise edge)\n"
        f"   0.000000 {arrival:>11}   input external delay\n"
        f"   0.000000 {arrival:>11} ^ {pin} (DHLx1_ASAP7_75t_R)\n"
        f"            {arrival:>11}   data arrival time\n"
        "-------------------------------------------------------------\n"
        f"            {slack:>11}   slack ({'VIOLATED' if slack.startswith('-') else 'MET'})\n"
        "\nTime Borrowing Information\n"
        "----------------------------------------------\n"
        + borrow_lines
        + "----------------------------------------------\n\n\n"
    )


_PULSE = (
    "clk pulse width                     500.000000\n"
    "library setup time                  -15.283400\n"
    "----------------------------------------------\n"
)

#: A latch past its closing edge, one borrowing and one whose data arrives before it opens, at two corners.
_REPORT = (
    "Latch D endpoints at corner tt\n"
    + _check(
        "f",
        "u_c (positive level-sensitive latch clocked by clk)",
        "u_c/D",
        "700.000000",
        "-215.283401",
        _PULSE
        + "max time borrow                     484.716614\n"
        + "actual time borrow                  484.716614\n",
        "tt",
    )
    + _check(
        "d",
        "u_a (positive level-sensitive latch clocked by clk)",
        "u_a/D",
        "200.000000",
        "0.000000",
        _PULSE
        + "max time borrow                     499.313477\n"
        + "actual time borrow                  200.000000\n",
        "tt",
    )
    + _check(
        "e",
        "u_b (negative level-sensitive latch clocked by clk)",
        "u_b/D",
        "200.000000",
        "299.999969",
        _PULSE
        + "max time borrow                     486.821198\n"
        + "actual time borrow                    0.000000\n",
        "tt",
    )
    + "Latch D endpoints at corner ss\n"
    + _check(
        "d",
        "u_a (positive level-sensitive latch clocked by clk)",
        "u_a/D",
        "200.000000",
        "0.000000",
        "user max time borrow                250.000000\n"
        + "actual time borrow                  200.000000\n",
        "ss",
    )
)


# -- the report parse --------------------------------------------------------------


def test_the_issue_block_parses_to_its_margin_to_the_closing_edge():
    (check,) = pnr_latch_borrow.parse_checks(_ISSUE_BLOCK)

    assert check == LatchEndpoint("u_lat/D", 0.0, 499.18, 200.14)
    # 299 ps to the closing edge, where `report_worst_slack -max` reads 0.00.
    assert check.margin == pytest.approx(299.04)


def test_each_corner_and_kind_of_latch_check_is_read():
    corners = pnr_latch_borrow.parse(_REPORT)

    assert list(corners) == ["tt", "ss"]
    tt = {c.pin: c for c in corners["tt"]}
    # Past its closing edge: the margin is the negative slack.
    assert tt["u_c/D"].margin == pytest.approx(-215.283401)
    # Borrowing: the time it may still borrow.
    assert tt["u_a/D"].margin == pytest.approx(499.313477 - 200.0)
    # Arriving before it opens: its slack plus the whole window.
    assert tt["u_b/D"].margin == pytest.approx(299.999969 + 486.821198)
    # `set_max_time_borrow` prints `user max time borrow`.
    (ss,) = corners["ss"]
    assert (ss.max_borrow, ss.margin) == (250.0, pytest.approx(50.0))


def test_a_check_without_its_borrowing_block_is_left_out():
    head = _ISSUE_BLOCK[: _ISSUE_BLOCK.index("Time Borrowing Information")]
    assert pnr_latch_borrow.parse_checks(head) == []
    assert pnr_latch_borrow.parse("") == {}


def test_a_check_without_its_pin_line_names_the_endpoint_instance():
    text = _ISSUE_BLOCK.replace(
        "   0.00  200.00 ^ d (in)\n   0.14  200.14 ^ u_lat/D (DHLx1_ASAP7_75t_R)\n", ""
    )
    (check,) = pnr_latch_borrow.parse_checks(text)
    assert check.pin == "u_lat"


def test_a_corner_summary_takes_the_smallest_margin():
    checks = pnr_latch_borrow.parse(_REPORT)["tt"]
    assert pnr_latch_borrow.summarize(checks, 0.001) == {
        "margin_ps": pytest.approx(-0.215283401),
        "endpoint": "u_c/D",
        "endpoints": 3,
        "borrowing": 2,
    }
    assert pnr_latch_borrow.summarize([], 1.0) == {
        "margin_ps": None,
        "endpoint": None,
        "endpoints": 0,
        "borrowing": 0,
    }


# -- the result fields -------------------------------------------------------------


def _fields(tmp_path, monkeypatch, log, report=None, *, primary="tt", ps=1.0):
    events = _capture_pnr_events(monkeypatch)
    backend = OpenRoadPnr(
        "demo/openroad", _pnr_cfg(tmp_path, None), str(tmp_path), MagicMock()
    )
    backend._ps_per_unit = ps
    if report is not None:
        os.makedirs(backend.artefact_dir, exist_ok=True)
        Path(backend.artefact_dir, pnr_latch_borrow.REPORT).write_text(report)
    platform = MagicMock(get_sta_corner=lambda: primary)
    return backend._latch_borrow_fields(platform, log), events


def test_the_result_carries_the_smallest_margin_over_the_corners(tmp_path, monkeypatch):
    log = "RB-LATCH-BORROW: tt 3\nRB-LATCH-BORROW: ss 3\n"
    fields, events = _fields(tmp_path, monkeypatch, log, _REPORT)

    assert fields["latch_borrow_margin_ps"] == pytest.approx(-215.283401)
    record = fields["latch_borrow"]
    assert (record["corner"], record["endpoint"]) == ("tt", "u_c/D")
    assert (record["endpoints"], record["borrowing"]) == (3, 2)
    assert record["corners"]["ss"] == {
        "pins": 3,
        "margin_ps": pytest.approx(50.0),
        "endpoint": "u_a/D",
        "endpoints": 1,
        "borrowing": 1,
    }
    assert not [e for e in events if e[1] == "pnr.latch_borrow_unavailable"]


def test_margins_are_converted_from_the_timer_unit(tmp_path, monkeypatch):
    report = "Latch D endpoints at corner typ\n" + _ISSUE_BLOCK.replace(
        "499.18", "0.49918"
    ).replace("200.14\n--", "0.20014\n--")
    fields, _ = _fields(
        tmp_path, monkeypatch, "RB-LATCH-BORROW: typ 1\n", report, ps=1000.0
    )
    assert fields["latch_borrow_margin_ps"] == pytest.approx(299.04)


def test_a_design_without_latches_has_no_field(tmp_path, monkeypatch):
    # A stale file from an earlier run is not read without the step's line.
    fields, events = _fields(tmp_path, monkeypatch, ">>> Final reports\n", _REPORT)
    assert fields == {}
    assert not events


def test_latches_without_a_timed_check_have_no_margin(tmp_path, monkeypatch):
    fields, events = _fields(
        tmp_path,
        monkeypatch,
        "RB-LATCH-BORROW: tt 2\n",
        "Latch D endpoints at corner tt\n",
    )
    assert "latch_borrow_margin_ps" not in fields
    assert (fields["latch_borrow"]["pins"], fields["latch_borrow"]["endpoints"]) == (
        2,
        0,
    )
    assert not events


@pytest.mark.parametrize(
    ("log", "report", "reason"),
    [
        ("RB-LATCH-BORROW: tt 3\n", None, "No such file"),
        (
            "rb: latch borrow report unavailable: boom\n",
            None,
            "rb: latch borrow report unavailable: boom",
        ),
        (
            "rb: latch borrow report at ss unavailable: boom\nRB-LATCH-BORROW: tt 3\n",
            _REPORT,
            "at ss unavailable",
        ),
    ],
    ids=["missing file", "no pins", "one corner failed"],
)
def test_an_unreadable_report_warns(tmp_path, monkeypatch, log, report, reason):
    fields, events = _fields(tmp_path, monkeypatch, log, report)
    level, warned = _one_event(events, "pnr.latch_borrow_unavailable")
    assert level == logging.WARNING
    assert reason in warned["reason"]
    assert warned["report"].endswith(f"demo_pnr/{pnr_latch_borrow.REPORT}")
    if report is None:
        assert fields == {}
    else:
        # The corner that was reported still counts.
        assert list(fields["latch_borrow"]["corners"]) == ["tt"]
    message = _human_message("pnr.latch_borrow_unavailable", warned)
    assert "latch borrow margin is unavailable" in message


def test_the_machine_row_carries_the_fields():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    record = {"margin_ps": 299.0, "endpoint": "u_lat/D", "corner": "tt"}
    results = PnrPassResults(
        name="demo/results",
        fields={"latch_borrow_margin_ps": 299.0, "latch_borrow": record},
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["latch_borrow_margin_ps"] == 299.0
    assert row["latch_borrow"] == record


# -- the summary table -------------------------------------------------------------


def _latch(margin, borrowing=1):
    return {
        "latch_borrow_margin_ps": margin,
        "latch_borrow": {"corners": {"tt": {"borrowing": borrowing}}},
    }


@pytest.mark.parametrize(
    ("res", "cell"),
    [
        (
            {"wns_setup_ps": 0.0, **_latch(299.04)},
            "+0.000 ns (latch borrow, +0.299 ns left)",
        ),
        (
            {"wns_setup_ps": 0.0, **_latch(-5.0)},
            "+0.000 ns (latch borrow, -0.005 ns left)",
        ),
        # No latch borrows: the slack of 0 is not a latch's.
        ({"wns_setup_ps": 0.0, **_latch(786.0, borrowing=0)}, "+0.000 ns"),
        ({"wns_setup_ps": 12.0, **_latch(299.0)}, "+0.012 ns"),
        ({"wns_setup_ps": 0.0}, "+0.000 ns"),
        ({}, "-"),
    ],
    ids=[
        "borrowing",
        "negative margin",
        "not borrowing",
        "positive",
        "no latches",
        "unread",
    ],
)
def test_the_setup_cell_names_a_borrowing_latch(res, cell):
    from rtl_buddy.rtl_buddy import _pnr_wns_setup_cell

    assert _pnr_wns_setup_cell(res) == cell


def test_a_minus_zero_setup_slack_is_not_a_latch_note():
    from rtl_buddy.rtl_buddy import _pnr_wns_setup_cell

    # The final reports print a slack in (-5 ps, 0) as -0.0: a real violation.
    assert "latch" not in _pnr_wns_setup_cell({"wns_setup_ps": -0.0, **_latch(299.0)})


# -- the flow step -----------------------------------------------------------------


def test_the_step_reports_each_corner_in_its_scene():
    single = pnr_latch_borrow.report_tcl([], "typ")
    assert single.startswith('\nputs ">>> Latch borrow"\n')
    assert single.endswith(
        'rb::latch_borrow::report RB-LATCH-BORROW: "Latch D endpoints at corner" '
        '$OUT_DIR/latch_borrow.rpt [list [list typ {}]] ""'
    )
    # A single-corner step names no corner and no mode.
    for absent in ("set_mode", "-corner", "-scene", "rb::latch_borrow::corner_args"):
        assert absent not in single
    multi = pnr_latch_borrow.report_tcl(["ss", "ff"], "ss")
    assert "proc rb::latch_borrow::corner_args" in multi
    assert "set_mode" not in multi
    assert multi.endswith(
        "[list [list ss [rb::latch_borrow::corner_args ss]] "
        '[list ff [rb::latch_borrow::corner_args ff]]] ""'
    )
    assert pnr_latch_borrow.report_tcl(["ss", "ff"], "ss", modes=True).endswith(
        "set rb_latch_borrow_mode [sta::cmd_mode_name]\n"
        'rb::latch_borrow::report RB-LATCH-BORROW: "Latch D endpoints at corner" '
        "$OUT_DIR/latch_borrow.rpt "
        "[list [list ss [rb::latch_borrow::corner_args ss] {set_mode ss}] "
        "[list ff [rb::latch_borrow::corner_args ff] {set_mode ff}]] "
        "{set_mode $::rb_latch_borrow_mode}"
    )


def test_the_report_is_cleared_before_a_run():
    assert pnr_latch_borrow.REPORT in pnr_openroad._FIXED_OUTPUT_NAMES


def _finish_script(tmp_path, *, stages):
    pnr_cfg = _pnr_cfg(tmp_path, CHECKPOINT_STAGES if stages else None, stages=stages)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    if stages:
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
        os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    script = Path(
        backend._write_script(_platform(_pdk(tmp_path)), pnr_cfg.get_floorplan())
    )
    if not stages:
        return {"pnr.tcl": script.read_text()}
    return {p.name: p.read_text() for p in Path(backend._ckpt_run_dir).glob("*.tcl")}


@pytest.mark.parametrize("stages", [False, True], ids=["one session", "stages"])
def test_the_step_runs_in_the_final_reports_only(tmp_path, stages):
    scripts = _finish_script(tmp_path, stages=stages)
    with_step = [n for n, s in scripts.items() if "rb::latch_borrow::report" in s]
    assert with_step == ["07_finish.tcl" if stages else "pnr.tcl"]
    text = scripts[with_step[0]]
    order = [
        'puts ">>> Final reports"',
        "report_tns -min\n",
        "> $OUT_DIR/timing.rpt\n",
        'puts ">>> Latch borrow"\n',
        "proc rb::latch_borrow::report",
        "rb::latch_borrow::report RB-LATCH-BORROW: ",
        "electrical.rpt",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)


#: A timer with latch pins `pins`, a mode per corner, and `report_checks` that records its arguments.
_STUB = r"""
namespace eval sta {}
set ::mode ss
set ::modes {}
set ::checks {}
proc sta::cmd_mode_name {} { return $::mode }
proc sta::find_scene {name} { return $name }
proc set_mode {name} { lappend ::modes $name; set ::mode $name }
proc all_registers {args} { return $::pins }
proc report_checks {args} {
    lappend ::checks [list $::mode {*}$args]
    if {[lindex $args end-1] eq ">>"} {
        set f [open [lindex $args end] a]; chan puts $f checks; close $f
    }
}
proc puts {args} { lappend ::lines [lindex $args end] }
set ::lines {}
"""


def test_the_tcl_reports_each_corner_in_its_mode(tmp_path):
    report = tmp_path / "latch_borrow.rpt"
    report.write_text("stale\n")
    result = _tcl_eval(
        _STUB
        + "set ::pins {u_a/D u_b/D}\n"
        + f"set OUT_DIR {tmp_path}\n"
        + pnr_latch_borrow.report_tcl(["ss", "ff"], "ss", modes=True)
        + "\nset result [list [lrange $::lines 1 end] $::modes $::checks]\n"
    )
    header = "{Latch D endpoints at corner"
    assert (
        f"{{{header} ss}} {{RB-LATCH-BORROW: ss 2}} {header} ff}} {{RB-LATCH-BORROW: ff 2}}}}"
        in result
    )
    # Each corner in its own mode, then the mode before is current again.
    assert "{ss ff ss}" in result
    assert "-scene ff" in result and "-group_path_count 2" in result
    # The stale file is gone; the header lines went to the stubbed `puts`.
    assert report.read_text() == "checks\nchecks\n"


def test_the_tcl_writes_nothing_without_latches(tmp_path):
    report = tmp_path / "latch_borrow.rpt"
    report.write_text("stale\n")
    result = _tcl_eval(
        _STUB
        + "set ::pins {}\n"
        + f"set OUT_DIR {tmp_path}\n"
        + pnr_latch_borrow.report_tcl([], "typ")
        + "\nset result [list [llength $::lines] [llength $::checks]]\n"
    )
    # Only the step's own `>>> Latch borrow` line.
    assert result.strip() == "1 0"
    assert not report.exists()


# -- in OpenROAD -------------------------------------------------------------------

_OPENROAD = shutil.which("openroad")
needs_openroad = pytest.mark.skipif(_OPENROAD is None, reason="openroad not installed")

_LEF = dedent("""\
    VERSION 5.8 ;
    BUSBITCHARS "[]" ;
    DIVIDERCHAR "/" ;
    UNITS
      DATABASE MICRONS 1000 ;
    END UNITS
    SITE core
      CLASS CORE ;
      SIZE 0.2 BY 2.0 ;
    END core
    LAYER met1
      TYPE ROUTING ; DIRECTION HORIZONTAL ; PITCH 0.2 ; WIDTH 0.1 ; SPACING 0.1 ;
    END met1
    MACRO LATH
      CLASS CORE ;
      ORIGIN 0 0 ;
      SIZE 0.6 BY 2.0 ;
      SITE core ;
      PIN D
        DIRECTION INPUT ;
        PORT
          LAYER met1 ; RECT 0.0 0.0 0.1 0.1 ;
        END
      END D
      PIN G
        DIRECTION INPUT ;
        PORT
          LAYER met1 ; RECT 0.2 0.0 0.3 0.1 ;
        END
      END G
      PIN Q
        DIRECTION OUTPUT ;
        PORT
          LAYER met1 ; RECT 0.5 0.0 0.6 0.1 ;
        END
      END Q
    END LATH
    END LIBRARY
    """)

#: A latch transparent while `G` is high, its setup check 0.02 ns before `G` falls.
_LIB = dedent("""\
    library (rb_latch_test) {
      delay_model : table_lookup ;
      time_unit : "1ns" ;
      voltage_unit : "1V" ;
      current_unit : "1mA" ;
      capacitive_load_unit (1, pf) ;
      leakage_power_unit : "1nW" ;
      nom_voltage : 1.0 ;
      cell (LATH) {
        area : 1.0 ;
        pin (D) {
          direction : input ;
          capacitance : 0.001 ;
          timing () {
            related_pin : "G" ;
            timing_type : setup_falling ;
            rise_constraint (scalar) { values ("0.02") ; }
            fall_constraint (scalar) { values ("0.02") ; }
          }
        }
        pin (G) { direction : input ; capacitance : 0.001 ; clock : true ; }
        pin (Q) {
          direction : output ;
          function : "IQ" ;
          timing () {
            related_pin : "D" ;
            timing_sense : positive_unate ;
            cell_rise (scalar) { values ("0.01") ; }
            cell_fall (scalar) { values ("0.01") ; }
            rise_transition (scalar) { values ("0.01") ; }
            fall_transition (scalar) { values ("0.01") ; }
          }
          timing () {
            related_pin : "G" ;
            timing_sense : non_unate ;
            timing_type : rising_edge ;
            cell_rise (scalar) { values ("0.01") ; }
            cell_fall (scalar) { values ("0.01") ; }
            rise_transition (scalar) { values ("0.01") ; }
            fall_transition (scalar) { values ("0.01") ; }
          }
        }
        latch (IQ, IQN) { enable : "G" ; data_in : "D" ; }
      }
    }
    """)

_NETLIST = dedent("""\
    module lat (input clk, input a, input b, output qa, output qb);
      LATH u_a (.G(clk), .D(a), .Q(qa));
      LATH u_b (.G(clk), .D(b), .Q(qb));
    endmodule
    """)


def _run(tmp_path, script: str) -> str:
    path = tmp_path / "run.tcl"
    path.write_text(script)
    proc = subprocess.run(
        [_OPENROAD, "-no_init", "-exit", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "[ERROR" not in out, out
    return out


@needs_openroad
def test_the_step_reads_each_latch_margin_in_openroad(tmp_path, monkeypatch):
    (tmp_path / "cells.lef").write_text(_LEF)
    (tmp_path / "cells.lib").write_text(_LIB)
    (tmp_path / "lat.v").write_text(_NETLIST)
    out = _run(
        tmp_path,
        dedent("""\
            set OUT_DIR [pwd]
            read_lef cells.lef
            read_liberty cells.lib
            read_verilog lat.v
            link_design lat
            create_clock -name clk -period 1 [get_ports clk]
            # Both arrive while the latches are open, 0.28 ns and 0.08 ns before they close.
            set_input_delay 0.2 -clock clk [get_ports a]
            set_input_delay 0.4 -clock clk [get_ports b]
            report_worst_slack -max -digits 6
            """)
        + pnr_latch_borrow.report_tcl([], "typ")
        + "\n",
    )
    assert "worst slack max 0.000000" in out
    report = (tmp_path / pnr_latch_borrow.REPORT).read_text()
    fields, events = _fields(tmp_path, monkeypatch, out, report, ps=1000.0)

    assert not events
    assert fields["latch_borrow_margin_ps"] == pytest.approx(80.0, abs=0.01)
    record = fields["latch_borrow"]
    assert (record["endpoint"], record["corner"]) == ("u_b/D", "typ")
    assert (record["endpoints"], record["borrowing"]) == (2, 2)


@needs_openroad
def test_the_step_is_silent_on_a_design_without_latches(tmp_path, monkeypatch):
    (tmp_path / "cells.lef").write_text(_LEF)
    (tmp_path / "cells.lib").write_text(_LIB)
    (tmp_path / "comb.v").write_text(
        "module comb (input a, output y);\n  assign y = a;\nendmodule\n"
    )
    out = _run(
        tmp_path,
        "set OUT_DIR [pwd]\nread_lef cells.lef\nread_liberty cells.lib\n"
        "read_verilog comb.v\nlink_design comb\n"
        + pnr_latch_borrow.report_tcl([], "typ")
        + "\n",
    )
    assert "RB-LATCH-BORROW" not in out and "rb: latch" not in out
    assert not (tmp_path / pnr_latch_borrow.REPORT).exists()
    assert _fields(tmp_path, monkeypatch, out)[0] == {}


_ASAP7 = os.environ.get("RTL_BUDDY_ASAP7_DIR")
needs_asap7 = pytest.mark.skipif(
    _OPENROAD is None or not _ASAP7,
    reason="needs openroad and RTL_BUDDY_ASAP7_DIR (an ORFS platforms/asap7 directory)",
)

#: The issue's reproducer, up to its reports.
_REPRODUCER = """\
module lat (input clk, input d, output q);
  wire l, n1, n2;
  DHLx1_ASAP7_75t_R u_lat (.CLK(clk), .D(d), .Q(l));
  INVx1_ASAP7_75t_R u_i1 (.A(l), .Y(n1));
  DFFHQNx1_ASAP7_75t_R u_ff (.CLK(clk), .D(n1), .QN(n2));
  INVx1_ASAP7_75t_R u_i2 (.A(n2), .Y(q));
endmodule
"""

_REPRODUCER_TCL = """\
set OUT_DIR [pwd]
set p $::env(RTL_BUDDY_ASAP7_DIR)
read_lef $p/lef/asap7_tech_1x_201209.lef
read_lef $p/lef/asap7sc7p5t_28_R_1x_220121a.lef
foreach l {INVBUF_RVT_TT_nldm_220122.lib.gz SEQ_RVT_TT_nldm_220123.lib} {
  read_liberty $p/lib/NLDM/asap7sc7p5t_$l
}
read_verilog lat.v
link_design lat
create_clock -name clk -period 1000 [get_ports clk]
set_input_delay 200 -clock clk [get_ports d]
set_output_delay 100 -clock clk [get_ports q]
initialize_floorplan -die_area {0 0 10 10} -core_area {1 1 9 9} -site asap7sc7p5t
source $p/openRoad/make_tracks.tcl
place_pins -hor_layers M4 -ver_layers M5
global_placement -skip_io
detailed_placement
source $p/setRC.tcl
estimate_parasitics -placement
report_worst_slack -max
"""


@needs_asap7
def test_the_issue_reproducer_reports_the_latch_margin(tmp_path, monkeypatch):
    (tmp_path / "lat.v").write_text(_REPRODUCER)
    out = _run(
        tmp_path,
        _REPRODUCER_TCL + pnr_latch_borrow.report_tcl([], "tt") + "\n",
    )
    assert "worst slack max 0.00" in out
    report = (tmp_path / pnr_latch_borrow.REPORT).read_text()
    fields, _ = _fields(tmp_path, monkeypatch, out, report)

    # About 299 ps to the closing edge of the 500 ps window.
    assert 290.0 < fields["latch_borrow_margin_ps"] < 300.0
    record = fields["latch_borrow"]
    assert (record["endpoint"], record["endpoints"], record["borrowing"]) == (
        "u_lat/D",
        1,
        1,
    )
