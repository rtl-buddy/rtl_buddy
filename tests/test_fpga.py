"""Tests for the rb fpga MVP flow (#285): config, Vivado backend, CLI.

No test here invokes a real Vivado — the backend tests monkeypatch
``run_managed_process`` with a fake that drops the sanitized fixture
reports from ``tests/fixtures/fpga/`` into the run directory, exactly
where the batch flow would have written them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from rtl_buddy.config.fpga import (
    FpgaConfig,
    FpgaRegConfig,
    FpgaSuiteConfig,
    FpgaToolConfig,
    FpgaToolConfigFile,
)
from rtl_buddy.config.fpga_platform import (
    FpgaPlatformConfig,
    FpgaPlatformConfigFile,
)
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.fpga_results import (
    FpgaFailResults,
    FpgaPassResults,
    FpgaSkipResults,
)
from rtl_buddy.tools import fpga_vivado as fpga_vivado_module
from rtl_buddy.tools.fpga_vivado import VivadoFpga
from rtl_buddy.tools.fpga_vivado_flow import REPORT_FILES

FIXTURES = Path(__file__).parent / "fixtures" / "fpga"


# ---------------------------------------------------------------------------
# FpgaToolConfig
# ---------------------------------------------------------------------------


def test_fpga_tool_cfg_exposes_name_and_executable():
    cfg = FpgaToolConfig(FpgaToolConfigFile(name="vivado", tool="/opt/Vivado/vivado"))
    assert cfg.get_name() == "vivado"
    assert cfg.get_executable() == "/opt/Vivado/vivado"


# ---------------------------------------------------------------------------
# FpgaSuiteConfig — YAML loading + initialise
# ---------------------------------------------------------------------------


_FPGA_YAML = dedent("""\
    rtl-buddy-filetype: fpga_config

    runs:
      - name: "demo_fpga"
        desc: "Demo run"
        tool: "vivado"
        model: "demo_top"
        model_path: "models.yaml"
        part: "xczu7ev-ffvc1156-2-e"
        xdc:
          - "constraints/demo.xdc"
        reglvl: 1000
""")

_MODELS_YAML = dedent("""\
    rtl-buddy-filetype: model_config
    models:
      - name: demo_top
        desc: demo model
        filelist:
          - src/demo_top.sv
""")


def _write_suite(tmp_path, fpga_yaml=_FPGA_YAML):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    p = tmp_path / "fpga.yaml"
    p.write_text(fpga_yaml)
    return p


def test_fpga_suite_loads_runs(tmp_path):
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path)))
    assert suite.get_run_names() == ["demo_fpga"]
    run = suite.get_runs("demo_fpga")[0]
    assert run.get_name() == "demo_fpga"
    assert run.get_tool_name() == "vivado"
    assert run.get_part() == "xczu7ev-ffvc1156-2-e"
    assert run.get_top() == "demo_top"
    assert run.get_reglvl("vivado") == 1000
    # xdc paths are resolved relative to fpga.yaml
    assert run.get_xdc_files() == [str(tmp_path / "constraints" / "demo.xdc")]


def test_fpga_suite_tool_defaults_to_vivado(tmp_path):
    yaml = _FPGA_YAML.replace('    tool: "vivado"\n', "")
    assert "tool" not in yaml
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))
    assert suite.get_runs("demo_fpga")[0].get_tool_name() == "vivado"


def test_fpga_suite_require_timing_met_defaults_false_and_parses(tmp_path):
    # Absent -> default False (unmet timing still PASSes).
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path)))
    assert suite.get_runs("demo_fpga")[0].get_require_timing_met() is False
    # Present -> parsed from the kebab-case YAML key.
    yaml = _FPGA_YAML.replace(
        '    tool: "vivado"\n', '    tool: "vivado"\n    require-timing-met: true\n'
    )
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))
    assert suite.get_runs("demo_fpga")[0].get_require_timing_met() is True


def test_fpga_suite_missing_part_raises(tmp_path):
    yaml = _FPGA_YAML.replace('    part: "xczu7ev-ffvc1156-2-e"\n', "")
    assert "part" not in yaml
    with pytest.raises(FatalRtlBuddyError, match="missing 'part'"):
        FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))


def test_fpga_suite_unknown_run_raises(tmp_path):
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path)))
    with pytest.raises(FatalRtlBuddyError, match="not found in suite"):
        suite.get_runs("does_not_exist")


def test_fpga_suite_missing_file_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        FpgaSuiteConfig(str(tmp_path / "missing.yaml"))


_FPGA_XFAIL_EXTRA = """\
  - name: "fpga_xfail"
    desc: "expected fail"
    model: "demo_top"
    model_path: "models.yaml"
    part: "xczu7ev-ffvc1156-2-e"
    xfail: true
  - name: "fpga_xfail_strict"
    desc: "expected fail, strict"
    model: "demo_top"
    model_path: "models.yaml"
    part: "xczu7ev-ffvc1156-2-e"
    xfail_strict: true
"""


def test_fpga_suite_loads_xfail_flags(tmp_path):
    yaml = _FPGA_YAML + _FPGA_XFAIL_EXTRA
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))
    assert suite.get_runs("demo_fpga")[0].is_xfail() is False
    assert suite.get_runs("fpga_xfail")[0].is_xfail() is True
    assert suite.get_runs("fpga_xfail")[0].get_xfail_strict() is False
    assert suite.get_runs("fpga_xfail_strict")[0].get_xfail_strict() is True


# ---------------------------------------------------------------------------
# reglvl polymorphism
# ---------------------------------------------------------------------------


def _make_fpga_cfg(
    tmp_path,
    *,
    reglvl=None,
    tool="vivado",
    filelist=None,
    xdc=None,
    require_timing_met=False,
):
    model = ModelConfig(
        name="demo_top",
        filelist=filelist if filelist is not None else [],
        path=str(tmp_path / "models.yaml"),
    )
    return FpgaConfig(
        name="demo_fpga",
        desc="demo",
        model=model,
        tool=tool,
        part="xczu7ev-ffvc1156-2-e",
        xdc_files=xdc or [],
        _reglvl=reglvl,
        tool_overrides=None,
        require_timing_met=require_timing_met,
    )


def test_fpga_reglvl_int_dict_default_and_malformed(tmp_path):
    assert _make_fpga_cfg(tmp_path, reglvl=500).get_reglvl("vivado") == 500
    cfg = _make_fpga_cfg(tmp_path, reglvl={"vivado": 250, "default": 100})
    assert cfg.get_reglvl("vivado") == 250
    assert cfg.get_reglvl("openxc7") == 100
    assert _make_fpga_cfg(tmp_path).get_reglvl("vivado") == 0
    with pytest.raises(FatalRtlBuddyError, match="Malformed fpga.yaml"):
        _make_fpga_cfg(tmp_path, reglvl="bogus").get_reglvl("vivado")


# ---------------------------------------------------------------------------
# FpgaResults shapes
# ---------------------------------------------------------------------------


def test_fpga_pass_result_carries_metrics():
    r = FpgaPassResults(
        name="demo/results",
        lut={"used": 1, "available": 230400, "util_pct": 0.01},
        ff={"used": 16, "available": 460800, "util_pct": 0.01},
        bram={"used": 0.5, "available": 312, "util_pct": 0.16},
        dsp={"used": 1, "available": 1728, "util_pct": 0.06},
        wns_ns=8.452,
        tns_ns=0.0,
        whs_ns=0.059,
        timing_met=True,
        total_power_w=0.636,
        drc_violations=3,
        drc_by_severity={"Critical Warning": 2, "Warning": 1},
        bitstream="/tmp/demo_top.bit",
    )
    assert r.is_pass()
    assert r.results["lut"]["available"] == 230400
    assert r.results["wns_ns"] == 8.452
    assert r.results["total_power_w"] == 0.636
    assert r.results["drc_by_severity"]["Warning"] == 1
    assert r.results["bitstream"] == "/tmp/demo_top.bit"


def test_fpga_pass_result_bitstream_none_is_explicit():
    """Without --bitstream the key is still present, valued None."""
    r = FpgaPassResults(name="demo/results")
    assert "bitstream" in r.results
    assert r.results["bitstream"] is None


def test_fpga_skip_is_pass_and_fail_is_not():
    assert FpgaSkipResults(name="d/results", desc="no tool").is_pass()
    assert not FpgaFailResults(name="d/results", desc="boom").is_pass()


# ---------------------------------------------------------------------------
# Backend registry — dispatch is data-driven, not hardcoded
# ---------------------------------------------------------------------------


def test_fpga_backends_registry_contains_vivado():
    from rtl_buddy.runner.fpga_runner import _FPGA_BACKENDS
    from rtl_buddy.tools.fpga_base import BaseFpga

    assert "vivado" in _FPGA_BACKENDS
    assert _FPGA_BACKENDS["vivado"] is VivadoFpga
    assert issubclass(VivadoFpga, BaseFpga)


# ---------------------------------------------------------------------------
# FpgaRunner — executable resolution, reglvl skip, unknown tool
# ---------------------------------------------------------------------------


def _run_with_stub_backend(runner):
    """Run an FpgaRunner with the registry's vivado entry stubbed out.

    The registry dict captures the class object at import time, so the
    dict entry (not the module attribute) is what must be patched.
    """
    from rtl_buddy.runner import fpga_runner as fpga_runner_module

    mock_backend = MagicMock()
    mock_backend.return_value.run.return_value = FpgaSkipResults(
        name="demo/results", desc="stub"
    )
    with patch.dict(fpga_runner_module._FPGA_BACKENDS, {"vivado": mock_backend}):
        runner.run()
    return mock_backend


def test_fpga_runner_resolves_executable_from_cfg_fpga_tools(tmp_path):
    from rtl_buddy.runner.fpga_runner import FpgaRunner

    tool_cfg = FpgaToolConfig(
        FpgaToolConfigFile(name="vivado", tool="/opt/Vivado/bin/vivado")
    )
    root_cfg = MagicMock()
    root_cfg.get_fpga_tool_cfg.return_value = tool_cfg
    runner = FpgaRunner(
        name="demo",
        root_cfg=root_cfg,
        fpga_cfg=_make_fpga_cfg(tmp_path),
        suite_dir=str(tmp_path),
    )
    mock_backend = _run_with_stub_backend(runner)
    _, kwargs = mock_backend.call_args
    assert kwargs["executable"] == "/opt/Vivado/bin/vivado"


def test_fpga_runner_falls_back_to_bare_tool_name_when_no_cfg(tmp_path):
    from rtl_buddy.runner.fpga_runner import FpgaRunner

    root_cfg = MagicMock()
    root_cfg.get_fpga_tool_cfg.return_value = None
    runner = FpgaRunner(
        name="demo",
        root_cfg=root_cfg,
        fpga_cfg=_make_fpga_cfg(tmp_path),
        suite_dir=str(tmp_path),
    )
    mock_backend = _run_with_stub_backend(runner)
    _, kwargs = mock_backend.call_args
    assert kwargs["executable"] == "vivado"


def test_fpga_runner_reglvl_filter_skips(tmp_path):
    from rtl_buddy.runner.fpga_runner import FpgaRunner

    runner = FpgaRunner(
        name="demo",
        root_cfg=MagicMock(),
        fpga_cfg=_make_fpga_cfg(tmp_path, reglvl=1000),
        suite_dir=str(tmp_path),
        reglvl_filter=0,
    )
    res = runner.run()
    assert isinstance(res, FpgaSkipResults)
    assert "reglvl 1000 above filter 0" in res.results["desc"]


def test_fpga_runner_unknown_tool_raises(tmp_path):
    from rtl_buddy.runner.fpga_runner import FpgaRunner

    root_cfg = MagicMock()
    root_cfg.get_fpga_tool_cfg.return_value = None
    runner = FpgaRunner(
        name="demo",
        root_cfg=root_cfg,
        fpga_cfg=_make_fpga_cfg(tmp_path, tool="quartus"),
        suite_dir=str(tmp_path),
    )
    with pytest.raises(FatalRtlBuddyError, match="unknown tool 'quartus'"):
        runner.run()


# ---------------------------------------------------------------------------
# FpgaRunner — require-timing-met gate
# ---------------------------------------------------------------------------


def _run_with_pass_backend(runner, *, timing_met):
    """Stub the vivado backend to return a PASS carrying the given timing_met."""
    from rtl_buddy.runner import fpga_runner as fpga_runner_module

    mock_backend = MagicMock()
    mock_backend.return_value.run.return_value = FpgaPassResults(
        name="demo/results",
        wns_ns=-1.25 if timing_met is False else 0.5,
        tns_ns=-3.0 if timing_met is False else 0.0,
        timing_met=timing_met,
        failing_endpoints=4 if timing_met is False else 0,
    )
    with patch.dict(fpga_runner_module._FPGA_BACKENDS, {"vivado": mock_backend}):
        return runner.run()


def _runner(tmp_path, **cfg_kwargs):
    from rtl_buddy.runner.fpga_runner import FpgaRunner

    root_cfg = MagicMock()
    root_cfg.get_fpga_tool_cfg.return_value = None
    return FpgaRunner(
        name="demo",
        root_cfg=root_cfg,
        fpga_cfg=_make_fpga_cfg(tmp_path, **cfg_kwargs),
        suite_dir=str(tmp_path),
    )


def test_fpga_require_timing_met_fails_unmet_run_and_keeps_metrics(tmp_path):
    res = _run_with_pass_backend(
        _runner(tmp_path, require_timing_met=True), timing_met=False
    )
    assert isinstance(res, FpgaFailResults)
    assert res.results["result"] == "FAIL"
    assert "timing not met" in res.results["desc"]
    assert "WNS=-1.25" in res.results["desc"]
    # metrics ride along so a closure loop still sees them on the fail
    assert res.results["wns_ns"] == -1.25
    assert res.results["timing_met"] is False
    assert res.results["failing_endpoints"] == 4


def test_fpga_require_timing_met_passes_when_timing_met(tmp_path):
    res = _run_with_pass_backend(
        _runner(tmp_path, require_timing_met=True), timing_met=True
    )
    assert isinstance(res, FpgaPassResults)
    assert res.results["result"] == "PASS"


def test_fpga_unmet_timing_passes_by_default(tmp_path):
    # Default (no require-timing-met): unmet timing still PASSes — metrics
    # carry the truth, matching rb pnr.
    res = _run_with_pass_backend(_runner(tmp_path), timing_met=False)
    assert isinstance(res, FpgaPassResults)
    assert res.results["result"] == "PASS"
    assert res.results["timing_met"] is False


def test_fpga_require_timing_met_does_not_gate_unknown_timing(tmp_path):
    # A backend that cannot measure timing (timing_met None, e.g. openxc7
    # without a timing report) is never gated — we cannot prove a miss.
    res = _run_with_pass_backend(
        _runner(tmp_path, require_timing_met=True), timing_met=None
    )
    assert isinstance(res, FpgaPassResults)
    assert res.results["result"] == "PASS"


# ---------------------------------------------------------------------------
# Human-mode messages for the new WARNING/ERROR events (no lossy fallback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, fields, expected_substrings",
    [
        (
            "fpga.no_vivado",
            {"fpga": "demo", "exe": "vivado"},
            ["demo", "vivado", "tool-check"],
        ),
        (
            "fpga.no_openxc7",
            {"fpga": "demo", "missing": ["yosys", "nextpnr-xilinx"]},
            ["demo", "yosys", "nextpnr-xilinx"],
        ),
        (
            "fpga.filelist_failed",
            {"fpga": "demo", "error": "bad .f"},
            ["demo", "filelist", "bad .f"],
        ),
        (
            "fpga.script_failed",
            {"fpga": "demo", "error": "boom"},
            ["demo", "script", "boom"],
        ),
        (
            "fpga.failed",
            {"fpga": "demo", "returncode": 1, "log": "v.log"},
            ["demo", "code 1", "v.log"],
        ),
        (
            "fpga.stage_failed",
            {"fpga": "demo", "stage": "nextpnr", "returncode": 2, "log": "s.log"},
            ["demo", "nextpnr", "code 2"],
        ),
        (
            "fpga.errors_in_log",
            {"fpga": "demo", "count": 3, "first": "ERROR: x", "log": "v.log"},
            ["demo", "3 ERROR", "ERROR: x"],
        ),
        (
            "fpga.timing_gate_failed",
            {"fpga": "demo", "wns_ns": -1.25, "failing_endpoints": 4},
            ["demo", "timing not met", "-1.25", "require-timing-met"],
        ),
        (
            "cdc.no_vivado",
            {"analysis": "two_clk", "exe": "vivado"},
            ["two_clk", "vivado", "tool-check"],
        ),
        (
            "cdc.vivado_waivers_unsupported",
            {"analysis": "two_clk", "waivers": "w.yaml"},
            ["two_clk", "waiver"],
        ),
    ],
)
def test_fpga_cdc_human_messages_are_specific(event, fields, expected_substrings):
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(event, fields)
    # Not the lossy fallback ("foo.bar" -> "foo bar").
    assert msg != event.replace(".", " ")
    for sub in expected_substrings:
        assert sub in msg, f"{event}: {sub!r} not in {msg!r}"


# ---------------------------------------------------------------------------
# VivadoFpga backend — skip / pass / fail without a real Vivado
# ---------------------------------------------------------------------------


def _make_backend(tmp_path, *, emit_bitstream=False):
    """Backend over a real one-file design in tmp_path."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "demo_top.sv").write_text(
        "module demo_top(input clk, output logic q);\n"
        "  always_ff @(posedge clk) q <= ~q;\n"
        "endmodule\n"
    )
    cfg = _make_fpga_cfg(
        tmp_path,
        filelist=["src/demo_top.sv"],
        xdc=[str(tmp_path / "demo.xdc")],
    )
    (tmp_path / "demo.xdc").write_text("create_clock -period 10 [get_ports clk]\n")
    return VivadoFpga(
        name="demo/vivado",
        fpga_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        executable="vivado",
        emit_bitstream=emit_bitstream,
    )


def _fake_vivado(returncode=0, log_text="", drop_reports=True, drop_bitstream=True):
    """Build a run_managed_process stand-in that fakes a Vivado batch run."""

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text(log_text)
        if drop_reports:
            for filename in REPORT_FILES.values():
                shutil.copy(FIXTURES / filename, cwd / filename)
        if drop_bitstream:
            (cwd / "demo_top.bit").write_bytes(b"\x00bitstream\x00")
        return ManagedProcessResult(returncode=returncode)

    return _run


def test_vivado_fpga_skips_when_executable_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(fpga_vivado_module.shutil, "which", lambda _name: None)
    res = backend.run()
    assert isinstance(res, FpgaSkipResults)
    assert "not found" in res.results["desc"]
    assert "tool-check" in res.results["desc"]


def test_vivado_fpga_pass_parses_fixture_reports(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, emit_bitstream=True)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(fpga_vivado_module, "run_managed_process", _fake_vivado())

    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results["lut"]["used"] == 1
    assert res.results["lut"]["available"] == 230400
    assert res.results["ff"]["used"] == 16
    assert res.results["bram"]["used"] == 0.5
    assert res.results["dsp"]["used"] == 1
    assert res.results["wns_ns"] == 8.452
    assert res.results["whs_ns"] == 0.059
    assert res.results["timing_met"] is True
    assert res.results["total_power_w"] == 0.636
    assert res.results["dynamic_power_w"] == 0.044
    assert res.results["static_power_w"] == 0.592
    assert res.results["drc_violations"] == 3
    assert res.results["drc_by_severity"] == {"Critical Warning": 2, "Warning": 1}
    assert len(res.results["methodology_warnings"]) == 49
    assert res.results["methodology_warnings"][0] == {
        "id": "TIMING-18#1",
        "severity": "Warning",
        "description": "Missing input or output delay",
    }
    assert res.results["bitstream"].endswith("demo_top.bit")

    # The rendered flow.tcl carries the part, the XDC, and the source.
    script = (Path(backend.artefact_dir) / "flow.tcl").read_text()
    assert "synth_design -top demo_top -part xczu7ev-ffvc1156-2-e" in script
    assert "report_methodology -file methodology.rpt" in script
    assert "read_xdc" in script
    assert "demo_top.sv" in script
    assert "write_bitstream -force demo_top.bit" in script
    # Bitgen-blocking I/O DRCs are downgraded for IP-level models that
    # carry no board pinout (report_drc still records them).
    assert "set_property SEVERITY {Warning} [get_drc_checks NSTD-1]" in script
    assert "set_property SEVERITY {Warning} [get_drc_checks UCIO-1]" in script


def test_vivado_fpga_no_bitstream_flag_skips_bitgen(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, emit_bitstream=False)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        fpga_vivado_module,
        "run_managed_process",
        _fake_vivado(drop_bitstream=False),
    )

    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results["bitstream"] is None

    script = (Path(backend.artefact_dir) / "flow.tcl").read_text()
    assert "write_bitstream" not in script
    assert "# (bitstream generation not requested)" in script


def test_vivado_fpga_fails_on_nonzero_exit(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        fpga_vivado_module,
        "run_managed_process",
        _fake_vivado(returncode=1, drop_reports=False, drop_bitstream=False),
    )
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "exited with code 1" in res.results["desc"]


def test_vivado_fpga_fails_on_error_lines_in_log(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    log = (
        "INFO: [Synth 8-7075] Helper process launched\n"
        "ERROR: [Synth 8-439] module 'missing_mod' not found\n"
        "ERROR: [Common 17-69] Command failed: Synthesis failed\n"
    )
    monkeypatch.setattr(
        fpga_vivado_module,
        "run_managed_process",
        _fake_vivado(log_text=log, drop_reports=False, drop_bitstream=False),
    )
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "2 ERROR(s)" in res.results["desc"]


def test_vivado_fpga_error_scan_ignores_non_bracketed_lines(tmp_path, monkeypatch):
    """`puts "ERROR something"` from user Tcl must not trip the scan."""
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        fpga_vivado_module,
        "run_managed_process",
        _fake_vivado(log_text="ERROR COUNT SUMMARY: 0 errors\n"),
    )
    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]


def test_vivado_fpga_fails_when_report_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )

    def _partial(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text("")
        shutil.copy(FIXTURES / "util.rpt", cwd / "util.rpt")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(fpga_vivado_module, "run_managed_process", _partial)
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "not produced" in res.results["desc"]


def test_vivado_fpga_fails_when_bitstream_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, emit_bitstream=True)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        fpga_vivado_module,
        "run_managed_process",
        _fake_vivado(drop_bitstream=False),
    )
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "bitstream not produced" in res.results["desc"]


def test_vivado_fpga_uses_failing_timing_fixture(tmp_path, monkeypatch):
    """A routed-but-timing-failed run still passes; metrics carry the truth."""
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text("")
        for filename in REPORT_FILES.values():
            shutil.copy(FIXTURES / filename, cwd / filename)
        shutil.copy(FIXTURES / "timing_summary_fail.rpt", cwd / "timing_summary.rpt")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(fpga_vivado_module, "run_managed_process", _run)
    res = backend.run()
    assert isinstance(res, FpgaPassResults)
    assert res.results["wns_ns"] == -0.882
    assert res.results["timing_met"] is False


# ---------------------------------------------------------------------------
# CLI wiring — --list, machine envelope, config errors -> exit 2
# ---------------------------------------------------------------------------


def _fpga_project(minimal_project: Path) -> Path:
    """Extend the minimal_project fixture with an fpga.yaml."""
    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "Demo FPGA run"
                tool: "vivado"
                model: "example"
                model_path: "models.yaml"
                part: "xczu7ev-ffvc1156-2-e"
        """)
    )
    return minimal_project


def _mock_vivado_env(monkeypatch, fake=None):
    monkeypatch.setattr(
        fpga_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        fpga_vivado_module, "run_managed_process", fake or _fake_vivado()
    )


def test_cli_fpga_list(minimal_project: Path):
    from typer.testing import CliRunner

    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)
    runner = CliRunner()
    rb = RtlBuddy(name="test_fpga_list")
    result = runner.invoke(rb.app, ["fpga", "--list"])
    assert result.exit_code == 0, result.output
    assert "demo_fpga" in result.output


def test_cli_fpga_machine_envelope(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)
    _mock_vivado_env(monkeypatch, _fake_vivado(drop_bitstream=False))
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_machine")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    assert payload["command"] == "fpga"
    assert payload["exit_code"] == 0
    rows = payload["payload"]["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "demo_fpga"
    assert row["result"] == "PASS"
    assert row["wns_ns"] == 8.452
    assert row["lut"]["used"] == 1
    # Power: total/dynamic/static watts — the FPGA answer to #103.
    assert row["total_power_w"] == 0.636
    assert row["dynamic_power_w"] == 0.044
    assert row["static_power_w"] == 0.592
    assert row["drc_violations"] == 3
    # Methodology findings ride through as {id, severity, description}.
    assert len(row["methodology_warnings"]) == 49
    assert row["methodology_warnings"][0]["id"] == "TIMING-18#1"
    # bitstream is explicit null when --bitstream was not passed.
    assert row["bitstream"] is None


def test_cli_fpga_bitstream_flag_carries_path(
    minimal_project: Path, capsys, monkeypatch
):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text("")
        for filename in REPORT_FILES.values():
            shutil.copy(FIXTURES / filename, cwd / filename)
        (cwd / "example.bit").write_bytes(b"\x00")
        return ManagedProcessResult(returncode=0)

    _mock_vivado_env(monkeypatch, _run)
    monkeypatch.setattr(
        "sys.argv", ["rb", "--machine", "fpga", "demo_fpga", "--bitstream"]
    )
    rb = RtlBuddy(name="test_fpga_bit")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    assert payload["payload"]["results"][0]["bitstream"].endswith("example.bit")


def test_cli_fpga_failing_timing_payload_carries_loop_fields(
    minimal_project: Path, capsys, monkeypatch
):
    """Machine JSON for a timing-failing run feeds the closure loop (#288)."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text("")
        for filename in REPORT_FILES.values():
            shutil.copy(FIXTURES / filename, cwd / filename)
        shutil.copy(FIXTURES / "timing_summary_fail.rpt", cwd / "timing_summary.rpt")
        return ManagedProcessResult(returncode=0)

    _mock_vivado_env(monkeypatch, _run)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_timing_loop")
    exit_code = rb.run()
    captured = capsys.readouterr()
    # Failing timing is not a flow failure — the metrics carry the truth.
    assert exit_code == 0, captured
    row = json.loads(captured.out)["payload"]["results"][0]
    assert row["result"] == "PASS"
    assert row["timing_met"] is False
    assert row["wns_ns"] == -0.882
    assert row["tns_ns"] == -81.047
    assert row["failing_endpoints"] == 101
    path = row["failing_paths"][0]
    assert path["slack_ns"] == -0.882
    assert path["source"] == "product_reg/DSP_A_B_DATA_INST/CLK"
    assert path["destination"] == "product_reg/DSP_M_DATA_INST/V[0]"
    assert path["path_type"] == "Setup"


def test_cli_fpga_fail_path_exits_1(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)
    _mock_vivado_env(
        monkeypatch,
        _fake_vivado(returncode=1, drop_reports=False, drop_bitstream=False),
    )
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_fail")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 1
    assert payload["payload"]["results"][0]["result"] == "FAIL"


def test_cli_fpga_skip_when_vivado_missing(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)
    monkeypatch.setattr(fpga_vivado_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_skip")
    exit_code = rb.run()
    captured = capsys.readouterr()
    # SKIP counts as a pass — the feature is optional.
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    row = payload["payload"]["results"][0]
    assert row["result"] == "SKIP"
    assert "not found" in row["desc"]


def test_cli_fpga_missing_config_exits_2(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    monkeypatch.setattr(
        "sys.argv", ["rb", "--machine", "fpga", "-c", "does-not-exist.yaml"]
    )
    rb = RtlBuddy(name="test_fpga_noconfig")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 2
    assert "error" in payload["payload"]


def test_cli_fpga_unknown_run_exits_2(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_project(minimal_project)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "nope"])
    rb = RtlBuddy(name="test_fpga_unknown_run")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "not found in suite" in payload["payload"]["error"]


def test_cli_fpga_unknown_tool_exits_2(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "Demo FPGA run"
                tool: "quartus"
                model: "example"
                model_path: "models.yaml"
                part: "xczu7ev-ffvc1156-2-e"
        """)
    )
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_unknown_tool")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "unknown tool 'quartus'" in payload["payload"]["error"]


def test_cli_fpga_reglvl_gates_run(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "Demo FPGA run"
                model: "example"
                model_path: "models.yaml"
                part: "xczu7ev-ffvc1156-2-e"
                reglvl: 1000
        """)
    )
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_reglvl")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    row = payload["payload"]["results"][0]
    assert row["result"] == "SKIP"
    assert "reglvl 1000 above 0" in row["desc"]


# ---------------------------------------------------------------------------
# P2 (#286): cfg-fpga-platforms, XDC ownership, regression/reglvl
# ---------------------------------------------------------------------------


def test_fpga_platform_cfg_fields_and_xdc_anchoring(tmp_path):
    cfg = FpgaPlatformConfigFile(
        name="zu7ev_board",
        part="xczu7ev-ffvc1156-2-e",
        board="generic-zu7ev",
        package="ffvc1156",
        xdc=["constraints/board.xdc"],
    )
    platform = FpgaPlatformConfig(cfg, str(tmp_path / "root_config.yaml"))
    assert platform.get_name() == "zu7ev_board"
    assert platform.get_part() == "xczu7ev-ffvc1156-2-e"
    assert platform.get_board() == "generic-zu7ev"
    assert platform.get_package() == "ffvc1156"
    # default XDC paths anchor at root_config.yaml's directory
    assert platform.get_xdc_files() == [str(tmp_path / "constraints" / "board.xdc")]


def test_fpga_platform_cfg_optional_fields_default_empty(tmp_path):
    cfg = FpgaPlatformConfigFile(name="bare", part="xczu7ev-ffvc1156-2-e")
    platform = FpgaPlatformConfig(cfg, str(tmp_path / "root_config.yaml"))
    assert platform.get_board() == ""
    assert platform.get_package() == ""
    assert platform.get_xdc_files() == []


_PLATFORM_ROOT_EXTRA = dedent("""\

    cfg-fpga-platforms:
      - name: "zu7ev_board"
        part: "xczu7ev-ffvc1156-2-e"
        board: "generic-zu7ev"
        xdc:
          - "constraints/board.xdc"
      - name: "vu19p_board"
        part: "xcvu19p-fsva3824-1-e"
""")


def _add_platforms_to_root(project: Path) -> None:
    root_yaml = project / "root_config.yaml"
    root_yaml.write_text(root_yaml.read_text() + _PLATFORM_ROOT_EXTRA)


def test_root_config_loads_fpga_platforms(minimal_project: Path):
    from rtl_buddy.config.root import RootConfig

    _add_platforms_to_root(minimal_project)
    root_cfg = RootConfig(name="test_fpga_platforms")
    platform = root_cfg.get_fpga_platform_cfg("zu7ev_board")
    assert platform.get_part() == "xczu7ev-ffvc1156-2-e"
    assert platform.get_xdc_files() == [
        str(minimal_project / "constraints" / "board.xdc")
    ]
    with pytest.raises(FatalRtlBuddyError, match="not found in cfg-fpga-platforms"):
        root_cfg.get_fpga_platform_cfg("nope")


def test_fpga_suite_loads_platform_ref(tmp_path):
    yaml = _FPGA_YAML.replace(
        '    part: "xczu7ev-ffvc1156-2-e"\n', '    platform: "zu7ev_board"\n'
    )
    suite = FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))
    run = suite.get_runs("demo_fpga")[0]
    assert run.get_platform() == "zu7ev_board"
    assert run.get_part() == ""


def test_fpga_suite_part_and_platform_is_config_error(tmp_path):
    yaml = _FPGA_YAML.replace(
        '    part: "xczu7ev-ffvc1156-2-e"\n',
        '    part: "xczu7ev-ffvc1156-2-e"\n    platform: "zu7ev_board"\n',
    )
    with pytest.raises(FatalRtlBuddyError, match="mutually exclusive"):
        FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))


def test_fpga_suite_neither_part_nor_platform_is_config_error(tmp_path):
    yaml = _FPGA_YAML.replace('    part: "xczu7ev-ffvc1156-2-e"\n', "")
    with pytest.raises(FatalRtlBuddyError, match="missing 'part'"):
        FpgaSuiteConfig(str(_write_suite(tmp_path, yaml)))


# ---------------------------------------------------------------------------
# resolve_target — the platform/inline-part resolution seam
# ---------------------------------------------------------------------------


def _make_platform(tmp_path, *, part="xcvu19p-fsva3824-1-e", xdc=None):
    cfg = FpgaPlatformConfigFile(name="plat", part=part, xdc=xdc or [])
    return FpgaPlatformConfig(cfg, str(tmp_path / "root_config.yaml"))


def test_resolve_target_inline_part(tmp_path):
    from rtl_buddy.tools.fpga_base import resolve_target

    cfg = _make_fpga_cfg(tmp_path, xdc=["/run/run.xdc"])
    target = resolve_target(cfg, root_cfg=None)
    assert target.part == "xczu7ev-ffvc1156-2-e"
    assert list(target.xdc_files) == ["/run/run.xdc"]


def test_resolve_target_platform_part_and_xdc_merge_order(tmp_path):
    """Platform default XDC come first; per-run XDC extend (later wins)."""
    from rtl_buddy.tools.fpga_base import resolve_target

    platform = _make_platform(tmp_path, xdc=["constraints/board.xdc"])
    root_cfg = MagicMock()
    root_cfg.get_fpga_platform_cfg.return_value = platform
    cfg = _make_fpga_cfg(tmp_path, xdc=["/run/run.xdc"])
    cfg.platform = "plat"
    cfg.part = ""
    target = resolve_target(cfg, root_cfg)
    root_cfg.get_fpga_platform_cfg.assert_called_once_with("plat")
    assert target.part == "xcvu19p-fsva3824-1-e"
    assert list(target.xdc_files) == [
        str(tmp_path / "constraints" / "board.xdc"),
        "/run/run.xdc",
    ]


def test_resolve_target_unknown_platform_raises(tmp_path):
    from rtl_buddy.tools.fpga_base import resolve_target

    root_cfg = MagicMock()
    root_cfg.get_fpga_platform_cfg.side_effect = FatalRtlBuddyError(
        "fpga platform 'nope' not found in cfg-fpga-platforms; available: []"
    )
    cfg = _make_fpga_cfg(tmp_path)
    cfg.platform = "nope"
    cfg.part = ""
    with pytest.raises(FatalRtlBuddyError, match="not found in cfg-fpga-platforms"):
        resolve_target(cfg, root_cfg)


def test_resolve_target_platform_without_root_cfg_raises(tmp_path):
    from rtl_buddy.tools.fpga_base import resolve_target

    cfg = _make_fpga_cfg(tmp_path)
    cfg.platform = "plat"
    cfg.part = ""
    with pytest.raises(FatalRtlBuddyError, match="requires a root_config.yaml"):
        resolve_target(cfg, root_cfg=None)


# ---------------------------------------------------------------------------
# CLI — platform refs end-to-end (mocked Vivado)
# ---------------------------------------------------------------------------


def test_cli_fpga_platform_ref_resolves_part_and_merges_xdc(
    minimal_project: Path, capsys, monkeypatch
):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _add_platforms_to_root(minimal_project)
    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "platform-ref run"
                model: "example"
                model_path: "models.yaml"
                platform: "zu7ev_board"
                xdc:
                  - "constraints/run.xdc"
        """)
    )
    _mock_vivado_env(monkeypatch, _fake_vivado(drop_bitstream=False))
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_platform_ref")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    assert payload["payload"]["results"][0]["result"] == "PASS"

    script = (minimal_project / "artefacts" / "demo_fpga" / "flow.tcl").read_text()
    # part comes from the platform, not the run
    assert "-part xczu7ev-ffvc1156-2-e" in script
    # platform XDC first, run XDC after (later read_xdc wins in Vivado)
    board_xdc = str(minimal_project / "constraints" / "board.xdc")
    run_xdc = str(minimal_project / "constraints" / "run.xdc")
    assert script.index(board_xdc) < script.index(run_xdc)


def test_cli_fpga_unknown_platform_exits_2(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _add_platforms_to_root(minimal_project)
    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "bad platform ref"
                model: "example"
                model_path: "models.yaml"
                platform: "does_not_exist"
        """)
    )
    _mock_vivado_env(monkeypatch)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_bad_platform")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "not found in cfg-fpga-platforms" in payload["payload"]["error"]


def test_cli_fpga_part_and_platform_exits_2(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "conflicting device selection"
                model: "example"
                model_path: "models.yaml"
                part: "xczu7ev-ffvc1156-2-e"
                platform: "zu7ev_board"
        """)
    )
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_fpga_conflict")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "mutually exclusive" in payload["payload"]["error"]


# ---------------------------------------------------------------------------
# FpgaRegConfig + rb fpga-regression
# ---------------------------------------------------------------------------


def test_fpga_reg_config_loads_suite_paths(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "models.yaml").write_text(_MODELS_YAML)
    (sandbox / "fpga.yaml").write_text(_FPGA_YAML)
    reg_yaml = tmp_path / "fpga_regression.yaml"
    reg_yaml.write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_reg_config
            fpga-configs:
              - "sandbox/fpga.yaml"
        """)
    )
    reg_cfg = FpgaRegConfig(name="reg", path=str(reg_yaml))
    suites = reg_cfg.get_suite_configs()
    assert len(suites) == 1
    assert suites[0].get_run_names() == ["demo_fpga"]


def test_fpga_reg_config_missing_file_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        FpgaRegConfig(name="reg", path=str(tmp_path / "missing.yaml"))


def _fpga_regression_project(minimal_project: Path) -> Path:
    """Two suites running the same RTL on two parts via platform refs.

    suite_a/run_zu7ev targets the ZU7EV platform at reglvl 0;
    suite_b/run_vu19p targets the VU19P platform at reglvl 1000.
    """
    _add_platforms_to_root(minimal_project)
    for suite, platform, reglvl in (
        ("suite_a", "zu7ev_board", 0),
        ("suite_b", "vu19p_board", 1000),
    ):
        suite_dir = minimal_project / suite
        suite_dir.mkdir()
        (suite_dir / "fpga.yaml").write_text(
            dedent(f"""\
                rtl-buddy-filetype: fpga_config
                runs:
                  - name: "run_{platform.removesuffix("_board")}"
                    desc: "same RTL on {platform}"
                    model: "example"
                    model_path: "../models.yaml"
                    platform: "{platform}"
                    reglvl: {reglvl}
            """)
        )
    (minimal_project / "fpga_regression.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_reg_config
            fpga-configs:
              - "suite_a/fpga.yaml"
              - "suite_b/fpga.yaml"
        """)
    )
    return minimal_project


def test_cli_fpga_regression_filters_by_reg_level(
    minimal_project: Path, capsys, monkeypatch
):
    """-l 0 runs only the reglvl-0 entry; the reglvl-1000 one is SKIP."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_regression_project(minimal_project)
    _mock_vivado_env(monkeypatch, _fake_vivado(drop_bitstream=False))
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga-regression"])
    rb = RtlBuddy(name="test_fpga_reg_l0")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    assert payload["command"] == "fpga-regression"
    rows = {r["name"]: r for r in payload["payload"]["results"]}
    assert rows["run_zu7ev"]["result"] == "PASS"
    assert rows["run_zu7ev"]["suite"].endswith("suite_a/fpga.yaml")
    assert rows["run_vu19p"]["result"] == "SKIP"
    assert "reglvl 1000 above 0" in rows["run_vu19p"]["desc"]


def test_cli_fpga_regression_runs_same_rtl_across_parts(
    minimal_project: Path, capsys, monkeypatch
):
    """-l 1000 runs both platforms; each flow.tcl targets its own part."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_regression_project(minimal_project)
    _mock_vivado_env(monkeypatch, _fake_vivado(drop_bitstream=False))
    monkeypatch.setattr(
        "sys.argv", ["rb", "--machine", "fpga-regression", "-l", "1000"]
    )
    rb = RtlBuddy(name="test_fpga_reg_l1000")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    rows = {r["name"]: r for r in payload["payload"]["results"]}
    assert rows["run_zu7ev"]["result"] == "PASS"
    assert rows["run_vu19p"]["result"] == "PASS"

    script_a = (
        minimal_project / "suite_a" / "artefacts" / "run_zu7ev" / "flow.tcl"
    ).read_text()
    script_b = (
        minimal_project / "suite_b" / "artefacts" / "run_vu19p" / "flow.tcl"
    ).read_text()
    assert "-part xczu7ev-ffvc1156-2-e" in script_a
    assert "-part xcvu19p-fsva3824-1-e" in script_b


def test_cli_fpga_regression_aggregates_failures(
    minimal_project: Path, capsys, monkeypatch
):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _fpga_regression_project(minimal_project)
    _mock_vivado_env(
        monkeypatch,
        _fake_vivado(returncode=1, drop_reports=False, drop_bitstream=False),
    )
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga-regression"])
    rb = RtlBuddy(name="test_fpga_reg_fail")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 1, captured
    payload = json.loads(captured.out)
    rows = {r["name"]: r for r in payload["payload"]["results"]}
    assert rows["run_zu7ev"]["result"] == "FAIL"


def test_cli_fpga_regression_missing_config_exits_2(
    minimal_project: Path, capsys, monkeypatch
):
    from rtl_buddy.rtl_buddy import RtlBuddy

    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga-regression"])
    rb = RtlBuddy(name="test_fpga_reg_noconfig")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "fpga_regression.yaml not found" in payload["payload"]["error"]
