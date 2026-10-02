"""Liberty `time_unit` detection and its use in the synthesis and P&R metrics."""

import gzip
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rtl_buddy.runner.synth_results import SynthFailResults, SynthPassResults
from rtl_buddy.tools import openroad_corners, synth_openroad, synth_yosys
from rtl_buddy.tools.liberty_units import (
    DEFAULT_PS_PER_UNIT,
    LibertyTimeUnitError,
    liberty_time_unit_ps,
    open_liberty,
    time_unit_ps,
)
from rtl_buddy.tools.power_openroad import _liberty_cell_names

from test_multi_corner import _PNR_LOG_TAIL, _pdk, _platform, _pnr_backend_over
from test_synth import _make_openroad, _make_synth_cfg, _make_yosys


def _header(unit: str | None, cell: str = "INVx1") -> str:
    unit_line = f'  time_unit : "{unit}" ;\n' if unit is not None else ""
    return (
        "library (tiny) {\n"
        "  delay_model : table_lookup ;\n"
        f"{unit_line}"
        '  voltage_unit : "1V" ;\n'
        f"  cell ({cell}) {{\n    area : 1.0 ;\n  }}\n"
        "}\n"
    )


def _lib(path: Path, unit: str | None, *, gz: bool = False, cell="INVx1") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _header(unit, cell)
    if gz:
        with gzip.open(path, "wt") as f:
            f.write(text)
    else:
        path.write_text(text)
    return str(path)


# Detection


@pytest.mark.parametrize(
    ("unit", "ps"),
    [("1ns", 1000.0), ("1ps", 1.0), ("10ps", 10.0), ("100ps", 100.0)],
)
def test_time_unit_is_read_from_the_header(tmp_path, unit, ps):
    assert time_unit_ps(_lib(tmp_path / "c.lib", unit)) == ps


def test_unquoted_time_unit_is_read(tmp_path):
    lib = tmp_path / "c.lib"
    lib.write_text("library (l) {\n  time_unit : 1ps ;\n}\n")
    assert time_unit_ps(str(lib)) == 1.0


def test_gzipped_liberty_is_read(tmp_path):
    lib = _lib(tmp_path / "c.lib.gz", "1ps", gz=True)
    assert time_unit_ps(lib) == 1.0
    with open_liberty(lib) as f:
        assert "cell (INVx1)" in f.read()


def test_gzip_is_detected_by_content_not_name(tmp_path):
    assert time_unit_ps(_lib(tmp_path / "c.lib", "1ps", gz=True)) == 1.0


def test_no_time_unit_is_the_liberty_default(tmp_path):
    assert time_unit_ps(_lib(tmp_path / "c.lib", None)) == DEFAULT_PS_PER_UNIT


def test_time_unit_inside_a_cell_is_ignored(tmp_path):
    lib = tmp_path / "c.lib"
    lib.write_text('library (l) {\n  cell (A) {\n    time_unit : "1ps" ;\n  }\n}\n')
    assert time_unit_ps(str(lib)) == DEFAULT_PS_PER_UNIT


def test_library_time_unit_after_a_cell_is_read(tmp_path):
    lib = tmp_path / "c.lib"
    lib.write_text(
        "library (l) {\n  cell (A) {\n    area : 1.0 ; /* { */\n"
        '    pin (Y) { function : "{A}" ; }\n  }\n  time_unit : "1ps" ;\n}\n'
    )
    assert time_unit_ps(str(lib)) == 1.0


def test_unreadable_liberty_has_no_unit(tmp_path):
    assert time_unit_ps(str(tmp_path / "missing.lib")) is None


def test_time_unit_that_is_not_a_time_is_an_error(tmp_path):
    with pytest.raises(LibertyTimeUnitError, match="not a time"):
        time_unit_ps(_lib(tmp_path / "c.lib", "1V"))


def test_libraries_that_agree_give_their_unit(tmp_path):
    paths = [
        _lib(tmp_path / "a.lib.gz", "1ps", gz=True),
        _lib(tmp_path / "b.lib", "1ps"),
        str(tmp_path / "missing.lib"),
        "",
    ]
    assert liberty_time_unit_ps(paths) == 1.0


def test_no_readable_library_gives_the_default(tmp_path):
    assert liberty_time_unit_ps([]) == DEFAULT_PS_PER_UNIT
    assert liberty_time_unit_ps([str(tmp_path / "x.lib")]) == DEFAULT_PS_PER_UNIT


def test_libraries_that_disagree_are_an_error(tmp_path):
    ns = _lib(tmp_path / "ns.lib", "1ns")
    ps = _lib(tmp_path / "ps.lib.gz", "1ps", gz=True)
    with pytest.raises(LibertyTimeUnitError) as err:
        liberty_time_unit_ps([ns, ps])
    assert err.value.units == {ns: "1ns", ps: "1ps"}
    assert ns in str(err.value) and ps in str(err.value)


# Yosys backend: ABC -D and the multi-clock warning


def _yosys_with_lib(tmp_path, lib_paths, sdc_text):
    sdc = tmp_path / "c.sdc"
    sdc.write_text(sdc_text)
    return _make_yosys(
        tmp_path,
        synth_cfg=_make_synth_cfg(lib_paths=lib_paths, constraints=str(sdc)),
    )


def _script_for(tmp_path, ys) -> str:
    sv = tmp_path / "top.sv"
    sv.write_text("")
    fl = tmp_path / "synth.f"
    fl.write_text(f"-v {sv}\n")
    return Path(ys._write_script(str(fl))).read_text()


@pytest.mark.parametrize(
    ("unit", "period", "d"),
    [("1ns", "1.25", 1250), ("1ps", "1250", 1250), ("100ps", "12.5", 1250)],
)
def test_abc_delay_target_is_the_period_in_ps(tmp_path, unit, period, d):
    lib = _lib(tmp_path / "c.lib", unit)
    ys = _yosys_with_lib(
        tmp_path, [lib], f"create_clock -period {period} [get_ports clk]\n"
    )
    assert f"abc -liberty {lib} -D {d} " in _script_for(tmp_path, ys)


def test_abc_delay_target_reads_a_gzipped_liberty(tmp_path):
    lib = _lib(tmp_path / "c.lib.gz", "1ps", gz=True)
    ys = _yosys_with_lib(tmp_path, [lib], "create_clock -period 800 [get_ports c]\n")
    script = _script_for(tmp_path, ys)
    assert f"read_liberty -lib {lib}" in script
    assert f"abc -liberty {lib} -D 800 " in script


def test_multi_clock_warning_reports_ns_on_a_ps_library(tmp_path, caplog):
    lib = _lib(tmp_path / "c.lib", "1ps")
    ys = _yosys_with_lib(
        tmp_path,
        [lib],
        "create_clock -period 2000 [get_ports a]\n"
        "create_clock -period 500 [get_ports b]\n",
    )
    with caplog.at_level(logging.WARNING):
        assert ys._parse_clock_period_ps(str(tmp_path / "c.sdc")) == 500
    (warning,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "synth.sdc_multi_clock"
    ]
    assert warning.rtl_fields["periods_ns"] == [2.0, 0.5]
    assert warning.rtl_fields["used_ns"] == 0.5


def test_yosys_run_fails_at_setup_on_mixed_units(tmp_path, monkeypatch, caplog):
    libs = [_lib(tmp_path / "a.lib", "1ns"), _lib(tmp_path / "b.lib", "1ps")]
    ys = _yosys_with_lib(tmp_path, libs, "create_clock -period 1 [get_ports c]\n")
    monkeypatch.setattr(
        synth_yosys,
        "run_managed_process",
        MagicMock(side_effect=AssertionError("yosys must not start")),
    )
    with caplog.at_level(logging.ERROR):
        res = ys.run()
    assert isinstance(res, SynthFailResults)
    assert res.results["fail_stage"] == "setup"
    assert "disagree on time_unit" in res.results["desc"]
    assert any(
        getattr(r, "rtl_event", None) == "synth.liberty_time_unit_error"
        for r in caplog.records
    )


# OpenROAD synthesis backend: WNS and TNS


@pytest.mark.parametrize(("unit", "scale"), [("1ns", 1000.0), ("1ps", 1.0)])
def test_openroad_synth_scales_slack_by_the_liberty_unit(
    tmp_path, monkeypatch, unit, scale
):
    lib = _lib(tmp_path / "c.lib.gz", unit, gz=True)
    or_synth = _make_openroad(tmp_path, synth_cfg=_make_synth_cfg(lib_paths=[lib]))
    monkeypatch.setattr(
        or_synth, "_write_or_script", lambda *_a: or_synth._or_script_path()
    )
    monkeypatch.setattr(or_synth, "_publish_phys_model", lambda **_kw: None)

    def _fake_run(_cmd, stdout, **_kw):
        stdout.write("Design area 10 um^2\nworst slack max -0.05\ntns max -1.5\n")
        return MagicMock(returncode=0)

    monkeypatch.setattr(synth_openroad.subprocess, "run", _fake_run)
    res = or_synth._run_or_stage(4, [], [lib])
    assert isinstance(res, SynthPassResults)
    assert res.results["wns_ps"] == pytest.approx(-0.05 * scale)
    assert res.results["tns_ps"] == pytest.approx(-1.5 * scale)


def test_openroad_synth_run_fails_at_setup_on_mixed_units(tmp_path):
    libs = [
        _lib(tmp_path / "a.lib", "1ns"),
        _lib(tmp_path / "b.lib.gz", "1ps", gz=True),
    ]
    or_synth = _make_openroad(tmp_path, synth_cfg=_make_synth_cfg(lib_paths=libs))
    res = or_synth.run()
    assert isinstance(res, SynthFailResults)
    assert res.results["fail_stage"] == "setup"
    assert "disagree on time_unit" in res.results["desc"]


def test_liberty_master_scan_reads_gzipped_liberty(tmp_path):
    lib = _lib(tmp_path / "m.lib.gz", "1ps", gz=True, cell="sram_macro")
    or_synth = _make_openroad(tmp_path)
    assert or_synth._masters_from_lef_and_liberty([], [lib]) == {"sram_macro"}


def test_power_cell_scan_reads_gzipped_liberty(tmp_path):
    lib = _lib(tmp_path / "m.lib.gz", "1ps", gz=True, cell="sram_macro")
    assert _liberty_cell_names([lib]) == {"sram_macro"}


# P&R


def _write_corner_libs(tmp_path, units: dict[str, str]):
    for corner, unit in units.items():
        _lib(tmp_path / "pdk" / "lib" / f"{corner}.lib", unit)


def test_parse_corner_timing_scales_by_the_unit():
    per_corner = openroad_corners.parse_corner_timing(
        "corner tt worst slack max -12.5\ncorner tt tns max -40\n", ["tt"], 1.0
    )
    assert per_corner == {"tt": {"wns_setup_ps": -12.5, "tns_ps": -40.0}}


def test_pnr_metrics_on_an_ns_platform_are_unchanged(tmp_path, monkeypatch):
    _write_corner_libs(tmp_path, {"tt": "1ns", "ss": "1ns", "ff": "1ns"})
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL).run()
    assert res.results["wns_setup_ps"] == 9.46 * 1000.0
    assert res.results["wns_hold_ps"] == 0.40 * 1000.0
    assert res.results["corners"]["tt"]["wns_setup_ps"] == 13.59 * 1000.0


def test_pnr_metrics_on_a_ps_platform_are_not_scaled(tmp_path, monkeypatch):
    _write_corner_libs(tmp_path, {"tt": "1ps", "ss": "1ps", "ff": "1ps"})
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL).run()
    assert res.results["result"] == "PASS"
    assert res.results["wns_setup_ps"] == pytest.approx(9.46)
    assert res.results["wns_hold_ps"] == pytest.approx(0.40)
    assert res.results["tns_ps"] == pytest.approx(0.0)
    assert res.results["corners"]["ss"]["wns_hold_ps"] == pytest.approx(2.22)


def test_pnr_run_fails_at_setup_when_corners_disagree(tmp_path, monkeypatch, caplog):
    _write_corner_libs(tmp_path, {"tt": "1ps", "ss": "1ns", "ff": "1ps"})
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    backend = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL)
    monkeypatch.setattr(
        "rtl_buddy.tools.pnr_openroad.subprocess.run",
        MagicMock(side_effect=AssertionError("openroad must not start")),
    )
    with caplog.at_level(logging.ERROR):
        res = backend.run()
    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "disagree on time_unit" in res.results["desc"]
    (event,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.liberty_time_unit_error"
    ]
    assert set(event.rtl_fields["units"].values()) == {"1ps", "1ns"}
