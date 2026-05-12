"""Tests for the P&R config schema, OpenRoadPnr backend, and rb pnr wiring."""

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
        str(tmp_path / "root_config.yaml"),
        lambda _name: pdk,
    )
    assert cfg.get_corner() == "typ"
    assert cfg.get_path().endswith("typ.lib")


def test_synth_platform_explicit_corner(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(name="nangate45_slow", pdk="nangate45", corner="slow"),
        str(tmp_path / "root_config.yaml"),
        lambda _name: pdk,
    )
    assert cfg.get_corner() == "slow"
    assert cfg.get_path().endswith("slow.lib")


def test_synth_platform_lef_paths_compose_pdk_and_extras(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    cfg = SynthPlatformConfig(
        SynthPlatformConfigFile(
            name="nangate45_typ", pdk="nangate45", lef_paths=["pdk/lef/extra.lef"]
        ),
        str(tmp_path / "root_config.yaml"),
        lambda _name: pdk,
    )
    assert cfg.get_lef_paths() == [
        str(tmp_path / "pdk" / "lef" / "tech.lef"),
        str(tmp_path / "pdk" / "lef" / "cells.lef"),
        str(tmp_path / "pdk" / "lef" / "extra.lef"),
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
    assert run.get_reglvl() == 1000
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
    assert "not found on PATH" in result.results["desc"]


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
