"""Tests for the auto-derived vacuity-cover pass (#134)."""

from pathlib import Path
from textwrap import dedent

from rtl_buddy.tools.fpv_vacuity import (
    extract_candidates,
    parse_vacuity_log,
    write_vacuity_module,
)
from rtl_buddy.config.fpv import FpvConfig
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.rtl_buddy import RtlBuddy


# ---------------------------------------------------------------------------
# extract_candidates — antecedent extraction from property files
# ---------------------------------------------------------------------------


def _make_props(tmp_path: Path, content: str, name: str = "props.sv") -> str:
    path = tmp_path / name
    path.write_text(dedent(content))
    return str(path)


def test_extract_skips_files_without_implications(tmp_path):
    p = _make_props(
        tmp_path,
        """
        // no implications here
        a_safe: assert property (@(posedge clk) signal == 1'b0);
        """,
    )
    assert extract_candidates([p]) == []


def test_extract_pulls_antecedent_for_basic_implication(tmp_path):
    p = _make_props(
        tmp_path,
        """
        my_prop: assert property (@(posedge clk) req |-> ack);
        """,
    )
    cands = extract_candidates([p])
    assert len(cands) == 1
    c = cands[0]
    assert c.label == "my_prop:"
    assert c.operator == "|->"
    assert c.antecedent == "req"
    assert c.clocking == "@(posedge clk)"


def test_extract_handles_disable_iff(tmp_path):
    p = _make_props(
        tmp_path,
        """
        my_prop: assert property (@(posedge clk) disable iff (rst) start |=> done);
        """,
    )
    cands = extract_candidates([p])
    assert len(cands) == 1
    c = cands[0]
    assert c.disable_iff == "disable iff (rst)"
    assert c.antecedent == "start"
    assert c.operator == "|=>"


def test_extract_handles_multiple_implications_in_file(tmp_path):
    p = _make_props(
        tmp_path,
        """
        p1: assert property (@(posedge clk) a |-> b);
        p2: assert property (@(posedge clk) c |=> d);
        p3: assert property (@(posedge clk) signal == 1);
        """,
    )
    cands = extract_candidates([p])
    assert len(cands) == 2
    assert [c.label for c in cands] == ["p1:", "p2:"]
    assert [c.operator for c in cands] == ["|->", "|=>"]


def test_extract_skips_missing_files(tmp_path):
    assert extract_candidates([str(tmp_path / "nope.sv")]) == []


# ---------------------------------------------------------------------------
# write_vacuity_module — synthesized SystemVerilog
# ---------------------------------------------------------------------------


def test_write_vacuity_module_emits_one_cover_per_candidate(tmp_path):
    p = _make_props(
        tmp_path,
        """
        p1: assert property (@(posedge clk) req |-> ack);
        p2: assert property (@(posedge clk) c |=> d);
        """,
    )
    cands = extract_candidates([p])
    out = tmp_path / "vacuity_covers.sv"
    write_vacuity_module(cands, str(out))
    text = out.read_text()
    # One cover per candidate, with the clocking preserved. Count the
    # ": cover property" prefix so the header comment is excluded.
    assert text.count(": cover property") == 2
    assert "@(posedge clk)" in text
    # The synthesized cover names embed the user's label so they can
    # be traced back to the original property.
    assert "cover_vacuity_1_p1" in text
    assert "cover_vacuity_2_p2" in text


def test_write_vacuity_module_handles_empty_candidate_list(tmp_path):
    out = tmp_path / "vacuity_covers.sv"
    write_vacuity_module([], str(out))
    text = out.read_text()
    # Always declares clk + rst_n as the canonical clocking ports, even
    # with zero candidates — so the module is syntactically valid.
    assert "module rtl_buddy_vacuity_covers (" in text
    assert "input logic clk" in text
    assert "input logic rst_n" in text
    assert "endmodule" in text
    # No declared covers when there's nothing to check (the header
    # comment intentionally mentions "cover property" — exclude it).
    assert ": cover property" not in text


def test_write_vacuity_module_emits_bind_when_requested(tmp_path):
    p = _make_props(
        tmp_path,
        """
        p_req: assert property (@(posedge clk) disable iff (!rst_n) req |-> ack);
        """,
    )
    cands = extract_candidates([p])
    out = tmp_path / "vacuity_covers.sv"
    write_vacuity_module(cands, str(out), bind_to="dut")
    text = out.read_text()
    # `req` was referenced in the antecedent — it becomes a port.
    assert "input logic req" in text
    # clk + rst_n always declared.
    assert "input logic clk" in text
    assert "input logic rst_n" in text
    # bind directive present so slang sees the cover module bound into
    # the DUT scope (slang doesn't infer free identifiers the way the
    # native verilog frontend does).
    assert "bind dut rtl_buddy_vacuity_covers" in text
    assert ".clk" in text and ".rst_n" in text and ".req" in text


# ---------------------------------------------------------------------------
# parse_vacuity_log — sby cover-mode output parsing
# ---------------------------------------------------------------------------


def test_parse_vacuity_log_marks_reached_covers():
    log = dedent("""
        SBY 13:01:00 [..] Reached cover statement at top.cov.cover_vacuity_1_p1 in step 3.
        SBY 13:01:00 [..] Reached cover statement at top.cov.cover_vacuity_2_p2 in step 5.
    """)
    assert parse_vacuity_log(log) == {
        "cover_vacuity_1_p1": True,
        "cover_vacuity_2_p2": True,
    }


def test_parse_vacuity_log_marks_unreached_covers():
    log = dedent("""
        SBY 13:01:00 [..] Reached cover statement at top.cov.cover_vacuity_1_p1 in step 3.
        SBY 13:01:00 [..] Unreached cover statement: top.cov.cover_vacuity_2_p2
    """)
    assert parse_vacuity_log(log) == {
        "cover_vacuity_1_p1": True,
        "cover_vacuity_2_p2": False,
    }


# ---------------------------------------------------------------------------
# FpvConfig.vacuity_enabled — default policy
# ---------------------------------------------------------------------------


def _make_cfg(mode: str, vacuity=None):
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
        vacuity=vacuity,
    )


def test_vacuity_default_on_for_bmc():
    assert _make_cfg("bmc").vacuity_enabled() is True


def test_vacuity_default_on_for_prove():
    assert _make_cfg("prove").vacuity_enabled() is True


def test_vacuity_default_off_for_cover():
    assert _make_cfg("cover").vacuity_enabled() is False


def test_vacuity_default_off_for_live():
    assert _make_cfg("live").vacuity_enabled() is False


def test_vacuity_explicit_override_wins():
    assert _make_cfg("bmc", vacuity=False).vacuity_enabled() is False
    assert _make_cfg("cover", vacuity=True).vacuity_enabled() is True


# ---------------------------------------------------------------------------
# Vacuity summary cell formatting
# ---------------------------------------------------------------------------


def test_format_vacuity_cell_silent_when_all_reachable():
    cell = RtlBuddy._format_vacuity_cell(
        {
            "candidates": 2,
            "vacuous": 0,
            "covers": [
                {"status": "reachable"},
                {"status": "reachable"},
            ],
        }
    )
    assert cell == "2 ok"


def test_format_vacuity_cell_loud_when_vacuous():
    cell = RtlBuddy._format_vacuity_cell(
        {
            "candidates": 3,
            "vacuous": 1,
            "covers": [
                {"status": "reachable"},
                {"status": "unreachable"},
                {"status": "reachable"},
            ],
        }
    )
    assert cell == "1/3 vacuous"


def test_format_vacuity_cell_reports_unknowns():
    cell = RtlBuddy._format_vacuity_cell(
        {
            "candidates": 2,
            "vacuous": 1,
            "covers": [
                {"status": "unreachable"},
                {"status": "unknown"},
            ],
        }
    )
    assert cell == "1/2 vacuous, 1 unknown"


def test_format_vacuity_cell_none_when_no_pass():
    assert RtlBuddy._format_vacuity_cell(None) is None
    assert RtlBuddy._format_vacuity_cell({"candidates": 0}) is None
