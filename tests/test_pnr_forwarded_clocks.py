"""A forwarded clock in a hardened block's timing model, `abstract: {forwarded-clocks: arc}` (rtl_buddy#1080).

The fixtures are the two models OpenROAD 26Q2-911's `write_timing_model` wrote for a block that clocks two registers from `clk_i` and forwards it to `clk_o` through a buffer: `fwd.lib` with the SDC's `create_generated_clock -source [get_ports clk_i] -divide_by 1 [get_ports clk_o]`, whose `clk_o` arcs are related to `clk_o`, and `fwd.forwarded.lib` with that clock deleted, whose `clk_o` arcs are `rising_edge` and `falling_edge` from `clk_i`.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pnr_events import _capture_pnr_events, _one_event
from test_pnr_block_power import _TECH_LEF
from test_pnr_harden import _backend, _fake_tools, _pdk, _platform, _pnr_cfg

from rtl_buddy.config.pnr import PnrAbstract, PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_abstract, pnr_openroad
from rtl_buddy.tools.pnr_forwarded_clocks import (
    ForwardedClock,
    ForwardedClockError,
    forwarded_clocks,
    port_names,
    rewrite,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "forwarded_clock"
_FORWARD = ForwardedClock("clk_fwd", "clk_i", "clk_o")
_INPUTS = {"clk_i", "d"}
_OUTPUTS = {"clk_o", "q"}


def _sdc(*lines: str) -> str:
    return "create_clock -name clk -period 1000.0000 [get_ports {clk_i}]\n" + "".join(
        f"create_generated_clock {line}\n" for line in lines
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "-name f -source [get_ports {clk_i}] -divide_by 1 [get_ports {clk_o}]",
            ForwardedClock("f", "clk_i", "clk_o"),
        ),
        (
            "-name f -add -source [get_ports {clk_i}] -master_clock [get_clocks {clk}] "
            "-multiply_by 1 -invert [get_ports {clk_o}]",
            ForwardedClock("f", "clk_i", "clk_o", invert=True),
        ),
        (
            "-name f -source [get_ports {clk_i}] -combinational -divide_by 1 "
            "[get_ports {clk_o}]",
            ForwardedClock("f", "clk_i", "clk_o"),
        ),
    ],
    ids=["divide by 1", "multiply by 1, inverted", "combinational"],
)
def test_a_ratio_1_clock_from_an_input_to_an_output_is_forwarded(line, expected):
    assert forwarded_clocks(_sdc(line), _INPUTS, _OUTPUTS) == ([expected], [])


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (
            "-name f -source [get_ports {clk_i}] -divide_by 2 [get_ports {clk_o}]",
            "its ratio is not 1",
        ),
        (
            "-name f -source [get_ports {q}] -divide_by 1 [get_ports {clk_o}]",
            "its -source is not one input port",
        ),
        (
            "-name f -source [get_pins {u_buf/A}] -divide_by 1 [get_ports {clk_o}]",
            "its -source is not one input port",
        ),
        (
            "-name f -source [get_ports {clk_i}] -edges {1 3 5} [get_ports {clk_o}]",
            "it changes the waveform",
        ),
        (
            "-name f -source [get_ports {clk_i}] -divide_by 1 [get_ports {clk_o q}]",
            "it is defined on more than one port",
        ),
    ],
    ids=[
        "divide by 2",
        "output source",
        "pin source",
        "edges",
        "two ports",
    ],
)
def test_another_generated_clock_on_an_output_is_named_and_left(line, reason):
    assert forwarded_clocks(_sdc(line), _INPUTS, _OUTPUTS) == ([], [("f", reason)])


def test_a_clock_on_an_internal_pin_is_not_considered():
    line = "-name f -source [get_ports {clk_i}] -divide_by 1 [get_pins {u_buf/Y}]"
    assert forwarded_clocks(_sdc(line), _INPUTS, _OUTPUTS) == ([], [])


def test_two_clocks_on_one_output_that_disagree_are_both_left():
    sdc = _sdc(
        "-name a -source [get_ports {clk_i}] -divide_by 1 [get_ports {clk_o}]",
        "-name b -add -source [get_ports {clk_i}] -multiply_by 1 -invert "
        "[get_ports {clk_o}]",
    )
    found, skipped = forwarded_clocks(sdc, _INPUTS, _OUTPUTS)
    assert found == []
    assert [name for name, _ in skipped] == ["a", "b"]


def test_the_ports_are_read_from_the_model():
    assert port_names(str(_FIXTURES / "fwd.lib")) == (_INPUTS, _OUTPUTS)


def _pin(text: str, name: str) -> str:
    start = text.index(f'pin("{name}")')
    return text[start : text.index("\n    }\n", start)]


def _staged(tmp_path) -> tuple[Path, Path]:
    lib = tmp_path / "fwd.lib"
    second = tmp_path / "fwd.forwarded.lib"
    shutil.copy(_FIXTURES / "fwd.lib", lib)
    shutil.copy(_FIXTURES / "fwd.forwarded.lib", second)
    return lib, second


def _values(text: str, table: str) -> str:
    return re.search(rf"{table}\(\w+\) {{\n.*\n\s*values\(\"([^\"]*)\"", text).group(1)


def test_the_output_gets_one_combinational_arc_from_the_edge_arcs(tmp_path):
    lib, second = _staged(tmp_path)
    before = lib.read_text()
    forwarded = second.read_text()

    rewrite(str(lib), str(second), [_FORWARD])
    after = lib.read_text()

    clk_o = _pin(after, "clk_o")
    assert clk_o.count("timing() {") == 1
    assert 'related_pin : "clk_i";' in clk_o
    assert "timing_sense : positive_unate;" in clk_o
    assert "timing_type : combinational;" in clk_o
    assert "_edge" not in clk_o and 'related_pin : "clk_o"' not in clk_o
    rising = forwarded[forwarded.index("timing_type : rising_edge") :]
    falling = forwarded[forwarded.index("timing_type : falling_edge") :]
    # Rise from the rising edge's arc, fall from the falling edge's.
    assert _values(clk_o, "cell_rise") == _values(rising, "cell_rise")
    assert _values(clk_o, "rise_transition") == _values(rising, "rise_transition")
    assert _values(clk_o, "cell_fall") == _values(falling, "cell_fall")
    assert _values(clk_o, "fall_transition") == _values(falling, "fall_transition")
    assert _values(clk_o, "cell_rise").startswith("22.10860,")
    assert _values(clk_o, "cell_fall").startswith("24.57448,")
    # Its table template is copied under its own name.
    assert "cell_rise(rb_forwarded_template_1)" in clk_o
    assert after.count("lu_table_template(rb_forwarded_template_1) {") == 1
    # The data arcs and the source pin's clock-tree paths are as written.
    for pin in ("clk_i", "d", "q"):
        assert _pin(after, pin) == _pin(before, pin)
    assert after.index("lu_table_template(rb_forwarded_template_1)") < after.index(
        'cell ("fwd")'
    )


def test_an_inverted_clock_takes_the_crossed_arcs_or_is_refused(tmp_path):
    lib, second = _staged(tmp_path)
    before = lib.read_text()

    with pytest.raises(
        ForwardedClockError, match="check the generated clock's -invert"
    ):
        rewrite(str(lib), str(second), [ForwardedClock("f", "clk_i", "clk_o", True)])
    # The model is left as it was.
    assert lib.read_text() == before


@pytest.mark.parametrize(
    ("clock", "message"),
    [
        (ForwardedClock("f", "clk_i", "clk_x"), "fwd.lib has no pin clk_x"),
        (ForwardedClock("f", "d", "clk_o"), "has no rising_edge arc from d"),
    ],
    ids=["no such pin", "no edge arc"],
)
def test_a_model_without_the_arcs_is_refused(tmp_path, clock, message):
    lib, second = _staged(tmp_path)
    with pytest.raises(ForwardedClockError, match=message):
        rewrite(str(lib), str(second), [clock])


def _abstract(tmp_path, lines: str) -> PnrAbstract:
    path = tmp_path / "pnr.yaml"
    path.write_text(
        "rtl-buddy-filetype: pnr_config\n"
        "runs:\n"
        "  - name: blk\n"
        "    desc: block\n"
        "    synth: s\n"
        "    synth-path: synth.yaml\n"
        "    floorplan: {utilization: 0.55}\n"
        "    platform: p\n" + lines
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0].get_abstract()


def test_the_key_defaults_to_none_and_takes_arc(tmp_path):
    assert _abstract(tmp_path, "    harden: true\n").forwarded_clocks == "none"
    arc = _abstract(
        tmp_path, "    harden: true\n    abstract: {forwarded-clocks: arc}\n"
    )
    assert arc == PnrAbstract(forwarded_clocks="arc")
    assert not arc.is_default()
    with pytest.raises(
        FatalRtlBuddyError, match="forwarded-clocks must be 'none' or 'arc', got True"
    ):
        _abstract(
            tmp_path, "    harden: true\n    abstract: {forwarded-clocks: true}\n"
        )
    with pytest.raises(FatalRtlBuddyError, match="abstract needs harden: true"):
        _abstract(tmp_path, "    abstract: {forwarded-clocks: arc}\n")


def test_only_arc_writes_the_second_model_and_enters_the_digest(tmp_path):
    default = pnr_abstract.harden_tcl(PnrAbstract())
    assert default == pnr_abstract.harden_tcl(None)
    assert "delete_generated_clock" not in default
    arc = pnr_abstract.harden_tcl(PnrAbstract(forwarded_clocks="arc"))
    assert arc.startswith(default)
    assert arc.endswith(
        "delete_generated_clock $rb_output_clocks }\n"
        "write_timing_model $OUT_DIR/abstract.partial/${DESIGN}.forwarded.lib\n"
    )

    platform = _platform(_pdk(tmp_path))
    plain = pnr_abstract.abstract_config(_pnr_cfg(tmp_path), platform)
    assert "abstract" not in plain
    with_arc = pnr_abstract.abstract_config(
        _pnr_cfg(tmp_path, abstract=PnrAbstract(forwarded_clocks="arc")), platform
    )
    assert with_arc["abstract"]["forwarded_clocks"] == "arc"


#: `write_sdc` of the block's SDC, as the flow writes `<top>.routed.sdc`.
_ROUTED_SDC = _sdc(
    "-name clk_fwd -source [get_ports {clk_i}] -divide_by 1 [get_ports {clk_o}]"
)


def _harden(tmp_path, monkeypatch, *, sdc=_ROUTED_SDC, second=True):
    """A hardening run with `forwarded-clocks: arc` whose fake OpenROAD stages the fixture models."""
    backend = _backend(
        tmp_path,
        monkeypatch,
        pnr_overrides={"abstract": PnrAbstract(forwarded_clocks="arc")},
    )
    _fake_tools(backend, monkeypatch)
    real_run = pnr_openroad.subprocess.run
    staging = Path(backend.artefact_dir) / pnr_abstract.ABSTRACT_STAGING_NAME

    def _run(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        if hasattr(kwargs.get("stdout"), "write"):
            text = (_FIXTURES / "fwd.lib").read_text()
            (staging / "demo_top.lib").write_text(text.replace('"fwd"', '"demo_top"'))
            if second:
                shutil.copy(
                    _FIXTURES / "fwd.forwarded.lib", staging / "demo_top.forwarded.lib"
                )
            (Path(backend.artefact_dir) / "demo_top.routed.sdc").write_text(sdc)
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    return backend


def test_a_hardened_block_publishes_the_arc_and_records_it(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    backend = _harden(tmp_path, monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    out = Path(backend.artefact_dir) / "abstract"
    assert sorted(p.name for p in out.iterdir()) == [
        "abstract.manifest.json",
        "demo_top.gds",
        "demo_top.lef",
        "demo_top.lib",
    ]
    lib = (out / "demo_top.lib").read_text()
    assert "timing_type : combinational;" in _pin(lib, "clk_o")
    manifest = json.loads((out / "abstract.manifest.json").read_text())
    assert manifest["forwarded_clocks"] == [
        {"from": "clk_i", "to": "clk_o", "sense": "positive_unate"}
    ]
    assert (
        manifest["outputs"]["lib"]["sha256"]
        == pnr_abstract.file_fingerprint(str(out / "demo_top.lib"), None)["sha256"]
    )
    _, fields = _one_event(events, "pnr.abstract_forwarded_clock")
    assert (fields["source"], fields["target"]) == ("clk_i", "clk_o")


def test_a_block_without_a_forwarded_clock_warns_and_publishes(tmp_path, monkeypatch):
    events = _capture_pnr_events(monkeypatch)
    backend = _harden(
        tmp_path,
        monkeypatch,
        sdc=_sdc(
            "-name half -source [get_ports {clk_i}] -divide_by 2 [get_ports {clk_o}]"
        ),
    )

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    out = Path(backend.artefact_dir) / "abstract"
    manifest = json.loads((out / "abstract.manifest.json").read_text())
    assert manifest["forwarded_clocks"] == []
    assert "rising_edge" in _pin((out / "demo_top.lib").read_text(), "clk_o")
    assert not list(out.glob("*.forwarded.lib"))
    level, skipped = _one_event(events, "pnr.abstract_forwarded_clock_skipped")
    assert skipped["reason"] == "its ratio is not 1"
    _one_event(events, "pnr.abstract_no_forwarded_clocks")
    assert "ratio is not 1" in _human_message(
        "pnr.abstract_forwarded_clock_skipped", skipped
    )


def test_a_missing_second_model_fails_the_abstract(tmp_path, monkeypatch):
    backend = _harden(tmp_path, monkeypatch, second=False)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "abstract"
    assert "demo_top.forwarded.lib" in res.results["desc"]
    assert not (Path(backend.artefact_dir) / "abstract").exists()


def test_an_arc_that_cannot_be_built_fails_the_abstract(tmp_path, monkeypatch):
    backend = _harden(
        tmp_path,
        monkeypatch,
        sdc=_sdc(
            "-name inv -source [get_ports {clk_i}] -divide_by 1 -invert "
            "[get_ports {clk_o}]"
        ),
    )

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "abstract"
    assert "abstract forwarded clock: pin clk_o" in res.results["desc"]
    assert not (Path(backend.artefact_dir) / "abstract").exists()
    assert not (Path(backend.artefact_dir) / "abstract.partial").exists()


# -- the parent's timing -------------------------------------------------------------

_OPENROAD = shutil.which("openroad")
needs_openroad = pytest.mark.skipif(_OPENROAD is None, reason="openroad not installed")

#: Two blocks, the second clocked by the first's forwarded clock.
_TOP = """\
module top (input clk, input d, output q, output clk_out);
  wire c1, q0;
  fwd u0 (.clk_i(clk), .d(d), .q(q0), .clk_o(c1));
  fwd u1 (.clk_i(c1), .d(q0), .q(q), .clk_o(clk_out));
endmodule
"""
#: The block as a macro, for OpenROAD to link the top.
_MACRO_LEF = (
    """\
VERSION 5.8 ;
MACRO fwd
  CLASS BLOCK ;
  SIZE 10 BY 10 ;
"""
    + "".join(
        f"""  PIN {pin}
    DIRECTION {direction} ;
    PORT
      LAYER met3 ; RECT {x} 0 {x + 0.5} 0.5 ;
    END
  END {pin}
"""
        for x, (pin, direction) in enumerate(
            (("clk_i", "INPUT"), ("d", "INPUT"), ("q", "OUTPUT"), ("clk_o", "OUTPUT"))
        )
    )
    + """END fwd
END LIBRARY
"""
)
_STA = """\
read_lef tech.lef
read_lef fwd.lef
read_liberty {lib}
read_verilog top.v
link_design top
create_clock -name clk -period 1000 [get_ports clk]
set_propagated_clock [all_clocks]
set_input_delay 100 -clock clk [get_ports d]
set_output_delay 100 -clock clk [get_ports q]
report_checks -to u1/d -format full_clock_expanded -digits 3
puts "RB-CLOCKS [lmap c [get_property [get_pins u1/clk_i] clocks] {{get_name $c}}]"
"""


def _time_chain(tmp_path, lib: Path) -> str:
    (tmp_path / "top.v").write_text(_TOP)
    (tmp_path / "tech.lef").write_text(_TECH_LEF)
    (tmp_path / "fwd.lef").write_text(_MACRO_LEF)
    script = tmp_path / f"{lib.stem}.tcl"
    script.write_text(_STA.format(lib=lib))
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
    return out


@needs_openroad
def test_the_second_block_of_a_chain_is_clocked_through_the_first(tmp_path):
    lib, second = _staged(tmp_path)
    written = _time_chain(tmp_path, lib)
    rewrite(str(lib), str(second), [_FORWARD])
    forwarded = _time_chain(tmp_path, lib)

    # As written, the clock stops at the first block.
    assert "No paths found." in written
    assert "RB-CLOCKS \n" in written or written.rstrip().endswith("RB-CLOCKS")
    # Rewritten, it reaches the second block's clk_i through the first's clk_o.
    assert "RB-CLOCKS clk" in forwarded
    assert re.search(r"\^ u0/clk_o \(fwd\)", forwarded)
    assert re.search(r"\^ u1/clk_i \(fwd\)", forwarded)
    assert "slack (MET)" in forwarded
