"""Multi-corner signoff shared by `rb pnr` and `rb power` (#104, #105).

The schema (`cfg-pnr-platforms.corners`), the Tcl both flows render for it,
and the parsing of what OpenROAD prints back. The log and report fixtures
are real OpenROAD 26Q2 output, captured from the project template's
`demo_tiny_alu_subsys_sky130_compute_mc_pnr` / `_mc_power` runs on sky130hd
at tt, ss_n40C_1v40 and ff_n40C_1v95.
"""

from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import openroad_corners

from test_pnr import _make_pnr_cfg, _render_flow
from test_power import _FakePlatform, _make_power_backend


def _pdk(tmp_path, corners=None):
    return PdkConfig(
        PdkConfigFile(
            name="sky130hd",
            site="unithd",
            corners=corners
            or {
                "tt": "pdk/lib/tt.lib",
                "ss": "pdk/lib/ss.lib",
                "ff": "pdk/lib/ff.lib",
            },
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
        ),
        str(tmp_path / "root_config.yaml"),
    )


def _platform(pdk, **overrides):
    base = dict(name="sky130hd_mc", pdk="sky130hd", cts_buffer="clkbuf_4")
    base.update(overrides)
    return PnrPlatformConfig(PnrPlatformConfigFile(**base), lambda _name: pdk)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_corners_list_selects_every_corner_primary_first(tmp_path):
    platform = _platform(_pdk(tmp_path), sta_corners=["ss", "tt", "ff"])

    assert platform.is_multi_corner()
    assert platform.get_sta_corners() == ["ss", "tt", "ff"]
    # The primary is the first entry, and the single-corner accessors
    # answer for it — so a caller written for one corner keeps working.
    assert platform.get_sta_corner() == "ss"
    assert platform.get_sta_lib_path() == str(tmp_path / "pdk/lib/ss.lib")
    assert list(platform.get_sta_corner_lib_paths().items()) == [
        ("ss", str(tmp_path / "pdk/lib/ss.lib")),
        ("tt", str(tmp_path / "pdk/lib/tt.lib")),
        ("ff", str(tmp_path / "pdk/lib/ff.lib")),
    ]


def test_neither_key_keeps_the_single_default_corner(tmp_path):
    platform = _platform(_pdk(tmp_path))

    assert not platform.is_multi_corner()
    assert platform.get_sta_corners() == ["tt"]
    assert platform.get_sta_corner() == "tt"


def test_corners_is_spelled_corners_in_yaml(tmp_path):
    from serde.yaml import from_yaml

    platform_file = from_yaml(
        PnrPlatformConfigFile,
        dedent("""\
            name: "sky130hd_mc"
            pdk: "sky130hd"
            corners: ["tt", "ss", "ff"]
        """),
    )
    platform = PnrPlatformConfig(platform_file, lambda _name: _pdk(tmp_path))
    assert platform.get_sta_corners() == ["tt", "ss", "ff"]


def test_corners_rejects_a_corner_the_pdk_does_not_declare(tmp_path):
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _platform(_pdk(tmp_path), sta_corners=["tt", "sf"])
    message = str(excinfo.value)
    assert "pnr platform 'sky130hd_mc'" in message
    assert "has no corner 'sf'" in message
    assert "available: ['tt', 'ss', 'ff']" in message


def test_corner_and_corners_are_mutually_exclusive(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="either 'corner' or 'corners'"):
        _platform(_pdk(tmp_path), sta_corner="tt", sta_corners=["tt", "ss"])


def test_an_empty_corners_list_is_an_error_not_a_default(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="at least one corner"):
        _platform(_pdk(tmp_path), sta_corners=[])


def test_corners_rejects_a_corner_listed_twice(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="'ss' is listed twice"):
        _platform(_pdk(tmp_path), sta_corners=["ss", "tt", "ss"])


def test_corners_rejects_a_name_tcl_or_a_file_name_cannot_carry(tmp_path):
    """A single `corner:` is only a key into `cfg-pdks.corners`; one of
    several becomes a scene name, a Tcl list word and a report file name."""
    pdk = _pdk(tmp_path, {"tt": "tt.lib", "slow corner": "ss.lib"})
    with pytest.raises(FatalRtlBuddyError, match="corner name 'slow corner'"):
        _platform(pdk, sta_corners=["tt", "slow corner"])
    # ...and the same name stays usable as the one corner of a platform.
    assert _platform(pdk, sta_corner="slow corner").get_sta_corner() == "slow corner"


def test_a_one_entry_corners_list_is_the_single_corner_run(tmp_path):
    platform = _platform(_pdk(tmp_path), sta_corners=["ss"])

    assert not platform.is_multi_corner()
    assert platform.get_sta_corner() == "ss"


# ---------------------------------------------------------------------------
# rb pnr — Tcl
# ---------------------------------------------------------------------------


def _write_macro(tmp_path):
    lib = tmp_path / "macro" / "sram.lib"
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text("")
    return str(lib)


def test_pnr_single_corner_script_reads_one_liberty_and_reports_no_corners(
    tmp_path,
):
    text = _render_flow(tmp_path, _platform(_pdk(tmp_path)))

    assert "read_liberty $LIBERTY\n" in text
    assert "define_corners" not in text
    assert "report_tns\nreport_checks" in text
    assert "Per-corner" not in text


def test_pnr_one_entry_corners_renders_byte_identically_to_corner(tmp_path):
    single = _render_flow(tmp_path, _platform(_pdk(tmp_path), sta_corner="ss"))
    listed = _render_flow(tmp_path, _platform(_pdk(tmp_path), sta_corners=["ss"]))
    assert listed == single


def test_pnr_multi_corner_script_defines_every_corner_before_reading(tmp_path):
    macro = _write_macro(tmp_path)
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    text = _render_flow(tmp_path, platform, lib_paths=[macro])

    lines = text.splitlines()
    define = lines.index("define_corners tt ss ff")
    reads = [ln for ln in lines if ln.startswith("read_liberty")]
    assert reads == [
        f"read_liberty -corner tt {tmp_path / 'pdk/lib/tt.lib'}",
        f"read_liberty -corner ss {tmp_path / 'pdk/lib/ss.lib'}",
        f"read_liberty -corner ff {tmp_path / 'pdk/lib/ff.lib'}",
        # A single-Liberty macro is read into every corner.
        f"read_liberty -corner tt {macro}",
        f"read_liberty -corner ss {macro}",
        f"read_liberty -corner ff {macro}",
    ]
    assert define < lines.index(reads[0])
    assert "read_liberty $LIBERTY" not in text
    assert "{{" not in text


def test_pnr_multi_corner_reports_follow_the_global_worst(tmp_path):
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    text = _render_flow(tmp_path, platform)

    # The global reports stay as they were: they are the worst across
    # corners and the scalar results are parsed from them.
    assert "report_worst_slack -max\nreport_worst_slack -min\nreport_tns\n" in text
    block = text.index('puts ">>> Per-corner timing"')
    assert text.index("report_tns\n") < block < text.index("report_checks")
    for corner in ("tt", "ss", "ff"):
        assert f"catch {{rb_report_corner_timing {corner}}} rb_err" in text
    # Both OpenSTA spellings, 3.0 first.
    assert "sta::worst_slack_scene $corner max" in text
    assert "sta::worst_slack_corner $corner max" in text


# ---------------------------------------------------------------------------
# rb pnr — log parsing and results
# ---------------------------------------------------------------------------

# The final-reports tail of a real multi-corner `pnr.log` (see the module
# docstring for the run): ss sets the setup worst, ff the hold worst.
_PNR_LOG_TAIL = """\
>>> Final reports
Design area 2580 um^2 43% utilization.
worst slack max 9.46
worst slack min 0.40
tns max 0.00
>>> Per-corner timing
corner tt worst slack max 13.59
corner tt worst slack min 0.64
corner tt tns max 0.00
corner ss worst slack max 9.46
corner ss worst slack min 2.22
corner ss tns max 0.00
corner ff worst slack max 14.06
corner ff worst slack min 0.40
corner ff tns max 0.00
>>> Write outputs
"""


def test_parse_corner_timing_reads_every_corner_in_config_order():
    per_corner = openroad_corners.parse_corner_timing(_PNR_LOG_TAIL, ["tt", "ss", "ff"])

    assert list(per_corner) == ["tt", "ss", "ff"]
    assert per_corner["ss"] == pytest.approx(
        {"wns_setup_ps": 9460.0, "wns_hold_ps": 2220.0, "tns_ps": 0.0}
    )
    assert openroad_corners.worst_corner(per_corner, "wns_setup_ps") == "ss"
    assert openroad_corners.worst_corner(per_corner, "wns_hold_ps") == "ff"


def test_parse_corner_timing_leaves_out_what_the_log_does_not_say():
    per_corner = openroad_corners.parse_corner_timing(
        "corner tt worst slack max 1.00\ncorner xx worst slack max -9.00\n",
        ["tt", "ss"],
    )
    assert per_corner == {"tt": {"wns_setup_ps": 1000.0}, "ss": {}}
    assert openroad_corners.worst_corner(per_corner, "wns_hold_ps") is None


def _pnr_backend_over(tmp_path, monkeypatch, platform, log_text):
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    (tmp_path / "models.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: model_config
        models:
          - name: "demo_top"
            filelist: []
        """)
    )
    (tmp_path / "synth.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: synth_config
        syntheses:
          - name: "demo_synth"
            desc: "demo"
            model: "demo_top"
            model_path: "models.yaml"
            tool: "openroad"
            reglvl: 0
        """)
    )
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)

    def _fake_run(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text(log_text)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)
    return backend


def test_multi_corner_pnr_result_keeps_the_worst_and_adds_each_corner(
    tmp_path, monkeypatch
):
    platform = _platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL).run()

    results = res.results
    assert results["result"] == "PASS"
    # The scalars every summary, gate and xfail reads: the global worst.
    assert results["wns_setup_ps"] == pytest.approx(9460.0)
    assert results["wns_hold_ps"] == pytest.approx(400.0)
    assert results["worst_setup_corner"] == "ss"
    assert results["worst_hold_corner"] == "ff"
    assert list(results["corners"]) == ["tt", "ss", "ff"]
    assert results["corners"]["tt"]["wns_setup_ps"] == pytest.approx(13590.0)


def test_single_corner_pnr_result_carries_no_corner_fields(tmp_path, monkeypatch):
    platform = _platform(_pdk(tmp_path))
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL).run()

    assert res.results["wns_setup_ps"] == pytest.approx(9460.0)
    for key in ("corners", "worst_setup_corner", "worst_hold_corner"):
        assert key not in res.results


def test_pnr_row_and_table_surface_the_worst_corners():
    from rtl_buddy.rtl_buddy import RtlBuddy, _pnr_worst_corner_cell
    from rtl_buddy.runner.pnr_results import PnrPassResults

    corners = {"ss": {"wns_setup_ps": 12640.0}, "ff": {"wns_hold_ps": 190.0}}
    results = PnrPassResults(
        name="demo/results",
        wns_setup_ps=12640.0,
        wns_hold_ps=190.0,
        fields={
            "corners": corners,
            "worst_setup_corner": "ss",
            "worst_hold_corner": "ff",
        },
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["corners"] == corners
    assert row["worst_setup_corner"] == "ss"
    assert _pnr_worst_corner_cell(results.results) == "setup ss / hold ff"
    assert _pnr_worst_corner_cell({"wns_setup_ps": 1.0}) == "-"


# ---------------------------------------------------------------------------
# rb power
# ---------------------------------------------------------------------------

_CORNERS = {
    "tt": "/pdk/fake/tt.lib",
    "ss": "/pdk/fake/ss.lib",
    "ff": "/pdk/fake/ff.lib",
}


def _mc_power_backend(tmp_path):
    return _make_power_backend(
        tmp_path, platform=_FakePlatform(liberty=_CORNERS["tt"], corners=_CORNERS)
    )


def _report(internal, switching, leakage, total):
    """A `report_power` design table, in OpenSTA's own layout."""
    return (
        "Group                  Internal  Switching    Leakage      Total\n"
        "                          Power      Power      Power      Power (Watts)\n"
        "----------------------------------------------------------------\n"
        "Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%\n"
        "----------------------------------------------------------------\n"
        f"Total                  {internal}   {switching}   {leakage}   {total} 100.0%\n"
    )


# Real per-corner totals from the template run (see the module docstring).
_CORNER_REPORTS = {
    "tt": _report("9.93e-05", "8.23e-05", "1.03e-09", "1.82e-04"),
    "ss": _report("4.62e-05", "4.61e-05", "1.05e-10", "9.23e-05"),
    "ff": _report("1.13e-04", "9.76e-05", "1.67e-09", "2.10e-04"),
}


def test_single_corner_power_script_is_unchanged(tmp_path):
    text = Path(_make_power_backend(tmp_path)._write_script()).read_text()

    assert "read_liberty /pdk/fake/nangate45_typ.lib\n" in text
    assert "define_corners" not in text
    assert "-corner" not in text


def test_multi_corner_power_script_reports_each_corner_then_the_worst(tmp_path):
    backend = _mc_power_backend(tmp_path)
    macro = tmp_path / "sram.lib"
    macro.write_text("")
    inputs = backend._resolve_inputs()
    backend._resolve_inputs = lambda: {**inputs, "macro_libs": [str(macro)]}
    lines = Path(backend._write_script()).read_text().splitlines()

    assert lines[1] == "define_corners tt ss ff"
    assert lines[2:8] == [
        "read_liberty -corner tt /pdk/fake/tt.lib",
        "read_liberty -corner ss /pdk/fake/ss.lib",
        "read_liberty -corner ff /pdk/fake/ff.lib",
        f"read_liberty -corner tt {macro}",
        f"read_liberty -corner ss {macro}",
        f"read_liberty -corner ff {macro}",
    ]
    for corner in ("tt", "ss", "ff"):
        report = backend._corner_report_path(corner)
        assert f"report_power -corner {corner} > {report}" in lines
    assert f'puts "{openroad_corners.POWER_CORNER_MARKER} $rb_power_corner"' in lines
    assert f"report_power -corner $rb_power_corner > {backend._report_path()}" in lines
    # The per-instance breakdown is of the same (worst) corner.
    assert any(
        "report_power -instances $rb_insts -corner $rb_power_corner" in ln
        for ln in lines
    )


def _run_mc_power(tmp_path, monkeypatch, *, reports, worst="ff"):
    from rtl_buddy.tools import power_openroad

    backend = _mc_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        log = f"{openroad_corners.POWER_CORNER_MARKER} {worst}\n" if worst else ""
        Path(cmd[cmd.index("-log") + 1]).write_text(log)
        for corner, text in reports.items():
            Path(backend._corner_report_path(corner)).write_text(text)
        # `power.rpt` is the worst corner's; without a marker, say ff's.
        Path(backend._report_path()).write_text(reports[worst or "ff"])
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    return backend, backend.run()


def test_multi_corner_power_reports_the_worst_corner_and_each_corner(
    tmp_path, monkeypatch
):
    _, res = _run_mc_power(tmp_path, monkeypatch, reports=_CORNER_REPORTS)

    results = res.results
    assert results["result"] == "PASS"
    assert results["worst_corner"] == "ff"
    assert results["total_w"] == pytest.approx(2.10e-04)
    assert list(results["corners"]) == ["tt", "ss", "ff"]
    assert results["corners"]["ss"]["total_w"] == pytest.approx(9.23e-05)
    assert results["corners"]["tt"]["leakage_w"] == pytest.approx(1.03e-09)


def test_multi_corner_power_fails_when_a_corner_report_is_missing(
    tmp_path, monkeypatch
):
    reports = {k: v for k, v in _CORNER_REPORTS.items() if k != "ss"}
    backend, res = _run_mc_power(tmp_path, monkeypatch, reports=reports)

    assert res.results["result"] == "FAIL"
    assert "corner 'ss'" in res.results["desc"]
    # Nothing this run wrote is left to answer for the next one.
    assert not Path(backend._report_path()).exists()
    assert not Path(backend._corner_report_path("tt")).exists()


def test_multi_corner_power_fails_without_the_worst_corner_marker(
    tmp_path, monkeypatch
):
    _, res = _run_mc_power(tmp_path, monkeypatch, reports=_CORNER_REPORTS, worst=None)
    assert res.results["result"] == "FAIL"
    assert "worst power corner" in res.results["desc"]


def test_a_previous_runs_corner_report_is_cleared(tmp_path, monkeypatch):
    """A corner dropped from the platform must not leave its report behind
    to be read as this run's (#469)."""
    backend = _mc_power_backend(tmp_path)
    stale = Path(backend.artefact_dir) / "power.sf.rpt"
    stale.write_text(_CORNER_REPORTS["tt"])
    _run_mc_power(tmp_path, monkeypatch, reports=_CORNER_REPORTS)
    assert not stale.exists()


def test_multi_corner_power_fingerprints_every_corner_liberty(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    single = _make_power_backend(tmp_path / "a", platform=_FakePlatform(_CORNERS["tt"]))
    multi = _make_power_backend(
        tmp_path / "b", platform=_FakePlatform(_CORNERS["tt"], corners=_CORNERS)
    )
    for backend in (single, multi):
        backend._write_script()
    assert single._phys_technology() != multi._phys_technology()


def test_power_row_and_table_carry_the_worst_corner():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.power_results import PowerPassResults

    results = PowerPassResults(
        name="demo/results",
        total_w=3.84e-05,
        worst_corner="ff",
        corners={"ff": {"total_w": 3.84e-05}},
    )
    row = RtlBuddy._power_result_row(None, {"power_name": "demo", "results": results})
    assert row["worst_corner"] == "ff"
    assert row["corners"] == {"ff": {"total_w": 3.84e-05}}

    single = PowerPassResults(name="demo/results", total_w=1.0)
    assert "worst_corner" not in single.results
    assert "corners" not in single.results


def test_corners_written_as_a_scalar_is_refused_by_name(tmp_path):
    """pyserde would turn `corners: ss` into ['s', 's']; the platform must
    say it wants a list instead (#104, #105)."""
    from serde.yaml import from_yaml

    from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
    from rtl_buddy.errors import FatalRtlBuddyError

    pdk = MagicMock()
    pdk.get_corners.return_value = ["tt", "ss"]
    cfg = from_yaml(PnrPlatformConfigFile, 'name: "p"\npdk: "k"\ncorners: ss\n')
    with pytest.raises(FatalRtlBuddyError, match=r"corners: \[ss\]"):
        PnrPlatformConfig(cfg, lambda _n: pdk)


def test_a_failing_corner_report_does_not_stop_the_flow():
    """The per-corner block is report-only; a Tcl error in it must not abort
    the script ahead of `write_db`."""
    import shutil as _sh
    import subprocess

    from rtl_buddy.tools import openroad_corners

    tclsh = _sh.which("tclsh")
    if tclsh is None:
        pytest.skip("no tclsh")
    script = (
        "set ::sta_report_default_digits 2\n"
        'proc rb_find_corner {n} { error "no such scene $n" }\n'
        + openroad_corners.timing_report_tcl(["tt", "ss"]).replace(
            openroad_corners.FIND_CORNER_PROC, ""
        )
        + '\nputs "REACHED_WRITE_DB"\n'
    )
    out = subprocess.run([tclsh], input=script, capture_output=True, text=True)
    assert "REACHED_WRITE_DB" in out.stdout
    assert "per-corner timing for ss unavailable" in out.stdout
