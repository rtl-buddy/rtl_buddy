"""Tests for rb saif (FST/VCD to SAIF v2.0).

Unit tests for the per-bit accumulator, then a golden end-to-end pass over a real VCD
read by the real pywellen.
"""

import re
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools import pywellen_compat
from rtl_buddy.tools.saif_from_trace import _bit_stats, convert


def test_bit_stats_single_clock_cycle():
    """1-bit clock: 0 to 1 at t=10, back to 0 at t=20, end at t=30 gives T0=20, T1=10,
    TC=2.
    """
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
    # One 0<->1 transition (10 to 20); x to 0 does not count.
    assert stats["TC"] == 1


def test_convert_missing_input_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        convert(tmp_path / "nope.fst", tmp_path / "out.saif")


def test_convert_out_of_range_pywellen_raises_before_any_api_touch(
    tmp_path, monkeypatch
):
    """A pywellen without the required API raises a FatalRtlBuddyError naming the
    version and supported range.
    """
    trace = tmp_path / "dump.vcd"
    trace.write_text(_VCD)
    monkeypatch.setitem(
        sys.modules, "pywellen", SimpleNamespace(Waveform=type("Waveform", (), {}))
    )
    monkeypatch.setattr(pywellen_compat, "pywellen_version", lambda: "0.24.2")
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        convert(trace, tmp_path / "out.saif")
    message = str(excinfo.value)
    assert "0.24.2" in message
    assert pywellen_compat.SUPPORTED_SPECIFIER in message
    assert "rb saif" in message


# Golden pass: real VCD, real pywellen, hand-derived SAIF numbers. top.clk: 1-bit, four
# edges. top.bus: 4-bit with z on the MSB from t=20. top.WIDTH: parameter, skipped.
# top.mem.[0]: memory element, skipped by name. top.sub.rst: x, 0, 1 in a nested scope.
# The timescale is 10 ps and the last change is at t=30, so TIMESCALE and DURATION are
# checked.

_VCD = """\
$timescale 10ps $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 # bus $end
$var parameter 32 ) WIDTH $end
$scope module mem $end
$var wire 1 ' [0] $end
$upscope $end
$scope module sub $end
$var wire 1 $ rst $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0000 #
b00000000000000000000000000000100 )
0'
x$
#10
1!
b0011 #
0$
#20
0!
bz101 #
1$
#30
1!
"""

_EXPECTED_NETS = {
    # 0@0 1@10 0@20 1@30, end 30: three 0<->1 transitions, 20 low, 10 high.
    "top/clk": {"T0": 20, "T1": 10, "TX": 0, "TZ": 0, "TC": 3, "IG": 0},
    # bus: 0b0000@0 0b0011@10 "z101"@20, held to end 30.
    "top/bus\\[0\\]": {"T0": 10, "T1": 20, "TX": 0, "TZ": 0, "TC": 1, "IG": 0},
    "top/bus\\[1\\]": {"T0": 20, "T1": 10, "TX": 0, "TZ": 0, "TC": 2, "IG": 0},
    "top/bus\\[2\\]": {"T0": 20, "T1": 10, "TX": 0, "TZ": 0, "TC": 1, "IG": 0},
    # MSB goes z at t=20 and stays: 10 ticks of TZ, and 0->z is no toggle.
    "top/bus\\[3\\]": {"T0": 20, "T1": 0, "TX": 0, "TZ": 10, "TC": 0, "IG": 0},
    # x@0 0@10 1@20, end 30: 10 ticks each, and x->0 is no toggle.
    "top/sub/rst": {"T0": 10, "T1": 10, "TX": 10, "TZ": 0, "TC": 1, "IG": 0},
}


def _write_vcd(tmp_path):
    vcd = tmp_path / "dump.vcd"
    vcd.write_text(_VCD)
    return vcd


def _parse_saif(text: str) -> tuple[dict, dict, list]:
    """Return (header fields, {net path: stats}, instance paths)."""
    header: dict[str, str] = {}
    nets: dict[str, dict[str, int]] = {}
    instances: list[str] = []
    path: list[str] = []
    kinds: list[str] = []
    net: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if line == ")":
            kind = kinds.pop()
            if kind == "instance":
                path.pop()
            elif kind == "net":
                net = None
            continue
        if not line.startswith("("):
            continue
        if line.endswith(")"):
            for key, value in re.findall(r"\((\w+) ([^()]*)\)", line):
                if net is None:
                    header[key] = value.strip()
                else:
                    nets[net][key] = int(value)
            continue
        body = line[1:].strip()
        if body == "SAIFILE" or body == "NET":
            kinds.append("group")
        elif body.startswith("INSTANCE"):
            path.append(body[len("INSTANCE") :].strip())
            instances.append("/".join(path))
            kinds.append("instance")
        else:
            net = "/".join([*path, body])
            nets[net] = {}
            kinds.append("net")

    assert not kinds, "unbalanced SAIF parentheses"
    return header, nets, instances


def test_convert_emits_the_golden_saif(tmp_path):
    saif = tmp_path / "out.saif"
    convert(_write_vcd(tmp_path), saif)
    header, nets, instances = _parse_saif(saif.read_text())

    assert header["SAIFVERSION"] == '"2.0"'
    assert header["DIRECTION"] == '"backward"'
    assert header["PROGRAM_NAME"] == '"rb saif"'
    # Native trace timescale, so values stay integral.
    assert header["TIMESCALE"] == "10 ps"
    assert header["DURATION"] == "30"

    assert nets == _EXPECTED_NETS

    assert instances[:1] == ["top"]
    assert "top/sub" in instances
    assert "top/mem" in instances


def test_convert_skips_parameters_and_memory_elements(tmp_path):
    """Parameters and memory-array elements are not emitted as nets."""
    saif = tmp_path / "out.saif"
    convert(_write_vcd(tmp_path), saif)
    _, nets, _ = _parse_saif(saif.read_text())

    assert "top/WIDTH" not in nets
    assert not [n for n in nets if "[0]" in n]
    # pywellen models a VCD array element as an unnamed child scope of `mem`, emitted as
    # an empty INSTANCE.
    assert not [n for n in nets if n.startswith("top/mem")]


def test_saif_cli_writes_the_same_file(minimal_project, tmp_path):
    """The real CLI entry point writes the same file, resolving paths against the
    command context.
    """
    vcd = _write_vcd(minimal_project)
    runner = CliRunner()
    rb = RtlBuddy(name="test_saif_cli")
    result = runner.invoke(rb.app, ["saif", str(vcd), "out.saif"])
    assert result.exit_code == 0, result.output

    header, nets, _ = _parse_saif((minimal_project / "out.saif").read_text())
    assert header["DURATION"] == "30"
    assert nets == _EXPECTED_NETS


def test_saif_cli_reports_a_missing_trace(minimal_project):
    runner = CliRunner()
    rb = RtlBuddy(name="test_saif_cli")
    result = runner.invoke(rb.app, ["saif", "nope.vcd", "out.saif"])
    assert result.exit_code != 0
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "trace file not found" in str(result.exception)


# FST is not covered end-to-end: no FST writer is available in CI.
# The VCD and FST readers share the same pywellen Waveform surface.
