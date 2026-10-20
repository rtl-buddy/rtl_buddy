"""`icg_split` deals the loads of an over-full clock-gating cell across copies of it (#854)."""

import difflib
import shutil
import subprocess
import sys

import pytest

from rtl_buddy.tools import icg_split
from rtl_buddy.tools.synth_yosys import icg_split_cmds

ICG = "$auto$clockgate.cc:372:execute$10"
GCLK = "$auto$clockgate.cc:375:execute$11"

_LIB = """attribute \\blackbox 1
module \\CLKGATE_X1
  wire input 1 \\CK
  wire input 2 \\E
  wire output 3 \\GCK
end
attribute \\blackbox 1
module \\ICG2
  wire input 1 \\CK
  wire output 2 \\GCK
  wire output 3 \\Q
end
"""


def _design(loads, *, icg_type="CLKGATE_X1", extra_wires="", extra_cells=""):
    """One ICG driving ``loads`` flops through ``GCLK``, flop i named f"\\ff{i:03d}"."""
    out = (
        f"  wire width {max(loads, 1)} \\d\n  wire width {max(loads, 1)} \\q\n"
        f"  wire \\clk\n  wire \\en\n  wire {GCLK}\n{extra_wires}"
        '  attribute \\src "t.v:1"\n'
        f"  cell \\{icg_type} {ICG}\n"
        "    connect \\CK \\clk\n    connect \\E \\en\n"
        f"    connect \\GCK {GCLK}\n  end\n"
    )
    if icg_type == "ICG2":
        out = out.replace("    connect \\E \\en\n", "    connect \\Q \\en\n")
    for i in range(loads):
        out += (
            f"  cell $_DFF_P_ \\ff{i:03d}\n"
            f"    connect \\C {GCLK}\n    connect \\D \\d [{i}]\n"
            f"    connect \\Q \\q [{i}]\n  end\n"
        )
    return f"autoidx 20\n{_LIB}module \\top\n{out}{extra_cells}end\n"


def _clock_of(text):
    """Each flop's clock connection, by flop name."""
    clocks, cell = {}, None
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ["cell"]:
            cell = words[2]
        elif words[:2] == ["connect", "\\C"] and cell:
            clocks[cell] = " ".join(words[2:])
    return clocks


def test_shares_are_near_equal_and_within_the_limit():
    assert icg_split._shares(40, 16) == [14, 13, 13]
    assert icg_split._shares(32, 16) == [16, 16]
    assert icg_split._shares(17, 16) == [9, 8]


def test_an_over_full_icg_is_split_in_name_order():
    text, report = icg_split.split_design(_design(40), 16)
    assert report == [f"icg_split: top {ICG}: 40 loads over 3 cells"]
    copies = [f"{ICG}$split1", f"{ICG}$split2"]
    for name in copies:
        block = text.split(f"cell \\CLKGATE_X1 {name}\n")[1].split("  end\n")[0]
        assert block.splitlines() == [
            "    connect \\CK \\clk",
            "    connect \\E \\en",
            f"    connect \\GCK {name}$gclk",
        ]
        assert f'  attribute \\src "t.v:1"\n  cell \\CLKGATE_X1 {name}\n' in text
        assert "module \\top\n" in text and f"  wire {name}$gclk\n" in text
    nets = [GCLK, *(f"{name}$gclk" for name in copies)]
    clocks = _clock_of(text)
    assert [clocks[f"\\ff{i:03d}"] for i in range(40)] == (
        [nets[0]] * 14 + [nets[1]] * 13 + [nets[2]] * 13
    )


def test_the_original_lines_are_kept_around_a_split():
    before = _design(20)
    after, _ = icg_split.split_design(before, 16)
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    assert [ln[2:] for ln in diff if ln.startswith("- ")] == [
        f"    connect \\C {GCLK}"
    ] * 10


def test_loads_through_an_alias_and_a_wide_port_are_moved_bitwise():
    wide = (
        "  cell \\MEM2 \\mem\n"
        f"    connect \\CLK {{ \\alias {GCLK} }}\n"
        "  end\n"
        f"  connect \\alias {GCLK}\n"
    )
    text, report = icg_split.split_design(
        _design(3, extra_wires="  wire \\alias\n", extra_cells=wide), 2
    )
    assert report == [f"icg_split: top {ICG}: 5 loads over 3 cells"]
    assert _clock_of(text) == {
        "\\ff000": GCLK,
        "\\ff001": GCLK,
        "\\ff002": f"{ICG}$split1$gclk",
    }
    assert (
        f"    connect \\CLK {{ {ICG}$split2$gclk [0] {ICG}$split1$gclk [0] }}\n" in text
    )
    assert f"  connect \\alias {GCLK}\n" in text


def test_groups_within_the_limit_and_other_cells_are_left_alone():
    user_icg = (
        "  cell \\CLKGATE_X1 \\u_icg\n"
        "    connect \\CK \\clk\n    connect \\E \\en\n    connect \\GCK \\g2\n  end\n"
    )
    flops = "".join(
        f"  cell $_DFF_P_ \\g{i:02d}\n    connect \\C \\g2\n  end\n" for i in range(30)
    )
    before = _design(
        16, extra_wires="  wire \\g2\n", extra_cells=user_icg + flops
    ).replace("autoidx 20\n", "autoidx 20\r\n")
    assert icg_split.split_design(before, 16) == (before, [])


def test_an_icg_with_two_outputs_is_not_split_and_warns():
    before = _design(40, icg_type="ICG2")
    after, report = icg_split.split_design(before, 16)
    assert after == before
    assert report == [
        f"{icg_split.WARNING_PREFIX}top {ICG}: ICG2 has 2 output bits, "
        "so its 40 loads are not split"
    ]
    assert icg_split.split_design(_design(10, icg_type="ICG2"), 16)[1] == []


def test_the_script_rewrites_a_design_file(tmp_path):
    source = tmp_path / "in.il"
    target = tmp_path / "out.il"
    source.write_text(_design(33))
    run = subprocess.run(
        [sys.executable, "-I", icg_split.__file__, str(source), str(target), "16"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == f"icg_split: top {ICG}: 33 loads over 3 cells\n"
    assert not source.exists()
    text = target.read_text()
    assert f"cell \\CLKGATE_X1 {ICG}$split2\n" in text
    assert f"cell \\CLKGATE_X1 {ICG}$split3\n" not in text


_YOSYS = shutil.which("yosys")

_PARAM_DESIGN = """\
(* blackbox *)
module CLKGATE_X1(input CK, input E, output GCK);
endmodule
(* blackbox *)
module blk #(parameter W = 4, parameter NAME = "x", parameter real R = 1.5,
             parameter signed [7:0] S = -3) (input clk, input [W-1:0] d, output [W-1:0] q);
endmodule
module top(input clk, input en, input [39:0] d, output reg [39:0] q, output [7:0] bq);
  always @(posedge clk) if (en) q <= d;
  blk #(.W(8), .NAME("hello"), .R(2.5), .S(-5)) u_blk(.clk(clk), .d(d[7:0]), .q(bq));
endmodule
"""


def _yosys_netlist(work, artefact_dir, max_flops):
    """Synthesise ``_PARAM_DESIGN`` with `clockgate`, through `icg_split_cmds` when ``max_flops``."""
    netlist = work / f"net{max_flops}.v"
    lines = [
        f'read_verilog -sv "{work / "p.v"}"',
        "synth -top top",
        "clockgate -pos CLKGATE_X1 E:CK:GCK -min_net_size 4",
        *(icg_split_cmds(max_flops, str(artefact_dir)) if max_flops else []),
        "opt_clean",
        f'write_verilog -noattr "{netlist}"',
    ]
    script = work / f"run{max_flops}.ys"
    script.write_text("\n".join(lines) + "\n")
    run = subprocess.run(
        [_YOSYS, "-s", str(script)], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr
    return netlist.read_text(), run.stdout


@pytest.mark.skipif(_YOSYS is None, reason="yosys not installed")
def test_the_rtlil_round_trip_keeps_parameter_types_when_nothing_splits(tmp_path):
    (tmp_path / "p.v").write_text(_PARAM_DESIGN)
    artefact_dir = tmp_path / "sp  ace#x;y"
    artefact_dir.mkdir()
    plain, _ = _yosys_netlist(tmp_path, artefact_dir, 0)
    high, log = _yosys_netlist(tmp_path, artefact_dir, 1000)
    assert "icg_split:" not in log
    assert high == plain
    assert ".S(-32'sd5)" in plain and ".R(2.500000)" in plain

    low, log = _yosys_netlist(tmp_path, artefact_dir, 16)
    assert f"icg_split: top {'$auto$clockgate.cc'}" in log
    assert low.count("CLKGATE_X1 ") == 3
    assert ".S(-32'sd5)" in low and ".R(2.500000)" in low
