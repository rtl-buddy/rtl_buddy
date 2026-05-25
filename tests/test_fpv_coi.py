"""Tests for cone-of-influence coverage (#136)."""

from textwrap import dedent

from rtl_buddy.tools.fpv_coi import (
    build_yosys_script,
    compute_coverage,
    parse_stat_blocks,
)
from rtl_buddy.config.fpv import FpvConfig
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.rtl_buddy import RtlBuddy


# ---------------------------------------------------------------------------
# Yosys script generation
# ---------------------------------------------------------------------------


def test_build_yosys_script_emits_total_and_selected_markers():
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=[],
        properties=["props.sv"],
        constraints=None,
        top="dut",
    )
    assert "=== RTL_BUDDY_COI_TOTAL ===" in script
    assert "=== RTL_BUDDY_COI_SELECTED ===" in script
    # `prep -flatten -top` collapses the hierarchy so $assert cells
    # from bound property submodules show up under the top module's
    # default stat output.
    assert "prep -flatten -top dut" in script
    # COI selection lives in the standard yosys selection language.
    assert "select -set property_cells t:$assert" in script
    assert "@property_cells %ci*" in script


def test_build_yosys_script_inlines_constraints_between_design_and_props():
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=["/inc/foo"],
        properties=["props.sv"],
        constraints="env_assumes.sv",
        top="dut",
    )
    # Order must be: incdirs → sources → constraints → properties so the
    # constraints' assume property statements are in scope when the
    # property file's assertions elaborate.
    lines = script.splitlines()
    inc_i = next(
        i for i, ln in enumerate(lines) if ln == "verilog_defaults -add -I /inc/foo"
    )
    src_i = next(i for i, ln in enumerate(lines) if ln == "read -sv -formal dut.sv")
    cons_i = next(
        i for i, ln in enumerate(lines) if ln == "read -sv -formal env_assumes.sv"
    )
    prop_i = next(i for i, ln in enumerate(lines) if ln == "read -sv -formal props.sv")
    assert inc_i < src_i < cons_i < prop_i


# ---------------------------------------------------------------------------
# Yosys stat parsing
# ---------------------------------------------------------------------------


_FAKE_YOSYS_LOG = dedent("""\
    yosys> read ...
    ...

    === RTL_BUDDY_COI_TOTAL ===

    6. Printing statistics.

    === counter ===

            +----------Local Count, excluding submodules.
            |
           18 wires
           21 wire bits
           12 cells
            2   $add
            5   $dff
            3   $eq
            2   $not

    === clk_gate ===

            +----------Local Count, excluding submodules.
            |
            4 wires
            5 wire bits
            2 cells
            1   $dff
            1   $and

    === RTL_BUDDY_COI_SELECTED ===

    7. Printing statistics.

    === counter ===

            +----------Local Count, excluding submodules.
            |
            9 wires
            9 wire bits
            6 cells
            1   $add
            3   $dff
            2   $eq

    === RTL_BUDDY_ASSUMES_TOTAL ===

    8. Printing statistics.

    === counter ===

            +----------Local Count, excluding submodules.
            |
            0 wires
            0 wire bits
            5 cells
            5   $assume

    === RTL_BUDDY_ASSUMES_IN_COI ===

    9. Printing statistics.

    === counter ===

            +----------Local Count, excluding submodules.
            |
            0 wires
            0 wire bits
            3 cells
            3   $assume

""")


def test_parse_stat_blocks_extracts_total_and_selected_blocks():
    blocks = parse_stat_blocks(_FAKE_YOSYS_LOG)
    total = blocks["RTL_BUDDY_COI_TOTAL"]
    selected = blocks["RTL_BUDDY_COI_SELECTED"]
    assert total["counter"]["cells"] == 12
    assert total["clk_gate"]["cells"] == 2
    assert selected["counter"]["cells"] == 6
    # `clk_gate` had no asserts in its COI, so it's absent from the
    # selected block — caller treats that as 0/total_clk_gate.
    assert "clk_gate" not in selected


def test_compute_coverage_aggregates_modules():
    blocks = parse_stat_blocks(_FAKE_YOSYS_LOG)
    summary = compute_coverage(blocks)
    # 12 + 2 = 14 total cells, 6 in the COI of an assert.
    assert summary["total_cells"] == 14
    assert summary["coi_cells"] == 6
    assert summary["percent"] == 6 / 14 * 100
    assert summary["per_module"]["counter"]["cells"] == 12
    assert summary["per_module"]["counter"]["coi_cells"] == 6
    # No assert touched clk_gate.
    assert summary["per_module"]["clk_gate"]["coi_cells"] == 0


def test_compute_coverage_rolls_up_dead_assumes():
    blocks = parse_stat_blocks(_FAKE_YOSYS_LOG)
    summary = compute_coverage(blocks)
    assumes = summary["assumes"]
    # 5 total `$assume` cells, 3 inside the assertion COI → 2 dead.
    assert assumes["total"] == 5
    assert assumes["in_assert_coi"] == 3
    assert assumes["dead"] == 2


def test_compute_coverage_handles_empty_design():
    summary = compute_coverage(
        {"RTL_BUDDY_COI_TOTAL": {}, "RTL_BUDDY_COI_SELECTED": {}}
    )
    assert summary["total_cells"] == 0
    assert summary["coi_cells"] == 0
    assert summary["percent"] == 0.0


# ---------------------------------------------------------------------------
# FpvConfig.coi_enabled — default policy
# ---------------------------------------------------------------------------


def _make_cfg(*, mode="bmc", coi=None):
    model = ModelConfig(name="m", filelist=[], path="/fake/models.yaml")
    return FpvConfig(
        name="v",
        desc="d",
        model=model,
        tool="sby",
        top="m",
        properties=[],
        mode=mode,
        depth=10,
        engines=["smtbmc yices"],
        _reglvl=None,
        constraints=None,
        tool_overrides=None,
        vacuity=False,
        coi=coi,
    )


def test_coi_default_on():
    assert _make_cfg().coi_enabled() is True


def test_coi_explicit_off():
    assert _make_cfg(coi=False).coi_enabled() is False


def test_coi_explicit_on_for_cover_mode():
    assert _make_cfg(mode="cover", coi=True).coi_enabled() is True


# ---------------------------------------------------------------------------
# COI summary cell formatting
# ---------------------------------------------------------------------------


def test_format_coi_cell_renders_percent_and_counts():
    cell = RtlBuddy._format_coi_cell(
        {"total_cells": 100, "coi_cells": 73, "percent": 73.0}
    )
    assert cell == "73% (73/100)"


def test_format_coi_cell_handles_zero_total():
    cell = RtlBuddy._format_coi_cell({"total_cells": 0, "coi_cells": 0, "percent": 0.0})
    assert cell is None


def test_format_coi_cell_none_when_no_data():
    assert RtlBuddy._format_coi_cell(None) is None


# ---------------------------------------------------------------------------
# Dead-assume cell formatting (#135)
# ---------------------------------------------------------------------------


def test_format_assumes_cell_silent_when_no_design_assumes():
    # No `$assume` cells in the design — nothing to flag, column hidden.
    assert (
        RtlBuddy._format_assumes_cell(
            {"assumes": {"total": 0, "in_assert_coi": 0, "dead": 0}}
        )
        is None
    )


def test_format_assumes_cell_quiet_when_all_used():
    cell = RtlBuddy._format_assumes_cell(
        {"assumes": {"total": 4, "in_assert_coi": 4, "dead": 0}}
    )
    assert cell == "4 used"


def test_format_assumes_cell_loud_when_dead():
    cell = RtlBuddy._format_assumes_cell(
        {"assumes": {"total": 5, "in_assert_coi": 3, "dead": 2}}
    )
    assert cell == "3 used, 2 dead"


def test_format_assumes_cell_none_when_no_coi_data():
    assert RtlBuddy._format_assumes_cell(None) is None
    assert RtlBuddy._format_assumes_cell({}) is None


def test_build_yosys_script_emits_assume_markers():
    script = build_yosys_script(
        sources=["dut.sv"],
        incdirs=[],
        properties=["props.sv"],
        constraints=None,
        top="dut",
    )
    assert "=== RTL_BUDDY_ASSUMES_TOTAL ===" in script
    assert "=== RTL_BUDDY_ASSUMES_IN_COI ===" in script
    # The intersection selection lives in standard yosys language.
    assert "@all_assumes @property_coi %i" in script
