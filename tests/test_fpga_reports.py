"""Tests for the Vivado batch-Tcl template and report parsers (#284).

The fixture ``.rpt`` files under ``tests/fixtures/fpga/`` are real,
sanitized Vivado 2022.1.2 reports from a routed run of a small
counter/multiplier/BRAM design on part ``xczu7ev-ffvc1156-2-e`` (10 ns
clock for the passing set, an over-constrained 0.05 ns clock for the
failing timing summary). They are the parser contract — no Vivado
install is needed to run these tests.
"""

from pathlib import Path

import pytest

from rtl_buddy.tools import fpga_vivado_flow as flow
from rtl_buddy.tools.fpga_vivado_reports import (
    parse_drc,
    parse_methodology,
    parse_power,
    parse_timing_summary,
    parse_utilization,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fpga"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# parse_utilization
# ---------------------------------------------------------------------------


def test_parse_utilization_canonical_resources():
    util = parse_utilization(_fixture("util.rpt"))

    # Headline CLB Logic numbers (UltraScale+ "CLB LUTs"/"CLB Registers").
    assert util["lut"] == {
        "used": 1,
        "fixed": 0,
        "available": 230400,
        "util_pct": 0.01,  # printed as "<0.01"
    }
    assert util["ff"] == {
        "used": 16,
        "fixed": 0,
        "available": 460800,
        "util_pct": 0.01,
    }
    # Half a Block RAM tile (one RAMB18) — fractional "Used" is preserved.
    assert util["bram"] == {
        "used": 0.5,
        "fixed": 0,
        "available": 312,
        "util_pct": 0.16,
    }
    assert util["dsp"] == {
        "used": 1,
        "fixed": 0,
        "available": 1728,
        "util_pct": 0.06,
    }


def test_parse_utilization_carries_all_site_type_rows():
    util = parse_utilization(_fixture("util.rpt"))
    resources = util["resources"]

    # Rows beyond the canonical four are captured too.
    assert resources["Bonded IOB"] == {
        "used": 109,
        "fixed": 0,
        "available": 360,
        "util_pct": 30.28,
    }
    assert resources["CARRY8"]["used"] == 2
    assert resources["BUFGCE"]["used"] == 1
    # Nested breakdown rows with blank Available cells parse as None.
    assert resources["RAMB18E2 only"]["used"] == 1
    assert resources["RAMB18E2 only"]["available"] is None
    # First occurrence wins for repeated site types: "LUT as Logic"
    # appears in both the CLB Logic and CLB Logic Distribution tables
    # with identical headline numbers.
    assert resources["LUT as Logic"]["available"] == 230400


def test_parse_utilization_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado utilization report"):
        parse_utilization("ERROR: nothing to see here\n")


# ---------------------------------------------------------------------------
# parse_timing_summary
# ---------------------------------------------------------------------------


def test_parse_timing_summary_pass():
    timing = parse_timing_summary(_fixture("timing_summary.rpt"))

    assert timing["wns_ns"] == 8.452
    assert timing["tns_ns"] == 0.0
    assert timing["tns_failing_endpoints"] == 0
    assert timing["tns_total_endpoints"] == 64
    assert timing["whs_ns"] == 0.059
    assert timing["ths_ns"] == 0.0
    assert timing["ths_failing_endpoints"] == 0
    assert timing["ths_total_endpoints"] == 64
    assert timing["wpws_ns"] == 4.458
    assert timing["tpws_ns"] == 0.0
    assert timing["timing_met"] is True


def test_parse_timing_summary_pass_has_no_failing_paths():
    timing = parse_timing_summary(_fixture("timing_summary.rpt"))
    assert timing["failing_endpoints"] == 0
    assert timing["failing_paths"] == []


def test_parse_timing_summary_per_clock_rows():
    timing = parse_timing_summary(_fixture("timing_summary.rpt"))
    assert len(timing["clocks"]) == 1
    clk = timing["clocks"][0]
    assert clk["clock"] == "clk"
    assert clk["wns_ns"] == 8.452
    assert clk["whs_ns"] == 0.059
    assert clk["tpws_total_endpoints"] == 19


def test_parse_timing_summary_failing():
    timing = parse_timing_summary(_fixture("timing_summary_fail.rpt"))

    # Over-constrained 0.05 ns (20 GHz) clock: negative setup slack.
    assert timing["wns_ns"] == -0.882
    assert timing["wns_ns"] < 0
    assert timing["tns_ns"] == -81.047
    assert timing["tns_failing_endpoints"] == 101
    assert timing["tns_total_endpoints"] == 101
    # Hold is still met; pulse width is not.
    assert timing["whs_ns"] == 0.055
    assert timing["wpws_ns"] == -1.519
    assert timing["tpws_failing_endpoints"] == 18
    assert timing["timing_met"] is False

    clk = timing["clocks"][0]
    assert clk["clock"] == "clk"
    assert clk["wns_ns"] == -0.882


def test_parse_timing_summary_failing_carries_loop_fields():
    """The timing-closure loop fields (#288): endpoint count + worst paths."""
    timing = parse_timing_summary(_fixture("timing_summary_fail.rpt"))

    # Setup (101) + hold (0) endpoints with negative slack.
    assert timing["failing_endpoints"] == 101

    # Only the VIOLATED block surfaces; the MET hold path does not.
    assert len(timing["failing_paths"]) == 1
    path = timing["failing_paths"][0]
    assert path["slack_ns"] == -0.882
    assert path["met"] is False
    assert path["source"] == "product_reg/DSP_A_B_DATA_INST/CLK"
    assert path["destination"] == "product_reg/DSP_M_DATA_INST/V[0]"
    assert path["path_group"] == "clk"
    assert path["path_type"] == "Setup"
    assert path["requirement_ns"] == 0.05
    assert path["data_path_delay_ns"] == 0.894
    assert path["logic_levels"] == 2


def test_parse_timing_summary_fallback_verdict_without_vivado_line():
    # No explicit verdict line: derived from WNS/WHS signs.
    text = (
        "| Design Timing Summary\n"
        "    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints"
        "      WHS(ns)      THS(ns)\n"
        "    -------      -------  ---------------------  -------------------"
        "      -------      -------\n"
        "     -1.500       -3.000                      2                   10"
        "        0.100        0.000\n"
    )
    timing = parse_timing_summary(text)
    assert timing["wns_ns"] == -1.5
    assert timing["whs_ns"] == 0.1
    assert timing["timing_met"] is False
    assert timing["clocks"] == []
    # Loop fields degrade gracefully: setup endpoints only (no hold
    # column in the truncated row), no Timing Details section at all.
    assert timing["failing_endpoints"] == 2
    assert timing["failing_paths"] == []


def test_parse_timing_summary_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado timing summary report"):
        parse_timing_summary("once upon a midnight dreary\n")


# ---------------------------------------------------------------------------
# parse_power
# ---------------------------------------------------------------------------


def test_parse_power_summary_values():
    power = parse_power(_fixture("power.rpt"))
    assert power == {
        "total_on_chip_w": 0.636,
        "dynamic_w": 0.044,
        "static_w": 0.592,
        "junction_temp_c": 25.6,
        "confidence_level": "Low",
    }


def test_parse_power_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado power report"):
        parse_power("watts? what watts?\n")


# ---------------------------------------------------------------------------
# parse_drc
# ---------------------------------------------------------------------------


def test_parse_drc_counts_and_violations():
    drc = parse_drc(_fixture("drc.rpt"))

    assert drc["total_violations"] == 3
    assert drc["by_severity"] == {"Critical Warning": 2, "Warning": 1}

    by_id = {v["id"]: v for v in drc["violations"]}
    assert set(by_id) == {"NSTD-1#1", "UCIO-1#1", "DPOP-4#1"}
    assert by_id["NSTD-1#1"]["severity"] == "Critical Warning"
    assert by_id["NSTD-1#1"]["description"] == "Unspecified I/O Standard"
    assert by_id["UCIO-1#1"]["description"] == "Unconstrained Logical Port"
    assert by_id["DPOP-4#1"]["severity"] == "Warning"
    assert by_id["DPOP-4#1"]["description"] == "MREG Output pipelining"


def test_parse_drc_clean_report():
    text = (
        "Report DRC\n\n"
        "1. REPORT SUMMARY\n"
        "-----------------\n"
        "             Violations found: 0\n"
    )
    drc = parse_drc(text)
    assert drc["total_violations"] == 0
    assert drc["by_severity"] == {}
    assert drc["violations"] == []


def test_parse_drc_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado DRC report"):
        parse_drc("no rules were harmed\n")


# ---------------------------------------------------------------------------
# parse_methodology
# ---------------------------------------------------------------------------


def test_parse_methodology_counts_and_warnings():
    meth = parse_methodology(_fixture("methodology.rpt"))

    # The fixture design constrains the clock but no I/O delays, so every
    # port flags TIMING-18.
    assert meth["total_warnings"] == 49
    assert meth["by_severity"] == {"Warning": 49}
    assert len(meth["warnings"]) == 49

    first = meth["warnings"][0]
    assert first["id"] == "TIMING-18#1"
    assert first["severity"] == "Warning"
    assert first["description"] == "Missing input or output delay"
    # Vendor rule ids are surfaced verbatim, one entry per instance.
    assert {w["id"] for w in meth["warnings"]} == {
        f"TIMING-18#{n}" for n in range(1, 50)
    }


def test_parse_methodology_clean_report():
    text = (
        "Report Methodology\n\n"
        "1. REPORT SUMMARY\n"
        "-----------------\n"
        "             Violations found: 0\n"
    )
    meth = parse_methodology(text)
    assert meth["total_warnings"] == 0
    assert meth["by_severity"] == {}
    assert meth["warnings"] == []


def test_parse_methodology_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado methodology report"):
        parse_methodology("all according to plan\n")
    # A DRC report is not a methodology report (and vice versa).
    with pytest.raises(ValueError, match="not a Vivado methodology report"):
        parse_methodology(_fixture("drc.rpt"))


# ---------------------------------------------------------------------------
# Flow Tcl template
# ---------------------------------------------------------------------------


def test_flow_template_stage_and_report_contract():
    # The data tables P1 builds on: stage order and the report set keyed
    # the same way as the parser names / fixture files.
    assert [stage for stage, _ in flow.FLOW_STAGES] == [
        "synth",
        "opt",
        "place",
        "route",
    ]
    assert flow.REPORT_FILES == {
        "utilization": "util.rpt",
        "timing_summary": "timing_summary.rpt",
        "power": "power.rpt",
        "drc": "drc.rpt",
        "methodology": "methodology.rpt",
    }


def test_render_flow_tcl_full_script():
    script = flow.render_flow_tcl(
        top="fpga_counter",
        part="xczu7ev-ffvc1156-2-e",
        verilog_sources=["counter.v", "alu.sv"],
        xdc_files=["counter.xdc"],
    )

    assert "read_verilog counter.v" in script
    assert "read_verilog -sv alu.sv" in script
    assert "read_xdc counter.xdc" in script
    assert "synth_design -top fpga_counter -part xczu7ev-ffvc1156-2-e" in script
    assert "report_utilization -file util.rpt" in script
    assert "report_timing_summary -file timing_summary.rpt" in script
    assert "report_power -file power.rpt" in script
    assert "report_drc -file drc.rpt" in script
    assert "report_methodology -file methodology.rpt" in script
    assert "write_bitstream -force fpga_counter.bit" in script
    # No leftover placeholders.
    assert "{{" not in script

    # Stage order is preserved: synth -> opt -> place -> route ->
    # reports -> bitstream.
    positions = [
        script.index("synth_design"),
        script.index("opt_design"),
        script.index("place_design"),
        script.index("route_design"),
        script.index("report_utilization"),
        script.index("write_bitstream"),
    ]
    assert positions == sorted(positions)


def test_render_flow_tcl_vhdl_and_no_xdc():
    script = flow.render_flow_tcl(
        top="top",
        part="xczu7ev-ffvc1156-2-e",
        verilog_sources=["a.vhd", "b.v"],
        xdc_files=[],
        bitstream="out.bit",
    )
    assert "read_vhdl a.vhd" in script
    assert "read_verilog b.v" in script
    assert "# (no XDC constraints provided)" in script
    assert "write_bitstream -force out.bit" in script


def test_render_flow_tcl_report_override():
    script = flow.render_flow_tcl(
        top="top",
        part="xczu7ev-ffvc1156-2-e",
        verilog_sources=["a.v"],
        xdc_files=[],
        report_files={"timing_summary": "custom_timing.rpt"},
    )
    assert "report_timing_summary -file custom_timing.rpt" in script
    assert "report_power" not in script


def test_render_flow_tcl_validates_inputs():
    common = dict(part="p", verilog_sources=["a.v"], xdc_files=[])
    with pytest.raises(RuntimeError, match="top module name is required"):
        flow.render_flow_tcl(top="", **common)
    with pytest.raises(RuntimeError, match="part name is required"):
        flow.render_flow_tcl(top="t", part="", verilog_sources=["a.v"], xdc_files=[])
    with pytest.raises(RuntimeError, match="at least one HDL source"):
        flow.render_flow_tcl(top="t", part="p", verilog_sources=[], xdc_files=[])
    with pytest.raises(RuntimeError, match="unknown report 'qor'"):
        flow.render_flow_tcl(
            top="t",
            part="p",
            verilog_sources=["a.v"],
            xdc_files=[],
            report_files={"qor": "qor.rpt"},
        )


def test_fixture_reports_match_template_report_set():
    """Every report the template emits has a fixture contract-testing it."""
    for filename in flow.REPORT_FILES.values():
        assert (FIXTURES / filename).is_file(), f"missing fixture {filename}"
