"""Tests for the supply-port order of the powered netlist and a hardened block's timing model (`pnr_supply_order`, rtl_buddy#889, #890).

Pinned behaviour:

- the supply ports, those the powered netlist declares and the logical one does not, go
  last, sorted by name, in the powered netlist's port list and declarations and in the
  timing model's cell group; nothing else moves;
- without the logical or powered port list nothing is rewritten; a file the rewrite
  could not order is checked with an independent reader and reported, by name, in a
  `pnr.supply_order_unverified` warning;
- a file is replaced atomically, so a failed write leaves the original; a read, decode
  or write error fails the run with `fail_stage: outputs`;
- a staged and a one-session run write byte-identical `<top>.routed.pwr.v` and
  `abstract/<top>.lib` (stub OpenROAD that orders the ports as each session does);
- with OpenROAD installed: the session that ran `pdngen` and a session that read its
  database write the same powered netlist and timing model once ordered, and the
  powered netlist connects every instance supply pin.
"""

import logging
import os
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

import pytest

from rtl_buddy.config.pnr import PnrAbstract
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.tools import pnr_supply_order
from rtl_buddy.tools.pnr_supply_order import (
    canonicalise,
    liberty_pins,
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
    assert netlist_ports(_READ_V, "other") is None
    assert netlist_ports("module top (a, \\b[0] ,\n  VDD);", "top") == [
        "a",
        "\\b[0]",
        "VDD",
    ]


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

    order = canonicalise(str(logical), str(powered), "top", str(lib))
    assert order.supplies == ["VDD", "VSS"]
    assert order.unverified == []
    assert powered.read_text() == _BUILT_V
    assert lib.read_text() == _lib(_PIN_EN, _BUS_A, _PIN_VDD, _PIN_VSS)
    assert logical.read_text() == _LOGICAL_V


def _files(tmp_path, logical=_LOGICAL_V, powered=_READ_V):
    paths = [tmp_path / n for n in ("t.v", "t.pwr.v", "t.lib")]
    for path, text in zip(paths, (logical, powered, _lib(_PIN_VDD, _BUS_A))):
        if text is not None:
            path.write_text(text)
    return paths


@pytest.mark.parametrize(
    "logical",
    [None, "", _LOGICAL_V[: _LOGICAL_V.index("a);")]],
    ids=["missing", "empty", "truncated"],
)
def test_without_the_logical_port_list_nothing_is_rewritten(tmp_path, logical):
    paths = _files(tmp_path, logical=logical)

    order = canonicalise(*map(str, paths[:2]), "top", str(paths[2]))

    assert order.supplies == []
    assert [f for f, _reason in order.unverified] == [str(paths[0])]
    assert paths[1].read_text() == _READ_V
    assert paths[2].read_text() == _lib(_PIN_VDD, _BUS_A)


def test_without_the_powered_port_list_nothing_is_rewritten(tmp_path):
    paths = _files(tmp_path, powered=_READ_V[:20])

    order = canonicalise(*map(str, paths[:2]), "top", str(paths[2]))

    assert [f for f, _reason in order.unverified] == [str(paths[1])]
    assert paths[2].read_text() == _lib(_PIN_VDD, _BUS_A)


def test_an_unrecognised_port_list_is_left_alone_and_reported(tmp_path):
    one_line = _READ_V.replace(",\n    ", ", ")
    paths = _files(tmp_path, powered=one_line)

    order = canonicalise(*map(str, paths[:2]), "top")

    assert order.supplies == ["VDD", "VSS"]
    assert paths[1].read_text() == one_line
    assert order.unverified == [(str(paths[1]), "supply ports VDD, VSS are not last")]


def test_an_unrecognised_liberty_layout_is_reported(tmp_path):
    paths = _files(tmp_path)
    next_line = _lib(_PIN_EN, _PIN_VDD.replace(") {", ")\n    {"), _PIN_VSS, _BUS_A)
    paths[2].write_text(next_line)

    order = canonicalise(*map(str, paths[:2]), "top", str(paths[2]))

    assert paths[1].read_text() == _BUILT_V
    assert order.unverified == [(str(paths[2]), "supply pins VDD, VSS are not last")]


def test_liberty_pins_reads_any_brace_layout():
    lib = (
        'library (x) {\n  cell ("top") {\n    pin("VDD")\n    {\n      /* { */\n    }\n'
        '    bus("a") { pin("a[0]") { x : "{"; } }\n    pin(en) { } }\n}\n'
    )
    assert liberty_pins(lib, "top") == ["VDD", "a", "en"]
    assert liberty_pins(lib, "other") is None


def test_a_failed_write_leaves_the_original(tmp_path, monkeypatch):
    paths = _files(tmp_path)

    def _fail(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pnr_supply_order.os, "replace", _fail)
    with pytest.raises(OSError, match="No space"):
        canonicalise(*map(str, paths[:2]), "top", str(paths[2]))

    assert paths[1].read_text() == _READ_V
    assert sorted(os.listdir(tmp_path)) == ["t.lib", "t.pwr.v", "t.v"]


def test_a_decoding_error_names_the_file(tmp_path):
    paths = _files(tmp_path)
    paths[2].write_bytes(b"library (x) { /* \xff */ }\n")

    with pytest.raises(UnicodeDecodeError, match=f"in {paths[2]}"):
        canonicalise(*map(str, paths[:2]), "top", str(paths[2]))


def test_the_supply_order_events_read_as_sentences():
    unverified = _human_message(
        "pnr.supply_order_unverified",
        {"pnr": "blk", "file": "/a/t.pwr.v", "reason": "supply ports VDD are not last"},
    )
    assert "/a/t.pwr.v: supply ports VDD are not last" in unverified
    failed = _human_message(
        "pnr.supply_order_failed", {"pnr": "blk", "error": "disk full"}
    )
    assert "cannot put the supply ports last: disk full" in failed


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


def _harden(tmp_path, monkeypatch, *, stages=True):
    tmp_path.mkdir()
    cfg = replace(
        _pnr_cfg(tmp_path, stages=stages), harden=True, abstract=PnrAbstract(gds=False)
    )
    backend, artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg, stages=stages)
    monkeypatch.setattr(backend, "_record_block_parameters", lambda *_a: None)
    return backend, artefacts


@pytest.mark.parametrize(
    "target, error",
    [
        ("_write", OSError(28, "No space left on device")),
        ("_read", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")),
    ],
    ids=["write", "decode"],
)
def test_a_supply_order_io_error_fails_the_run_with_fail_stage_outputs(
    tmp_path, monkeypatch, caplog, target, error
):
    backend, artefacts = _harden(tmp_path / "run", monkeypatch)
    calls = []

    def _raise(*_a):
        calls.append(target)
        raise error

    monkeypatch.setattr(pnr_supply_order, target, _raise)
    with caplog.at_level(logging.ERROR):
        res = backend.run()

    assert calls == [target]
    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "outputs"
    assert res.results["desc"].startswith("cannot put the supply ports last: ")
    assert [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.supply_order_failed"
    ]
    assert not (artefacts / "abstract").exists()
    assert not (artefacts / "demo_top.routed.pwr.v").exists()


def test_a_run_whose_logical_netlist_has_no_port_list_passes_and_warns(
    tmp_path, monkeypatch, caplog
):
    backend, artefacts = _harden(tmp_path / "run", monkeypatch)
    real_canonicalise = pnr_supply_order.canonicalise

    def _truncated(logical, *args):
        Path(logical).write_text("module demo_top (clk,\n")
        return real_canonicalise(logical, *args)

    monkeypatch.setattr(pnr_supply_order, "canonicalise", _truncated)
    with caplog.at_level(logging.WARNING):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results
    warnings = [
        r.rtl_fields
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.supply_order_unverified"
    ]
    assert warnings == [
        {
            "pnr": backend.pnr_cfg.get_name(),
            "file": str(artefacts / "demo_top.routed.v"),
            "reason": "no port list of module demo_top; supply order unchanged",
        }
    ]


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
        order = canonicalise(files[0], files[1], "top", files[2])
        assert order.supplies == ["VDD", "VSS"] and order.unverified == []
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
