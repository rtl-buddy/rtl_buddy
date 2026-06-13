"""Tests for the Vivado ``report_cdc`` second-opinion backend (#287).

No test invokes a real Vivado — the backend tests monkeypatch
``run_managed_process`` with a fake that drops a sanitized fixture
``cdc.rpt`` (real Vivado 2022.1.2 output from a 2-clock design with an
unsynchronized crossing) into the artefact directory.
"""

from __future__ import annotations

import shutil
from contextlib import nullcontext
from pathlib import Path

import pytest

from rtl_buddy.config.cdc import (
    CdcConfig,
    CdcToolConfig,
    CdcToolConfigFile,
    CdcToolOptsFile,
)
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.cdc_results import (
    CdcFailResults,
    CdcPassResults,
    CdcSkipResults,
)
from rtl_buddy.runner.cdc_runner import _CDC_BACKENDS, CdcRunner
from rtl_buddy.tools import cdc_vivado as cdc_vivado_module
from rtl_buddy.tools.cdc_rtl_buddy import RtlBuddyCdc
from rtl_buddy.tools.cdc_vivado import VivadoCdc, parse_report_cdc, render_cdc_tcl

FIXTURES = Path(__file__).parent / "fixtures" / "cdc"

PART = "xczu7ev-ffvc1156-2-e"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# parse_report_cdc — fixture contract
# ---------------------------------------------------------------------------


def test_parse_report_cdc_violating_fixture():
    parsed = parse_report_cdc(_fixture("vivado_cdc_violations.rpt"))

    # Summary: one Critical CDC-1 (unsynchronized) + one Info CDC-3
    # (properly ASYNC_REG-synchronized). Only the Critical counts as a
    # violation; both are crossings and both are surfaced verbatim.
    assert parsed["violations"] == 1
    assert parsed["crossings"] == 2
    assert parsed["by_id"]["CDC-1"] == {
        "severity": "Critical",
        "count": 1,
        "description": "1-bit unknown CDC circuitry",
    }
    assert parsed["by_id"]["CDC-3"]["severity"] == "Info"

    findings = parsed["findings"]
    assert len(findings) == 2
    bad = findings[0]
    assert bad["id"] == "CDC-1"
    assert bad["severity"] == "Critical"
    assert bad["description"] == "1-bit unknown CDC circuitry"
    assert bad["source"] == "a_reg_bad_reg/C"
    assert bad["destination"] == "dout_bad_reg/D"
    assert bad["source_clock"] == "clk_a"
    assert bad["destination_clock"] == "clk_b"


def test_parse_report_cdc_clean_fixture():
    parsed = parse_report_cdc(_fixture("vivado_cdc_clean.rpt"))
    assert parsed["violations"] == 0
    assert parsed["crossings"] == 1
    assert [f["id"] for f in parsed["findings"]] == ["CDC-3"]


def test_parse_report_cdc_rejects_garbage():
    with pytest.raises(ValueError, match="not a Vivado CDC report"):
        parse_report_cdc("clocks, what clocks?\n")


# ---------------------------------------------------------------------------
# render_cdc_tcl
# ---------------------------------------------------------------------------


def test_render_cdc_tcl_contents():
    script = render_cdc_tcl(
        top="cdc_demo",
        part=PART,
        verilog_sources=["a.sv", "b.v"],
        sdc_file="cdc_demo.sdc",
    )
    assert "read_verilog -sv a.sv" in script
    assert "read_verilog b.v" in script
    assert "read_xdc cdc_demo.sdc" in script
    assert f"synth_design -top cdc_demo -part {PART}" in script
    assert "report_cdc -details -file cdc.rpt" in script
    # Elaboration only — no implementation stages.
    assert "place_design" not in script
    assert "route_design" not in script


def test_render_cdc_tcl_validates_inputs():
    with pytest.raises(RuntimeError, match="top module name is required"):
        render_cdc_tcl(top="", part=PART, verilog_sources=["a.sv"], sdc_file="a.sdc")
    with pytest.raises(RuntimeError, match="part name is required"):
        render_cdc_tcl(top="t", part="", verilog_sources=["a.sv"], sdc_file="a.sdc")
    with pytest.raises(RuntimeError, match="at least one HDL source"):
        render_cdc_tcl(top="t", part=PART, verilog_sources=[], sdc_file="a.sdc")


# ---------------------------------------------------------------------------
# VivadoCdc backend
# ---------------------------------------------------------------------------


def _tool_cfg(part: str | None = PART) -> CdcToolConfig:
    return CdcToolConfig(
        CdcToolConfigFile(
            name="vivado",
            tool="vivado",
            opts=CdcToolOptsFile(part=part),
        )
    )


def _make_backend(tmp_path, *, part=PART, tool_overrides=None, waivers=None):
    sv = tmp_path / "cdc_demo.sv"
    sv.write_text("module cdc_demo(); endmodule\n")
    sdc = tmp_path / "cdc_demo.sdc"
    sdc.write_text("create_clock -name clk_a -period 10.000 [get_ports clk_a]\n")
    if waivers is not None:
        Path(waivers).write_text("# waivers\n")

    model = ModelConfig(name="cdc_demo", filelist=[f"-v {sv}"], path=str(tmp_path))
    cdc_cfg = CdcConfig(
        name="demo_cdc",
        desc="vivado cdc demo",
        model=model,
        tool="vivado",
        constraints=str(sdc),
        waivers=waivers,
        _reglvl=None,
        tool_overrides=tool_overrides,
    )
    return VivadoCdc(
        name="t/vivado",
        cdc_cfg=cdc_cfg,
        tool_cfg=_tool_cfg(part=part),
        suite_dir=str(tmp_path),
    )


def _fake_vivado(fixture_name=None, returncode=0, log_text=""):
    """run_managed_process stand-in that fakes a Vivado CDC batch run."""

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "vivado.log").write_text(log_text)
        if fixture_name is not None:
            shutil.copy(FIXTURES / fixture_name, cwd / "cdc.rpt")
        return ManagedProcessResult(returncode=returncode)

    return _run


def _mock_env(monkeypatch, fake):
    monkeypatch.setattr(
        cdc_vivado_module.shutil, "which", lambda _name: "/usr/bin/vivado"
    )
    monkeypatch.setattr(
        cdc_vivado_module, "task_status", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(cdc_vivado_module, "run_managed_process", fake)


def test_vivado_cdc_fail_carries_verbatim_findings(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _mock_env(monkeypatch, _fake_vivado("vivado_cdc_violations.rpt"))

    res = backend.run()
    assert isinstance(res, CdcFailResults)
    assert res.results["violations"] == 1
    assert res.results["crossings"] == 2
    assert res.results["backend"] == "vivado"
    # Vivado's own rule ids/severities ride through untranslated.
    assert [f["id"] for f in res.results["findings"]] == ["CDC-1", "CDC-3"]
    assert res.results["findings"][0]["severity"] == "Critical"

    # The rendered Tcl elaborates with the configured part.
    script = (Path(backend.artefact_dir) / "cdc.tcl").read_text()
    assert f"synth_design -top cdc_demo -part {PART}" in script
    assert "report_cdc -details -file cdc.rpt" in script


def test_vivado_cdc_pass_on_clean_fixture(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _mock_env(monkeypatch, _fake_vivado("vivado_cdc_clean.rpt"))

    res = backend.run()
    assert isinstance(res, CdcPassResults)
    assert res.results["violations"] == 0
    assert res.results["crossings"] == 1
    assert res.results["backend"] == "vivado"
    # Info-severity findings are still surfaced on a PASS.
    assert [f["id"] for f in res.results["findings"]] == ["CDC-3"]


def test_vivado_cdc_part_override_via_tool_overrides(tmp_path, monkeypatch):
    backend = _make_backend(
        tmp_path, tool_overrides={"vivado": {"part": "xcau20p-ffvb676-1-e"}}
    )
    _mock_env(monkeypatch, _fake_vivado("vivado_cdc_clean.rpt"))

    backend.run()
    script = (Path(backend.artefact_dir) / "cdc.tcl").read_text()
    assert "-part xcau20p-ffvb676-1-e" in script


def test_vivado_cdc_missing_part_is_config_error(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, part=None)
    _mock_env(monkeypatch, _fake_vivado("vivado_cdc_clean.rpt"))
    with pytest.raises(FatalRtlBuddyError, match="opts.part"):
        backend.run()


def test_vivado_cdc_skips_when_vivado_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(cdc_vivado_module.shutil, "which", lambda _name: None)
    res = backend.run()
    assert isinstance(res, CdcSkipResults)
    assert "not found" in res.results["desc"]
    assert "tool-check" in res.results["desc"]


def test_vivado_cdc_fails_on_nonzero_exit(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _mock_env(monkeypatch, _fake_vivado(fixture_name=None, returncode=1))
    res = backend.run()
    assert isinstance(res, CdcFailResults)
    assert "exited with code 1" in res.results["desc"]


def test_vivado_cdc_fails_on_error_lines_in_log(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    log = "ERROR: [Synth 8-439] module 'missing_mod' not found\n"
    _mock_env(monkeypatch, _fake_vivado(fixture_name=None, log_text=log))
    res = backend.run()
    assert isinstance(res, CdcFailResults)
    assert "ERROR(s) in Vivado log" in res.results["desc"]


def test_vivado_cdc_fails_when_report_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _mock_env(monkeypatch, _fake_vivado(fixture_name=None))
    res = backend.run()
    assert isinstance(res, CdcFailResults)
    assert "no CDC report produced" in res.results["desc"]


# ---------------------------------------------------------------------------
# CdcRunner dispatch
# ---------------------------------------------------------------------------


class _StubRootCfg:
    def __init__(self, tool_cfg):
        self._tool_cfg = tool_cfg

    def get_cdc_tool_cfg(self, name):
        if self._tool_cfg is None or self._tool_cfg.get_name() != name:
            raise FatalRtlBuddyError(f"CDC tool '{name}' not found in cfg-cdc-tools")
        return self._tool_cfg


def _runner_for_tool(tmp_path, tool_name, tool_cfg):
    model = ModelConfig(name="m", filelist=[], path=str(tmp_path))
    cdc_cfg = CdcConfig(
        name="dispatch_cdc",
        desc="d",
        model=model,
        tool=tool_name,
        constraints=str(tmp_path / "m.sdc"),
        waivers=None,
        _reglvl=None,
        tool_overrides=None,
    )
    return CdcRunner(
        name="t",
        root_cfg=_StubRootCfg(tool_cfg),
        cdc_cfg=cdc_cfg,
        suite_dir=str(tmp_path),
    )


def test_cdc_backend_registry_contents():
    assert _CDC_BACKENDS["rtl-buddy-cdc"] is RtlBuddyCdc
    assert _CDC_BACKENDS["vivado"] is VivadoCdc


def test_cdc_runner_dispatches_to_vivado_backend(tmp_path, monkeypatch):
    runner = _runner_for_tool(tmp_path, "vivado", _tool_cfg())
    seen = {}

    def _fake_run(self):
        seen["backend"] = type(self)
        seen["name"] = self.name
        return CdcPassResults(name="dispatch_cdc")

    monkeypatch.setattr(VivadoCdc, "run", _fake_run)
    res = runner.run()
    assert seen["backend"] is VivadoCdc
    assert seen["name"] == "t/vivado"
    assert res.is_pass()


def test_cdc_runner_unknown_tool_errors_cleanly(tmp_path):
    tool_cfg = CdcToolConfig(
        CdcToolConfigFile(name="spyglass", tool="spyglass", opts=CdcToolOptsFile())
    )
    runner = _runner_for_tool(tmp_path, "spyglass", tool_cfg)
    with pytest.raises(FatalRtlBuddyError, match="unknown tool 'spyglass'"):
        runner.run()


def test_cdc_runner_unconfigured_tool_still_errors(tmp_path):
    """A tool name with no cfg-cdc-tools entry fails at lookup, before
    the registry is consulted (pre-existing behavior, kept)."""
    runner = _runner_for_tool(tmp_path, "vivado", None)
    with pytest.raises(FatalRtlBuddyError, match="not found in cfg-cdc-tools"):
        runner.run()
