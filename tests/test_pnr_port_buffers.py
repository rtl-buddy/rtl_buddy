"""Tests for the `buffer-ports:` mapping: which ports get a buffer, clock outputs left unbuffered, and output buffers held at their pins through global placement.

The Tcl in `rtl_buddy/pnr/port_buffers.tcl` and the step that frees the held buffers run under a bare Tcl interpreter against stand-ins for the OpenROAD commands they call; the OpenROAD half is checked by their place in the rendered flow.
"""

from importlib.resources import files
from textwrap import dedent

import pytest

from rtl_buddy.config.pnr import PnrSuiteConfig, PortBuffers
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import _FREE_PORT_BUFFERS_TCL

from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _platform, _render_flow
from test_pnr_macro_pack import _tcl_eval

_GLOBAL_PLACEMENT = "global_placement -density"


def _run(tmp_path, extra=""):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk
            desc: block
            synth: s
            synth-path: synth.yaml
            platform: p
        """)
        + extra
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0]


def _flow(tmp_path, ports, **platform):
    return _render_flow(
        tmp_path, _platform(_make_pdk_cfg(tmp_path), **platform), buffer_ports=ports
    )


# --- configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("", None),
        ("    harden: true\n", PortBuffers()),
        ("    buffer-ports: true\n", PortBuffers()),
        ("    buffer-ports: false\n", None),
        ("    harden: true\n    buffer-ports: false\n", None),
        ("    buffer-ports: {}\n", PortBuffers()),
        ("    buffer-ports: {inputs: false}\n", PortBuffers(inputs=False)),
        ("    buffer-ports: {outputs: false}\n", PortBuffers(outputs=False)),
        (
            "    buffer-ports: {inputs: false, skip-clock-outputs: true}\n",
            PortBuffers(inputs=False, skip_clock_outputs=True),
        ),
        (
            "    buffer-ports: {hold-outputs-at-pins: true, inputs: null}\n",
            PortBuffers(hold_outputs_at_pins=True),
        ),
    ],
)
def test_buffer_ports_takes_a_boolean_or_a_mapping(tmp_path, extra, expected):
    run = _run(tmp_path, extra)
    assert run.get_port_buffers() == expected
    assert run.get_buffer_ports() is (expected is not None)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("on-outputs", "buffer-ports must be true, false or a mapping"),
        ("[inputs]", "buffer-ports must be true, false or a mapping"),
        ("{outputs: 1}", r"buffer-ports.outputs must be true or false, got 1"),
        (
            "{skip-clock-outputs: 'yes'}",
            r"buffer-ports.skip-clock-outputs must be true or false",
        ),
        (
            "{inputs: false, outputs: false}",
            "buffers no port with inputs and outputs both false; "
            "write buffer-ports: false",
        ),
        (
            "{outputs: false, skip-clock-outputs: true}",
            r"buffer-ports.skip-clock-outputs needs outputs: true",
        ),
        (
            "{outputs: false, hold-outputs-at-pins: true}",
            r"buffer-ports.hold-outputs-at-pins needs outputs: true",
        ),
    ],
)
def test_a_bad_buffer_ports_value_is_refused(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError, match=message):
        _run(tmp_path, f"    buffer-ports: {value}\n")


def test_an_unknown_buffer_ports_key_is_refused(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="did you mean 'skip-clock-outputs'"):
        _run(tmp_path, "    buffer-ports: {skip-clocks: true}\n")


# --- the rendered flow --------------------------------------------------------


@pytest.mark.parametrize(
    "ports",
    [True, PortBuffers(), PortBuffers(inputs=True, outputs=True)],
    ids=["true", "empty-mapping", "both-written"],
)
def test_every_port_renders_as_buffer_ports_true_always_has(tmp_path, ports):
    text = _flow(tmp_path, ports, port_buffer="BUF_X2")
    assert (
        '\nputs ">>> Port buffers"\nbuffer_ports -inputs -outputs -buffer_cell BUF_X2\n'
    ) in text
    assert text == _flow(tmp_path, True, port_buffer="BUF_X2")
    assert "rb::port_buffers" not in text
    assert "free to move" not in text


def test_the_unused_forms_leave_global_placement_as_it_was(tmp_path):
    """Without `hold-outputs-at-pins` nothing joins global placement and the step after it."""
    for ports in (None, True, PortBuffers(inputs=False, skip_clock_outputs=True)):
        text = _flow(tmp_path, ports)
        after = text[text.index("\n", text.index(_GLOBAL_PLACEMENT)) :]
        assert after.startswith('\n\nputs ">>> Repair tie fanout"'), ports


@pytest.mark.parametrize(
    ("ports", "command"),
    [
        (PortBuffers(inputs=False), "buffer_ports -outputs\n"),
        (PortBuffers(outputs=False), "buffer_ports -inputs\n"),
    ],
)
def test_one_side_maps_to_its_buffer_ports_flag(tmp_path, ports, command):
    text = _flow(tmp_path, ports)
    assert f'puts ">>> Port buffers"\n{command}' in text
    assert text.count("buffer_ports") == 1


def test_skip_clock_outputs_buffers_the_inputs_then_the_outputs_through_the_procs(
    tmp_path,
):
    text = _flow(tmp_path, PortBuffers(skip_clock_outputs=True), port_buffer="BUF_X2")
    inputs = text.index("\nbuffer_ports -inputs -buffer_cell BUF_X2\n")
    outputs = text.index("\nrb::port_buffers::outputs 1 0 -buffer_cell BUF_X2\n")
    assert text.index("proc rb::port_buffers::outputs") < inputs < outputs
    assert text.index("place_pins -hor_layers") < inputs
    assert outputs < text.index(_GLOBAL_PLACEMENT)
    assert "free to move" not in text


def test_skip_clock_outputs_without_inputs_buffers_only_the_outputs(tmp_path):
    text = _flow(tmp_path, PortBuffers(inputs=False, skip_clock_outputs=True))
    assert "buffer_ports -inputs" not in text
    assert "\nrb::port_buffers::outputs 1 0\n" in text


def test_held_buffers_are_freed_right_after_global_placement(tmp_path):
    text = _flow(tmp_path, PortBuffers(inputs=False, hold_outputs_at_pins=True))
    held = text.index("\nrb::port_buffers::outputs 0 1\n")
    placed = text.index(_GLOBAL_PLACEMENT)
    freed = text.index(_FREE_PORT_BUFFERS_TCL)
    assert held < placed < freed < text.index("repair_design")
    assert text[placed:freed].count("\n") == 1


def test_a_staged_run_holds_in_the_floorplan_session_and_frees_in_the_place_session(
    tmp_path, monkeypatch
):
    from test_pnr_checkpoints import _backend, _pnr_cfg

    cfg = _pnr_cfg(tmp_path, stages=True)
    cfg.buffer_ports = PortBuffers(hold_outputs_at_pins=True)
    backend, _ = _backend(tmp_path, monkeypatch, stages=True, pnr_cfg=cfg)
    backend._prepare_flow(
        backend.root_cfg.get_pnr_platform_cfg.return_value, cfg.get_floorplan()
    )
    floorplan = backend._stage_script("floorplan", run_dir="<run>")
    place = backend._stage_script("place", run_dir="<run>")
    assert "rb::port_buffers::outputs 0 1" in floorplan
    assert _FREE_PORT_BUFFERS_TCL not in floorplan
    assert place.index(_GLOBAL_PLACEMENT) < place.index(_FREE_PORT_BUFFERS_TCL)
    assert "rb::port_buffers" not in place


# --- hardened digest ----------------------------------------------------------


def test_a_mapping_of_the_defaults_keeps_the_true_digest(tmp_path):
    platform = _platform(_make_pdk_cfg(tmp_path))

    def _digest(ports):
        return pnr_abstract.abstract_config(
            _make_pnr_cfg(tmp_path, harden=True, buffer_ports=ports), platform
        )

    assert _digest(PortBuffers())["buffer_ports"] is True
    assert pnr_abstract.config_digest(_digest(PortBuffers())) == (
        pnr_abstract.config_digest(_digest(True))
    )
    held = _digest(PortBuffers(hold_outputs_at_pins=True))
    assert held["buffer_ports"] == {
        "inputs": True,
        "outputs": True,
        "skip_clock_outputs": False,
        "hold_outputs_at_pins": True,
    }
    assert pnr_abstract.config_digest(held) != pnr_abstract.config_digest(_digest(True))


# --- Tcl ----------------------------------------------------------------------


def _procs() -> str:
    return files("rtl_buddy.pnr").joinpath("port_buffers.tcl").read_text()


#: Two rows of 0.2-wide sites, 0 to 100 in x, at y 10 (R0) and y 12 (MX).
_ROWS = "{{0 10 100 12 2 R0} {0 12 100 14 2 MX}}"


@pytest.mark.parametrize(
    ("taken", "x", "y", "width", "site"),
    [
        ("", 51, 13, 4, "50 12 MX"),
        ("", -30, 0, 4, "0 10 R0"),
        ("", 99, 10, 4, "96 10 R0"),
        ("", 40, 10, 200, ""),
        ("10 {{50 54}} 12 {{50 54}}", 49, 10, 4, "46 10 R0"),
        ("10 {{48 52}} 12 {{48 52}}", 49, 10, 4, "52 10 R0"),
        ("10 {{46 52} {52 56}} 12 {{46 52} {52 56}}", 50, 10, 4, "56 10 R0"),
        ("10 {{94 100}} 12 {{94 100}}", 99, 10, 4, "90 10 R0"),
        ("10 {{0 100}}", 50, 10, 4, "50 12 MX"),
    ],
    ids=[
        "inside",
        "clamped-low",
        "clamped-high",
        "too-wide",
        "taken-free-to-the-left",
        "taken-free-to-the-right",
        "taken-past-two-spans",
        "taken-at-the-row-end",
        "row-full",
    ],
)
def test_nearest_site_snaps_into_the_nearest_free_site(taken, x, y, width, site):
    out = _tcl_eval(
        _procs()
        + f"\nset result [rb::port_buffers::nearest_site {_ROWS} {{{taken}}} {x} {y} {width}]\n"
    )
    assert out == site


#: Drops the progress lines the procs print, so the answer is all a run writes.
_QUIET = """
rename puts rb_test_puts
proc puts {args} { if {[llength $args] > 1} { rb_test_puts {*}$args } }
"""

#: Stand-ins for the OpenROAD commands the procs call. Output ports `y` (data) and
#: `ck` (a forwarded clock, its net `n_ck` shared with the clock input); `buffer_ports
#: -outputs` buffers every output whose net is not dont-touch, as OpenROAD does.
_STUBS = (
    _QUIET
    + r"""
namespace eval ord {}
namespace eval sta {}
namespace eval odb {}
proc ord::get_db_block {} { return blk }
proc odb::dbBoolProperty_create {obj name value} {
    lappend ::calls [list $obj mark $name $value]
}
set ::insts {u_drv}
set ::net_of {y n_y ck n_ck}
set ::dont_touch {n_ck_user 1}
set ::iterms {n_y {it_drv} n_ck {}}
set ::calls {}
proc all_outputs {} { return {p_y p_ck} }
proc get_full_name {obj} { return [string range $obj 2 end] }
proc sta::get_port_pin {port} { return "pin_[string range $port 2 end]" }
proc sta::is_clock {pin} { return [expr {$pin eq "pin_ck"}] }
proc sta::is_clock_src {pin} { return 0 }
proc sta::sta_to_db_port {port} { return "bt_[string range $port 2 end]" }
proc buffer_ports {args} {
    lappend ::calls [list buffer_ports {*}$args]
    foreach port {y ck} {
        set net [dict get $::net_of $port]
        if {[dict exists $::dont_touch $net]} { continue }
        set buf "out_$port"
        lappend ::insts $buf
        dict set ::net_of $port "n_$buf"
        dict set ::iterms "n_$buf" [list "it_$buf"]
    }
}
proc blk {method args} {
    switch $method {
        getInsts { return $::insts }
        getRows { return {row0} }
        getBTerms { return {bt_y bt_ck} }
    }
}
proc row0 {method args} {
    switch $method {
        getBBox { return box_row }
        getSpacing { return 2 }
        getOrient { return R0 }
    }
}
proc box_row {method} { return [dict get {xMin 0 yMin 10 xMax 100 yMax 12} $method] }
proc box_pin {method} { return [dict get {xMin 100 yMin 40 xMax 102 yMax 42} $method] }
proc bt_y {method args} { bterm y $method }
proc bt_ck {method args} { bterm ck $method }
proc bterm {port method} {
    switch $method {
        getNet { return [dict get $::net_of $port] }
        getIoType { return OUTPUT }
        getBBox { return box_pin }
        getName { return $port }
    }
}
proc net {name method args} {
    switch $method {
        isDoNotTouch { return [dict exists $::dont_touch $name] }
        setDoNotTouch {
            lappend ::calls [list $name setDoNotTouch {*}$args]
            if {[lindex $args 0]} { dict set ::dont_touch $name 1 } else { dict unset ::dont_touch $name }
        }
        getITerms { return [dict get $::iterms $name] }
    }
}
foreach n {n_y n_ck} { proc $n {method args} "net $n \$method {*}\$args" }
proc iterm {inst method} {
    switch $method {
        getInst { return $inst }
        isOutputSignal { return 1 }
    }
}
foreach i {drv out_y out_ck} {
    proc "it_$i" {method} "iterm [expr {$i eq {drv} ? {u_drv} : $i}] \$method"
    proc "n_$i" {method args} "net n_$i \$method {*}\$args"
}
proc inst {name method args} {
    switch $method {
        getName { return $name }
        getMaster { return master }
        default { lappend ::calls [list $name $method {*}$args] }
    }
}
foreach i {u_drv out_y out_ck} { proc $i {method args} "inst $i \$method {*}\$args" }
proc master {method} { return [dict get {getWidth 4 getHeight 2 getName BUF} $method] }
"""
)


def _outputs(skip: int, hold: int) -> list[str]:
    out = _tcl_eval(
        _STUBS
        + _procs()
        + f"\nrb::port_buffers::outputs {skip} {hold} -buffer_cell BUF\n"
        + 'set result [join $::calls "\\n"]\n'
    )
    return out.splitlines()


def test_skipped_clock_outputs_are_dont_touch_only_for_buffer_ports():
    assert _outputs(1, 0) == [
        "n_ck setDoNotTouch 1",
        "buffer_ports -outputs -buffer_cell BUF",
        "n_ck setDoNotTouch 0",
    ]


def test_without_skipping_every_output_gets_a_buffer():
    assert _outputs(0, 0) == ["buffer_ports -outputs -buffer_cell BUF"]


def test_held_buffers_go_to_the_row_site_nearest_their_pin_and_are_firm():
    # Pin centre (101, 41); the 4-wide buffer's lower left is clamped to x 96 of the one row.
    assert _outputs(1, 1) == [
        "n_ck setDoNotTouch 1",
        "buffer_ports -outputs -buffer_cell BUF",
        "n_ck setDoNotTouch 0",
        "out_y setOrient R0",
        "out_y setLocation 96 10",
        "out_y setPlacementStatus FIRM",
        "out_y mark rb_held_port_buffer 1",
    ]


def test_held_buffers_at_one_pin_take_neighbouring_sites():
    # Both outputs share the pin stand-in, so the second buffer goes left of the first.
    assert _outputs(0, 1)[1:] == [
        "out_y setOrient R0",
        "out_y setLocation 96 10",
        "out_y setPlacementStatus FIRM",
        "out_y mark rb_held_port_buffer 1",
        "out_ck setOrient R0",
        "out_ck setLocation 92 10",
        "out_ck setPlacementStatus FIRM",
        "out_ck mark rb_held_port_buffer 1",
    ]


def test_a_net_dont_touch_before_stays_so():
    out = _tcl_eval(
        _STUBS
        + _procs()
        + "set ::dont_touch {n_ck 1}\n"
        + "rb::port_buffers::outputs 1 0\n"
        + 'set result [join $::calls "\\n"]\n'
    )
    assert out.splitlines() == ["buffer_ports -outputs"]


#: Stand-ins for the freeing step: outputs `a` (a held buffer, marked and FIRM),
#: `b` (a FIRM driver the hold step did not place), `c` (a marked buffer a hook
#: set PLACED) and `d` (a marked buffer sharing its net with an unmarked load),
#: and input `in` (a marked FIRM cell).
_FREE_STUBS = (
    _QUIET
    + r"""
namespace eval ord {}
namespace eval odb {}
proc ord::get_db_block {} { return blk }
proc blk {method} { return {bt_a bt_b bt_c bt_d bt_in} }
set ::calls {}
proc odb::dbBoolProperty_find {obj name} {
    if {$name eq "rb_held_port_buffer" && $obj in {i_a i_c i_d i_in}} {
        return "mark_$obj"
    }
    return NULL
}
proc odb::dbProperty_destroy {prop} { lappend ::calls [list $prop destroyed] }
foreach {port status io} {
    a FIRM OUTPUT  b FIRM OUTPUT  c PLACED OUTPUT  d FIRM OUTPUT  in FIRM INPUT
} {
    proc bt_$port {method} "return \[dict get {getNet n_$port getIoType $io} \$method\]"
    set iterms [expr {$port eq "d" ? "it_d it_load" : "it_$port"}]
    proc n_$port {method} "return \[dict get {getITerms {$iterms}} \$method\]"
    proc it_$port {method} "return \[dict get {getInst i_$port} \$method\]"
    proc i_$port {method args} "
        switch \$method {
            getPlacementStatus { return $status }
            default { lappend ::calls \[list i_$port \$method {*}\$args\] }
        }
    "
}
proc it_load {method} { return i_load }
proc i_load {method args} { return FIRM }
"""
)


def test_the_free_step_frees_only_the_buffers_the_hold_step_marked():
    out = _tcl_eval(
        _FREE_STUBS + _FREE_PORT_BUFFERS_TCL + 'set result [join $::calls "\\n"]\n'
    )
    assert out.splitlines() == [
        "mark_i_a destroyed",
        "i_a setPlacementStatus PLACED",
        "mark_i_c destroyed",
        "mark_i_d destroyed",
        "i_d setPlacementStatus PLACED",
    ]
