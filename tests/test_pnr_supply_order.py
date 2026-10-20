"""Tests for the supply-port order of the powered netlist and a hardened block's timing model (`pnr_supply_order`, rtl_buddy#889, #890).

Pinned behaviour:

- the supply ports, those the powered netlist declares and the logical one does not, go
  last, sorted by name, in the powered netlist's port list and declarations and in the
  timing model's cell group; nothing else moves;
- a staged and a one-session run write byte-identical `<top>.routed.pwr.v` and
  `abstract/<top>.lib` (stub OpenROAD that orders the ports as each session does);
- with OpenROAD installed: the session that ran `pdngen` and a session that read its
  database write the same powered netlist and timing model once ordered, and the
  powered netlist connects every instance supply pin.
"""

import re
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

from test_pnr_checkpoints import _backend, _pnr_cfg
from test_pnr_supply_ports import (
    _CALL,
    _CELL_LEF,
    _PDN,
    _openroad,
    _source,
    needs_openroad,
)
from test_pnr_block_power import _TECH_LEF

from rtl_buddy.config.pnr import PnrAbstract
from rtl_buddy.tools.pnr_supply_order import (
    canonicalise,
    netlist_ports,
    order_liberty,
    order_netlist,
)

# As OpenROAD writes them: the session that read the database, then the one that ran
# `pdngen`.
_READ_V = dedent("""\
    module top (en,
        z,
        VDD,
        VSS,
        a);
     input en;
     output z;
     inout VDD;
     inout VSS;
     input [1:0] a;


     inv u0 (.A(a[0]),
        .Y(z),
        .VDD(VDD),
        .VSS(VSS));
    endmodule
    """)
_BUILT_V = dedent("""\
    module top (en,
        z,
        a,
        VDD,
        VSS);
     input en;
     output z;
     input [1:0] a;
     inout VDD;
     inout VSS;


     inv u0 (.A(a[0]),
        .Y(z),
        .VDD(VDD),
        .VSS(VSS));
    endmodule
    """)
_LOGICAL_V = dedent("""\
    module top (en,
        z,
        a);
     input en;
     output z;
     input [1:0] a;


     inv u0 (.A(a[0]),
        .Y(z));
    endmodule
    """)

_PIN_EN = '    pin("en") {\n      direction : input;\n    }\n'
_PIN_VDD = (
    '    pin("VDD") {\n      direction : input;\n      capacitance : 0.0000;\n    }\n'
)
_PIN_VSS = (
    '    pin("VSS") {\n      direction : input;\n      capacitance : 0.0000;\n    }\n'
)
# The member pins sit at the bus's own indent, as OpenSTA writes them.
_BUS_A = (
    '    bus("a") {\n      bus_type : a;\n'
    '    pin("a[1]") {\n      direction : input;\n    }\n'
    '    pin("a[0]") {\n      timing() {\n        related_pin : "en";\n      }\n    }\n'
    "    }\n"
)


def _lib(*groups: str) -> str:
    return (
        'library (top) {\n  type ("a") {\n    bit_width : 2;\n  }\n\n'
        '  cell ("top") {\n    area : 3.000 \n    is_macro_cell : true;\n'
        + "".join(groups)
        + "  }\n\n}\n"
    )


def test_netlist_ports_reads_the_port_list():
    assert netlist_ports(_READ_V, "top") == ["en", "z", "VDD", "VSS", "a"]
    assert netlist_ports(_READ_V, "other") == []


def test_the_supply_ports_go_last_in_the_port_list_and_declarations():
    assert order_netlist(_READ_V, "top", {"VDD", "VSS"}) == _BUILT_V
    assert order_netlist(_BUILT_V, "top", {"VDD", "VSS"}) == _BUILT_V


def test_the_supply_ports_are_sorted_by_name():
    swapped = _BUILT_V.replace("    VDD,\n    VSS);", "    VSS,\n    VDD);").replace(
        " inout VDD;\n inout VSS;", " inout VSS;\n inout VDD;"
    )
    assert swapped != _BUILT_V
    assert order_netlist(swapped, "top", {"VDD", "VSS"}) == _BUILT_V


def test_the_supply_pin_groups_go_last_in_the_cell():
    read = _lib(_PIN_EN, _PIN_VDD, _PIN_VSS, _BUS_A)
    built = _lib(_PIN_EN, _BUS_A, _PIN_VDD, _PIN_VSS)

    assert order_liberty(read, "top", {"VDD", "VSS"}) == built
    assert order_liberty(built, "top", {"VDD", "VSS"}) == built
    assert (
        order_liberty(_lib(_PIN_EN, _PIN_VSS, _BUS_A, _PIN_VDD), "top", {"VDD", "VSS"})
        == built
    )


def test_text_without_the_module_or_cell_is_left_alone():
    assert order_netlist(_READ_V, "other", {"VDD"}) == _READ_V
    lib = _lib(_PIN_VDD, _BUS_A)
    assert order_liberty(lib, "other", {"VDD"}) == lib
    assert order_liberty(lib, "top", set()) == lib


def test_canonicalise_finds_the_supply_ports_and_rewrites_both_files(tmp_path):
    logical, powered, lib = (tmp_path / n for n in ("t.v", "t.pwr.v", "t.lib"))
    logical.write_text(_LOGICAL_V)
    powered.write_text(_READ_V)
    lib.write_text(_lib(_PIN_EN, _PIN_VDD, _PIN_VSS, _BUS_A))

    assert canonicalise(str(logical), str(powered), "top", str(lib)) == ["VDD", "VSS"]
    assert powered.read_text() == _BUILT_V
    assert lib.read_text() == _lib(_PIN_EN, _BUS_A, _PIN_VDD, _PIN_VSS)
    assert logical.read_text() == _LOGICAL_V


def test_canonicalise_without_a_netlist_changes_nothing(tmp_path):
    lib = tmp_path / "t.lib"
    lib.write_text(_lib(_PIN_VDD, _BUS_A))

    assert (
        canonicalise(str(tmp_path / "t.v"), str(tmp_path / "t.pwr.v"), "top", str(lib))
        == []
    )
    assert lib.read_text() == _lib(_PIN_VDD, _BUS_A)


def _gds_less_harden(tmp_path, monkeypatch, *, stages):
    tmp_path.mkdir()
    cfg = replace(
        _pnr_cfg(tmp_path, stages=stages), harden=True, abstract=PnrAbstract(gds=False)
    )
    backend, artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg, stages=stages)
    monkeypatch.setattr(backend, "_record_block_parameters", lambda *_a: None)
    res = backend.run()
    assert res.results["result"] == "PASS", res.results
    return artefacts


def test_a_staged_and_a_one_session_run_write_the_same_powered_netlist_and_model(
    tmp_path, monkeypatch
):
    one = _gds_less_harden(tmp_path / "one", monkeypatch, stages=False)
    staged = _gds_less_harden(tmp_path / "staged", monkeypatch, stages=True)

    for name in ("demo_top.routed.pwr.v", "abstract/demo_top.lib", "demo_top.routed.v"):
        assert (one / name).read_bytes() == (staged / name).read_bytes(), name
    assert netlist_ports(
        (staged / "demo_top.routed.pwr.v").read_text(), "demo_top"
    ) == [
        "clk",
        "d",
        "VDD",
        "VSS",
    ]
    pins = re.findall(
        r'(?:pin|bus)\("([^"]+)"\)', (staged / "abstract/demo_top.lib").read_text()
    )
    assert pins == ["clk", "d", "d[0]", "VDD", "VSS"]


# A Liberty for `_CELL_LEF`'s inverter, so the session can write a timing model.
_CELL_LIB = dedent("""\
    library (cells) {
      delay_model : table_lookup;
      time_unit : "1ns";
      voltage_unit : "1V";
      current_unit : "1mA";
      capacitive_load_unit (1, pf);
      pulling_resistance_unit : "1kohm";
      nom_voltage : 1.8;
      cell (inv) {
        area : 1.0;
        pg_pin (VDD) { pg_type : primary_power; voltage_name : VDD; }
        pg_pin (VSS) { pg_type : primary_ground; voltage_name : VSS; }
        pin (A) { direction : input; capacitance : 0.002; }
        pin (Y) {
          direction : output;
          function : "!A";
          timing () {
            related_pin : "A";
            timing_sense : negative_unate;
            cell_rise (scalar) { values ("0.1"); }
            cell_fall (scalar) { values ("0.1"); }
            rise_transition (scalar) { values ("0.05"); }
            fall_transition (scalar) { values ("0.05"); }
          }
        }
      }
    }
    """)

_NETLIST = dedent("""\
    module top (en, a, y, z);
      input en;
      input [1:0] a;
      output [1:0] y;
      output z;
      inv u0 (.A(a[0]), .Y(y[0]));
      inv u1 (.A(a[1]), .Y(y[1]));
      inv u2 (.A(en), .Y(z));
    endmodule
    """)

_WRITE = "write_verilog {0}.v\nwrite_verilog -include_pwr_gnd {0}.pwr.v\nwrite_timing_model {0}.lib\n"


@needs_openroad
def test_openroad_sessions_write_one_powered_netlist_and_model_once_ordered(tmp_path):
    for name, text in (
        ("tech.lef", _TECH_LEF),
        ("cells.lef", _CELL_LEF),
        ("cells.lib", _CELL_LIB),
        ("top.v", _NETLIST),
        ("pdn.tcl", _PDN),
        ("supply_ports.tcl", _source()),
    ):
        (tmp_path / name).write_text(text)
    _openroad(
        tmp_path,
        dedent("""\
            read_liberty cells.lib
            read_lef tech.lef
            read_lef cells.lef
            read_verilog top.v
            link_design top
            initialize_floorplan -die_area {0 0 100 100} -core_area {10 10 90 90} -site core
            place_inst -name u0 -location {10 10} -status PLACED
            place_inst -name u1 -location {20 10} -status PLACED
            place_inst -name u2 -location {30 10} -status PLACED
            source pdn.tcl
            pdngen
            source supply_ports.tcl
            """)
        + _CALL
        + _WRITE.format("built")
        + "write_db built.odb\n",
    )
    _openroad(
        tmp_path, "read_liberty cells.lib\nread_db built.odb\n" + _WRITE.format("read")
    )
    texts = {}
    for session in ("built", "read"):
        files = [str(tmp_path / f"{session}{ext}") for ext in (".v", ".pwr.v", ".lib")]
        texts[session, "before"] = (
            Path(files[1]).read_text() + Path(files[2]).read_text()
        )
        assert canonicalise(files[0], files[1], "top", files[2]) == ["VDD", "VSS"]
        texts[session] = Path(files[1]).read_text() + Path(files[2]).read_text()

    assert texts["built", "before"] != texts["read", "before"]
    assert texts["built"] == texts["read"]
    assert texts["built"] == texts["built", "before"]
    powered = (tmp_path / "read.pwr.v").read_text()
    assert netlist_ports(powered, "top")[-2:] == ["VDD", "VSS"]
    for inst in ("u0", "u1", "u2"):
        assert re.search(rf"inv {inst} \([^;]*\.VDD\(VDD\),\s*\.VSS\(VSS\)\);", powered)


def test_the_powered_netlist_is_a_flow_output():
    from rtl_buddy.tools.pnr_openroad import run_output_paths

    assert "/a/demo_top.routed.pwr.v" in run_output_paths("/a", "demo_top")
