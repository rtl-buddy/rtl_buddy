"""Tests for the routed netlist's supply-port form (`rtl_buddy/pnr/supply_ports.tcl`, rtl_buddy#885).

Pinned behaviour:

- `<top>.routed.v` is the logical netlist: `write_verilog` without `-include_pwr_gnd`;
  `<top>.routed.pwr.v` is the powered one, written beside it;
- the `finish` session, and only it, re-asserts the supply bterms after fill and the
  `before-reports` hooks and before the final reports, the routed outputs and a
  hardened block's timing model, in a one-session and in a staged run;
- the re-assertion sets each POWER or GROUND bterm's IO type to the value it already
  has, sets nothing else and leaves signal bterms alone;
- with OpenROAD installed: the session that ran `pdngen` and a session that read its
  database then write identical netlists without supply ports, and without the
  re-assertion the first declares them (the case the step exists for).
"""

import shutil
import subprocess
from importlib.resources import files
from textwrap import dedent

import pytest
from test_pnr import _make_pdk_cfg, _platform, _render_flow
from test_pnr_block_power import _TECH_LEF
from test_pnr_macro_pack import _tcl_eval
from test_pnr_stages import _NAMES, _STAGES, _only_run, _staged

_CALL = "rb::supply_ports::reassert\n"
_ROUTED_V = "write_verilog $OUT_DIR/${DESIGN}.routed.v\n"
_PWR_V = "write_verilog -include_pwr_gnd $OUT_DIR/${DESIGN}.routed.pwr.v\n"


def _source() -> str:
    return files("rtl_buddy.pnr").joinpath("supply_ports.tcl").read_text()


def test_the_routed_netlist_is_written_without_supply_pins(tmp_path):
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))

    assert text.count("\nwrite_verilog ") == 2
    assert "\n" + _ROUTED_V in text
    assert text.count("include_pwr_gnd") == 1
    assert "\n" + _PWR_V in text


def test_finish_reasserts_the_supply_ports_before_the_reports_and_outputs(tmp_path):
    pdk = _make_pdk_cfg(tmp_path, rcx_rules="pdk/rcx.rules")
    text = _render_flow(tmp_path, _platform(pdk), harden=True)

    assert text.count(_CALL) == 1
    at = text.index(_CALL)
    for before in ("filler_placement", "extract_parasitics", "write_spef"):
        assert text.index("\n" + before) < at
    for after in (
        'puts ">>> Final reports"',
        "read_spef",
        "report_worst_slack",
        "write_def",
        _ROUTED_V,
        _PWR_V,
        "write_db",
        "write_timing_model",
    ):
        assert at < text.index("\n" + after)


def test_a_staged_run_reasserts_the_supply_ports_in_its_finish_session(
    tmp_path, monkeypatch
):
    backend, artefacts = _staged(tmp_path, monkeypatch)

    assert backend.run().results["result"] == "PASS"
    run_dir = _only_run(artefacts)
    reasserting = [
        stage
        for stage in _STAGES
        if _CALL in (run_dir / f"{_NAMES[stage]}.tcl").read_text()
    ]
    assert reasserting == ["finish"]


# Stub ODB: a block of three bterms, each a command that answers its getters and logs
# its setters.
_STUB_ODB = r"""
namespace eval ord {}
set calls {}
proc make_bterm {name sig io} {
    proc $name {method args} "
        switch -- \$method {
            getSigType {return $sig}
            getIoType {return $io}
            default {lappend ::calls \[list $name \$method {*}\$args\]}
        }"
}
make_bterm clk SIGNAL INPUT
make_bterm VDD POWER INOUT
make_bterm VSS GROUND INOUT
proc block {method} { return {clk VDD VSS} }
proc ord::get_db_block {} { return block }
"""


def test_reassert_sets_each_supply_bterm_io_type_again_and_nothing_else():
    calls = _tcl_eval(
        _source() + _STUB_ODB + "\nrb::supply_ports::reassert\nset result $calls"
    )

    assert calls == "{VDD setIoType INOUT} {VSS setIoType INOUT}"


_OPENROAD = shutil.which("openroad")
needs_openroad = pytest.mark.skipif(_OPENROAD is None, reason="openroad not installed")

_CELL_LEF = dedent("""\
    VERSION 5.8 ;
    MACRO inv
      CLASS CORE ;
      ORIGIN 0 0 ;
      SIZE 0.92 BY 2.72 ;
      SITE core ;
      PIN A
        DIRECTION INPUT ;
        USE SIGNAL ;
        PORT
          LAYER met1 ; RECT 0.1 1.0 0.3 1.2 ;
        END
      END A
      PIN Y
        DIRECTION OUTPUT ;
        USE SIGNAL ;
        PORT
          LAYER met1 ; RECT 0.6 1.0 0.8 1.2 ;
        END
      END Y
      PIN VDD
        DIRECTION INOUT ;
        USE POWER ;
        PORT
          LAYER met1 ; RECT 0 2.48 0.92 2.96 ;
        END
      END VDD
      PIN VSS
        DIRECTION INOUT ;
        USE GROUND ;
        PORT
          LAYER met1 ; RECT 0 -0.24 0.92 0.24 ;
        END
      END VSS
    END inv
    END LIBRARY
    """)

_NETLIST = dedent("""\
    module top (a, y);
      input [1:0] a;
      output [1:0] y;
      inv u0 (.A(a[0]), .Y(y[0]));
      inv u1 (.A(a[1]), .Y(y[1]));
    endmodule
    """)

# `-pins` makes pdngen create the VDD and VSS bterms in the session, as a platform's
# PDN script does.
_PDN = dedent("""\
    add_global_connection -net {VDD} -pin_pattern {^VDD$} -power
    add_global_connection -net {VSS} -pin_pattern {^VSS$} -ground
    global_connect
    set_voltage_domain -name {CORE} -power {VDD} -ground {VSS}
    define_pdn_grid -name {grid} -voltage_domains {CORE} -pins {met5}
    add_pdn_stripe -grid {grid} -layer {met4} -width {1.600} -pitch {27.200} -offset {13.600}
    add_pdn_stripe -grid {grid} -layer {met5} -width {1.600} -pitch {27.200} -offset {13.600}
    add_pdn_connect -grid {grid} -layers {met4 met5}
    """)

_DIRECTIONS = dedent("""\
    foreach port {VDD VSS} {
      puts "RB-DIR $port [get_property [get_ports $port] direction]"
    }
    """)


def _openroad(tmp_path, script: str) -> str:
    path = tmp_path / f"{len(list(tmp_path.glob('*.tcl')))}.tcl"
    path.write_text(script)
    proc = subprocess.run(
        [_OPENROAD, "-exit", "-no_init", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    return out


def _pdn_then_read_db(tmp_path, *, reassert: bool):
    """Write the netlist in the session that ran pdngen and in one that read its database.

    Returns the two netlists and the first session's output.
    """
    (tmp_path / "tech.lef").write_text(_TECH_LEF)
    (tmp_path / "cells.lef").write_text(_CELL_LEF)
    (tmp_path / "top.v").write_text(_NETLIST)
    (tmp_path / "pdn.tcl").write_text(_PDN)
    (tmp_path / "supply_ports.tcl").write_text(_source())
    built = _openroad(
        tmp_path,
        dedent("""\
            read_lef tech.lef
            read_lef cells.lef
            read_verilog top.v
            link_design top
            initialize_floorplan -die_area {0 0 100 100} -core_area {10 10 90 90} -site core
            place_inst -name u0 -location {10 10} -status PLACED
            place_inst -name u1 -location {20 10} -status PLACED
            source pdn.tcl
            pdngen
            source supply_ports.tcl
            """)
        + (_CALL if reassert else "")
        + _DIRECTIONS
        + "write_verilog built.v\nwrite_db built.odb\n",
    )
    _openroad(tmp_path, "read_db built.odb\n" + _DIRECTIONS + "write_verilog read.v\n")
    return (tmp_path / "built.v").read_text(), (tmp_path / "read.v").read_text(), built


@needs_openroad
def test_openroad_writes_one_netlist_without_supply_ports_from_either_session(
    tmp_path,
):
    built, read, out = _pdn_then_read_db(tmp_path, reassert=True)

    assert built == read
    assert "VDD" not in built and "VSS" not in built
    assert "RB-DIR VDD power" in out and "RB-DIR VSS ground" in out


@needs_openroad
def test_openroad_without_the_reassertion_declares_the_supply_ports(tmp_path):
    built, read, out = _pdn_then_read_db(tmp_path, reassert=False)

    assert " inout VDD;" in built and " inout VSS;" in built
    assert "VDD" not in read
    assert "RB-DIR VDD bidirect" in out
