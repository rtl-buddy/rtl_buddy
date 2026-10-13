"""ABC's `-D` delay target from the SDC, on both synthesis backends (#788)."""

import logging
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.synth import SynthConfig, SynthToolConfig, SynthToolConfigFile
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth

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
    assert "{D}" in rec.getMessage()


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
