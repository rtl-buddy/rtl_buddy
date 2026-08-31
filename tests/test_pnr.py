"""Tests for the P&R config schema, OpenRoadPnr backend, and rb pnr wiring."""

import os
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PnrConfig, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.config.synth import SynthPlatformConfig, SynthPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.pnr_results import (
    PnrFailResults,
    PnrPassResults,
    PnrSkipResults,
)


# ---------------------------------------------------------------------------
# PdkConfig
# ---------------------------------------------------------------------------


def _make_pdk_cfg(tmp_path, **overrides):
    base = dict(
        name="nangate45",
        site="FreePDK45_38x28_10R_NP_162NW_34O",
        corners={"typ": "pdk/lib/typ.lib", "slow": "pdk/lib/slow.lib"},
        tech_lef="pdk/lef/tech.lef",
        macro_lef="pdk/lef/cells.lef",
        tie_hi="LOGIC1_X1/Z",
        tie_lo="LOGIC0_X1/Z",
        fill_cells=["FILLCELL_X1", "FILLCELL_X2"],
    )
    base.update(overrides)
    return PdkConfig(PdkConfigFile(**base), str(tmp_path / "root_config.yaml"))


def test_pdk_resolves_corner_paths(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    assert pdk.get_corner_path("typ") == str(tmp_path / "pdk" / "lib" / "typ.lib")
    assert pdk.get_corner_path("slow") == str(tmp_path / "pdk" / "lib" / "slow.lib")
    assert pdk.get_corners() == ["typ", "slow"]
    assert pdk.get_default_corner() == "typ"


def test_pdk_unknown_corner_raises(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="has no corner 'fast'"):
        pdk.get_corner_path("fast")


def test_pdk_no_corners_raises(tmp_path):
    pdk = _make_pdk_cfg(tmp_path, corners={})
    with pytest.raises(FatalRtlBuddyError, match="declares no corners"):
        pdk.get_default_corner()


def test_pdk_exposes_site_tie_and_fill(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    assert pdk.get_site() == "FreePDK45_38x28_10R_NP_162NW_34O"
    assert pdk.get_tie_hi() == "LOGIC1_X1/Z"
    assert pdk.get_tie_lo() == "LOGIC0_X1/Z"
    assert pdk.get_fill_cells() == ["FILLCELL_X1", "FILLCELL_X2"]


# ---------------------------------------------------------------------------
# SynthPlatformConfig — pdk lookup, corner resolution, lef composition
# ---------------------------------------------------------------------------


def test_synth_platform_defaults_to_first_corner(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_typ", pdk="nangate45"),
        lambda _name: pdk,
    )
    assert cfg.get_corner() == "typ"
    assert cfg.get_path().endswith("typ.lib")


def test_synth_platform_explicit_corner(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_slow", pdk="nangate45", corner="slow"),
        lambda _name: pdk,
    )
    assert cfg.get_corner() == "slow"
    assert cfg.get_path().endswith("slow.lib")


def test_synth_platform_lef_paths_are_pdk_lefs_only(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_typ", pdk="nangate45"),
        lambda _name: pdk,
    )
    assert cfg.get_lef_paths() == [
        str(tmp_path / "pdk" / "lef" / "tech.lef"),
        str(tmp_path / "pdk" / "lef" / "cells.lef"),
    ]


# ---------------------------------------------------------------------------
# PnrPlatformConfig — pdk + sta corner
# ---------------------------------------------------------------------------


def test_pnr_platform_defaults_to_first_corner(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = PnrPlatformConfig(
        PnrPlatformConfigFile(name="nangate45_typ", pdk="nangate45"),
        lambda _name: pdk,
    )
    assert cfg.get_sta_corner() == "typ"
    assert cfg.get_sta_lib_path().endswith("typ.lib")


def test_pnr_platform_unknown_sta_corner_raises(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="has no corner 'fast'"):
        PnrPlatformConfig(
            PnrPlatformConfigFile(
                name="nangate45_fast", pdk="nangate45", sta_corner="fast"
            ),
            lambda _name: pdk,
        )


# ---------------------------------------------------------------------------
# PnrSuiteConfig — YAML loading + initialise
# ---------------------------------------------------------------------------


_PNR_YAML = dedent("""\
    rtl-buddy-filetype: pnr_config

    runs:
      - name: "demo_pnr"
        desc: "Demo run"
        tool: "openroad"
        synth: "demo_synth_nangate45"
        synth-path: "../synth/synth.yaml"
        constraints: "../synth/constraints.sdc"
        platform: "nangate45_typ"
        floorplan:
          utilization: 0.6
          aspect: 1.0
          core-margin: 3.0
        reglvl: 1000
""")


def test_pnr_suite_loads_runs(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(_PNR_YAML)
    suite = PnrSuiteConfig(str(pnr_yaml))
    assert suite.get_run_names() == ["demo_pnr"]
    run = suite.get_runs("demo_pnr")[0]
    assert run.get_name() == "demo_pnr"
    assert run.get_platform() == "nangate45_typ"
    assert run.get_floorplan().utilization == pytest.approx(0.6)
    assert run.get_floorplan().core_margin == pytest.approx(3.0)
    assert run.get_reglvl("openroad") == 1000
    # synth-path and constraints are resolved relative to pnr.yaml
    assert run.get_synth_suite_path() == str(tmp_path.parent / "synth" / "synth.yaml")
    assert run.get_constraints() == str(tmp_path.parent / "synth" / "constraints.sdc")


def test_pnr_suite_missing_synth_raises(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(
        dedent("""\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: "demo_pnr"
                desc: "Demo run"
                tool: "openroad"
                synth-path: "../synth/synth.yaml"
                constraints: "../synth/constraints.sdc"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'synth'"):
        PnrSuiteConfig(str(pnr_yaml))


def test_pnr_suite_missing_platform_raises(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(
        dedent("""\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: "demo_pnr"
                desc: "Demo run"
                tool: "openroad"
                synth: "demo_synth_nangate45"
                synth-path: "../synth/synth.yaml"
                constraints: "../synth/constraints.sdc"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'platform'"):
        PnrSuiteConfig(str(pnr_yaml))


def test_pnr_suite_unknown_run_raises(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(_PNR_YAML)
    suite = PnrSuiteConfig(str(pnr_yaml))
    with pytest.raises(FatalRtlBuddyError, match="not found in suite"):
        suite.get_runs("does_not_exist")


# ---------------------------------------------------------------------------
# OpenRoadPnr — backend skip / template render (without invoking openroad)
# ---------------------------------------------------------------------------


def _make_pnr_cfg(tmp_path):
    from rtl_buddy.config.pnr import PnrFloorplan

    return PnrConfig(
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
    )


def test_pnr_runner_resolves_executable_from_cfg_pnr_tools(tmp_path):
    """PnrRunner should resolve the executable via cfg-pnr-tools when present."""
    from rtl_buddy.config.pnr import PnrToolConfig, PnrToolConfigFile
    from rtl_buddy.runner.pnr_runner import PnrRunner

    tool_cfg = PnrToolConfig(
        PnrToolConfigFile(name="openroad", tool="/opt/openroad/bin/openroad")
    )
    root_cfg = MagicMock()
    root_cfg.get_pnr_tool_cfg.return_value = tool_cfg
    runner = PnrRunner(
        name="demo",
        root_cfg=root_cfg,
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        reglvl_filter=1000,
    )
    with patch("rtl_buddy.runner.pnr_runner.OpenRoadPnr") as mock_backend:
        mock_backend.return_value.run.return_value = PnrSkipResults(
            name="demo/results", desc="stub"
        )
        runner.run()
    _, kwargs = mock_backend.call_args
    assert kwargs["openroad_executable"] == "/opt/openroad/bin/openroad"


def test_pnr_runner_falls_back_to_bare_tool_name_when_no_cfg(tmp_path):
    """Without a matching cfg-pnr-tools entry, the bare tool name is used."""
    from rtl_buddy.runner.pnr_runner import PnrRunner

    root_cfg = MagicMock()
    root_cfg.get_pnr_tool_cfg.return_value = None
    runner = PnrRunner(
        name="demo",
        root_cfg=root_cfg,
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        reglvl_filter=1000,
    )
    with patch("rtl_buddy.runner.pnr_runner.OpenRoadPnr") as mock_backend:
        mock_backend.return_value.run.return_value = PnrSkipResults(
            name="demo/results", desc="stub"
        )
        runner.run()
    _, kwargs = mock_backend.call_args
    assert kwargs["openroad_executable"] == "openroad"


def test_openroad_pnr_skips_when_executable_missing(tmp_path):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        openroad_executable="this-binary-does-not-exist-xyz",
    )
    with patch("shutil.which", return_value=None):
        result = backend.run()
    assert isinstance(result, PnrFailResults)
    assert "not found" in result.results["desc"]


def test_openroad_pnr_template_substitutes_all_placeholders(tmp_path):
    """Templating should resolve every `{{ key }}` placeholder."""
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pdk = _make_pdk_cfg(tmp_path)
    platform = PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            cts_buffer="BUF_X4",
        ),
        lambda _name: pdk,
    )
    # routing-layers default empty strings → still substitute, just produce empty values.

    pnr_cfg = _make_pnr_cfg(tmp_path)
    # Stub the synth-side resolution so we don't have to materialize a synth.yaml.
    resolved_synth = MagicMock()
    resolved_synth.get_top.return_value = "demo_top"
    resolved_synth.get_name.return_value = "demo_synth"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=resolved_synth)

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=pnr_cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    script_path = backend._write_script(platform, pnr_cfg.get_floorplan())
    text = Path(script_path).read_text()

    assert "set DESIGN          demo_top" in text
    assert "set SITE            FreePDK45_38x28_10R_NP_162NW_34O" in text
    assert "set CORE_UTIL_PCT   55.00" in text
    assert "set TIEHI_CELL_PORT LOGIC1_X1/Z" in text
    assert "set CTS_BUF         BUF_X4" in text
    # No leftover placeholders
    assert "{{" not in text
    assert "}}" not in text


# ---------------------------------------------------------------------------
# PnrResults shapes
# ---------------------------------------------------------------------------


def test_pnr_pass_result_carries_metrics():
    r = PnrPassResults(
        name="demo/results",
        area_um2=3213.0,
        cell_count=1392,
        wns_setup_ps=4350.0,
        wns_hold_ps=80.0,
        tns_ps=0.0,
        drc_count=0,
    )
    assert r.is_pass()
    assert r.results["area_um2"] == 3213.0
    assert r.results["cell_count"] == 1392
    assert r.results["wns_setup_ps"] == 4350.0
    assert r.results["drc_count"] == 0


def test_pnr_skip_is_pass():
    r = PnrSkipResults(name="demo/results", desc="reglvl above filter")
    assert r.is_pass()
    assert r.results["result"] == "SKIP"


def test_pnr_fail_is_not_pass():
    r = PnrFailResults(name="demo/results", desc="OpenROAD exited with code 1")
    assert not r.is_pass()
    assert r.results["result"] == "FAIL"


# ---------------------------------------------------------------------------
# OpenROAD version probe + KLayout helpers
# ---------------------------------------------------------------------------


def test_parse_version_token_handles_yyqn_and_semver():
    from rtl_buddy.tools.pnr_openroad import _parse_version_token

    assert _parse_version_token("26Q2-911-g731f") == (26, 2)
    assert _parse_version_token("v2.0-1234-gabcd") == (2, 0)
    assert _parse_version_token("25Q1") == (25, 1)
    # Comparison: 26Q2 ranks above 25Q1
    assert _parse_version_token("26Q2") > _parse_version_token("25Q1")
    # Unparseable falls back to a string tuple
    assert _parse_version_token("nightly-build") == ("nightly-build",)


def test_resolve_klayout_exe_uses_path_first(monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")
    assert pnr_openroad._resolve_klayout_exe() == "/opt/klayout"


def test_resolve_klayout_exe_returns_none_when_missing(monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: None)
    assert pnr_openroad._resolve_klayout_exe() is None


def test_pnr_pass_result_carries_gds_and_png_paths():
    r = PnrPassResults(
        name="demo/results",
        area_um2=100.0,
        gds_path="/tmp/demo.gds",
        png_path="/tmp/demo.png",
    )
    assert r.results["gds_path"] == "/tmp/demo.gds"
    assert r.results["png_path"] == "/tmp/demo.png"


def test_openroad_pnr_png_implies_gds():
    """`--png` without `--gds` should still trigger GDS streamout."""
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend = OpenRoadPnr.__new__(OpenRoadPnr)
    # Mimic __init__ for just the gds-implication knob.
    OpenRoadPnr.__init__(
        backend,
        name="demo",
        pnr_cfg=MagicMock(get_name=MagicMock(return_value="demo")),
        suite_dir=str(__import__("tempfile").mkdtemp()),
        root_cfg=MagicMock(),
        emit_gds=False,
        emit_png=True,
    )
    assert backend.emit_gds is True
    assert backend.emit_png is True


def test_def2stream_treats_nonzero_exit_as_warning_when_gds_exists(
    tmp_path, monkeypatch
):
    """KLayout's def2stream exits non-zero when a LEF-only macro (e.g. ORFS
    fakeram45) has no matching GDS body, but the streamout still produces a
    valid GDS with the macro as an empty placeholder. The runner should keep
    that GDS (so `--png` can still render it) and just log a warning."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_pdk_cfg(
        tmp_path,
        klayout_tech="pdk/klayout/tech.lyt",
        cell_gds="pdk/gds/cells.gds",
    )
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    design = "demo_top"
    out_gds = Path(backend.artefact_dir) / f"{design}.gds"

    def _fake_run(cmd, **_kwargs):
        # Simulate KLayout writing a non-empty GDS but exiting non-zero
        # because of a benign per-cell `[ERROR]` line.
        out_gds.write_bytes(b"\x00\x06\x00\x02\x00\x07")
        result = MagicMock()
        result.returncode = 1
        result.stdout = "[ERROR] LEF Cell 'foo' has no matching GDS/OAS cell.\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    returned = backend._run_def2stream(platform, design)
    assert returned == str(out_gds), (
        "non-empty GDS should be returned even on non-zero exit"
    )
    assert out_gds.exists() and out_gds.stat().st_size > 0


def test_def2stream_treats_empty_gds_as_failure(tmp_path, monkeypatch):
    """If KLayout fails before producing any GDS bytes the runner should
    still return None so downstream PNG render is skipped."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_pdk_cfg(
        tmp_path,
        klayout_tech="pdk/klayout/tech.lyt",
        cell_gds="pdk/gds/cells.gds",
    )
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    def _fake_run(cmd, **_kwargs):
        # No GDS file written, exit non-zero.
        result = MagicMock()
        result.returncode = 1
        result.stdout = "fatal: unable to load tech file"
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    returned = backend._run_def2stream(platform, "demo_top")
    assert returned is None


def test_def2stream_ignores_a_previous_runs_gds(tmp_path, monkeypatch):
    """A failed streamout must not return the GDS an earlier run left behind
    — it would then be rendered and reported as this run's layout (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_pdk_cfg(
        tmp_path,
        klayout_tech="pdk/klayout/tech.lyt",
        cell_gds="pdk/gds/cells.gds",
    )
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    stale_gds = Path(backend.artefact_dir) / "demo_top.gds"
    stale_gds.write_bytes(b"\x00\x06\x00\x02\x00\x07")

    def _fake_run(cmd, **_kwargs):
        # KLayout dies before writing anything.
        result = MagicMock()
        result.returncode = 1
        result.stdout = "fatal: unable to load tech file"
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    assert backend._run_def2stream(platform, "demo_top") is None
    assert not stale_gds.exists()


_PNR_XFAIL_YAML = dedent("""\
    rtl-buddy-filetype: pnr_config

    runs:
      - name: "pnr_xfail"
        desc: "expected-fail pnr, non-strict"
        tool: "openroad"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
        xfail: true
      - name: "pnr_xfail_strict"
        desc: "expected-fail pnr, strict"
        tool: "openroad"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
        xfail_strict: true
      - name: "pnr_normal"
        desc: "normal"
        tool: "openroad"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
""")


def test_pnr_suite_loads_xfail_flags(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(_PNR_XFAIL_YAML)
    suite = PnrSuiteConfig(str(pnr_yaml))
    assert suite.get_runs("pnr_xfail")[0].is_xfail() is True
    assert suite.get_runs("pnr_xfail")[0].get_xfail_strict() is False
    assert suite.get_runs("pnr_xfail_strict")[0].is_xfail() is True
    assert suite.get_runs("pnr_xfail_strict")[0].get_xfail_strict() is True
    assert suite.get_runs("pnr_normal")[0].is_xfail() is False


# ---------------------------------------------------------------------------
# Pre-run artefact clearing (#469)
# ---------------------------------------------------------------------------


def test_pnr_clear_list_covers_every_non_log_template_output():
    """`run_output_paths` and `flow.tcl.template` must not drift apart: every
    non-log file the template writes under `$OUT_DIR` has to be cleared before
    the run, or a failed rerun leaves it behind at its fixed path (#469)."""
    import re
    from importlib.resources import files
    from rtl_buddy.tools import pnr_openroad

    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    written = set(re.findall(r"\$OUT_DIR/(\S+)", template))
    # Logs are deliberately exempt — each is the one artefact worth keeping
    # when the tool dies early.
    written = {name for name in written if not name.endswith(".log")}
    assert written, "no $OUT_DIR write targets found; did the template move?"

    fixed = set(pnr_openroad._FIXED_OUTPUT_NAMES)
    suffixes = pnr_openroad._MANAGED_OUTPUT_SUFFIXES
    missed = {
        name for name in written if name not in fixed and not name.endswith(suffixes)
    }
    assert not missed, f"not cleared before a run: {sorted(missed)}"

    # `run_output_paths` documents the same set by name; keep it in step.
    named = {
        os.path.basename(p)
        for p in pnr_openroad.run_output_paths("/artefacts", "${DESIGN}")
    }
    assert written <= named, f"undocumented output: {sorted(written - named)}"


def test_pnr_run_ignores_a_previous_runs_drc_report_and_odb(tmp_path, monkeypatch):
    """A run that reaches OpenROAD but writes nothing must score zero DRCs
    rather than a previous run's violation count, and must not leave the
    previous ODB for `rb power` to analyse (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

    # `_make_pnr_cfg` points at `<tmp>/synth.yaml::demo_synth`; the run
    # resolves it to name the design's artefacts.
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

    platform = MagicMock()
    platform.get_pdk.return_value = _make_pdk_cfg(tmp_path)
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)

    artefacts = Path(backend.artefact_dir)
    stale_drc = artefacts / "route.drc.rpt"
    stale_drc.write_text("violation 1\nviolation 2\nviolation 3\n")
    stale_odb = artefacts / "demo_top.routed.odb"
    stale_odb.write_bytes(b"\x00stale odb\x00")
    stale_gds = artefacts / "demo_top.gds"
    stale_gds.write_bytes(b"\x00stale gds\x00")

    def _fake_run(cmd, **_kwargs):
        # OpenROAD exits 0 and writes only its log.
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    res = backend.run()

    assert res.results["drc_count"] == 0
    assert not stale_drc.exists()
    assert not stale_odb.exists()
    assert not stale_gds.exists()


def test_pnr_missing_openroad_still_clears_the_odb(tmp_path, monkeypatch):
    """pnr's DEF/ODB are the fixed-path inputs `rb power` resolves, so the
    clear runs before even the tool-availability check: a box without
    OpenROAD must not leave the previous ODB for a later `rb power` (#469)."""
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

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    artefacts = Path(backend.artefact_dir)
    stale_odb = artefacts / "demo_top.routed.odb"
    stale_odb.write_bytes(b"\x00stale odb\x00")
    stale_drc = artefacts / "route.drc.rpt"
    stale_drc.write_text("violation\n")

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: None)

    res = backend.run()

    # The tool-missing message is unchanged...
    assert "not found" in res.results["desc"]
    # ...but nothing is left for `rb power` to pick up.
    assert not stale_odb.exists()
    assert not stale_drc.exists()


def test_pnr_unresolvable_synth_ref_does_not_preempt_the_tool_error(
    tmp_path, monkeypatch
):
    """Naming the design needs the synth back-reference, but a broken one must
    not hijack the error the run would otherwise report (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),  # points at a synth.yaml that is absent
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    artefacts = Path(backend.artefact_dir)
    stale_drc = artefacts / "route.drc.rpt"
    stale_drc.write_text("violation\n")
    # Design-named outputs cannot be named without the synth reference, but
    # `rb power` accepts the ODB by existence alone, so they must go anyway.
    design_named = {
        name: artefacts / name
        for name in (
            "old_top.routed.odb",
            "old_top.routed.v",
            "old_top.routed.sdc",
            "old_top.def",
            "old_top.gds",
            "old_top.png",
        )
    }
    for path in design_named.values():
        path.write_bytes(b"\x00stale\x00")

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: None)

    res = backend.run()

    assert "not found" in res.results["desc"]
    assert not stale_drc.exists()
    for name, path in design_named.items():
        assert not path.exists(), name
