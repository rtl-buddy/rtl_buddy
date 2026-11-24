"""The `clock-latency:` run key's report (rtl_buddy#1079): its validation, where the final reports measure the groups, and the result field read from their lines.

The parser cases read lines as OpenROAD 26Q2 printed them for a placed four-register design on a single-corner and a two-corner ASAP7 platform. The end-to-end case runs the generated step on such a design; it needs OpenROAD and `RTL_BUDDY_ASAP7_DIR`, an ORFS `platforms/asap7` directory.
"""

import logging
import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pnr_events import _capture_pnr_events, _one_event
from pnr_stub_openroad import _pdk, _platform, _pnr_cfg
from tcl_eval import _tcl_eval

from rtl_buddy.config.pnr import (
    CHECKPOINT_STAGES,
    ClockLatencyGroup,
    PnrConfigFile,
    PnrFloorplanFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr, _clock_latency_tcl

_GROUPS = (
    ClockLatencyGroup("boundary_in", ("input-capture",), 90.0, 10.0),
    ClockLatencyGroup("boundary_out", ("output-launch",)),
    ClockLatencyGroup("fwd_regs", ("r2/CLK", "r3/CLK"), 100.0, 10.0),
    ClockLatencyGroup("none", ("u_nope/CLK",)),
    ClockLatencyGroup("data", ("r0/D", "r1/CLK")),
)
_STEP = (
    "rb::clock_latency::report RB-CLOCK-LATENCY: [list "
    "[list boundary_in [list {input-capture}]] "
    "[list boundary_out [list {output-launch}]] "
    "[list fwd_regs [list {r2/CLK} {r3/CLK}]] "
    "[list none [list {u_nope/CLK}]] "
    "[list data [list {r0/D} {r1/CLK}]]]"
)


def _load(tmp_path, value):
    return PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(utilization=0.5),
        clock_latency=value,
    ).initialise(str(tmp_path))


def test_clock_latency_loads_each_group(tmp_path):
    groups = _load(
        tmp_path,
        [
            {
                "name": "in",
                "sinks": "input-capture",
                "target-ps": 90,
                "tolerance-ps": 5,
            },
            {"name": "fwd.regs", "sinks": ["u_blk.r*/CLK", "output-launch"]},
            {"name": "neg", "sinks": ["r0/CLK"], "target-ps": -3.5},
        ],
    ).get_clock_latency()
    assert groups == (
        ClockLatencyGroup("in", ("input-capture",), 90.0, 5.0),
        ClockLatencyGroup("fwd.regs", ("u_blk.r*/CLK", "output-launch")),
        ClockLatencyGroup("neg", ("r0/CLK",), -3.5, None),
    )
    assert _load(tmp_path, None).get_clock_latency() == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "clock-latency must be a list of groups"),
        ({"name": "a"}, "clock-latency must be a list of groups"),
        (["a"], "clock-latency[0] must be a mapping with name and sinks"),
        ([{"sinks": "r0/CLK"}], "clock-latency[0].name must be a name"),
        ([{"name": "a b", "sinks": "r0/CLK"}], "clock-latency[0].name must be a name"),
        ([{"name": "a"}], "clock-latency[0].sinks must be a selector"),
        ([{"name": "a", "sinks": []}], "clock-latency[0].sinks must be a selector"),
        (
            [{"name": "a", "sinks": ["r0/CLK", "input_capture"]}],
            "clock-latency[0].sinks[1] must be input-capture or output-launch, or "
            "<instance>/<pin>",
        ),
        (
            [{"name": "a", "sinks": ["r0/CLK", "r0/CLK"]}],
            "clock-latency[0].sinks repeats an entry",
        ),
        (
            [{"name": "a", "sinks": "r0/CLK", "target-ps": "90"}],
            "clock-latency[0].target-ps must be a number in ps",
        ),
        (
            [{"name": "a", "sinks": "r0/CLK", "target-ps": 90, "tolerance-ps": -1}],
            "clock-latency[0].tolerance-ps must be a number of at least 0 in ps",
        ),
        (
            [{"name": "a", "sinks": "r0/CLK", "tolerance-ps": 5}],
            "clock-latency[0].tolerance-ps needs target-ps",
        ),
        (
            [{"name": "a", "sinks": "r0/CLK"}, {"name": "a", "sinks": "r1/CLK"}],
            "clock-latency names group 'a' twice",
        ),
    ],
    ids=[
        "empty",
        "mapping",
        "string group",
        "no name",
        "bad name",
        "no sinks",
        "empty sinks",
        "unknown selector",
        "repeated sink",
        "string target",
        "negative tolerance",
        "tolerance without target",
        "repeated group",
    ],
)
def test_a_bad_clock_latency_names_the_run_and_entry(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError) as err:
        _load(tmp_path, value)
    assert str(err.value).startswith("pnr run 'r': ")
    assert message in str(err.value)


def _scripts(tmp_path, *, groups=(), kind="one session", template=None, corners=False):
    pnr_cfg = _pnr_cfg(
        tmp_path,
        CHECKPOINT_STAGES if kind == "checkpoints" else None,
        stages=kind == "stages",
    )
    pnr_cfg.clock_latency = tuple(groups)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    if kind != "one session":
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
        os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    if template is not None:
        backend._load_template = lambda: template
    pdk = (
        _pdk(tmp_path, corners={"ss": "pdk/lib/ss.lib", "ff": "pdk/lib/ff.lib"})
        if corners
        else _pdk(tmp_path)
    )
    platform = _platform(pdk, **({"corners": ["ss", "ff"]} if corners else {}))
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
    written = {"pnr.tcl": script.read_text()}
    if backend._ckpt_run_dir:
        for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
            written[path.name] = path.read_text()
    return written


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_without_the_key_every_script_renders_as_the_flow_without_it(tmp_path, kind):
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    assert template.count("{{ clock_latency_reports }}") == 1
    template = template.replace("{{ clock_latency_reports }}", "")

    now = _scripts(tmp_path, kind=kind)
    before = _scripts(tmp_path, kind=kind, template=template)

    assert now == before
    assert all("clock_latency" not in text for text in now.values())


@pytest.mark.parametrize("kind", ["one session", "stages"])
def test_the_groups_are_measured_in_the_final_reports(tmp_path, kind):
    scripts = _scripts(tmp_path, groups=_GROUPS, kind=kind)
    text = scripts["pnr.tcl" if kind == "one session" else "07_finish.tcl"]
    order = [
        'puts ">>> Final reports"',
        "report_tns -min\n",
        'puts ">>> Clock latency (clock-latency)"\n',
        "proc rb::insts::pins",
        "proc rb::clock_latency::report",
        _STEP + " [list [list typ {}]]\n",
        "report_checks -path_delay max",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    if kind == "stages":
        sessions = {name: s for name, s in scripts.items() if name != "pnr.tcl"}
        assert [n for n, s in sessions.items() if "rb::clock_latency" in s] == [
            "07_finish.tcl"
        ]


def test_a_multi_corner_platform_measures_each_corner_in_its_scene():
    step = _clock_latency_tcl(_GROUPS[:1], ["ss", "ff"], "ss")
    assert step.endswith(
        "rb::clock_latency::report RB-CLOCK-LATENCY: "
        "[list [list boundary_in [list {input-capture}]]] "
        "[list [list ss [rb::clock_latency::corner_args ss]] "
        "[list ff [rb::clock_latency::corner_args ff]]]"
    )
    assert _clock_latency_tcl((), ["ss"], "ss") == ""


def test_each_corner_with_a_mode_is_measured_in_that_mode():
    step = _clock_latency_tcl(_GROUPS[:1], ["ss", "ff"], "ss", modes=True)
    assert step.endswith(
        "[list [list ss [rb::clock_latency::corner_args ss] ss] "
        "[list ff [rb::clock_latency::corner_args ff] ff]]"
    )


#: A timer whose pin `b` is clocked only in mode `ff`, as when the two corners' SDCs set a clock mux's select apart. Each arrival is 10 in `ss` and 8 in `ff`.
_MODES = r"""
namespace eval sta {}
set ::mode ss
set ::modes {}
proc sta::cmd_mode_name {} { return $::mode }
proc sta::find_scene {name} { return $name }
proc set_mode {name} { lappend ::modes $name; set ::mode $name }
proc get_property {pin property} {
    if {$pin eq "a" || ($pin eq "b" && $::mode eq "ff")} { return clk }
    return {}
}
proc with_output_to_variable {var cmd} {
    upvar 1 $var text
    set text [uplevel 1 $cmd]
}
proc report_arrival {args} {
    set scene [lindex $args [expr {[lsearch $args -scene] + 1}]]
    set t [expr {$scene eq "ff" ? 8.0 : 10.0}]
    return "  (clk ^) r $t:$t f $t:$t
"
}
proc puts {line} { lappend ::lines $line }
"""


def test_a_secondary_corner_counts_the_sinks_its_own_mode_clocks():
    source = "".join(
        files("rtl_buddy.pnr").joinpath(name).read_text()
        for name in ("insts.tcl", "clock_latency.tcl")
    )
    result = _tcl_eval(
        _MODES
        + source
        + "proc rb::clock_latency::sinks {specs} { return {a b} }\n"
        + "rb::clock_latency::report T [list [list g {x}] [list h {y}]] [list "
        + "[list ss [rb::clock_latency::corner_args ss] ss] "
        + "[list ff [rb::clock_latency::corner_args ff] ff]]\n"
        + 'set result [join [list [join $::lines |] $::modes $::mode] "\\n"]'
    )
    lines, modes, current = result.split("\n")
    assert lines.split("|") == [
        "T g ss 2 1 10.0 10.0 10.0",
        "T g ff 2 2 8.0 8.0 8.0",
        "T h ss 2 1 10.0 10.0 10.0",
        "T h ff 2 2 8.0 8.0 8.0",
    ]
    # Each corner's mode while it is measured, and the first mode again after each group.
    assert modes.split() == ["ss", "ff", "ss", "ss", "ff", "ss"]
    assert current == "ss"


def test_the_report_changes_no_abstract(tmp_path):
    cfg = _pnr_cfg(tmp_path, None)
    platform = _platform(_pdk(tmp_path))
    default = pnr_abstract.abstract_config(cfg, platform)
    cfg.clock_latency = _GROUPS
    assert pnr_abstract.abstract_config(cfg, platform) == default


#: The final reports' lines for `_GROUPS` from OpenROAD 26Q2-911, single corner `tt` (ASAP7, ps).
_SINGLE = """\
>>> Final reports
>>> Clock latency (clock-latency)
RB-CLOCK-LATENCY: boundary_in tt 1 1 85.514442 85.514442 85.514442
RB-CLOCK-LATENCY: boundary_out tt 2 2 85.514442 107.305008 96.40972500000001
RB-CLOCK-LATENCY: fwd_regs tt 2 2 107.305008 107.305008 107.305008
RB-CLOCK-LATENCY: none tt 0 0 - - -
RB-CLOCK-LATENCY: data tt 2 1 85.514442 85.514442 85.514442
"""
#: The same at corners `ss` and `ff`.
_MULTI = """\
>>> Final reports
RB-CLOCK-LATENCY: boundary_in ss 1 1 122.818298 122.818298 122.818298
RB-CLOCK-LATENCY: boundary_in ff 1 1 63.817764 63.817764 63.817764
RB-CLOCK-LATENCY: fwd_regs ss 2 2 154.288696 154.288696 154.288696
RB-CLOCK-LATENCY: fwd_regs ff 2 2 80.072289 80.072289 80.072289
"""


def _fields(tmp_path, monkeypatch, text, *, primary="tt", ps_per_unit=1.0):
    events = _capture_pnr_events(monkeypatch)
    cfg = _pnr_cfg(tmp_path, None)
    cfg.clock_latency = _GROUPS
    backend = OpenRoadPnr("demo/openroad", cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = ps_per_unit
    platform = MagicMock(get_sta_corner=lambda: primary)
    return backend._clock_latency_fields(platform, text), events


def test_each_group_is_a_result_field_at_the_primary_corner(tmp_path, monkeypatch):
    fields, events = _fields(tmp_path, monkeypatch, _SINGLE)
    latency = fields["clock_latency"]

    assert latency["boundary_out"] == {
        "sinks": 2,
        "timed_sinks": 2,
        "min_ps": pytest.approx(85.514442),
        "max_ps": pytest.approx(107.305008),
        "mean_ps": pytest.approx(96.409725),
        "target_ps": None,
        "tolerance_ps": None,
        "corners": {
            "tt": {
                "min_ps": pytest.approx(85.514442),
                "max_ps": pytest.approx(107.305008),
                "mean_ps": pytest.approx(96.409725),
            }
        },
    }
    assert latency["boundary_in"]["within_tolerance"] is True
    assert latency["fwd_regs"]["within_tolerance"] is True
    assert latency["none"]["min_ps"] is None
    assert "within_tolerance" not in latency["none"]

    level, unclocked = _one_event(
        [e for e in events if e[2].get("group") == "data"],
        "pnr.clock_latency_unclocked",
    )
    assert level == logging.WARNING
    assert (unclocked["sinks"], unclocked["timed"]) == (2, 1)
    _, empty = _one_event(
        [e for e in events if e[2].get("group") == "none"],
        "pnr.clock_latency_unclocked",
    )
    assert empty["sinks"] == 0
    assert not [e for e in events if e[1] == "pnr.clock_latency_off_target"]


def test_a_group_outside_its_tolerance_warns(tmp_path, monkeypatch):
    fields, events = _fields(tmp_path, monkeypatch, _MULTI, primary="ss")
    latency = fields["clock_latency"]

    # Judged at the primary corner only; `ff` is in `corners`.
    assert latency["fwd_regs"]["max_ps"] == pytest.approx(154.288696)
    assert latency["fwd_regs"]["corners"]["ff"]["max_ps"] == pytest.approx(80.072289)
    assert latency["fwd_regs"]["within_tolerance"] is False
    assert latency["boundary_in"]["within_tolerance"] is False
    level, off = _one_event(
        [e for e in events if e[2].get("group") == "fwd_regs"],
        "pnr.clock_latency_off_target",
    )
    assert level == logging.WARNING
    assert off["corner"] == "ss"
    message = _human_message("pnr.clock_latency_off_target", off)
    assert "fwd_regs" in message and "154.3 ps at ss, outside 100 ± 10 ps" in message


def test_times_are_converted_from_the_timer_unit(tmp_path, monkeypatch):
    fields, _ = _fields(
        tmp_path,
        monkeypatch,
        "RB-CLOCK-LATENCY: fwd_regs tt 2 2 0.1 0.2 0.15\n",
        ps_per_unit=1000.0,
    )
    record = fields["clock_latency"]["fwd_regs"]
    assert (record["min_ps"], record["max_ps"]) == (
        pytest.approx(100.0),
        pytest.approx(200.0),
    )


def test_without_groups_or_lines_there_is_no_field(tmp_path, monkeypatch):
    fields, _ = _fields(tmp_path, monkeypatch, ">>> Final reports\n")
    assert fields == {}


def test_the_machine_row_carries_the_groups():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    latency = {"fwd_regs": {"min_ps": 1.0, "max_ps": 2.0, "mean_ps": 1.5}}
    results = PnrPassResults(name="demo/results", fields={"clock_latency": latency})
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["clock_latency"] == latency


def test_the_unclocked_warning_names_the_missing_sinks():
    assert "names no sink" in _human_message(
        "pnr.clock_latency_unclocked",
        {"pnr": "b", "group": "g", "sinks": 0, "timed": 0},
    )
    assert "no clock reaches 1 of its 2 sink(s)" in _human_message(
        "pnr.clock_latency_unclocked",
        {"pnr": "b", "group": "g", "sinks": 2, "timed": 1},
    )


# -- end to end --------------------------------------------------------------------

_OPENROAD = shutil.which("openroad")
_ASAP7 = os.environ.get("RTL_BUDDY_ASAP7_DIR")
needs_asap7 = pytest.mark.skipif(
    _OPENROAD is None or not _ASAP7,
    reason="needs openroad and RTL_BUDDY_ASAP7_DIR (an ORFS platforms/asap7 directory)",
)

_NETLIST = """\
module fwd (input clk_i, input d, output q, output clk_o, output q2);
  wire n1, n2;
  DFFHQNx1_ASAP7_75t_R r0 (.CLK(clk_i), .D(d),  .QN(n1));
  DFFHQNx1_ASAP7_75t_R r1 (.CLK(clk_i), .D(n1), .QN(q));
  BUFx4_ASAP7_75t_R u_clk_buf (.A(clk_i), .Y(clk_o));
  DFFHQNx1_ASAP7_75t_R r2 (.CLK(clk_o), .D(q),  .QN(n2));
  DFFHQNx1_ASAP7_75t_R r3 (.CLK(clk_o), .D(n2), .QN(q2));
endmodule
"""

_SETUP = """\
set p $::env(RTL_BUDDY_ASAP7_DIR)
read_lef $p/lef/asap7_tech_1x_201209.lef
read_lef $p/lef/asap7sc7p5t_28_R_1x_220121a.lef
foreach l {INVBUF_RVT_TT_nldm_220122.lib.gz SEQ_RVT_TT_nldm_220123.lib SIMPLE_RVT_TT_nldm_211120.lib.gz} {
  read_liberty $p/lib/NLDM/asap7sc7p5t_$l
}
read_verilog fwd.v
link_design fwd
create_clock -name clk -period 1000 [get_ports clk_i]
set_input_delay 100 -clock clk [get_ports d]
set_output_delay 100 -clock clk [get_ports {q q2}]
initialize_floorplan -die_area {0 0 20 20} -core_area {1.08 1.08 18.92 18.92} -site asap7sc7p5t
source $p/openRoad/make_tracks.tcl
set_wire_rc -signal -layer M3
set_wire_rc -clock -layer M5
place_pins -hor_layers M4 -ver_layers M5
global_placement -skip_initial_place -density 0.6
detailed_placement
set_propagated_clock [all_clocks]
clock_tree_synthesis -buf_list BUFx4_ASAP7_75t_R -sink_clustering_enable
estimate_parasitics -placement
"""


@needs_asap7
def test_the_report_measures_each_group_in_openroad(tmp_path, monkeypatch):
    (tmp_path / "fwd.v").write_text(_NETLIST)
    script = tmp_path / "report.tcl"
    script.write_text(
        _SETUP
        + _clock_latency_tcl(_GROUPS, [], "tt")
        + "\nforeach pin {r0/CLK r1/CLK r2/CLK r3/CLK} {\n"
        + '  puts "RB-PIN $pin [get_property [get_pins $pin] arrival_max_rise]"\n'
        + "}\n"
    )
    proc = subprocess.run(
        [_OPENROAD, "-exit", "-no_init", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "[ERROR" not in out, out
    pins = {
        line.split()[1]: float(line.split()[2])
        for line in out.splitlines()
        if line.startswith("RB-PIN ")
    }
    fields, _ = _fields(tmp_path, monkeypatch, out)
    latency = fields["clock_latency"]

    # r0 captures d; r1 and r3 launch q and q2; r2 and r3 hang off the forwarded clock.
    assert latency["boundary_in"]["max_ps"] == pytest.approx(pins["r0/CLK"])
    assert latency["boundary_out"]["min_ps"] == pytest.approx(
        min(pins["r1/CLK"], pins["r3/CLK"])
    )
    assert latency["boundary_out"]["max_ps"] == pytest.approx(
        max(pins["r1/CLK"], pins["r3/CLK"])
    )
    assert latency["fwd_regs"]["max_ps"] == pytest.approx(
        max(pins["r2/CLK"], pins["r3/CLK"])
    )
    assert latency["none"]["sinks"] == 0
    assert (latency["data"]["sinks"], latency["data"]["timed_sinks"]) == (2, 1)


#: `d` reaches `ra`'s data pin; the shared reset reaches both registers' reset pins, `en` an integrated clock gate's enable, and `e2` `rd`'s clock through a plain gate.
_RESET_NETLIST = """\
module top (input clk_i, input d, input rst_n, input en, input e2, output q, output q3);
  wire n1, gclk, n3, g2;
  DFFASRHQNx1_ASAP7_75t_R ra (.CLK(clk_i), .D(d), .RESETN(rst_n), .SETN(1'b1), .QN(n1));
  DFFASRHQNx1_ASAP7_75t_R rb (.CLK(clk_i), .D(n1), .RESETN(rst_n), .SETN(1'b1), .QN(q));
  ICGx1_ASAP7_75t_R u_icg (.CLK(clk_i), .ENA(en), .SE(1'b0), .GCLK(gclk));
  DFFHQNx1_ASAP7_75t_R rc (.CLK(gclk), .D(n1), .QN(n3));
  AND2x2_ASAP7_75t_R u_and (.A(clk_i), .B(e2), .Y(g2));
  DFFHQNx1_ASAP7_75t_R rd (.CLK(g2), .D(n3), .QN(q3));
endmodule
"""


@needs_asap7
def test_input_capture_takes_only_registers_whose_data_pin_an_input_reaches(tmp_path):
    (tmp_path / "top.v").write_text(_RESET_NETLIST)
    setup = _SETUP[: _SETUP.index("read_verilog")]
    script = tmp_path / "select.tcl"
    script.write_text(
        setup
        + "read_verilog top.v\nlink_design top\n"
        + "create_clock -name clk -period 1000 [get_ports clk_i]\n"
        + "set_input_delay 100 -clock clk [all_inputs -no_clocks]\n"
        + "namespace eval rb {}\n"
        + files("rtl_buddy.pnr").joinpath("insts.tcl").read_text()
        + files("rtl_buddy.pnr").joinpath("clock_latency.tcl").read_text()
        + 'puts "RB-SELECTED [lsort [lmap pin [rb::clock_latency::selected input-capture]'
        + ' {get_full_name $pin}]]"\n'
    )
    proc = subprocess.run(
        [_OPENROAD, "-exit", "-no_init", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "RB-SELECTED ra/CLK\n" in out, out
