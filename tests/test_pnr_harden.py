"""`harden:` — a P&R run that publishes a hard-macro abstract (#95).

The run renders two extra OpenROAD commands, forces a strict stream-out,
and on success publishes `abstract/<top>.{lef,lib,gds}` plus a fingerprint
manifest, all or nothing. OpenROAD and KLayout are faked: the fakes write
what the real tools would, so the tests exercise the backend's own logic.
"""

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import GdsMode, PnrConfig, PnrFloorplan, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_abstract, pnr_openroad
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

_GDS = b"\x00\x06\x00\x02\x00\x07block"
_LEF = "MACRO demo_top\nEND demo_top\n"
_LIB = "library (demo_top) { cell (demo_top) { } }\n"


def _touch(*paths):
    for path in paths:
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch()


def _pdk(tmp_path):
    pdk = PdkConfig(
        PdkConfigFile(
            name="nangate45",
            site="core",
            corners={"typ": "pdk/lib/typ.lib"},
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
            tie_hi="LOGIC1_X1/Z",
            tie_lo="LOGIC0_X1/Z",
            fill_cells=["FILLCELL_X1"],
            klayout_tech="pdk/klayout/tech.lyt",
            cell_gds="pdk/gds/cells.gds",
        ),
        str(tmp_path / "root_config.yaml"),
    )
    _touch(
        pdk.get_klayout_tech(),
        pdk.get_tech_lef(),
        pdk.get_macro_lef(),
        *pdk.get_cell_gds_paths(),
    )
    Path(pdk.get_tech_lef()).write_text("TECH\n")
    return pdk


def _platform(pdk, **overrides):
    base = dict(name="nangate45_typ", pdk="nangate45", cts_buffer="BUF_X4")
    base.update(overrides)
    return PnrPlatformConfig(PnrPlatformConfigFile(**base), lambda _name: pdk)


def _pnr_cfg(tmp_path, **overrides):
    base = dict(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(tmp_path / "constraints.sdc"),
        platform="nangate45_typ",
        floorplan=PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=1000,
        tool_overrides=None,
        harden=True,
    )
    base.update(overrides)
    return PnrConfig(**base)


def _write_synth(tmp_path):
    """A synth entry the back-reference resolves, with a synth.f behind it."""
    (tmp_path / "root_config.yaml").write_text("")
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl/demo_top.sv").write_text("module demo_top; endmodule\n")
    (tmp_path / "constraints.sdc").write_text("create_clock -period 10 clk\n")
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
            tool: "yosys"
            reglvl: 0
        """)
    )
    synth_art = tmp_path / "artefacts/demo_synth"
    synth_art.mkdir(parents=True, exist_ok=True)
    (synth_art / "synth_netlist.v").write_text("module demo_top; endmodule\n")
    (synth_art / "synth.f").write_text(
        "+incdir+../../rtl\n+define+SYNTH\n../../rtl/demo_top.sv\n"
    )


def _backend(tmp_path, monkeypatch, *, platform=None, pnr_overrides=None, **kw):
    _write_synth(tmp_path)
    pdk = _pdk(tmp_path)
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **k: nullcontext())
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: "/opt/klayout")
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform or _platform(pdk)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_pnr_cfg(tmp_path, **(pnr_overrides or {})),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        **kw,
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: "26Q2-1")
    return backend


def _fake_tools(backend, monkeypatch, *, lef=True, lib=True, missing=()):
    """OpenROAD that routes and stages the requested views; KLayout that
    streams out a GDS with `missing` cells empty."""
    artefacts = Path(backend.artefact_dir)
    staging = artefacts / pnr_abstract.ABSTRACT_STAGING_NAME
    calls = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        if "-log" in cmd:
            Path(cmd[cmd.index("-log") + 1]).write_text(
                "Design area 100.0 um^2 10% utilization\n"
            )
            (artefacts / "demo_top.routed.odb").write_bytes(b"\x00odb\x00")
            (artefacts / "demo_top.def").write_text("DESIGN demo_top ;\n")
            staging.mkdir(exist_ok=True)
            if lef:
                (staging / "demo_top.lef").write_text(_LEF)
            if lib:
                (staging / "demo_top.lib").write_text(_LIB)
            result.returncode = 0
            return result
        (artefacts / "demo_top.gds").write_bytes(_GDS)
        (artefacts / "def2stream.report.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "design": "demo_top",
                    "complete": not missing,
                    "missing_cells": list(missing),
                    "allowed_empty_cells": [],
                    "orphan_cells": [],
                    "other_errors": 0,
                    "errors": len(missing),
                }
            )
        )
        result.returncode = len(missing)
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# Configuration and rendering
# ---------------------------------------------------------------------------


def _suite(tmp_path, extra=""):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent(f"""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk
            desc: block
            synth: s
            synth-path: synth.yaml
            platform: p
        {extra}""")
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0]


def test_harden_defaults_off_and_parses(tmp_path):
    assert _suite(tmp_path).get_harden() is False
    assert _suite(tmp_path, "    harden: true\n").get_harden() is True


def _render(tmp_path, harden):
    pnr_cfg = _pnr_cfg(tmp_path, harden=harden)
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    backend = OpenRoadPnr(
        name="d", pnr_cfg=pnr_cfg, suite_dir=str(tmp_path), root_cfg=MagicMock()
    )
    platform = _platform(_pdk(tmp_path))
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


def test_a_run_that_does_not_harden_renders_the_flow_unchanged(tmp_path):
    """Acceptance 6: no `harden:` means the script it always had."""
    text = _render(tmp_path, harden=False)
    assert "write_abstract_lef" not in text
    assert "write_timing_model" not in text
    assert 'routed.odb\n\nputs ">>> DONE"' in text


def test_a_hardening_run_writes_its_views_after_the_database(tmp_path):
    text = _render(tmp_path, harden=True)
    staged = f"$OUT_DIR/{pnr_abstract.ABSTRACT_STAGING_NAME}"
    lef = text.index(
        f"write_abstract_lef -bloat_occupied_layers {staged}/${{DESIGN}}.lef"
    )
    lib = text.index(f"write_timing_model {staged}/${{DESIGN}}.lib")
    assert text.index("write_db") < lef < lib < text.index('puts ">>> DONE"')


def test_harden_forces_a_strict_stream_out(tmp_path, monkeypatch):
    """A hole in a hardened block is never acceptable, whatever was asked."""
    backend = _backend(tmp_path, monkeypatch, gds_mode="preview")
    assert backend.emit_gds is True
    assert backend.gds_mode == GdsMode.STRICT


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_hardening_run_publishes_the_abstract_and_its_manifest(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    _fake_tools(backend, monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    out = Path(backend.artefact_dir) / "abstract"
    assert sorted(p.name for p in out.iterdir()) == [
        "abstract.manifest.json",
        "demo_top.gds",
        "demo_top.lef",
        "demo_top.lib",
    ]
    assert not (Path(backend.artefact_dir) / "abstract.partial").exists()
    assert res.results["abstract_dir"] == str(out)
    assert res.results["abstract_manifest"] == str(out / "abstract.manifest.json")

    manifest = json.loads((out / "abstract.manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["block"] == "demo_top"
    assert manifest["pnr_run"] == "demo_pnr"
    assert manifest["platform"] == "nangate45_typ"
    assert manifest["pdk"] == "nangate45"
    assert manifest["tool"]["version"] == "26Q2-1"
    assert manifest["technology"]["tech_lef"]["path"] == "pdk/lef/tech.lef"
    assert manifest["technology"]["liberty"]["path"] == "pdk/lib/typ.lib"
    # Published paths, project-relative — never the staging directory.
    assert manifest["outputs"]["gds"] == {
        "path": "artefacts/demo_pnr/abstract/demo_top.gds",
        "size": len(_GDS),
        "sha256": _sha(_GDS),
    }
    assert manifest["outputs"]["lib"]["sha256"] == _sha(_LIB.encode())
    assert manifest["outputs"]["lef"]["sha256"] == _sha(_LEF.encode())
    inputs = manifest["inputs"]
    # The synth filelist's sources, not its option lines.
    assert [r["path"] for r in inputs["rtl"]] == ["rtl/demo_top.sv"]
    assert inputs["netlist"]["path"] == "artefacts/demo_synth/synth_netlist.v"
    assert inputs["sdc"]["path"] == "constraints.sdc"
    assert inputs["pin_constraints"] is None
    assert inputs["lef"][0]["path"] == "pdk/lef/tech.lef"
    assert manifest["config"]["floorplan"]["utilization"] == 0.55
    # What the run is configured to read, not only what it read.
    assert manifest["config"]["constraints"] == "constraints.sdc"
    assert manifest["config"]["synth"] == {"name": "demo_synth", "path": "synth.yaml"}
    assert manifest["config"]["lib_paths"] == []
    assert manifest["config"]["sha256"] == pnr_abstract.config_digest(
        {k: v for k, v in manifest["config"].items() if k != "sha256"}
    )


@pytest.mark.parametrize("dropped", ["lef", "lib"])
def test_a_view_openroad_did_not_write_fails_the_run_and_publishes_nothing(
    tmp_path, monkeypatch, dropped
):
    backend = _backend(tmp_path, monkeypatch)
    _fake_tools(backend, monkeypatch, **{dropped: False})

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "abstract"
    assert f"demo_top.{dropped}" in res.results["desc"]
    assert not (Path(backend.artefact_dir) / "abstract").exists()
    assert not (Path(backend.artefact_dir) / "abstract.partial").exists()
    # P&R itself finished: its database stays for `rb power`.
    assert (Path(backend.artefact_dir) / "demo_top.routed.odb").exists()


def test_an_incomplete_stream_out_fails_as_export_and_publishes_nothing(
    tmp_path, monkeypatch
):
    backend = _backend(tmp_path, monkeypatch)
    _fake_tools(backend, monkeypatch, missing=["sram_macro"])

    res = backend.run()

    assert res.results["fail_stage"] == "export"
    assert not (Path(backend.artefact_dir) / "abstract").exists()
    assert not (Path(backend.artefact_dir) / "abstract.partial").exists()


def test_a_rerun_withdraws_the_previous_abstract_even_when_it_fails_early(
    tmp_path, monkeypatch
):
    """The abstract describes the result a rerun replaces (#469)."""
    backend = _backend(tmp_path, monkeypatch)
    _fake_tools(backend, monkeypatch)
    assert backend.run().is_pass()
    assert (Path(backend.artefact_dir) / "abstract").is_dir()

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: None)
    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert not (Path(backend.artefact_dir) / "abstract").exists()


def test_a_run_that_stopped_hardening_clears_the_old_abstract(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    _fake_tools(backend, monkeypatch)
    assert backend.run().is_pass()

    plain = _backend(tmp_path, monkeypatch, pnr_overrides={"harden": False})
    _fake_tools(plain, monkeypatch)
    assert plain.run().is_pass()
    assert not (Path(plain.artefact_dir) / "abstract").exists()


def test_harden_on_a_multi_corner_platform_is_refused_before_openroad(
    tmp_path, monkeypatch
):
    pdk = _pdk(tmp_path)
    platform = MagicMock(wraps=_platform(pdk))
    platform.is_multi_corner.return_value = True
    platform.get_pdk.return_value = pdk
    backend = _backend(tmp_path, monkeypatch, platform=platform)
    calls = _fake_tools(backend, monkeypatch)

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "single-corner" in res.results["desc"]
    assert not calls


def test_filelist_sources_skips_options_and_resolves_against_the_filelist(tmp_path):
    fl = tmp_path / "a/synth.f"
    fl.parent.mkdir()
    fl.write_text(
        "// comment\n+incdir+inc\n+define+X=1\n-y lib\n-f other.f\n"
        "top.sv\n-v ../cells.v\n\n"
    )
    assert pnr_abstract.filelist_sources(str(fl)) == [
        str(tmp_path / "a/top.sv"),
        str(tmp_path / "cells.v"),
    ]
    assert pnr_abstract.filelist_sources(str(tmp_path / "absent.f")) == []
