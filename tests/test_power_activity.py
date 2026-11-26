"""How `rb power` reports what OpenSTA did with a trace's activity (`tools/power_activity.py`)."""

import shutil
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from power_support import _FakePdk, _FakePlatform

from rtl_buddy.config.power import PowerActivity, PowerConfig, PowerSuiteConfig
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.runner.power_results import PowerPassResults
from rtl_buddy.tools import power_activity, power_openroad
from rtl_buddy.tools.power_openroad import OpenRoadPower

OPENROAD = shutil.which("openroad")

_TOTAL_RPT = (
    "Group                  Internal  Switching    Leakage      Total\n"
    "Total                  1.00e-06   2.00e-06   3.00e-07   3.30e-06 100.0%\n"
)

# `report_activity_annotation -report_annotated` as OpenSTA prints it, sorted by pin name.
_ANNOTATED_RPT = dedent("""\
    saif           16
    unannotated     2
    Annotated pins:
     saif clk
     saif m
     saif ub/A
     saif uf/CK
     saif um/CK
     saif um/Q
     saif uo/A
     saif uo/Y
    """)

_CENSUS = dedent(f"""\
    {power_activity.CENSUS_HEADER}
    pin clock 2.00000e+08 0.500 clk
    pin unknown 0.00000e+00 0.000 m
    pin clock 2.00000e+08 0.500 uf/CK
    pin clock 2.00000e+08 0.500 um/CK
    pin unknown 0.00000e+00 0.000 um/Q
    pin unknown 0.00000e+00 0.000 uo/A
    pin unknown 0.00000e+00 0.000 uo/Y
    pin unknown 0.00000e+00 0.000 spare/A
    pin input 1.00000e+07 0.500 d2
    origin unknown 5
    origin clock 3
    origin saif 9
    origin input 1
    """)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


# --- parsing and the join ---------------------------------------------------------------


def test_annotation_counts_stop_at_the_listing():
    """A pin named like a count row must not be read as one."""
    text = _ANNOTATED_RPT + " saif 99\n"
    assert power_activity.parse_annotation_counts(text) == {
        "saif": 16,
        "unannotated": 2,
    }


def test_annotation_counts_need_the_unannotated_row():
    assert power_activity.parse_annotation_counts("saif 3\n") is None


def test_the_audit_splits_annotated_fallbacks_into_clock_and_unused(tmp_path):
    """An annotated pin that takes the SDC clock's rate is `clock`, one with no activity is
    unused; an unannotated pin with no activity (`spare/A`) and a primary input at the
    default rate (`d2`) are in neither.
    """
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", _ANNOTATED_RPT),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    assert fields == {
        "activity_annotation": {"saif": 16, "unannotated": 2},
        "activity_origins": {"clock": 3, "input": 1, "saif": 9, "unknown": 5},
        "activity_unused": {
            "count": 4,
            "annotated": 16,
            "by_origin": {"unknown": 4},
            "examples": ["m", "um/Q", "uo/A", "uo/Y"],
        },
        "activity_annotated_clock": {
            "count": 3,
            "examples": ["clk", "uf/CK", "um/CK"],
        },
    }


def test_an_unannotated_listing_gives_the_same_join(tmp_path):
    """With fewer unannotated pins than annotated, the script lists those instead; the
    annotated census pins are then the ones the listing does not name.
    """
    unannotated = dedent("""\
        saif           16
        unannotated     2
        Unannotated pins:
         d2
         spare/A
        """)
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", unannotated),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    assert fields["activity_unused"]["examples"] == ["m", "um/Q", "uo/A", "uo/Y"]
    assert fields["activity_unused"]["count"] == 4
    assert fields["activity_annotated_clock"]["count"] == 3


def test_an_empty_unannotated_listing_means_every_pin_was_annotated(tmp_path):
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", "saif 9\nunannotated 0\nUnannotated pins:\n"),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    # `spare/A` and `d2` included: with nothing unannotated, every census pin was annotated.
    assert fields["activity_unused"]["count"] == 6
    assert fields["activity_unused"]["by_origin"] == {"input": 1, "unknown": 5}


def test_another_sources_annotations_are_not_taken_from_an_unannotated_listing(
    tmp_path,
):
    """The unannotated listing's complement includes pins the SDC annotated; with another
    source in the counts, the join is skipped rather than miscounted.
    """
    fields = power_activity.audit_activity(
        _write(
            tmp_path, "ann.rpt", "saif 15\ninput 1\nunannotated 0\nUnannotated pins:\n"
        ),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    assert set(fields) == {"activity_annotation", "activity_origins"}


def test_the_unannotated_listing_needs_every_annotation_to_be_the_traces():
    tcl = "\n".join(
        power_activity.annotation_tcl(
            "a.rpt", "a.tmp", list_pins=True, trace_origin="vcd"
        )
    )
    assert 'if {$rb_k eq "vcd"} {set rb_trace $rb_n}' in tcl
    assert "$rb_trace == $rb_ann" in tcl


def test_examples_are_capped_and_counts_are_not(tmp_path):
    census = power_activity.CENSUS_HEADER + "\n"
    census += "".join(f"pin unknown 0 0 u{i:02d}/Y\n" for i in range(12))
    census += "origin unknown 12\n"
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", "vcd 12\nunannotated 0\nUnannotated pins:\n"),
        _write(tmp_path, "census.rpt", census),
        "vcd",
        examples=3,
    )
    assert fields["activity_unused"]["count"] == 12
    assert fields["activity_unused"]["examples"] == ["u00/Y", "u01/Y", "u02/Y"]


def test_a_pin_annotated_by_another_source_is_not_the_traces(tmp_path):
    """In an annotated listing only the trace's own origin counts."""
    fields = power_activity.audit_activity(
        _write(
            tmp_path,
            "ann.rpt",
            "saif 1\ninput 1\nunannotated 0\nAnnotated pins:\n input m\n saif uo/A\n",
        ),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    assert fields["activity_unused"]["examples"] == ["uo/A"]


@pytest.mark.parametrize(
    "census",
    [
        pytest.param(None, id="opted-out"),
        pytest.param("not a census\norigin saif 3\n", id="foreign-header"),
        pytest.param(
            power_activity.CENSUS_HEADER + "\norigin  9\n", id="no-origin-field"
        ),
        pytest.param(
            power_activity.CENSUS_HEADER + "\npin unknown x y a/B\norigin unknown 1\n",
            id="bad-number",
        ),
    ],
)
def test_without_a_usable_census_only_the_counts_are_reported(tmp_path, census):
    """An OpenSTA whose `activity` property has no origin field cannot be audited."""
    census_path = _write(tmp_path, "census.rpt", census) if census else None
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", _ANNOTATED_RPT), census_path, "saif"
    )
    assert fields == {"activity_annotation": {"saif": 16, "unannotated": 2}}


def test_counts_without_a_listing_give_origins_but_no_join(tmp_path):
    fields = power_activity.audit_activity(
        _write(tmp_path, "ann.rpt", "saif 16\nunannotated 2\n"),
        _write(tmp_path, "census.rpt", _CENSUS),
        "saif",
    )
    assert set(fields) == {"activity_annotation", "activity_origins"}


def test_no_annotation_report_gives_no_fields(tmp_path):
    assert (
        power_activity.audit_activity(str(tmp_path / "missing.rpt"), None, "saif") == {}
    )


def test_the_warning_names_count_origins_and_examples():
    text = _human_message(
        "power.trace_activity_unused",
        {
            "power": "p",
            "trace": "saif",
            "count": 7,
            "annotated": 16,
            "by_origin": {"unknown": 7},
            "examples": ["um/Q", "uo/A"],
            "report": "activity_census.rpt",
        },
    )
    assert "7 of the 16 pins the SAIF annotated" in text
    assert "(unknown: 7)" in text
    assert "um/Q, uo/A, +5 more" in text


# --- the generated script ---------------------------------------------------------------


def _backend(tmp_path, *, mode="dynamic", saif=True, vcd=False, origin_census=True):
    netlist = tmp_path / "synth_netlist.v"
    netlist.write_text("module top(); endmodule\n")
    sdc = tmp_path / "constraints.sdc"
    sdc.write_text("create_clock -period 10 [get_ports clk]\n")
    trace = tmp_path / ("dump.saif" if saif else "dump.vcd")
    trace.write_text("(SAIFILE)\n")
    cfg = PowerConfig(
        name="demo_power",
        desc="demo",
        tool="openroad",
        mode=mode,
        netlist_source="synth",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        pnr_name=None,
        pnr_suite_path=None,
        constraints=str(sdc),
        platform="typ",
        activity=PowerActivity(
            saif=str(trace) if saif else None,
            vcd=str(trace) if vcd else None,
            scope="top",
            default_toggle_rate=0.1,
            default_static_prob=0.5,
            origin_census=origin_census,
        ),
        _reglvl=None,
        tool_overrides=None,
    )
    backend = OpenRoadPower(
        name="demo/openroad",
        power_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    backend._resolve_inputs = lambda: {
        "netlist": str(netlist),
        "odb": None,
        "sdc": str(sdc),
        "top": "top",
    }
    backend._resolve_platform = lambda: _FakePlatform()
    return backend


def _script(backend) -> str:
    return Path(backend._write_script()).read_text()


@pytest.mark.parametrize(
    "kwargs, origin",
    [
        pytest.param({"saif": True}, "saif", id="saif"),
        pytest.param({"saif": False, "vcd": True}, "vcd", id="vcd"),
    ],
)
def test_a_trace_run_audits_after_the_by_product_marker(tmp_path, kwargs, origin):
    """Both blocks follow `_DETAIL_MARKER`, so an error in them cannot fail the run, and
    publish by rename from staging names.
    """
    backend = _backend(tmp_path, **kwargs)
    script = _script(backend)
    marker = script.index(OpenRoadPower._DETAIL_MARKER)
    annotation = backend._activity_annotation_path()
    census = backend._activity_census_path()
    assert script.index("report_activity_annotation") > marker
    assert script.index("get_property $rb_pin activity") > marker
    assert f'$rb_org ne "{origin}"' in script
    assert f"file rename -force {{{annotation}.tmp}} {{{annotation}}}" in script
    assert f"file rename -force {{{census}.tmp}} {{{census}}}" in script
    assert script.index("get_property $rb_pin activity") < script.index("\nexit")


def test_the_annotation_block_lists_the_shorter_side(tmp_path):
    script = _script(_backend(tmp_path))
    assert "report_activity_annotation -report_unannotated" in script
    assert "report_activity_annotation -report_annotated" in script
    assert "$rb_unann < $rb_ann" in script


def test_origin_census_off_keeps_only_the_counts(tmp_path):
    script = _script(_backend(tmp_path, origin_census=False))
    assert "report_activity_annotation >" in script
    assert "-report_annotated" not in script
    assert "-report_unannotated" not in script
    assert "get_property $rb_pin activity" not in script


@pytest.mark.parametrize(
    "mode, saif",
    [
        pytest.param("static", True, id="static"),
        pytest.param("dynamic", False, id="synthetic"),
    ],
)
def test_a_run_without_a_trace_has_nothing_to_audit(tmp_path, mode, saif):
    script = _script(_backend(tmp_path, mode=mode, saif=saif))
    assert "report_activity_annotation" not in script
    assert "rb_origin" not in script


def test_origin_census_loads_from_power_yaml(tmp_path):
    (tmp_path / "power.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: census_on
                desc: d
                mode: dynamic
                synth: s
                synth-path: synth.yaml
                constraints: c.sdc
                platform: typ
                activity: {saif: dump.saif}
              - name: census_off
                desc: d
                mode: dynamic
                synth: s
                synth-path: synth.yaml
                constraints: c.sdc
                platform: typ
                activity: {saif: dump.saif, origin-census: false}
            """)
    )
    suite = PowerSuiteConfig(str(tmp_path / "power.yaml"))
    [on] = suite.get_runs("census_on")
    [off] = suite.get_runs("census_off")
    assert on.get_activity().origin_census is True
    assert off.get_activity().origin_census is False


# --- the run ----------------------------------------------------------------------------


def _run(tmp_path, monkeypatch, *, annotation=None, census=None, **kwargs):
    """Run a backend whose fake OpenROAD writes the given audit files."""
    backend = _backend(tmp_path, **kwargs)
    events = []
    real = power_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(power_openroad, "log_event", _record)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        if annotation is not None:
            Path(backend._activity_annotation_path()).write_text(annotation)
        if census is not None:
            Path(backend._activity_census_path()).write_text(census)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    return backend, backend.run(), events


def _named(events, name):
    return [(level, fields) for level, event, fields in events if event == name]


def test_unused_annotation_warns_and_lands_in_the_results(tmp_path, monkeypatch):
    import logging

    _backend_, result, events = _run(
        tmp_path, monkeypatch, annotation=_ANNOTATED_RPT, census=_CENSUS
    )
    assert isinstance(result, PowerPassResults)
    assert result.results["activity_unused"]["count"] == 4
    assert result.results["activity_annotated_clock"]["count"] == 3
    assert result.results["activity_annotation"] == {"saif": 16, "unannotated": 2}
    [(level, fields)] = _named(events, "power.trace_activity_unused")
    assert level == logging.WARNING
    assert fields["count"] == 4
    assert fields["annotated"] == 16
    assert fields["examples"] == ["m", "um/Q", "uo/A", "uo/Y"]
    assert _named(events, "power.activity_annotation")
    # `--machine` carries all four fields.
    from rtl_buddy.rtl_buddy import RtlBuddy

    row = RtlBuddy._power_result_row(
        None, {"power_name": "demo_power", "results": result}
    )
    for key in (
        "activity_annotation",
        "activity_origins",
        "activity_unused",
        "activity_annotated_clock",
    ):
        assert row[key] == result.results[key]


def test_a_fully_used_trace_does_not_warn(tmp_path, monkeypatch):
    census = power_activity.CENSUS_HEADER + "\norigin saif 16\n"
    _b, result, events = _run(
        tmp_path,
        monkeypatch,
        annotation="saif 16\nunannotated 0\nUnannotated pins:\n",
        census=census,
    )
    assert result.results["activity_unused"]["count"] == 0
    assert result.results["activity_origins"] == {"saif": 16}
    assert not _named(events, "power.trace_activity_unused")


def test_a_missing_audit_costs_only_the_fields(tmp_path, monkeypatch):
    """An OpenSTA without `report_activity_annotation` writes neither file; the run passes."""
    _b, result, events = _run(tmp_path, monkeypatch)
    assert isinstance(result, PowerPassResults)
    assert not any(k.startswith("activity_a") for k in result.results)
    assert "activity_unused" not in result.results
    assert not _named(events, "power.trace_activity_unused")


def test_a_previous_runs_audit_does_not_survive_the_clear(tmp_path):
    backend = _backend(tmp_path)
    paths = [
        backend._activity_annotation_path(),
        backend._activity_census_path(),
        backend._staging_path(backend._activity_annotation_path()),
        backend._staging_path(backend._activity_census_path()),
    ]
    for path in paths:
        Path(path).write_text("stale\n")
    assert backend._clear_stale_report() is None
    assert [p for p in paths if Path(p).exists()] == []


# --- real OpenROAD ----------------------------------------------------------------------

# A buffer, a flop with an `ff` group, and a macro with a clock-to-output arc and a `memory`
# group but no `ff`: the macro's output and the pins downstream of it are what OpenSTA's
# activity search never reaches.
_LIB = dedent("""\
    library (rb_activity_test) {
      delay_model : table_lookup ;
      time_unit : "1ns" ; voltage_unit : "1V" ; current_unit : "1mA" ;
      pulling_resistance_unit : "1kohm" ; capacitive_load_unit (1, pf) ;
      leakage_power_unit : "1nW" ; nom_voltage : 1.0 ;
      nom_process : 1 ; nom_temperature : 25 ;
      operating_conditions (tt) { process : 1 ; temperature : 25 ; voltage : 1 ; }
      default_operating_conditions : tt ;
      cell (BUF) {
        area : 1 ;
        pin (A) { direction : input ; capacitance : 0.01 ; }
        pin (Y) { direction : output ; function : "A" ;
          timing () { related_pin : "A" ; timing_sense : positive_unate ;
            cell_rise (scalar) { values ("0.1") ; } cell_fall (scalar) { values ("0.1") ; }
            rise_transition (scalar) { values ("0.1") ; }
            fall_transition (scalar) { values ("0.1") ; } } }
      }
      cell (DFF) {
        area : 2 ;
        ff (IQ, IQN) { clocked_on : "CK" ; next_state : "D" ; }
        pin (D) { direction : input ; capacitance : 0.01 ;
          timing () { related_pin : "CK" ; timing_type : setup_rising ;
            rise_constraint (scalar) { values ("0.05") ; }
            fall_constraint (scalar) { values ("0.05") ; } } }
        pin (CK) { direction : input ; capacitance : 0.01 ; clock : true ; }
        pin (Q) { direction : output ; function : "IQ" ;
          timing () { related_pin : "CK" ; timing_type : rising_edge ;
            cell_rise (scalar) { values ("0.1") ; } cell_fall (scalar) { values ("0.1") ; }
            rise_transition (scalar) { values ("0.1") ; }
            fall_transition (scalar) { values ("0.1") ; } } }
      }
      cell (MEM) {
        area : 10 ;
        memory () { type : ram ; address_width : 1 ; word_width : 1 ; }
        pin (CK) { direction : input ; capacitance : 0.01 ; clock : true ; }
        pin (D) { direction : input ; capacitance : 0.01 ;
          timing () { related_pin : "CK" ; timing_type : setup_rising ;
            rise_constraint (scalar) { values ("0.05") ; }
            fall_constraint (scalar) { values ("0.05") ; } } }
        pin (Q) { direction : output ;
          timing () { related_pin : "CK" ; timing_type : rising_edge ;
            cell_rise (scalar) { values ("0.1") ; } cell_fall (scalar) { values ("0.1") ; }
            rise_transition (scalar) { values ("0.1") ; }
            fall_transition (scalar) { values ("0.1") ; } } }
      }
    }
    """)

_LEF_PIN = "PIN {n} DIRECTION {d} ; PORT LAYER M1 ; RECT {x} 0 {x2} 0.1 ; END END {n}\n"


def _lef_macro(name, cls, pins):
    body = "".join(
        _LEF_PIN.format(n=n, d=d, x=i * 0.2, x2=i * 0.2 + 0.1)
        for i, (n, d) in enumerate(pins)
    )
    return (
        f"MACRO {name} CLASS {cls} ; SIZE 1 BY 1 ; SYMMETRY X Y ;\n{body}END {name}\n"
    )


_LEF = (
    "VERSION 5.8 ;\nUNITS DATABASE MICRONS 1000 ; END UNITS\n"
    "LAYER M1 TYPE ROUTING ; DIRECTION HORIZONTAL ; PITCH 0.2 ; WIDTH 0.1 ; END M1\n"
    + _lef_macro("BUF", "CORE", [("A", "INPUT"), ("Y", "OUTPUT")])
    + _lef_macro("DFF", "CORE", [("D", "INPUT"), ("CK", "INPUT"), ("Q", "OUTPUT")])
    + _lef_macro("MEM", "BLOCK", [("D", "INPUT"), ("CK", "INPUT"), ("Q", "OUTPUT")])
    + "END LIBRARY\n"
)

_NETLIST = dedent("""\
    module top (input clk, input d, output q, output m);
      wire n1, n2, n3;
      BUF ub (.A(d), .Y(n1));
      DFF uf (.CK(clk), .D(n1), .Q(n2));
      MEM um (.CK(clk), .D(n2), .Q(n3));
      BUF uo (.A(n3), .Y(m));
      BUF uq (.A(n2), .Y(q));
    endmodule
    """)

_R = "(T0 50) (T1 50) (TX 0)"
# Every one of the 16 pins annotated: four ports and twelve cell pins.
_SAIF = dedent(f"""\
    (SAIFILE (SAIFVERSION "2.0") (DIRECTION "backward") (DESIGN "top") (DIVIDER /)
    (TIMESCALE 1 ns) (DURATION 100)
    (INSTANCE top
     (NET (clk {_R} (TC 20)) (d {_R} (TC 4)) (q {_R} (TC 2)) (m {_R} (TC 6)))
     (INSTANCE ub (NET (A {_R} (TC 4)) (Y {_R} (TC 4))))
     (INSTANCE uf (NET (CK {_R} (TC 20)) (D {_R} (TC 4)) (Q {_R} (TC 2))))
     (INSTANCE um (NET (CK {_R} (TC 20)) (D {_R} (TC 2)) (Q {_R} (TC 6))))
     (INSTANCE uo (NET (A {_R} (TC 6)) (Y {_R} (TC 6))))
     (INSTANCE uq (NET (A {_R} (TC 2)) (Y {_R} (TC 2))))
    ))
    """)


_SDC = (
    "create_clock -name clk -period 10 [get_ports clk]\n"
    "set_input_delay 0 -clock clk [get_ports d]\n"
    "set_load 1 [all_outputs]\n"
)


def _real_run(tmp_path, *, saif=_SAIF, sdc=_SDC, corners=None):
    """Run the backend on real OpenROAD over the three-cell library above.

    ``corners`` maps corner -> Liberty text, primary first, for a multi-corner platform.
    """
    (tmp_path / "cells.lib").write_text(_LIB)
    (tmp_path / "cells.lef").write_text(_LEF)
    backend = _backend(tmp_path)
    Path(backend.power_cfg.activity.saif).write_text(saif)
    netlist = tmp_path / "top.v"
    netlist.write_text(_NETLIST)
    sdc_path = tmp_path / "top.sdc"
    sdc_path.write_text(sdc)
    backend._resolve_inputs = lambda: {
        "netlist": str(netlist),
        "odb": None,
        "sdc": str(sdc_path),
        "top": "top",
    }
    corner_paths = None
    if corners:
        corner_paths = {}
        for name, text in corners.items():
            path = tmp_path / f"cells_{name}.lib"
            path.write_text(text)
            corner_paths[name] = str(path)
    backend._resolve_platform = lambda: _FakePlatform(
        liberty=str(tmp_path / "cells.lib"),
        pdk=_FakePdk(str(tmp_path / "cells.lef")),
        corners=corner_paths,
    )
    return backend, backend.run()


_EXPECTED_ORIGINS = {"clock": 3, "saif": 9, "unknown": 4}
_EXPECTED_UNUSED = {
    "count": 4,
    "annotated": 16,
    "by_origin": {"unknown": 4},
    "examples": ["m", "um/Q", "uo/A", "uo/Y"],
}
_EXPECTED_CLOCK = {"count": 3, "examples": ["clk", "uf/CK", "um/CK"]}


@pytest.mark.skipif(OPENROAD is None, reason="openroad not installed")
def test_real_openroad_reports_the_macro_outputs_the_saif_did_not_drive(tmp_path):
    """The SAIF annotates all 16 pins, and `report_power` uses 9 of them: the three clock
    pins take the SDC clock, the macro output and the three pins downstream keep activity 0.
    """
    _backend_, result = _real_run(tmp_path)

    assert isinstance(result, PowerPassResults), result.results
    assert result.results["activity_annotation"] == {"saif": 16, "unannotated": 0}
    assert result.results["activity_origins"] == _EXPECTED_ORIGINS
    assert result.results["activity_unused"] == _EXPECTED_UNUSED
    assert result.results["activity_annotated_clock"] == _EXPECTED_CLOCK


@pytest.mark.skipif(OPENROAD is None, reason="openroad not installed")
def test_real_openroad_does_not_count_an_sdc_annotation_as_the_traces(tmp_path):
    """The SAIF leaves out the macro output and the SDC sets its activity instead. With
    nothing unannotated the script would list the (empty) unannotated pins, whose complement
    includes the SDC's pin; it lists the annotated pins by source instead, so only the
    SAIF's own unused pins are reported.
    """
    saif = _SAIF.replace(" (Q {_R} (TC 6))".format(_R=_R), "")
    assert saif != _SAIF
    sdc = _SDC + "set_power_activity -pins [get_pins um/Q] -activity 0.1 -duty 0.5\n"
    backend, result = _real_run(tmp_path, saif=saif, sdc=sdc)

    assert isinstance(result, PowerPassResults), result.results
    assert result.results["activity_annotation"] == {
        "saif": 15,
        "input": 1,
        "unannotated": 0,
    }
    assert "Annotated pins:" in Path(backend._activity_annotation_path()).read_text()
    assert result.results["activity_unused"] == {
        "count": 3,
        "annotated": 15,
        "by_origin": {"unknown": 3},
        "examples": ["m", "uo/A", "uo/Y"],
    }


@pytest.mark.skipif(OPENROAD is None, reason="openroad not installed")
def test_real_openroad_audits_the_worst_corner_of_a_multi_corner_run(tmp_path):
    """The second corner's higher supply makes it the worst; the census selects it as the
    command corner and finds the same activities, since the corners share one SDC mode.
    """
    hot = _LIB.replace("nom_voltage : 1.0", "nom_voltage : 1.5").replace(
        "voltage : 1 ;", "voltage : 1.5 ;"
    )
    assert hot != _LIB
    backend, result = _real_run(tmp_path, corners={"cool": _LIB, "hot": hot})

    assert isinstance(result, PowerPassResults), result.results
    assert result.results["worst_corner"] == "hot"
    script = Path(backend._script_path()).read_text()
    assert "sta::set_cmd_scene $rb_scene" in script
    assert script.index("rb_power_corner") < script.index("set_cmd_scene")
    assert result.results["activity_origins"] == _EXPECTED_ORIGINS
    assert result.results["activity_unused"] == _EXPECTED_UNUSED
    assert result.results["activity_annotated_clock"] == _EXPECTED_CLOCK
