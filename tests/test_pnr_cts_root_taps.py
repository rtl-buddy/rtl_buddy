"""The `cts-root-taps:` run key (rtl_buddy#1078): its validation, where the flow takes the pins off their nets and puts them back around CTS, the result field and the config digest.

The end-to-end cases run the generated steps around a real `clock_tree_synthesis`; they need OpenROAD and `RTL_BUDDY_ASAP7_DIR`, an ORFS `platforms/asap7` directory.
"""

import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tcl_eval import _tcl_eval
from pnr_stub_openroad import _pdk, _platform, _pnr_cfg

from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PnrConfigFile, PnrFloorplanFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr, _cts_root_taps_tcl

_DETACH = (
    "if {[rb::has_rows]} { rb::cts_root_taps::detach [list "
    "[list {cts-root-taps[0]} {u_blk.u_clk_buf/A}] "
    "[list {cts-root-taps[1]} {g_fwd[*].u_buf/A}]] }\n"
)
_REATTACH = "rb::cts_root_taps::reattach RB-CTS-ROOT-TAP:\n"


def _scripts(tmp_path, *, taps=(), kind="one session", template=None, platform=None):
    """Every script a run writes, by file name; `template` replaces the flow template."""
    hooks = {}
    for point in ("before-cts", "before-repair-clock-nets", "after-cts"):
        hook = tmp_path / f"{point}.tcl"
        hook.write_text("")
        hooks[point] = (str(hook),)
    pnr_cfg = _pnr_cfg(
        tmp_path,
        CHECKPOINT_STAGES if kind == "checkpoints" else None,
        stages=kind == "stages",
    )
    pnr_cfg.cts_root_taps = tuple(taps)
    pnr_cfg.hooks = hooks
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
    if platform is None:
        platform = _platform(_pdk(tmp_path), cts_repair_clock_nets=True)
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
    written = {"pnr.tcl": script.read_text()}
    if backend._ckpt_run_dir:
        for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
            written[path.name] = path.read_text()
    return written


def _load(tmp_path, value):
    return PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(utilization=0.5),
        cts_root_taps=value,
    ).initialise(str(tmp_path))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("u_blk.u_clk_buf/A", ("u_blk.u_clk_buf/A",)),
        (
            ["u_a/A", "g_fwd[*].u_buf/*", "top/u_b/A"],
            ("u_a/A", "g_fwd[*].u_buf/*", "top/u_b/A"),
        ),
    ],
    ids=["unset", "one pin", "a list"],
)
def test_cts_root_taps_takes_a_pin_or_a_list(tmp_path, value, expected):
    assert _load(tmp_path, value).get_cts_root_taps() == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            [],
            "cts-root-taps must be an <instance>/<pin> name or a list of them, got []",
        ),
        (3, "cts-root-taps must be an <instance>/<pin> name or a list of them, got 3"),
        (["u_a"], "cts-root-taps[0] must be <instance>/<pin>"),
        (["u_a/A", "/A"], "cts-root-taps[1] must be <instance>/<pin>"),
        (["u_a/"], "cts-root-taps[0] must be <instance>/<pin>"),
        ([7], "cts-root-taps[0] must be <instance>/<pin>"),
        (["u a/A"], "cts-root-taps[0] must be <instance>/<pin>"),
        (["u_a/A", "u_a/A"], "cts-root-taps repeats an entry"),
    ],
    ids=[
        "empty",
        "number",
        "no pin",
        "no instance",
        "empty pin",
        "number entry",
        "space",
        "repeat",
    ],
)
def test_a_bad_cts_root_taps_names_the_run_and_entry(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError) as err:
        _load(tmp_path, value)
    assert str(err.value).startswith("pnr run 'r': ")
    assert message in str(err.value)


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_without_the_key_every_script_renders_as_the_flow_without_it(tmp_path, kind):
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    assert template.count("{{ cts_root_taps_reattach_block }}") == 1
    template = template.replace("{{ cts_root_taps_reattach_block }}", "")

    now = _scripts(tmp_path, kind=kind)
    before = _scripts(tmp_path, kind=kind, template=template)

    assert now == before
    assert all("cts_root_taps" not in text for text in now.values())


@pytest.mark.parametrize("kind", ["checkpoints", "stages"])
def test_the_taps_leave_their_nets_just_for_cts_and_its_repair(tmp_path, kind):
    scripts = _scripts(
        tmp_path, taps=("u_blk.u_clk_buf/A", "g_fwd[*].u_buf/A"), kind=kind
    )
    text = scripts["pnr.tcl" if kind == "checkpoints" else "03_cts.tcl"]
    order = [
        # A one-session run ends the place stage first, so its checkpoint holds the taps.
        *(
            ["rb::ckpt::checkpoint_for clock_tree_synthesis enter\n"]
            if kind == "checkpoints"
            else []
        ),
        ">>> Hook before-cts: ",
        "if {[rb::has_rows]} { repair_clock_inverters }\n",
        'puts ">>> CTS root taps (cts-root-taps)"\n',
        "proc rb::insts::match",
        "proc rb::cts_root_taps::detach",
        _DETACH,
        "clock_tree_synthesis \\\n",
        ">>> Hook before-repair-clock-nets: ",
        "repair_clock_nets\n",
        _REATTACH,
        ">>> Hook after-cts: ",
        'puts ">>> Detail placement (post-CTS)"',
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    assert text.count(_DETACH) == text.count(_REATTACH) == 1
    if kind == "stages":
        sessions = {name: s for name, s in scripts.items() if name != "pnr.tcl"}
        assert [n for n, s in sessions.items() if "rb::cts_root_taps" in s] == [
            "03_cts.tcl"
        ]


def test_the_taps_follow_the_clock_inverters_off_and_without_hooks(tmp_path):
    detach, reattach = _cts_root_taps_tcl(("u_a/A",))
    platform = _platform(_pdk(tmp_path), cts_repair_clock_inverters=False)
    pnr_cfg = _pnr_cfg(tmp_path, CHECKPOINT_STAGES)
    pnr_cfg.cts_root_taps = ("u_a/A",)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    text = Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()

    assert "{ repair_clock_inverters }" not in text
    assert "\nrb::ckpt::checkpoint_for clock_tree_synthesis enter" + detach in text
    assert reattach in text


def test_the_key_enters_the_config_digest_only_when_set(tmp_path):
    cfg = _pnr_cfg(tmp_path, None)
    platform = _platform(_pdk(tmp_path))
    default = pnr_abstract.abstract_config(cfg, platform)
    assert "cts_root_taps" not in default

    cfg.cts_root_taps = ("u_blk.u_clk_buf/A",)
    tapped = pnr_abstract.abstract_config(cfg, platform)
    assert tapped["cts_root_taps"] == ["u_blk.u_clk_buf/A"]
    assert pnr_abstract.config_digest(tapped) != pnr_abstract.config_digest(default)


def test_each_tap_is_a_result_field_with_its_net(tmp_path):
    backend = OpenRoadPnr(
        "demo/openroad", _pnr_cfg(tmp_path, None), str(tmp_path), MagicMock()
    )
    log = (
        ">>> rb: CTS root tap u_blk.u_clk_buf/A on net clk_i\n"
        "RB-CTS-ROOT-TAP: u_blk.u_clk_buf/A clk_i\n"
        "RB-CTS-ROOT-TAP: g_fwd[0].u_buf/A clk_i\n"
    )
    assert backend._cts_root_taps_fields(log) == {
        "cts_root_taps": [
            {"pin": "u_blk.u_clk_buf/A", "net": "clk_i"},
            {"pin": "g_fwd[0].u_buf/A", "net": "clk_i"},
        ]
    }
    assert backend._cts_root_taps_fields("no taps\n") == {}


def test_the_machine_row_carries_the_taps():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    taps = [{"pin": "u_blk.u_clk_buf/A", "net": "clk_i"}]
    results = PnrPassResults(name="demo/results", fields={"cts_root_taps": taps})
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["cts_root_taps"] == taps


#: Instances with each pin's use, named as OpenROAD stores a Yosys netlist's names. Each terminal handle answers `label` with `<instance>/<pin>`, for the test.
_PIN_BLOCK = r"""
set insts {
    u_blk.u_clk_buf {A SIGNAL Y SIGNAL VDD POWER VSS GROUND}
    {g_fwd\[0\].u_buf} {A SIGNAL Y SIGNAL}
    r0 {CLK SIGNAL D SIGNAL QN SIGNAL}
}
set handles {}
set n 0
foreach {name pins} $insts {
    set h inst[incr n]
    interp alias {} $h {} inst $name
    dict set handles $name $h
}
proc inst {name cmd args} {
    switch -- $cmd {
        getName { return $name }
        getITerms {
            set out {}
            foreach {pin use} [dict get $::insts $name] {
                set h iterm[incr ::n]
                interp alias {} $h {} iterm $name $pin $use
                lappend out $h
            }
            return $out
        }
    }
}
proc iterm {inst pin use cmd args} {
    switch -- $cmd {
        getMTerm {
            set h mterm[incr ::n]
            interp alias {} $h {} mterm $pin $use
            return $h
        }
        label { return $inst/$pin }
    }
}
proc mterm {pin use cmd args} {
    switch -- $cmd {
        getName { return $pin }
        getSigType { return $use }
    }
}
proc block {cmd args} {
    switch -- $cmd {
        findInst {
            set name [lindex $args 0]
            if {[dict exists $::handles $name]} { return [dict get $::handles $name] }
            return NULL
        }
        getInsts { return [dict values $::handles] }
    }
}
"""


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("u_blk.u_clk_buf/A", "u_blk.u_clk_buf/A"),
        ("u_blk.u_clk_buf/*", "u_blk.u_clk_buf/A u_blk.u_clk_buf/Y"),
        ("g_fwd[0].u_buf/A", r"{g_fwd\[0\].u_buf/A}"),
        ("*/A", r"u_blk.u_clk_buf/A {g_fwd\[0\].u_buf/A}"),
        ("r0/C*", "r0/CLK"),
        ("u_nope/A", ""),
    ],
    ids=[
        "one pin",
        "pin glob, supplies left out",
        "netlist name",
        "instance glob",
        "pin prefix",
        "no instance",
    ],
)
def test_a_pin_is_named_as_instance_slash_pin(spec, expected):
    source = files("rtl_buddy.pnr").joinpath("insts.tcl").read_text()
    result = _tcl_eval(
        _PIN_BLOCK
        + source
        + f"\nset result [lmap t [rb::insts::pins block {{{spec}}}] {{$t label}}]"
    )
    assert result == expected


# -- end to end --------------------------------------------------------------------

_OPENROAD = shutil.which("openroad")
_ASAP7 = os.environ.get("RTL_BUDDY_ASAP7_DIR")
needs_asap7 = pytest.mark.skipif(
    _OPENROAD is None or not _ASAP7,
    reason="needs openroad and RTL_BUDDY_ASAP7_DIR (an ORFS platforms/asap7 directory)",
)

#: A block that forwards its clock through a dont-touch buffer; `flat` also clocks two registers from the forwarded clock, as a flat run of two such blocks does.
_NETLIST = """\
module fwd (input clk_i, input d, output q, output clk_o{ports});
  wire n1;
  DFFHQNx1_ASAP7_75t_R r0 (.CLK(clk_i), .D(d),  .QN(n1));
  DFFHQNx1_ASAP7_75t_R r1 (.CLK(clk_i), .D(n1), .QN(q));
  BUFx4_ASAP7_75t_R u_clk_buf (.A(clk_i), .Y(clk_o));
{more}endmodule
"""
_FLAT = """\
  wire n2;
  DFFHQNx1_ASAP7_75t_R r2 (.CLK(clk_o), .D(q),  .QN(n2));
  DFFHQNx1_ASAP7_75t_R r3 (.CLK(clk_o), .D(n2), .QN(q2));
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
initialize_floorplan -die_area {0 0 20 20} -core_area {1.08 1.08 18.92 18.92} -site asap7sc7p5t
source $p/openRoad/make_tracks.tcl
set_wire_rc -signal -layer M3
set_wire_rc -clock -layer M5
place_pins -hor_layers M4 -ver_layers M5
global_placement -skip_initial_place -density 0.6
detailed_placement
set_propagated_clock [all_clocks]
set_dont_touch [get_cells u_clk_buf]
namespace eval rb {}
proc rb::has_rows {} { return [expr {[llength [[ord::get_db_block] getRows]] > 0}] }
"""

_REPORT = """\
proc rb_net {inst pin} {
  set net [[[[ord::get_db_block] findInst $inst] findITerm $pin] getNet]
  expr {$net eq "NULL" ? "none" : [$net getName]}
}
proc rb_driver {net} {
  foreach t [[[ord::get_db_block] findNet $net] getITerms] {
    if {[$t isOutputSignal]} { return [[$t getInst] getName] }
  }
  return port
}
foreach {inst pin} $rb_pins {
  set net [rb_net $inst $pin]
  puts "RB-NET $inst/$pin $net [rb_driver $net]"
}
set inst [[ord::get_db_block] findInst u_clk_buf]
puts "RB-STATE [$inst isDoNotTouch] [$inst getPlacementStatus] [lmap c [all_clocks] {get_name $c}]"
"""


def _cts_run(tmp_path, *, flat: bool) -> dict:
    (tmp_path / "fwd.v").write_text(
        _NETLIST.format(ports=", output q2" if flat else "", more=_FLAT if flat else "")
    )
    detach, reattach = _cts_root_taps_tcl(("u_clk_buf/A",))
    pins = "u_clk_buf A r0 CLK r1 CLK" + (" r2 CLK r3 CLK" if flat else "")
    script = tmp_path / "cts.tcl"
    script.write_text(
        _SETUP
        + detach
        + "clock_tree_synthesis -buf_list BUFx4_ASAP7_75t_R -sink_clustering_enable\n"
        + reattach
        + f"set rb_pins {{{pins}}}\n"
        + _REPORT
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
    nets = {}
    for line in out.splitlines():
        if line.startswith("RB-NET "):
            _, pin, net, driver = line.split()
            nets[pin] = (net, driver)
    state = next(line for line in out.splitlines() if line.startswith("RB-STATE "))
    return {"nets": nets, "state": state, "log": out}


@needs_asap7
def test_a_forwarding_buffer_stays_on_the_port_net_while_cts_builds_the_register_tree(
    tmp_path,
):
    run = _cts_run(tmp_path, flat=False)

    assert run["nets"]["u_clk_buf/A"] == ("clk_i", "port")
    for reg in ("r0/CLK", "r1/CLK"):
        net, driver = run["nets"][reg]
        assert net != "clk_i" and driver.startswith("clkbuf_")
    # Dont-touch and placement are as before, and no clock is left behind.
    assert run["state"] == "RB-STATE 1 PLACED clk"
    assert "RB-CTS-ROOT-TAP: u_clk_buf/A clk_i" in run["log"]


@needs_asap7
def test_a_forwarded_clock_that_clocks_registers_gets_its_own_tree(tmp_path):
    run = _cts_run(tmp_path, flat=True)

    assert run["nets"]["u_clk_buf/A"] == ("clk_i", "port")
    for reg in ("r0/CLK", "r1/CLK", "r2/CLK", "r3/CLK"):
        net, driver = run["nets"][reg]
        assert driver.startswith("clkbuf_"), run["log"]
    # The forwarded clock's tree hangs off the buffer, which CTS did not clone.
    assert all("clone" not in driver for _, driver in run["nets"].values())
    assert "temporary clock rb_cts_root_tap_0 on its output" in run["log"]
    assert run["state"] == "RB-STATE 1 PLACED clk"
