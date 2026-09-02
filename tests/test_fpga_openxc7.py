"""Tests for the openXC7 fpga backend (#288): parser, backend, CLI.

nextpnr-xilinx and prjxray are never invoked (they are not assumed
installed anywhere CI runs) — the backend tests monkeypatch
``run_managed_process`` with a fake that drops the fixture logs from
``tests/fixtures/fpga/`` into the run directory. The fixture logs are
hand-built to nextpnr's / yosys's documented output formats, not
captured from a real run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.fpga import FpgaConfig
from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.fpga_results import (
    FpgaFailResults,
    FpgaPassResults,
    FpgaSkipResults,
)
from rtl_buddy.tools import fpga_openxc7 as fpga_openxc7_module
from rtl_buddy.tools.fpga_openxc7 import OpenXc7Fpga
from rtl_buddy.tools.fpga_openxc7_reports import parse_nextpnr_log

FIXTURES = Path(__file__).parent / "fixtures" / "fpga"

_PART = "xc7a35tcsg324-1"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# parse_nextpnr_log
# ---------------------------------------------------------------------------


def test_parse_nextpnr_log_pass_utilization_and_timing():
    metrics = parse_nextpnr_log(_fixture("nextpnr_xilinx_pass.log"))

    assert metrics["lut"] == {"used": 142, "available": 32600, "util_pct": 0.0}
    assert metrics["ff"] == {"used": 97, "available": 65200, "util_pct": 0.0}
    assert metrics["bram"] == {"used": 1, "available": 100, "util_pct": 1.0}
    assert metrics["dsp"] == {"used": 1, "available": 90, "util_pct": 1.0}
    # Non-canonical bel buckets are captured too.
    assert metrics["bels"]["CARRY4"]["used"] == 16

    assert metrics["fmax_mhz"] == 160.0
    # 1000/100 - 1000/160 = 3.75 ns of positive slack.
    assert metrics["wns_ns"] == 3.75
    assert metrics["timing_met"] is True
    assert metrics["failing_paths"] == []

    assert len(metrics["clocks"]) == 1
    clk = metrics["clocks"][0]
    assert clk["clock"] == "clk"
    assert clk["target_mhz"] == 100.0
    assert clk["met"] is True
    # Critical path endpoints from the report section.
    assert clk["source"] == "count_reg.0_SLICE_FFX.Q"
    assert clk["destination"] == "count_reg.7_SLICE_FFX.D"


def test_parse_nextpnr_log_fail_carries_loop_fields():
    metrics = parse_nextpnr_log(_fixture("nextpnr_xilinx_fail.log"))

    assert metrics["timing_met"] is False
    assert metrics["fmax_mhz"] == 160.0
    # 1000/250 - 1000/160 = -2.25 ns.
    assert metrics["wns_ns"] == -2.25
    assert metrics["failing_paths"] == [
        {
            "clock": "clk",
            "slack_ns": -2.25,
            "fmax_mhz": 160.0,
            "target_mhz": 250.0,
            "source": "mult_a_reg.3_SLICE_FFX.Q",
            "destination": "product_reg.11_SLICE_FFX.D",
        }
    ]


def test_parse_nextpnr_log_rejects_garbage():
    with pytest.raises(ValueError, match="not a nextpnr log"):
        parse_nextpnr_log("Info: nothing useful here\n")


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


def test_fpga_backends_registry_contains_openxc7():
    from rtl_buddy.runner.fpga_runner import _FPGA_BACKENDS
    from rtl_buddy.tools.fpga_base import BaseFpga

    assert "openxc7" in _FPGA_BACKENDS
    assert _FPGA_BACKENDS["openxc7"] is OpenXc7Fpga
    assert issubclass(OpenXc7Fpga, BaseFpga)
    # The default backend stays vivado (openxc7 is 7-series only).
    from rtl_buddy.config.fpga import FpgaConfigFile

    assert FpgaConfigFile.__dataclass_fields__["tool"].default == "vivado"


# ---------------------------------------------------------------------------
# OpenXc7Fpga backend — mocked pipeline, no real toolchain
# ---------------------------------------------------------------------------


def _make_backend(tmp_path, *, part=_PART, emit_bitstream=False, tool_overrides=None):
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "demo_top.sv").write_text(
        "module demo_top(input clk, output logic q);\n"
        "  always_ff @(posedge clk) q <= ~q;\n"
        "endmodule\n"
    )
    (tmp_path / "demo.xdc").write_text("create_clock -period 10 [get_ports clk]\n")
    model = ModelConfig(
        name="demo_top",
        filelist=["src/demo_top.sv"],
        path=str(tmp_path / "models.yaml"),
    )
    cfg = FpgaConfig(
        name="demo_fpga",
        desc="demo",
        model=model,
        tool="openxc7",
        part=part,
        xdc_files=[str(tmp_path / "demo.xdc")],
        _reglvl=None,
        tool_overrides=tool_overrides,
    )
    return OpenXc7Fpga(
        name="demo/openxc7",
        fpga_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        executable="openxc7",
        emit_bitstream=emit_bitstream,
    )


_CHIPDB_OVERRIDES = {"openxc7": {"chipdb": "/opt/chipdb/xc7a35t.bin"}}


def _fake_pipeline(
    nextpnr_log="nextpnr_xilinx_pass.log",
    fail_stage=None,
    calls=None,
):
    """Build a run_managed_process stand-in faking the openXC7 stages."""

    def _run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        cwd = Path(kwargs["cwd"])
        exe = os.path.basename(cmd[0])
        if exe == fail_stage:
            return ManagedProcessResult(returncode=1)
        if exe == "yosys":
            kwargs["stdout"].write(_fixture("yosys_openxc7.log"))
            (cwd / "demo_top.json").write_text("{}")
        elif exe == "nextpnr-xilinx":
            kwargs["stdout"].write(_fixture(nextpnr_log))
            (cwd / "demo_top.fasm").write_text("# fasm\n")
        elif exe == "fasm2frames":
            # Frames go to stdout (binary); the log handle is stderr.
            kwargs["stdout"].write(b"\x00frames\x00")
        elif exe == "xc7frames2bit":
            (cwd / "demo_top.bit").write_bytes(b"\x00bitstream\x00")
        return ManagedProcessResult(returncode=0)

    return _run


def _mock_toolchain(monkeypatch, fake=None):
    monkeypatch.setattr(
        fpga_openxc7_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        fpga_openxc7_module, "run_managed_process", fake or _fake_pipeline()
    )


def test_openxc7_rejects_non_7series_part(tmp_path):
    backend = _make_backend(tmp_path, part="xczu7ev-ffvc1156-2-e")
    with pytest.raises(FatalRtlBuddyError, match="7-series"):
        backend.run()


def test_openxc7_skips_when_toolchain_missing(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(fpga_openxc7_module.shutil, "which", lambda _name: None)
    res = backend.run()
    assert isinstance(res, FpgaSkipResults)
    assert "tool-check --explain nextpnr-xilinx" in res.results["desc"]


def test_openxc7_skips_when_chipdb_unresolved(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _mock_toolchain(monkeypatch)
    monkeypatch.delenv("CHIPDB", raising=False)
    res = backend.run()
    assert isinstance(res, FpgaSkipResults)
    assert "chipdb" in res.results["desc"]


def test_openxc7_skips_when_prjxray_db_unresolved(tmp_path, monkeypatch):
    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    _mock_toolchain(monkeypatch)
    monkeypatch.delenv("PRJXRAY_DB_DIR", raising=False)
    res = backend.run()
    assert isinstance(res, FpgaSkipResults)
    assert "prjxray" in res.results["desc"]


def test_openxc7_mocked_pipeline_passes(tmp_path, monkeypatch):
    calls: list = []
    backend = _make_backend(tmp_path, tool_overrides=_CHIPDB_OVERRIDES)
    _mock_toolchain(monkeypatch, _fake_pipeline(calls=calls))

    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results["lut"] == {"used": 142, "available": 32600, "util_pct": 0.0}
    assert res.results["ff"]["used"] == 97
    assert res.results["bram"]["used"] == 1
    assert res.results["dsp"]["used"] == 1
    assert res.results["fmax_mhz"] == 160.0
    assert res.results["wns_ns"] == 3.75
    assert res.results["timing_met"] is True
    assert res.results["bitstream"] is None
    # Metrics the open flow cannot produce stay absent, not fabricated.
    for absent in ("tns_ns", "whs_ns", "total_power_w", "drc_violations"):
        assert absent not in res.results

    # Stage pipeline: yosys -> nextpnr (no prjxray without --bitstream).
    assert [os.path.basename(c[0]) for c in calls] == ["yosys", "nextpnr-xilinx"]
    nextpnr_cmd = calls[1]
    assert nextpnr_cmd[nextpnr_cmd.index("--chipdb") + 1] == "/opt/chipdb/xc7a35t.bin"
    assert "--xdc" in nextpnr_cmd
    assert nextpnr_cmd[nextpnr_cmd.index("--json") + 1] == "demo_top.json"
    assert nextpnr_cmd[nextpnr_cmd.index("--fasm") + 1] == "demo_top.fasm"

    # The yosys script reads the source and targets xc7.
    script = (Path(backend.artefact_dir) / "synth.ys").read_text()
    assert "read_verilog -sv" in script
    assert "demo_top.sv" in script
    assert "synth_xilinx -flatten -abc9 -arch xc7 -top demo_top" in script
    assert "write_json demo_top.json" in script


def test_openxc7_forwards_filelist_incdirs(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, tool_overrides=_CHIPDB_OVERRIDES)
    (tmp_path / "inc").mkdir()
    backend.fpga_cfg.model.filelist = ["+incdir+inc", "src/demo_top.sv"]
    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    script = (Path(backend.artefact_dir) / "synth.ys").read_text()
    assert f"read_verilog -sv -I {tmp_path / 'inc'} " in script


def test_openxc7_chipdb_from_env_dir(tmp_path, monkeypatch):
    calls: list = []
    backend = _make_backend(tmp_path)
    _mock_toolchain(monkeypatch, _fake_pipeline(calls=calls))
    monkeypatch.setenv("CHIPDB", "/opt/nextpnr-xilinx/chipdb")
    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    nextpnr_cmd = calls[1]
    assert (
        nextpnr_cmd[nextpnr_cmd.index("--chipdb") + 1]
        == f"/opt/nextpnr-xilinx/chipdb/{_PART}.bin"
    )


def test_openxc7_bitstream_runs_prjxray_stages(tmp_path, monkeypatch):
    calls: list = []
    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    _mock_toolchain(monkeypatch, _fake_pipeline(calls=calls))
    monkeypatch.setenv("PRJXRAY_DB_DIR", "/opt/prjxray-db")

    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results["bitstream"].endswith("demo_top.bit")

    assert [os.path.basename(c[0]) for c in calls] == [
        "yosys",
        "nextpnr-xilinx",
        "fasm2frames",
        "xc7frames2bit",
    ]
    fasm_cmd = calls[2]
    assert fasm_cmd[fasm_cmd.index("--part") + 1] == _PART
    # xc7a -> artix7 family directory of the prjxray database.
    assert fasm_cmd[fasm_cmd.index("--db-root") + 1] == "/opt/prjxray-db/artix7"
    bit_cmd = calls[3]
    assert (
        bit_cmd[bit_cmd.index("--part_file") + 1]
        == f"/opt/prjxray-db/artix7/{_PART}/part.yaml"
    )
    assert bit_cmd[bit_cmd.index("--output_file") + 1] == "demo_top.bit"
    # fasm2frames' stdout was captured into the frames file.
    assert (Path(backend.artefact_dir) / "demo_top.frames").read_bytes() != b""


def test_openxc7_ignores_a_previous_runs_bitstream(tmp_path, monkeypatch):
    """xc7frames2bit exiting 0 without writing must not promote the `.bit` an
    earlier run left behind, and each stage must consume the handoff file this
    run's predecessor wrote rather than a previous run's (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    monkeypatch.setenv("PRJXRAY_DB_DIR", "/opt/prjxray-db")

    artefacts = Path(backend.artefact_dir)
    (artefacts / "demo_top.bit").write_bytes(b"\x00stale bitstream\x00")
    (artefacts / "demo_top.json").write_text('{"stale": true}')
    (artefacts / "demo_top.fasm").write_text("# stale fasm\n")

    # The stage handoffs are cleared too, so what nextpnr and fasm2frames
    # consume is what this run's yosys / nextpnr actually wrote.
    seen: dict[str, str] = {}

    def _no_bitstream(cmd, **kwargs):
        # Every stage "succeeds" but xc7frames2bit writes nothing.
        exe = os.path.basename(cmd[0])
        if exe == "nextpnr-xilinx":
            seen["json"] = (artefacts / "demo_top.json").read_text()
        if exe == "fasm2frames":
            seen["fasm"] = (artefacts / "demo_top.fasm").read_text()
        if exe == "xc7frames2bit":
            return ManagedProcessResult(returncode=0)
        return _fake_pipeline()(cmd, **kwargs)

    _mock_toolchain(monkeypatch, _no_bitstream)
    res = backend.run()

    assert isinstance(res, FpgaFailResults)
    assert "bitstream not produced" in res.results["desc"]
    assert not (artefacts / "demo_top.bit").exists()
    # Neither downstream stage saw the previous run's handoff files.
    assert seen["json"] == "{}"
    assert seen["fasm"] == "# fasm\n"


def test_openxc7_clears_the_bitstream_without_emit_bitstream(tmp_path, monkeypatch):
    """Matches the Vivado backend: a run not asked for a bitstream still
    removes a previous one, so the artefact dir describes the latest run
    (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    stale_bit = artefacts / "demo_top.bit"
    stale_bit.write_bytes(b"\x00stale\x00")

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results.get("bitstream") is None
    assert not stale_bit.exists()


def test_openxc7_clears_stale_frames_without_emit_bitstream(tmp_path, monkeypatch):
    """`<top>.frames` is on the up-front clear list even though the stage that
    writes it truncates it: a run without `--bitstream` never reaches that
    stage, so nothing else would ever remove it (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    stale_frames = artefacts / "demo_top.frames"
    stale_frames.write_bytes(b"\x00stale frames\x00")

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert not stale_frames.exists()


def test_openxc7_filelist_failure_still_clears_artefacts(tmp_path, monkeypatch):
    """The clear sits above the filelist step, so a bitstream rerun that dies
    before any stage runs leaves no frames or bitstream behind (#469)."""
    from rtl_buddy.errors import FilelistError
    from rtl_buddy.tools.fpga_openxc7 import OpenXc7Fpga

    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    monkeypatch.setenv("PRJXRAY_DB_DIR", "/opt/prjxray-db")

    artefacts = Path(backend.artefact_dir)
    stale = {
        name: artefacts / name
        for name in ("demo_top.frames", "demo_top.bit", "demo_top.json")
    }
    for path in stale.values():
        path.write_bytes(b"\x00stale\x00")

    _mock_toolchain(monkeypatch, _fake_pipeline())

    def _boom(*a, **kw):
        raise FilelistError("source file vanished")

    monkeypatch.setattr(OpenXc7Fpga, "_write_filelist", _boom)

    res = backend.run()

    assert isinstance(res, FpgaFailResults)
    assert "Filelist error" in res.results["desc"]
    for name, path in stale.items():
        assert not path.exists(), name


def test_openxc7_clears_a_previous_tops_artefacts(tmp_path, monkeypatch):
    """The outputs are named after the design's top, so editing a run's model
    or top would strand the previous top's files in the same artefact dir —
    still at the paths an edit-back would resolve. They go by suffix (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    old_top = {
        name: artefacts / name
        for name in (
            "old_top.bit",
            "old_top.json",
            "old_top.fasm",
            "old_top.frames",
        )
    }
    for path in old_top.values():
        path.write_bytes(b"\x00previous top\x00")
    # Fixed-name inputs the run owns must survive: they carry no managed suffix.
    kept = artefacts / "synth.ys"
    kept.write_text("# generated script\n")

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    for name, path in old_top.items():
        assert not path.exists(), name
    assert kept.exists()


def test_openxc7_failing_timing_still_passes_with_loop_fields(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, tool_overrides=_CHIPDB_OVERRIDES)
    _mock_toolchain(monkeypatch, _fake_pipeline(nextpnr_log="nextpnr_xilinx_fail.log"))
    res = backend.run()
    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert res.results["timing_met"] is False
    assert res.results["wns_ns"] == -2.25
    assert res.results["fmax_mhz"] == 160.0
    path = res.results["failing_paths"][0]
    assert path["source"] == "mult_a_reg.3_SLICE_FFX.Q"
    assert path["destination"] == "product_reg.11_SLICE_FFX.D"


def test_openxc7_stage_failure_names_the_stage(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, tool_overrides=_CHIPDB_OVERRIDES)
    _mock_toolchain(monkeypatch, _fake_pipeline(fail_stage="yosys"))
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "yosys exited with code 1" in res.results["desc"]


def test_openxc7_error_lines_in_stage_log_fail(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path, tool_overrides=_CHIPDB_OVERRIDES)

    def _run(cmd, **kwargs):
        if os.path.basename(cmd[0]) == "yosys":
            kwargs["stdout"].write("ERROR: syntax error, unexpected TOK_ENDMODULE\n")
        return ManagedProcessResult(returncode=0)

    _mock_toolchain(monkeypatch, _run)
    res = backend.run()
    assert isinstance(res, FpgaFailResults)
    assert "1 ERROR(s) in yosys log" in res.results["desc"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _openxc7_project(minimal_project: Path) -> Path:
    (minimal_project / "fpga.yaml").write_text(
        dedent(f"""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "Demo openXC7 run"
                tool: "openxc7"
                model: "example"
                model_path: "models.yaml"
                part: "{_PART}"
                tool_overrides:
                  openxc7:
                    chipdb: "/opt/chipdb/xc7a35t.bin"
        """)
    )
    return minimal_project


def test_cli_openxc7_machine_envelope(minimal_project: Path, capsys, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    _openxc7_project(minimal_project)

    def _run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        exe = os.path.basename(cmd[0])
        if exe == "yosys":
            kwargs["stdout"].write(_fixture("yosys_openxc7.log"))
            (cwd / "example.json").write_text("{}")
        elif exe == "nextpnr-xilinx":
            kwargs["stdout"].write(_fixture("nextpnr_xilinx_fail.log"))
            (cwd / "example.fasm").write_text("")
        return ManagedProcessResult(returncode=0)

    _mock_toolchain(monkeypatch, _run)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_openxc7_machine")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 0, captured
    payload = json.loads(captured.out)
    row = payload["payload"]["results"][0]
    assert row["result"] == "PASS"
    # The timing-closure loop fields ride through machine mode.
    assert row["timing_met"] is False
    assert row["wns_ns"] == -2.25
    assert row["fmax_mhz"] == 160.0
    assert row["failing_paths"][0]["clock"] == "clk"
    # openXC7 cannot measure these; the keys are absent, not null.
    for absent in ("total_power_w", "drc_violations", "whs_ns"):
        assert absent not in row


def test_cli_openxc7_non_7series_part_exits_2(
    minimal_project: Path, capsys, monkeypatch
):
    from rtl_buddy.rtl_buddy import RtlBuddy

    (minimal_project / "fpga.yaml").write_text(
        dedent("""\
            rtl-buddy-filetype: fpga_config
            runs:
              - name: "demo_fpga"
                desc: "openXC7 on an UltraScale+ part"
                tool: "openxc7"
                model: "example"
                model_path: "models.yaml"
                part: "xczu7ev-ffvc1156-2-e"
        """)
    )
    _mock_toolchain(monkeypatch)
    monkeypatch.setattr("sys.argv", ["rb", "--machine", "fpga", "demo_fpga"])
    rb = RtlBuddy(name="test_openxc7_bad_part")
    exit_code = rb.run()
    captured = capsys.readouterr()
    assert exit_code == 2, captured
    payload = json.loads(captured.out)
    assert "7-series" in payload["payload"]["error"]


def test_openxc7_bad_platform_still_clears_artefacts(tmp_path, monkeypatch):
    """Target resolution now runs after the clear, so an unknown `platform:`
    (or a non-7-series part) does not raise over a previous run's netlist,
    FASM and deployable bitstream (#469)."""
    model = ModelConfig(
        name="demo_top", filelist=[], path=str(tmp_path / "models.yaml")
    )
    cfg = FpgaConfig(
        name="demo_fpga",
        desc="demo",
        model=model,
        tool="openxc7",
        part="",
        platform="no_such_platform",
        xdc_files=[],
        _reglvl=None,
        tool_overrides=_CHIPDB_OVERRIDES,
    )
    backend = OpenXc7Fpga(
        name="demo/openxc7",
        fpga_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=None,  # makes resolve_target raise
        executable="openxc7",
        emit_bitstream=True,
    )
    monkeypatch.setattr(
        fpga_openxc7_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    artefacts = Path(backend.artefact_dir)
    stale = {
        name: artefacts / name
        for name in ("demo_top.bit", "demo_top.json", "demo_top.fasm")
    }
    for path in stale.values():
        path.write_bytes(b"\x00stale\x00")

    with pytest.raises(FatalRtlBuddyError, match="platform"):
        backend.run()

    for name, path in stale.items():
        assert not path.exists(), name


def test_openxc7_non_7series_part_still_clears_artefacts(tmp_path, monkeypatch):
    """The 7-series gate is a config error raised after the clear too."""
    backend = _make_backend(tmp_path, part="xczu7ev-ffvc1156-2-e", emit_bitstream=True)
    monkeypatch.setattr(
        fpga_openxc7_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    stale_bit = Path(backend.artefact_dir) / "demo_top.bit"
    stale_bit.write_bytes(b"\x00stale\x00")

    with pytest.raises(FatalRtlBuddyError, match="7-series"):
        backend.run()

    assert not stale_bit.exists()


def test_openxc7_clear_spares_the_test_runners_result_json(tmp_path, monkeypatch):
    """Artefact directories are keyed on a run's *name*, and names are not
    required to be unique across commands — an `rb fpga` run and a simulation
    test called the same thing share `artefacts/<name>/`. The `.json` suffix
    clear must not eat the test runner's durable `result.json` (#469)."""
    from rtl_buddy.tools.artifact_paths import RESULT_JSON_NAME

    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    result_json = artefacts / RESULT_JSON_NAME
    result_json.write_text('{"result": "PASS", "name": "demo_fpga"}')
    dispatch_envelope = artefacts / "result-1234.json"
    dispatch_envelope.write_text('{"result": "PASS"}')
    # The backend's own output still goes.
    stale_netlist = artefacts / "old_top.json"
    stale_netlist.write_text('{"stale": true}')

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert result_json.read_text() == '{"result": "PASS", "name": "demo_fpga"}'
    assert dispatch_envelope.exists()
    assert not stale_netlist.exists()


def test_openxc7_clear_spares_a_co_named_cdc_analysis(tmp_path, monkeypatch):
    """Artefact directories are keyed on a run's name, so an FPGA run and a
    CDC analysis called the same thing share `artefacts/<name>/`. The `.json`
    suffix clear must not eat the analyzer's report or its domain maps (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    siblings = {
        "cdc.json": '{"summary": {"violations": 3}}',
        "cdc.txt": "3 violations\n",
        "domain_map.json": '{"clocks": []}',
        "reset_map.json": '{"reset_synchronizers": []}',
    }
    for name, body in siblings.items():
        (artefacts / name).write_text(body)
    stale_netlist = artefacts / "old_top.json"
    stale_netlist.write_text('{"stale": true}')

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    for name, body in siblings.items():
        assert (artefacts / name).read_text() == body, name
    assert not stale_netlist.exists()


def test_openxc7_stage_failure_removes_the_earlier_stages_outputs(
    tmp_path, monkeypatch
):
    """Each stage writes its output before the next reads it, so a pipeline
    that dies at `fasm2frames` leaves the netlist and FASM its predecessors
    wrote. A run that reports FAIL publishes nothing (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    monkeypatch.setenv("PRJXRAY_DB_DIR", "/opt/prjxray-db")
    artefacts = Path(backend.artefact_dir)

    def _dies_at_fasm2frames(cmd, **kwargs):
        if os.path.basename(cmd[0]) == "fasm2frames":
            return ManagedProcessResult(returncode=1)
        return _fake_pipeline()(cmd, **kwargs)

    _mock_toolchain(monkeypatch, _dies_at_fasm2frames)
    res = backend.run()

    assert isinstance(res, FpgaFailResults)
    assert "fasm2frames exited with code 1" in res.results["desc"]
    # yosys and nextpnr had already published these.
    assert not (artefacts / "demo_top.json").exists()
    assert not (artefacts / "demo_top.fasm").exists()
    assert not (artefacts / "demo_top.bit").exists()


def test_openxc7_clear_spares_a_co_named_tests_build_stamp(tmp_path, monkeypatch):
    """An unshared simulator build writes `rb-compile-stamp.json` straight
    into `artefacts/<test>/`, so a co-named FPGA run's `.json` clear would
    silently invalidate the build cache and force a recompile (#469)."""
    from rtl_buddy.tools.artifact_paths import SHARED_BUILD_STAMP_NAME

    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    stamp = Path(backend.artefact_dir) / SHARED_BUILD_STAMP_NAME
    stamp.write_text('{"compile_key": "abc123"}')

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert stamp.read_text() == '{"compile_key": "abc123"}'


def test_openxc7_clear_spares_a_co_named_graph_build(tmp_path, monkeypatch):
    """`rb graph` writes `graph.json`, `graph-meta.json` and the results
    overlay directly into `artefacts/graph/`, so an FPGA run named `graph`
    shares the directory and the `.json` suffix clear would take all three
    (#469)."""
    from rtl_buddy.graph.config_tier import GRAPH_JSON_NAME, GRAPH_META_NAME
    from rtl_buddy.graph.results import RESULTS_OVERLAY_NAME

    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    graph_outputs = {
        GRAPH_JSON_NAME: '{"schema_version": 1, "nodes": []}',
        GRAPH_META_NAME: '{"fingerprint": "abc123"}',
        RESULTS_OVERLAY_NAME: '{"results": {}}',
    }
    for name, body in graph_outputs.items():
        (artefacts / name).write_text(body)
    stale_netlist = artefacts / "old_top.json"
    stale_netlist.write_text('{"stale": true}')

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    for name, body in graph_outputs.items():
        assert (artefacts / name).read_text() == body, name
    assert not stale_netlist.exists()


def test_openxc7_clear_spares_a_co_named_cov_and_xplr_output(tmp_path, monkeypatch):
    """Same for `rb cov`'s manifest and model, which sit directly in a
    user-named coverage directory (#469)."""
    from rtl_buddy.cov.manifest import MANIFEST_FILENAME
    from rtl_buddy.cov.model import MODEL_FILENAME

    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)
    for name in (MANIFEST_FILENAME, MODEL_FILENAME):
        (artefacts / name).write_text('{"kept": true}')

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    for name in (MANIFEST_FILENAME, MODEL_FILENAME):
        assert (artefacts / name).exists(), name


def test_openxc7_clears_its_own_netlist_when_the_top_collides(tmp_path, monkeypatch):
    """A design topped `graph` writes `graph.json`, which is `rb graph`'s
    protected name. If the flow cannot clear its own netlist, a Yosys run
    that exits 0 without writing hands the *previous* run's JSON to nextpnr
    and the run reports success against a design it never synthesised
    (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    monkeypatch.setattr(type(backend.fpga_cfg), "get_top", lambda _self: "graph")

    artefacts = Path(backend.artefact_dir)
    stale_netlist = artefacts / "graph.json"
    stale_netlist.write_text('{"from": "a previous run"}')

    seen: dict[str, bool] = {}

    def _yosys_writes_nothing(cmd, **kwargs):
        exe = os.path.basename(cmd[0])
        if exe == "yosys":
            # Exits 0 with a clean log, but produces no netlist.
            kwargs["stdout"].write(_fixture("yosys_openxc7.log"))
            return ManagedProcessResult(returncode=0)
        if exe == "nextpnr-xilinx":
            seen["netlist_present"] = (artefacts / "graph.json").exists()
            kwargs["stdout"].write(_fixture("nextpnr_xilinx_pass.log"))
            (artefacts / "graph.fasm").write_text("# fasm\n")
        return ManagedProcessResult(returncode=0)

    _mock_toolchain(monkeypatch, _yosys_writes_nothing)
    backend.run()

    # nextpnr must not have been handed the previous run's netlist.
    assert seen["netlist_present"] is False


def test_openxc7_stage_failure_clears_a_colliding_top_netlist(tmp_path, monkeypatch):
    """And the failure path clears it too — a FAIL publishes nothing, even
    when the netlist's name belongs to a sibling command (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=True, tool_overrides=_CHIPDB_OVERRIDES
    )
    monkeypatch.setattr(type(backend.fpga_cfg), "get_top", lambda _self: "manifest")
    monkeypatch.setenv("PRJXRAY_DB_DIR", "/opt/prjxray-db")
    artefacts = Path(backend.artefact_dir)

    def _dies_at_fasm2frames(cmd, **kwargs):
        exe = os.path.basename(cmd[0])
        if exe == "fasm2frames":
            return ManagedProcessResult(returncode=1)
        if exe == "yosys":
            kwargs["stdout"].write(_fixture("yosys_openxc7.log"))
            (artefacts / "manifest.json").write_text("{}")
            return ManagedProcessResult(returncode=0)
        if exe == "nextpnr-xilinx":
            kwargs["stdout"].write(_fixture("nextpnr_xilinx_pass.log"))
            (artefacts / "manifest.fasm").write_text("# fasm\n")
        return ManagedProcessResult(returncode=0)

    _mock_toolchain(monkeypatch, _dies_at_fasm2frames)
    res = backend.run()

    assert isinstance(res, FpgaFailResults)
    assert not (artefacts / "manifest.json").exists()
    assert not (artefacts / "manifest.fasm").exists()


def test_openxc7_clears_a_renamed_tops_protected_netlist(tmp_path, monkeypatch):
    """Ownership is durable. A run topped `graph` claims `graph.json`; after
    the top is renamed, `own` names only the new top and `graph.json` matches
    `rb graph`'s protected name again — without the ledger it would survive
    every clear from then on (#469)."""
    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)

    def _pipeline(top):
        def _run(cmd, **kwargs):
            exe = os.path.basename(cmd[0])
            if exe == "yosys":
                kwargs["stdout"].write(_fixture("yosys_openxc7.log"))
                (artefacts / f"{top}.json").write_text("{}")
                return ManagedProcessResult(returncode=0)
            if exe == "nextpnr-xilinx":
                kwargs["stdout"].write(_fixture("nextpnr_xilinx_pass.log"))
                (artefacts / f"{top}.fasm").write_text("# fasm\n")
            return ManagedProcessResult(returncode=0)

        return _run

    # Run 1: top is `graph`, which is also `rb graph`'s protected basename.
    monkeypatch.setattr(type(backend.fpga_cfg), "get_top", lambda _self: "graph")
    _mock_toolchain(monkeypatch, _pipeline("graph"))
    assert isinstance(backend.run(), FpgaPassResults)
    assert (artefacts / "graph.json").exists()

    # Run 2: the top is renamed. The previous top's netlist is this flow's
    # output, not a sibling's, and must go.
    monkeypatch.setattr(type(backend.fpga_cfg), "get_top", lambda _self: "other_top")
    _mock_toolchain(monkeypatch, _pipeline("other_top"))
    assert isinstance(backend.run(), FpgaPassResults)

    assert not (artefacts / "graph.json").exists()
    assert not (artefacts / "graph.fasm").exists()
    assert (artefacts / "other_top.json").exists()


def test_openxc7_does_not_inherit_a_co_named_pnr_runs_claim(tmp_path, monkeypatch):
    """A P&R run and an FPGA run sharing a name share `artefacts/<name>/` and
    therefore one ledger. A claim bypasses the suffix filter, so inheriting
    P&R's would delete its routed database outright even though
    `.routed.odb` is none of the FPGA suffixes (#469)."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    backend = _make_backend(
        tmp_path, emit_bitstream=False, tool_overrides=_CHIPDB_OVERRIDES
    )
    artefacts = Path(backend.artefact_dir)

    # The co-named P&R run claims and writes its outputs here first.
    clear_managed_outputs(
        artefacts,
        (".def", ".routed.odb"),
        owner="demo_fpga",
        own=["demo_top.def", "demo_top.routed.odb"],
        own_flow="pnr-openroad",
    )
    odb = artefacts / "demo_top.routed.odb"
    odb.write_bytes(b"the P&R run's routed database")

    _mock_toolchain(monkeypatch, _fake_pipeline())
    res = backend.run()

    assert isinstance(res, FpgaPassResults), res.results["desc"]
    assert odb.exists(), "the FPGA run inherited the P&R run's claim"
