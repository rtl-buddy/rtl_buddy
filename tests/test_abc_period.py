"""ABC's `-D` delay target from the SDC, on both synthesis backends (#788)."""

import logging
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.synth import SynthConfig, SynthToolConfig, SynthToolConfigFile
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth, sdc_clock_periods

from test_split_liberty import _Root, _ns_lib

BACKENDS = ["yosys", "openroad"]


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


def _synth(tmp_path, backend, sdc: Path | None, *, abc_script=None):
    sv = tmp_path / "top.sv"
    sv.write_text("module top; endmodule\n")
    fl = tmp_path / "synth.f"
    fl.write_text(f"{sv}\n")
    lib = _ns_lib(tmp_path / "cells.lib")
    overrides = {backend: {"abc_script": abc_script}} if abc_script else None
    cfg = SynthConfig(
        name=f"g_{backend}",
        desc="g",
        model=ModelConfig(name="top", filelist=[], path=str(tmp_path / "models.yaml")),
        tool=backend,
        constraints=str(sdc) if sdc else None,
        params=None,
        defines=None,
        platform="p",
        _reglvl=None,
        tool_overrides=overrides,
        lib_paths=[],
    )
    kwargs = dict(
        name=f"t/{backend}",
        synth_cfg=cfg,
        tool_cfg=SynthToolConfig(SynthToolConfigFile(name=backend, tool=backend)),
        suite_dir=str(tmp_path),
        root_cfg=_Root([lib]),
    )
    if backend == "yosys":
        synth = YosysSynth(**kwargs)
        return synth, Path(synth._write_script(str(fl))).read_text()
    synth = OpenRoadSynth(**kwargs)
    return synth, Path(synth._write_yosys_script(str(fl))).read_text()


def _abc_line(script: str) -> str:
    return next(ln for ln in script.splitlines() if ln.startswith("abc "))


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_literal_clock_gives_abc_its_period(tmp_path, backend):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 10.0 [get_ports clk]\n")
    synth, script = _synth(
        tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
    )
    assert " -D 10000 -script " in _abc_line(script)
    kwargs = {"mapped": True} if backend == "yosys" else {}
    assert synth._phys_options(**kwargs)["abc_period_ps"] == 10000


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_clock_in_a_sourced_sdc_gives_abc_its_period(tmp_path, backend, tcl_backend):
    (tmp_path / "clocks.sdc").write_text(
        "create_clock -name clk -period 10.0 [get_ports clk]\n"
    )
    sdc = tmp_path / "top.sdc"
    sdc.write_text(
        "source [file join [file dirname [file normalize [info script]]] clocks.sdc]\n"
    )
    _synth_obj, script = _synth(tmp_path, backend, sdc)
    assert " -D 10000 -script " in _abc_line(script)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_literal_relative_source_is_followed_by_either_reader(
    tmp_path, backend, constraint_backend
):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "clocks.sdc").write_text("create_clock -period 4 clk\n")
    sdc = tmp_path / "top.sdc"
    sdc.write_text("source sub/clocks.sdc\n")
    _synth_obj, script = _synth(tmp_path, backend, sdc)
    assert " -D 4000 -script " in _abc_line(script)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_clock_behind_a_proc_over_get_ports_gives_abc_its_period(
    tmp_path, backend, caplog, tcl_backend
):
    sdc = tmp_path / "top.sdc"
    sdc.write_text(
        "proc make_clocks {} {\n"
        "  foreach p [get_ports {clk_a clk_b}] {\n"
        "    create_clock -name $p -period 5 [get_ports $p]\n"
        "  }\n"
        "}\n"
        "make_clocks\n"
    )
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(tmp_path, backend, sdc)
    assert " -D 5000 -script " in _abc_line(script)
    # The retry reads the file; no fallback warning.
    assert not _events(caplog, "constraints.tcl_error")


_LOOP_SDC = (
    "array set periods {clk_a 2000 clk_b 4000}\n"
    "foreach c [array names periods] {"
    " create_clock -name $c -period $periods($c) [get_ports $c] }\n"
)
_VARIABLE_SDC = "set p 2000\ncreate_clock -name clk -period $p [get_ports clk]\n"
#: A sourced helper that checks port directions, which the reader cannot answer.
_HELPER_TCL = (
    "proc window {kind clock max patterns} {\n"
    "  foreach p [get_ports $patterns] {\n"
    "    if {[get_property $p direction] ne $kind} {\n"
    '      return -code error "port [get_full_name $p] is not an $kind"\n'
    "    }\n"
    "  }\n"
    "  set_input_delay -clock $clock -max $max [get_ports $patterns]\n"
    "}\n"
)
_SOURCED_HELPER_SDC = (
    "source [file join [file dirname [info script]] helpers.tcl]\n"
    "set clk_period 2000\n"
    "create_clock -name clk -period $clk_period [get_ports clk]\n"
    "window input clk 500 {a b}\n"
)
_FORMS = {
    "loop": _LOOP_SDC,
    "variable": _VARIABLE_SDC,
    "sourced-helper": _SOURCED_HELPER_SDC,
}


def _write_form(tmp_path, form):
    (tmp_path / "helpers.tcl").write_text(_HELPER_TCL)
    sdc = tmp_path / "top.sdc"
    sdc.write_text(_FORMS[form])
    return sdc


@pytest.mark.parametrize("form", list(_FORMS))
@pytest.mark.parametrize("backend", BACKENDS)
def test_a_computed_clock_gives_abc_its_period(
    tmp_path, backend, form, caplog, tcl_backend
):
    sdc = _write_form(tmp_path, form)
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(
            tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
        )
    # The ns Liberty takes the SDC in ns, so 2000 is 2,000,000 ps.
    assert " -D 2000000 -script " in _abc_line(script)
    assert not _events(caplog, "constraints.tcl_error")
    assert not _events(caplog, "synth.sdc_no_clock")


@pytest.mark.parametrize(
    "form, unresolved, per_clock",
    [
        ("loop", 1, "synth.sdc_clock_unread"),
        ("variable", 1, "synth.sdc_period_unevaluated"),
        ("sourced-helper", 1, "synth.sdc_period_unevaluated"),
    ],
)
@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unresolved_clock_is_named_and_not_called_missing(
    tmp_path, backend, form, unresolved, per_clock, caplog, tokenizer_backend
):
    sdc = _write_form(tmp_path, form)
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(
            tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
        )
    assert " -D " not in _abc_line(script)
    [clock] = _events(caplog, per_clock)
    assert clock.rtl_fields["line"] in (2, 3)
    [rec] = _events(caplog, "synth.sdc_no_clock")
    assert rec.rtl_fields["unresolved"] == unresolved
    message = rec.getMessage()
    assert "could not resolve" in message
    assert "no create_clock period found" not in message
    assert "{D}" in message


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unread_clock_says_where_it_is(tmp_path, backend, caplog, tokenizer_backend):
    sdc = _write_form(tmp_path, "loop")
    with caplog.at_level(logging.WARNING):
        _synth(tmp_path, backend, sdc, abc_script="strash; map")
    [rec] = _events(caplog, "synth.sdc_clock_unread")
    assert rec.rtl_fields["sdc"] == str(sdc)
    assert rec.rtl_fields["line"] == 2
    assert rec.rtl_fields["enclosing"] == "foreach"
    assert "`foreach`" in rec.getMessage()
    [no_clock] = _events(caplog, "synth.sdc_no_clock")
    assert "abc runs unconstrained" in no_clock.getMessage()


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_period_that_is_not_a_number_is_unresolved(tmp_path, backend, caplog):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("set p fast\ncreate_clock -name c -period $p [get_ports clk]\n")
    with caplog.at_level(logging.WARNING):
        _synth(tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put")
    [clock] = _events(caplog, "synth.sdc_period_unevaluated")
    assert clock.rtl_fields["value"] == "fast"
    assert clock.rtl_fields["line"] == 2
    [rec] = _events(caplog, "synth.sdc_no_clock")
    assert rec.rtl_fields["unresolved"] == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_period_with_d_in_the_script_warns_loudly(tmp_path, backend, caplog):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("set_input_delay 1 [get_ports a]\n")
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(
            tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
        )
    assert " -D " not in _abc_line(script)
    [rec] = _events(caplog, "synth.sdc_no_clock")
    assert rec.levelno == logging.WARNING
    assert rec.rtl_fields["abc_delay_target"] is True
    assert rec.rtl_fields["unresolved"] == 0
    assert rec.getMessage() == (
        f'no create_clock period found in SDC "{sdc}"; '
        "abc-script uses {D}, so ABC maps with no delay target"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_period_without_d_in_the_script_says_so(tmp_path, backend, caplog):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("set_input_delay 1 [get_ports a]\n")
    with caplog.at_level(logging.WARNING):
        _synth(tmp_path, backend, sdc, abc_script="strash; map")
    [rec] = _events(caplog, "synth.sdc_no_clock")
    assert rec.rtl_fields["abc_delay_target"] is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_sdc_passes_no_period_and_does_not_warn(tmp_path, backend, caplog):
    with caplog.at_level(logging.WARNING):
        synth, script = _synth(tmp_path, backend, None)
    assert " -D " not in _abc_line(script)
    assert not _events(caplog, "synth.sdc_no_clock")


def test_a_sourced_sdc_reads_the_sourcing_sdcs_periods(
    tmp_path, monkeypatch, tcl_backend
):
    # The #956 example, read as `rb synth` reads it.
    (tmp_path / "top.sdc").write_text("array set per { clk 10.0 }\nsource inc.sdc\n")
    (tmp_path / "inc.sdc").write_text(
        "create_clock -name clk -period $per(clk) [get_ports clk]\n"
    )
    monkeypatch.chdir(tmp_path)
    assert sdc_clock_periods("top.sdc", lambda: 1000.0, "s") == (10000, 0)


#: A platform SDC that sets per-clock periods and sources the shared SDC that creates the clocks.
_SPLIT_TOP = (
    "set per(clk_a) 2000\nset per(clk_b) 4000\n"
    "source [file join [file dirname [file normalize [info script]]] clocks.sdc]\n"
)
_SPLIT_CLOCKS = (
    "foreach c {clk_a clk_b} {\n"
    "  create_clock -name $c -period $per($c) [get_ports $c]\n"
    "}\n"
)
#: The #946 example.
_PERIOD_HELPER_SDC = (
    "proc helper {clk} {\n"
    "  set p [get_property [get_clocks $clk] period]\n"
    "  return [expr {$p - 100}]\n"
    "}\n"
    "set period 2000\n"
    "foreach c {a b} {\n"
    "  create_clock -name ${c}_clk -period $period [get_ports ${c}_clk]\n"
    "}\n"
    "helper a_clk\n"
)


@pytest.mark.parametrize("form", ["split", "period-helper"])
@pytest.mark.parametrize("backend", BACKENDS)
def test_a_split_or_helper_sdc_gives_abc_its_period(
    tmp_path, backend, form, caplog, tcl_backend
):
    sdc = tmp_path / "top.sdc"
    if form == "split":
        (tmp_path / "clocks.sdc").write_text(_SPLIT_CLOCKS)
        sdc.write_text(_SPLIT_TOP)
    else:
        sdc.write_text(_PERIOD_HELPER_SDC)
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(
            tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
        )
    assert " -D 2000000 -script " in _abc_line(script)
    assert [r.rtl_event for r in caplog.records if r.levelno >= logging.WARNING] == [
        "synth.sdc_multi_clock"
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_clock_in_a_skipped_statement_is_named_and_unresolved(
    tmp_path, backend, caplog, tcl_backend
):
    sdc = tmp_path / "c.sdc"
    sdc.write_text(
        "set_load 1 [get_ports a]\n"
        "create_clock -name gen -period [expr {2 * [get_property [get_clocks nope] period]}] g\n"
    )
    with caplog.at_level(logging.WARNING):
        _synth_obj, script = _synth(
            tmp_path, backend, sdc, abc_script="strash; &get -n; &nf {D}; &put"
        )
    assert " -D " not in _abc_line(script)
    [skipped] = _events(caplog, "constraints.tcl_statement_skipped")
    assert skipped.rtl_fields["line"] == 2
    [clock] = _events(caplog, "synth.sdc_clock_unread")
    assert clock.rtl_fields["line"] == 2
    assert clock.rtl_fields["error"] == skipped.rtl_fields["message"]
    assert "statement the Tcl reader skipped" in clock.getMessage()
    assert "tokenizer" not in clock.getMessage()
    [rec] = _events(caplog, "synth.sdc_no_clock")
    assert rec.rtl_fields["unresolved"] == 1
    assert "could not resolve" in rec.getMessage()
    assert not _events(caplog, "constraints.tcl_error")


def test_a_clock_created_before_its_loop_failed_is_not_unresolved(
    tmp_path, caplog, tcl_backend
):
    sdc = tmp_path / "c.sdc"
    sdc.write_text(
        "foreach c {clk} {\n"
        "  create_clock -name $c -period 2 [get_ports $c]\n"
        "  set_load [expr {[get_property [get_ports $c] cap] * 2}] $c\n"
        "}\n"
    )
    with caplog.at_level(logging.WARNING):
        assert sdc_clock_periods(str(sdc), lambda: 1000.0, "s") == (2000, 0)
    assert [r.rtl_event for r in caplog.records if r.levelno >= logging.WARNING] == [
        "constraints.tcl_statement_skipped"
    ]
