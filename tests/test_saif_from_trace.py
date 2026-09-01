"""Smoke tests for rb saif (FST/VCD → SAIF v2.0)."""

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.saif_from_trace import _bit_stats, convert


def test_bit_stats_single_clock_cycle():
    """1-bit clock: 0 → 1 at t=10 → 0 at t=20; end at t=30 → T0=20, T1=10, TC=2."""
    changes = [(0, 0), (10, 1), (20, 0)]
    stats = _bit_stats(changes, bit=0, end_t=30)
    assert stats["T0"] == 20
    assert stats["T1"] == 10
    assert stats["TX"] == 0
    assert stats["TZ"] == 0
    assert stats["TC"] == 2


def test_bit_stats_no_transitions_stays_zero():
    changes = [(0, 0)]
    stats = _bit_stats(changes, bit=0, end_t=100)
    assert stats["T0"] == 100
    assert stats["T1"] == 0
    assert stats["TC"] == 0


def test_bit_stats_multibit_picks_correct_bit():
    """8-bit signal: 0x00 → 0x02 (bit 1 = 0→1) → 0x00 (bit 1 = 1→0).

    Bit 0 sees no change; bit 1 sees 2 toggles.
    """
    changes = [(0, 0x00), (10, 0x02), (20, 0x00)]
    assert _bit_stats(changes, bit=0, end_t=30)["TC"] == 0
    assert _bit_stats(changes, bit=1, end_t=30)["TC"] == 2


def test_bit_stats_string_x_handled():
    """4-state strings: 'x' contributes to TX and breaks toggle counting."""
    changes = [(0, "x"), (10, 0), (20, 1)]
    stats = _bit_stats(changes, bit=0, end_t=30)
    assert stats["TX"] == 10
    assert stats["T0"] == 10
    assert stats["T1"] == 10
    # 0↔1 transition once (10→20). x→0 does not count.
    assert stats["TC"] == 1


def test_convert_missing_input_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        convert(tmp_path / "nope.fst", tmp_path / "out.saif")


# ---------------------------------------------------------------------------
# End-to-end: real VCD -> SAIF through the live pywellen >=0.25 API.
#
# top.clk (1-bit), top.data (8-bit bus), top.sub.rst (1-bit, nested scope).
# clk toggles 0->1->0; the last change at t=10 fixes the duration.
# ---------------------------------------------------------------------------

_VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 8 # data $end
$scope module sub $end
$var wire 1 % rst $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b00000000 #
0%
#5
1!
b00000001 #
1%
#10
0!
b00000010 #
"""


def _write_vcd(tmp_path):
    vcd = tmp_path / "dump.vcd"
    vcd.write_text(_VCD)
    return vcd


def test_convert_writes_saif_structure(tmp_path):
    saif = tmp_path / "out.saif"
    convert(_write_vcd(tmp_path), saif)
    text = saif.read_text()

    # Header: native timescale, backward direction, computed duration (max t).
    assert '(SAIFVERSION "2.0")' in text
    assert '(DIRECTION "backward")' in text
    assert "(TIMESCALE 1 ns)" in text
    assert "(DURATION 10)" in text

    # Hierarchy: nested INSTANCE for top and its child scope sub.
    assert "(INSTANCE top" in text
    assert "(INSTANCE sub" in text

    # 1-bit nets by name; the 8-bit bus expanded to per-bit nets.
    assert "(clk" in text
    assert "(rst" in text
    assert "(data\\[0\\]" in text
    assert "(data\\[7\\]" in text


def test_convert_emits_toggle_and_state_blocks(tmp_path):
    saif = tmp_path / "out.saif"
    convert(_write_vcd(tmp_path), saif)
    text = saif.read_text()

    # clk toggles 0->1->0, so its TC is exactly 2 (only 0<->1 transitions count).
    assert "(TC 2)" in text
    # Every net carries the full per-bit state + glitch block.
    assert "(IG 0)" in text
    assert "(T0 " in text and "(T1 " in text
