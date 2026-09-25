"""Tests for the power-analysis config schema."""

import hashlib
import os
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config.power import (
    PowerActivity,
    PowerActivityFile,
    PowerConfig,
    PowerSuiteConfig,
    PowerToolConfig,
    PowerToolConfigFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.phys.manifest import load_manifest, resolve


# ---------------------------------------------------------------------------
# PowerToolConfig — minimal name/executable resolution
# ---------------------------------------------------------------------------


def test_power_tool_cfg_exposes_name_and_executable():
    cfg = PowerToolConfig(PowerToolConfigFile(name="openroad", tool="openroad"))
    assert cfg.get_name() == "openroad"
    assert cfg.get_executable() == "openroad"


# ---------------------------------------------------------------------------
# PowerSuiteConfig — YAML loading + initialise
# ---------------------------------------------------------------------------


_POWER_YAML_STATIC = dedent("""\
    rtl-buddy-filetype: power_config

    runs:
      - name: "demo_static_power"
        desc: "Static power"
        tool: "openroad"
        mode: "static"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        constraints: "../synth/constraints.sdc"
        platform: "nangate45_typ"
        reglvl: 1000
""")


_POWER_YAML_DYNAMIC_SYNTHETIC = dedent("""\
    rtl-buddy-filetype: power_config

    runs:
      - name: "demo_dynamic_synth_activity"
        desc: "Dynamic power, synthetic activity"
        tool: "openroad"
        mode: "dynamic"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        constraints: "../synth/constraints.sdc"
        platform: "nangate45_typ"
        activity:
          default-toggle-rate: 0.25
          default-static-prob: 0.5
        reglvl: 0
""")


_POWER_YAML_DYNAMIC_SAIF = dedent("""\
    rtl-buddy-filetype: power_config

    runs:
      - name: "demo_dynamic_saif"
        desc: "Dynamic power, from SAIF"
        tool: "openroad"
        mode: "dynamic"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        constraints: "../synth/constraints.sdc"
        platform: "nangate45_typ"
        activity:
          saif: "../sim/dma_traffic.saif"
          scope: "tb.dut"
        reglvl: 1
""")


def test_power_suite_loads_static_run(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_STATIC)
    suite = PowerSuiteConfig(str(p))
    assert suite.get_run_names() == ["demo_static_power"]
    run = suite.get_runs("demo_static_power")[0]
    assert run.get_name() == "demo_static_power"
    assert run.get_mode() == "static"
    assert run.get_platform() == "nangate45_typ"
    assert run.get_reglvl("openroad") == 1000
    assert run.get_synth_suite_path() == str(tmp_path.parent / "synth" / "synth.yaml")
    assert run.get_constraints() == str(tmp_path.parent / "synth" / "constraints.sdc")
    activity = run.get_activity()
    assert activity.saif is None
    assert activity.vcd is None
    assert activity.has_trace() is False
    assert activity.default_toggle_rate == pytest.approx(0.1)
    assert activity.default_static_prob == pytest.approx(0.5)


def test_power_suite_loads_dynamic_synthetic_activity(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_DYNAMIC_SYNTHETIC)
    suite = PowerSuiteConfig(str(p))
    run = suite.get_runs("demo_dynamic_synth_activity")[0]
    assert run.get_mode() == "dynamic"
    activity = run.get_activity()
    assert activity.has_trace() is False
    assert activity.default_toggle_rate == pytest.approx(0.25)
    assert activity.default_static_prob == pytest.approx(0.5)


def test_power_suite_loads_dynamic_with_saif(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_DYNAMIC_SAIF)
    suite = PowerSuiteConfig(str(p))
    run = suite.get_runs("demo_dynamic_saif")[0]
    activity = run.get_activity()
    assert activity.has_trace() is True
    assert activity.saif == str(tmp_path.parent / "sim" / "dma_traffic.saif")
    assert activity.vcd is None
    assert activity.scope == "tb.dut"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_power_suite_missing_synth_raises(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                synth-path: "../synth/synth.yaml"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'synth'"):
        PowerSuiteConfig(str(p))


def test_power_suite_missing_synth_path_raises(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                synth: "demo_synth"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'synth-path'"):
        PowerSuiteConfig(str(p))


def test_power_suite_missing_platform_raises(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                synth: "demo_synth"
                synth-path: "../synth/synth.yaml"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'platform'"):
        PowerSuiteConfig(str(p))


def test_power_suite_saif_and_vcd_mutually_exclusive(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                tool: "openroad"
                mode: "dynamic"
                synth: "demo_synth"
                synth-path: "../synth/synth.yaml"
                platform: "nangate45_typ"
                activity:
                  saif: "x.saif"
                  vcd: "x.vcd"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="mutually exclusive"):
        PowerSuiteConfig(str(p))


def test_power_suite_scope_without_trace_raises(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                tool: "openroad"
                mode: "dynamic"
                synth: "demo_synth"
                synth-path: "../synth/synth.yaml"
                platform: "nangate45_typ"
                activity:
                  scope: "tb.dut"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="scope is set but no"):
        PowerSuiteConfig(str(p))


def test_power_suite_unknown_run_raises(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_STATIC)
    suite = PowerSuiteConfig(str(p))
    with pytest.raises(FatalRtlBuddyError, match="not found in suite"):
        suite.get_runs("does_not_exist")


# ---------------------------------------------------------------------------
# reglvl polymorphism
# ---------------------------------------------------------------------------


def _make_power_cfg(reglvl):
    return PowerConfig(
        name="demo",
        desc="demo",
        tool="openroad",
        mode="static",
        netlist_source="synth",
        synth_name="demo_synth",
        synth_suite_path="/tmp/synth.yaml",
        pnr_name=None,
        pnr_suite_path=None,
        constraints=None,
        platform="nangate45_typ",
        activity=PowerActivity(
            saif=None,
            vcd=None,
            scope=None,
            default_toggle_rate=0.1,
            default_static_prob=0.5,
        ),
        _reglvl=reglvl,
        tool_overrides=None,
    )


def test_power_reglvl_int_uniform():
    assert _make_power_cfg(500).get_reglvl("openroad") == 500


def test_power_reglvl_per_tool_dict():
    cfg = _make_power_cfg({"openroad": 250, "primetime": 750})
    assert cfg.get_reglvl("openroad") == 250
    assert cfg.get_reglvl("primetime") == 750


def test_power_reglvl_dict_default_fallback():
    cfg = _make_power_cfg({"default": 100, "primetime": 200})
    assert cfg.get_reglvl("openroad") == 100


def test_power_reglvl_none_defaults_to_zero():
    assert _make_power_cfg(None).get_reglvl("openroad") == 0


def test_power_reglvl_malformed_raises():
    cfg = _make_power_cfg("bogus")
    with pytest.raises(FatalRtlBuddyError, match="Malformed power.yaml"):
        cfg.get_reglvl("openroad")


# ---------------------------------------------------------------------------
# Activity dataclass smoke
# ---------------------------------------------------------------------------


def test_power_activity_has_trace():
    a = PowerActivity(
        saif=None,
        vcd=None,
        scope=None,
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )
    assert a.has_trace() is False

    a2 = PowerActivity(
        saif="/tmp/x.saif",
        vcd=None,
        scope=None,
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )
    assert a2.has_trace() is True

    a3 = PowerActivity(
        saif=None,
        vcd="/tmp/x.vcd",
        scope=None,
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )
    assert a3.has_trace() is True


def test_power_activity_file_default_values():
    """Defaults on PowerActivityFile match the schema doc."""
    a = PowerActivityFile()
    assert a.saif is None
    assert a.vcd is None
    assert a.scope is None
    assert a.default_toggle_rate == pytest.approx(0.1)
    assert a.default_static_prob == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Activity-source resolution (lives on PowerConfig so every backend agrees)
# ---------------------------------------------------------------------------


def _activity(saif=None, vcd=None):
    return PowerActivity(
        saif=saif,
        vcd=vcd,
        scope=None,
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )


def _make_power_cfg_with(mode, activity):
    return PowerConfig(
        name="demo",
        desc="demo",
        tool="openroad",
        mode=mode,
        netlist_source="synth",
        synth_name="demo_synth",
        synth_suite_path="/tmp/synth.yaml",
        pnr_name=None,
        pnr_suite_path=None,
        constraints=None,
        platform="nangate45_typ",
        activity=activity,
        _reglvl=0,
        tool_overrides=None,
    )


def test_activity_source_static_is_default():
    cfg = _make_power_cfg_with("static", _activity())
    assert cfg.get_activity_source() == "default"


def test_activity_source_dynamic_with_saif():
    cfg = _make_power_cfg_with("dynamic", _activity(saif="/tmp/x.saif"))
    assert cfg.get_activity_source() == "saif"


def test_activity_source_dynamic_with_vcd():
    cfg = _make_power_cfg_with("dynamic", _activity(vcd="/tmp/x.vcd"))
    assert cfg.get_activity_source() == "vcd"


def test_activity_source_dynamic_no_trace_is_synthetic():
    cfg = _make_power_cfg_with("dynamic", _activity())
    assert cfg.get_activity_source() == "synthetic"


def test_activity_source_static_ignores_trace():
    """Static mode trumps trace presence — no activity command is emitted."""
    cfg = _make_power_cfg_with("static", _activity(saif="/tmp/x.saif"))
    assert cfg.get_activity_source() == "default"


# ---------------------------------------------------------------------------
# Backend registry — dispatch is data-driven, not hardcoded
# ---------------------------------------------------------------------------


def test_power_backends_registry_contains_openroad():
    from rtl_buddy.runner.power_runner import _POWER_BACKENDS
    from rtl_buddy.tools.power_base import BasePower
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    assert "openroad" in _POWER_BACKENDS
    assert _POWER_BACKENDS["openroad"] is OpenRoadPower
    assert issubclass(OpenRoadPower, BasePower)


# ---------------------------------------------------------------------------
# Post-PnR power: netlist-source selector + pnr/pnr-path fields
# ---------------------------------------------------------------------------


_POWER_YAML_PNR_SOURCE = dedent("""\
    rtl-buddy-filetype: power_config

    runs:
      - name: "demo_pnr_power"
        desc: "Post-PnR power with SPEF"
        tool: "openroad"
        mode: "dynamic"
        netlist-source: "pnr"
        pnr: "demo_pnr_nangate45"
        pnr-path: "../pnr/pnr.yaml"
        platform: "nangate45_typ"
        activity:
          default-toggle-rate: 0.2
          default-static-prob: 0.5
        reglvl: 1000
""")


def test_power_default_netlist_source_is_synth(tmp_path):
    """Backward-compat: existing yamls without netlist-source default to synth."""
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_STATIC)
    suite = PowerSuiteConfig(str(p))
    run = suite.get_runs("demo_static_power")[0]
    assert run.get_netlist_source() == "synth"
    assert run.get_pnr_name() is None
    assert run.get_pnr_suite_path() is None


def test_power_pnr_source_loads_paths(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_PNR_SOURCE)
    suite = PowerSuiteConfig(str(p))
    run = suite.get_runs("demo_pnr_power")[0]
    assert run.get_netlist_source() == "pnr"
    assert run.get_pnr_name() == "demo_pnr_nangate45"
    assert run.get_pnr_suite_path() == str(tmp_path.parent / "pnr" / "pnr.yaml")
    # synth fields are not required for the pnr path
    assert run.get_synth_name() is None
    assert run.get_synth_suite_path() is None


def test_power_pnr_source_requires_pnr_name(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                tool: "openroad"
                mode: "dynamic"
                netlist-source: "pnr"
                pnr-path: "../pnr/pnr.yaml"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="requires 'pnr'"):
        PowerSuiteConfig(str(p))


def test_power_pnr_source_requires_pnr_path(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                tool: "openroad"
                mode: "dynamic"
                netlist-source: "pnr"
                pnr: "demo_pnr_nangate45"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="requires 'pnr-path'"):
        PowerSuiteConfig(str(p))


# ---------------------------------------------------------------------------
# phys-run — naming the synthesis run the power half is published beside
# ---------------------------------------------------------------------------


def _phys_run_yaml(value: str, *, source: str = "synth") -> str:
    upstream = (
        '    synth: "demo_synth"\n    synth-path: "../synth/synth.yaml"\n'
        if source == "synth"
        else '    pnr: "demo_pnr"\n    pnr-path: "../pnr/pnr.yaml"\n'
    )
    return (
        "rtl-buddy-filetype: power_config\n"
        "runs:\n"
        '  - name: "demo_power"\n'
        '    desc: "demo"\n'
        f'    netlist-source: "{source}"\n'
        f"{upstream}"
        f'    phys-run: "{value}"\n'
        '    platform: "nangate45_typ"\n'
    )


def test_power_phys_run_is_absent_by_default(tmp_path):
    """The co-location convention is unchanged for every config that says
    nothing: the power half is published into the run's own directory."""
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_YAML_STATIC)
    run = PowerSuiteConfig(str(p)).get_runs("demo_static_power")[0]
    assert run.get_phys_run() is None


def test_power_phys_run_names_a_synthesis_run(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_phys_run_yaml("nightly_synth"))
    run = PowerSuiteConfig(str(p)).get_runs("demo_power")[0]
    assert run.get_phys_run() == "nightly_synth"


def test_power_phys_run_rejects_a_path(tmp_path):
    """A run name, not a directory. The value is joined onto the synth
    suite's `artefacts/`, and refusing a separator here is what keeps that
    join from reaching anywhere else (#589)."""
    p = tmp_path / "power.yaml"
    p.write_text(_phys_run_yaml("../../elsewhere/artefacts/nightly"))
    with pytest.raises(FatalRtlBuddyError, match="not a path"):
        PowerSuiteConfig(str(p))


def test_power_phys_run_rejects_a_bare_parent_directory(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_phys_run_yaml(".."))
    with pytest.raises(FatalRtlBuddyError, match="not a directory"):
        PowerSuiteConfig(str(p))


def test_power_phys_run_requires_a_synth_netlist_source(tmp_path):
    """A `netlist-source: pnr` run records no netlist hash, so its half can
    never merge with a synthesis' — pointing it at one would replace the
    module rows rather than complete them."""
    p = tmp_path / "power.yaml"
    p.write_text(_phys_run_yaml("demo_synth", source="pnr"))
    with pytest.raises(FatalRtlBuddyError, match="netlist-source"):
        PowerSuiteConfig(str(p))


def test_power_synth_source_still_requires_synth_fields(tmp_path):
    """Synth-source path keeps its existing required-field validation."""
    p = tmp_path / "power.yaml"
    p.write_text(
        dedent("""\
            rtl-buddy-filetype: power_config
            runs:
              - name: "demo"
                desc: "demo"
                tool: "openroad"
                netlist-source: "synth"
                platform: "nangate45_typ"
        """)
    )
    with pytest.raises(FatalRtlBuddyError, match="missing 'synth'"):
        PowerSuiteConfig(str(p))


def test_power_get_top_dispatches_on_netlist_source():
    """get_top() picks the right resolver based on netlist_source."""
    from unittest.mock import MagicMock

    cfg = _make_power_cfg(0)
    cfg.synth_name = "demo_synth"
    cfg.synth_suite_path = "/nope/synth.yaml"
    cfg.resolve_synth_cfg = MagicMock(return_value=MagicMock(get_top=lambda: "TOP_S"))
    assert cfg.get_top() == "TOP_S"

    cfg.netlist_source = "pnr"
    inner = MagicMock()
    inner.resolve_synth_cfg.return_value.get_top.return_value = "TOP_P"
    cfg.resolve_pnr_cfg = MagicMock(return_value=inner)
    assert cfg.get_top() == "TOP_P"


def test_power_resolve_synth_cfg_fatal_when_not_configured():
    cfg = _make_power_cfg(0)
    cfg.synth_name = None
    cfg.synth_suite_path = None
    with pytest.raises(FatalRtlBuddyError, match="resolve_synth_cfg.*not configured"):
        cfg.resolve_synth_cfg()


def test_power_resolve_pnr_cfg_fatal_when_not_configured():
    cfg = _make_power_cfg(0)
    with pytest.raises(FatalRtlBuddyError, match="resolve_pnr_cfg.*not configured"):
        cfg.resolve_pnr_cfg()


# ---------------------------------------------------------------------------
# PowerPassResults — netlist_source surfaces for the table renderer
# ---------------------------------------------------------------------------


def test_power_pass_results_carries_netlist_source():
    from rtl_buddy.runner.power_results import PowerPassResults

    r = PowerPassResults(
        name="demo/results",
        mode="dynamic",
        netlist_source="pnr",
        total_w=1.1e-3,
        internal_w=7.1e-4,
        switching_w=3.4e-4,
        leakage_w=6.0e-5,
        activity_source="saif",
    )
    assert r.results["netlist_source"] == "pnr"
    assert r.results["mode"] == "dynamic"
    assert r.results["activity_source"] == "saif"


def test_power_pass_results_omits_netlist_source_when_none():
    from rtl_buddy.runner.power_results import PowerPassResults

    r = PowerPassResults(name="demo/results", mode="static")
    assert "netlist_source" not in r.results


_POWER_XFAIL_YAML = dedent("""\
    rtl-buddy-filetype: power_config

    runs:
      - name: "power_xfail"
        desc: "expected-fail power, non-strict"
        tool: "openroad"
        mode: "static"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
        xfail: true
      - name: "power_xfail_strict"
        desc: "expected-fail power, strict"
        tool: "openroad"
        mode: "static"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
        xfail_strict: true
      - name: "power_normal"
        desc: "normal"
        tool: "openroad"
        mode: "static"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
""")


def test_power_suite_loads_xfail_flags(tmp_path):
    p = tmp_path / "power.yaml"
    p.write_text(_POWER_XFAIL_YAML)
    suite = PowerSuiteConfig(str(p))
    assert suite.get_runs("power_xfail")[0].is_xfail() is True
    assert suite.get_runs("power_xfail")[0].get_xfail_strict() is False
    assert suite.get_runs("power_xfail_strict")[0].is_xfail() is True
    assert suite.get_runs("power_xfail_strict")[0].get_xfail_strict() is True
    assert suite.get_runs("power_normal")[0].is_xfail() is False


# ---------------------------------------------------------------------------
# OpenRoadPower backend — stale-report masking (#469)
# ---------------------------------------------------------------------------


class _FakePdk:
    """A `cfg-pdks` corner as the power script reads it: two LEF paths."""

    def __init__(self, tech_lef, macro_lef=None, fill_cells=()):
        self._tech_lef = tech_lef
        self._macro_lef = macro_lef
        # `filler_placement` puts tens of thousands of these in a routed
        # database; they have no Liberty and no power by construction.
        self._fill_cells = list(fill_cells)

    def get_name(self):
        return "fake_pdk"

    def get_fill_cells(self):
        return list(self._fill_cells)

    def get_tech_lef(self):
        return self._tech_lef

    def get_macro_lef(self):
        return self._macro_lef


class _FakePlatform:
    """A `cfg-pnr-platforms` entry: one Liberty, over one PDK corner.

    Named paths rather than a `MagicMock`, because the power fingerprint
    digests the technology the script reads and a mock answers every call
    with a different object (#570).
    """

    def __init__(self, liberty="/pdk/fake/nangate45_typ.lib", pdk=None, corners=None):
        self._liberty = liberty
        self._pdk = pdk or _FakePdk("/pdk/fake/tech.lef")
        # corner -> Liberty, primary first, for a multi-corner platform
        # (#104, #105); `None` is the single-corner platform every other
        # test uses.
        self._corners = corners

    def get_sta_lib_path(self):
        return self._liberty

    def is_multi_corner(self):
        return bool(self._corners) and len(self._corners) > 1

    def get_sta_corner_lib_paths(self):
        return dict(self._corners or {})

    def get_pdk(self):
        return self._pdk


def _make_power_backend(tmp_path, platform=None):
    """An OpenRoadPower over a synthetic netlist, with input/platform
    resolution stubbed out — the run() gate under test is downstream of both."""
    from unittest.mock import MagicMock
    from rtl_buddy.config.power import PowerActivity
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    netlist = tmp_path / "synth_netlist.v"
    netlist.write_text("module demo_top(); endmodule\n")
    sdc = tmp_path / "constraints.sdc"
    sdc.write_text("create_clock -period 10 [get_ports clk]\n")

    cfg = PowerConfig(
        name="demo_power",
        desc="demo",
        tool="openroad",
        mode="static",
        netlist_source="synth",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        pnr_name=None,
        pnr_suite_path=None,
        constraints=str(sdc),
        platform="nangate45_typ",
        activity=PowerActivity(
            saif=None,
            vcd=None,
            scope=None,
            default_toggle_rate=0.1,
            default_static_prob=0.5,
        ),
        _reglvl=None,
        tool_overrides=None,
    )
    backend = OpenRoadPower(
        name="demo/openroad",
        power_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    backend._resolve_inputs = lambda: {
        "netlist": str(netlist),
        "odb": None,
        "sdc": str(sdc),
        "top": "demo_top",
    }
    backend._resolve_platform = lambda: platform or _FakePlatform()
    return backend


def test_power_ignores_a_previous_runs_report(tmp_path, monkeypatch):
    """OpenROAD exiting 0 with no [ERROR] is not proof it rewrote power.rpt;
    a report left by an earlier run must not be quoted as this run's (#469)."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    backend = _make_power_backend(tmp_path)
    stale = Path(backend.artefact_dir) / "power.rpt"
    stale.write_text(
        "Group                  Internal  Switching    Leakage      Total\n"
        "Total                  1.00e-03   2.00e-03   3.00e-04   3.30e-03 100.0%\n"
    )

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        # Writes the log (OpenROAD's -log) but no report.
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "power report not produced" in res.results["desc"]
    assert not stale.exists()


def test_power_writes_report_then_fails_publishes_nothing(tmp_path, monkeypatch):
    """`report_power` writes before the script ends, so OpenROAD can exit
    non-zero with `power.rpt` on disk. A FAIL publishes nothing (#469)."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    backend = _make_power_backend(tmp_path)
    report = Path(backend.artefact_dir) / "power.rpt"

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _writes_then_dies(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        report.write_text("Total 1.0e-03 2.0e-03 3.0e-04 3.3e-03 100.0%\n")
        return MagicMock(returncode=1)

    monkeypatch.setattr(power_openroad.subprocess, "run", _writes_then_dies)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "exited with code 1" in res.results["desc"]
    assert not report.exists()


def test_power_unparsable_report_publishes_nothing(tmp_path, monkeypatch):
    """Same for a report with no parseable `Total` row (#469)."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    backend = _make_power_backend(tmp_path)
    report = Path(backend.artefact_dir) / "power.rpt"

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _writes_garbage(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        report.write_text("no totals here\n")
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _writes_garbage)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "could not parse Total line" in res.results["desc"]
    assert not report.exists()


def test_power_missing_input_without_openroad_is_a_config_error(tmp_path, monkeypatch):
    """`_write_script` is where this flow validates its configuration. Running
    it after the availability check reported a missing SDC as "openroad not
    found" on a box that merely lacks the tool, and left the previous report
    in place after a failed run (#469)."""
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    backend = _make_power_backend(tmp_path)
    stale = Path(backend.artefact_dir) / "power.rpt"
    stale.write_text("Total 1.0e-03 2.0e-03 3.0e-04 3.3e-03 100.0%\n")

    # No SDC, and no OpenROAD either.
    backend._resolve_inputs = lambda: {
        "netlist": str(tmp_path / "synth_netlist.v"),
        "odb": None,
        "sdc": None,
        "top": "demo_top",
    }
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: None)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "script generation error" in res.results["desc"]
    assert "constraints (SDC path) is required" in res.results["desc"]
    assert not stale.exists()


def test_power_valid_config_without_openroad_keeps_the_report(tmp_path, monkeypatch):
    """The no-deletion behaviour survives for a genuinely valid config: a box
    without OpenROAD never ran it, so it must not delete a report a box that
    has it produced (#469)."""
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    backend = _make_power_backend(tmp_path)
    kept = Path(backend.artefact_dir) / "power.rpt"
    kept.write_text("Total 1.0e-03 2.0e-03 3.0e-04 3.3e-03 100.0%\n")

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: None)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "not found" in res.results["desc"]
    assert kept.exists()


# ---------------------------------------------------------------------------
# Per-instance power -> phys-model.json (#558, delivering #114)
# ---------------------------------------------------------------------------


_TOTAL_RPT = (
    "Group                  Internal  Switching    Leakage      Total\n"
    "Total                  2.53e-05   1.52e-06   1.41e-06   2.83e-05 100.0%\n"
)

_INSTANCE_RPT = (
    "   Internal  Switching    Leakage      Total\n"
    "      Power      Power      Power      Power (Watts)\n"
    "--------------------------------------------\n"
    "   2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_\n"
    "   1.52e-07   7.79e-08   3.62e-08   2.66e-07 _18_\n"
)

_INSTANCE_CELLS = "_18_ XOR2_X1\nu_sub/_64_ DFF_X1\n"


def test_power_script_walks_the_hierarchy_for_per_instance_numbers(tmp_path):
    """One `report_power -instances` call over the whole cell list, not one
    call per cell: the per-cell spelling reruns propagation every time."""
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert "set rb_insts [get_cells -hierarchical *]" in script
    assert (
        f"report_power -instances $rb_insts > {backend._instances_report_path()}"
        in (script)
    )
    # The design-total report is written first, so a failure in the walk
    # cannot cost the run its headline numbers.
    assert script.index("report_power >") < script.index("report_power -instances")


def test_power_script_wraps_the_walk_in_a_catch(tmp_path):
    """A Tcl error escaping to the top level would abort the script and take
    the exit code with it, failing a run whose totals are already on disk."""
    backend = _make_power_backend(tmp_path)

    lines = Path(backend._write_script()).read_text().splitlines()
    walk = lines.index("  set rb_insts [get_cells -hierarchical *]")

    assert lines[walk - 1] == "catch {"
    assert "}" in lines[walk:]


def test_power_script_records_the_liberty_cell_of_each_instance(tmp_path):
    """`report_power` prints the path and the powers, never the master — so
    the walk writes the mapping the model's module column needs. Under a
    staging name, like the report beside it: a `foreach` that raised part-way
    would otherwise leave half the mapping at the published path (#560)."""
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    staged = backend._staging_path(backend._instances_cells_path())
    assert f"open {staged} w" in script
    assert "get_property $rb_inst ref_name" in script
    assert f"file rename -force {staged} {backend._instances_cells_path()}" in script


def _run_power_with(tmp_path, monkeypatch, *, instances=None, cells=None, log=""):
    """Run an OpenRoadPower whose fake OpenROAD writes the given reports."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text(log)
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        if instances is not None:
            Path(backend._instances_report_path()).write_text(instances)
        if cells is not None:
            Path(backend._instances_cells_path()).write_text(cells)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    return backend, backend.run()


def _run_prepared_power(backend, monkeypatch, *, instances=None, cells=None):
    """`_run_power_with`, for a backend the caller has already shaped."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        if instances is not None:
            Path(backend._instances_report_path()).write_text(instances)
        if cells is not None:
            Path(backend._instances_cells_path()).write_text(cells)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    return backend.run()


def _make_pnr_power_backend(tmp_path, routed_sdc_text, pnr_run="demo_pnr"):
    """A `netlist-source: pnr` backend with no explicit `constraints:`.

    Which is the ordinary spelling: the routed SDC is an artefact of the
    `rb pnr` run this reads, so nobody names it in `power.yaml`.
    `_resolve_inputs` is the thing that knows where it is, and it is
    stubbed here exactly as the synth fixture stubs it.

    ``pnr_run`` names the upstream `rb pnr` entry, so a caller can build
    two backends that differ in nothing but which routed database they
    read.
    """
    backend = _make_power_backend(tmp_path)
    backend.power_cfg.netlist_source = "pnr"
    backend.power_cfg.constraints = None
    backend.power_cfg.pnr_name = pnr_run
    pnr_artefact = tmp_path / "pnr_artefacts" / pnr_run
    pnr_artefact.mkdir(parents=True, exist_ok=True)
    odb = pnr_artefact / "demo_top.routed.odb"
    odb.write_bytes(b"")
    routed = pnr_artefact / "demo_top.routed.sdc"
    routed.write_text(routed_sdc_text)
    backend._resolve_inputs = lambda: {
        "netlist": None,
        "odb": str(odb),
        "sdc": str(routed),
        "top": "demo_top",
    }
    return backend, routed


def test_a_pnr_power_run_records_the_routed_sdc_it_actually_read(tmp_path, monkeypatch):
    """The config block is what tells two runs apart, and for a `pnr` run
    the constraints are not in the config at all: with no explicit
    `constraints:` the analysis reads `<pnr artefact>/<top>.routed.sdc`,
    the post-CTS constraints the router wrote. Publishing the config
    field recorded `null` and hashed nothing, so two analyses against
    two different routed SDCs -- different clock periods, a different
    CTS -- fingerprinted identically while measuring different timing."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import sha256_of

    backend, routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    config = load_model(result.results["phys_model"])["provenance"]["power"]["config"]
    # Project-relative, as every path in these documents is.
    assert config["constraints"].endswith("demo_top.routed.sdc")
    assert config["constraints_sha256"] == sha256_of(routed)


def test_two_pnr_runs_with_different_routed_sdcs_read_apart(tmp_path, monkeypatch):
    """The point of recording it: the hash is what a reader compares."""
    from rtl_buddy.phys.model import load_model

    hashes = []
    for period in ("3", "7"):
        root = tmp_path / f"p{period}"
        root.mkdir()
        backend, _ = _make_pnr_power_backend(
            root, f"create_clock -period {period} [get_ports clk]\n"
        )
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        hashes.append(recorded["config"]["constraints_sha256"])

    assert hashes[0] and hashes[1] and hashes[0] != hashes[1]


def test_a_trace_rewritten_in_place_gives_the_run_a_new_identity(tmp_path, monkeypatch):
    """`rb test` overwrites `artefacts/<test>/dump.saif` every time the
    test behind it runs, and `rb saif` converts it in place, so the path a
    `power.yaml` names is a name and not an identity. Two analyses of the
    same netlist against two captures of one trace are two measurements,
    and before the hash their activity blocks were byte-identical."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.provenance import activity_label
    from rtl_buddy.phys.publish import sha256_of
    from rtl_buddy.config.power import PowerActivity

    backend = _make_power_backend(tmp_path)
    trace = tmp_path / "verif" / "demo" / "artefacts" / "csr_smoke" / "dump.saif"
    trace.parent.mkdir(parents=True)
    backend.power_cfg.mode = "dynamic"
    backend.power_cfg.activity = PowerActivity(
        saif=str(trace),
        vcd=None,
        scope="tb/u_dut",
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )

    blocks = []
    for capture in ("(SAIFILE first)\n", "(SAIFILE second)\n"):
        trace.write_text(capture)
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        assert recorded["activity"]["trace_sha256"] == sha256_of(trace)
        blocks.append(recorded["activity"])

    # Everything a reader sees is the same; the identity is not.
    assert blocks[0]["trace"] == blocks[1]["trace"]
    assert blocks[0]["test"] == blocks[1]["test"] == "csr_smoke"
    assert activity_label(blocks[0]) == activity_label(blocks[1])
    assert blocks[0]["trace_sha256"] != blocks[1]["trace_sha256"]
    assert blocks[0] != blocks[1]


def _saif_backend(tmp_path):
    """A dynamic backend reading a SAIF the test can rewrite."""
    from rtl_buddy.config.power import PowerActivity

    backend = _make_power_backend(tmp_path)
    trace = tmp_path / "verif" / "demo" / "artefacts" / "csr_smoke" / "dump.saif"
    trace.parent.mkdir(parents=True, exist_ok=True)
    backend.power_cfg.mode = "dynamic"
    backend.power_cfg.activity = PowerActivity(
        saif=str(trace),
        vcd=None,
        scope="tb/u_dut",
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )
    return backend, trace


def test_the_trace_is_hashed_before_openroad_reads_it(tmp_path, monkeypatch):
    """The finding (#570 round-15 review, Codex P2). The hash was taken in
    `_publish_phys_model`, *after* an analysis that runs for minutes, so a
    `dump.saif` the test behind it re-captured mid-run was identified by
    its replacement and the document claimed bytes the watts beside them
    were never measured from. The identity is now taken as the subprocess
    is launched, which is the only moment the file on disk is the file
    being read."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import sha256_of
    from rtl_buddy.tools import power_openroad

    backend, trace = _saif_backend(tmp_path)
    trace.write_text("(SAIFILE measured)\n")
    measured = sha256_of(trace)
    seen = []

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        # Taken already, by the time the tool that reads the trace starts.
        seen.append(backend._trace_sha256)
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        Path(backend._instances_report_path()).write_text(_INSTANCE_RPT)
        Path(backend._instances_cells_path()).write_text(_INSTANCE_CELLS)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    result = backend.run()

    assert seen == [measured]
    activity = load_model(result.results["phys_model"])["provenance"]["power"][
        "activity"
    ]
    assert activity["trace_sha256"] == measured


def test_a_trace_rewritten_under_the_run_is_recorded_as_unknown(
    tmp_path, monkeypatch, caplog
):
    """Hashing at the start narrows the window; it does not close it. So
    the hash is confirmed when OpenROAD returns, and a trace that moved
    in between has *no* identity this run can vouch for — the first hash
    names bytes OpenROAD may not have finished reading, the second names
    bytes it certainly did not start with. `null` is the model's word for
    unknown, and the warning is what stops it reading as "this run
    measured no trace"."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import sha256_of
    from rtl_buddy.tools import power_openroad

    backend, trace = _saif_backend(tmp_path)
    trace.write_text("(SAIFILE first)\n")

    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        # The next run of the test behind this trace, landing mid-analysis.
        trace.write_text("(SAIFILE recaptured under the run)\n")
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        Path(backend._instances_report_path()).write_text(_INSTANCE_RPT)
        Path(backend._instances_cells_path()).write_text(_INSTANCE_CELLS)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)

    with caplog.at_level("WARNING"):
        result = backend.run()

    activity = load_model(result.results["phys_model"])["provenance"]["power"][
        "activity"
    ]
    # Neither hash is recorded: not the bytes at the start, not the ones
    # on disk now.
    assert activity["trace_sha256"] is None
    assert sha256_of(trace) is not None
    # The path is still recorded — what the run read is known, which
    # bytes it read is not.
    assert activity["trace"].endswith("dump.saif")
    assert "trace_changed_during_run" in caplog.text


def test_a_static_run_hashes_no_trace_and_reads_none(tmp_path, monkeypatch):
    """The fixture's own shape: `mode: static` with a trace still named in
    the config. The Tcl emits no `read_saif`, so there is nothing to
    identify -- and a VCD is the largest file in an artefact tree, which
    is reason enough not to read one the analysis ignored."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.config.power import PowerActivity

    backend = _make_power_backend(tmp_path)
    trace = tmp_path / "verif" / "demo" / "artefacts" / "csr_smoke" / "dump.saif"
    trace.parent.mkdir(parents=True)
    trace.write_text("(SAIFILE)\n")
    backend.power_cfg.activity = PowerActivity(
        saif=str(trace),
        vcd=None,
        scope="tb/u_dut",
        default_toggle_rate=0.1,
        default_static_prob=0.5,
    )

    reads = []
    real_open = open

    def _counting_open(path, *args, **kwargs):
        if str(path) == str(trace):
            reads.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _counting_open)
    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    activity = load_model(result.results["phys_model"])["provenance"]["power"][
        "activity"
    ]
    assert activity["source"] == "default"
    assert activity["trace"] is None and activity["trace_sha256"] is None
    assert reads == []


def test_the_power_options_digest_ignores_the_field_no_backend_reads(
    tmp_path, monkeypatch
):
    """`tool_overrides` is accepted in `power.yaml` and read by nothing --
    `PowerConfig.get_tool_overrides()` has no caller -- so two analyses
    that differ only in it are the same analysis. Digesting it reported a
    difference the numbers cannot have, and implied the block had been
    applied."""
    from rtl_buddy.phys.model import load_model

    digests = []
    for overrides in (None, {"openroad": {"corner": "fast"}}):
        root = tmp_path / f"cfg{len(digests)}"
        root.mkdir()
        backend = _make_power_backend(root)
        backend.power_cfg.tool_overrides = overrides
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        digests.append(recorded["config"]["options_sha256"])

    assert digests[0] is not None
    assert digests[0] == digests[1]


def test_two_power_runs_over_two_netlists_do_not_share_a_fingerprint(
    tmp_path, monkeypatch
):
    """The finding (#570 round-16, Codex P2). `netlist_source` names the
    *kind* of upstream, not which one, so two power entries differing only
    in `synth:`/`synth-path:` fed OpenROAD two different netlists and came
    out with one config fingerprint — and `rb phys runs` reads manifests
    only, so a listing showed two designs as one experiment."""
    from rtl_buddy.phys.model import load_model

    digests = []
    for index, netlist_text in enumerate(
        (
            "module demo_top(); endmodule\n",
            "module demo_top(); // a second synthesis\nendmodule\n",
        )
    ):
        root = tmp_path / f"run{index}"
        root.mkdir()
        backend = _make_power_backend(root)
        upstream = root / "upstream_netlist.v"
        upstream.write_text(netlist_text)
        backend.power_cfg.synth_name = f"synth{index}"
        backend._resolve_inputs = lambda root=root, upstream=upstream: {
            "netlist": str(upstream),
            "odb": None,
            "sdc": str(root / "constraints.sdc"),
            "top": "demo_top",
        }
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        digests.append(recorded["config"]["options_sha256"])

    assert digests[0] is not None
    assert digests[0] != digests[1]


def test_two_pnr_power_runs_over_two_databases_do_not_share_a_fingerprint(
    tmp_path, monkeypatch
):
    """The other half of the same finding. A `netlist-source: pnr` run
    records no netlist hash at all, so without the resolved database's path
    two analyses of two routed designs — same platform, same activity, same
    routed SDC text — were one fingerprint."""
    from rtl_buddy.phys.model import load_model

    digests = []
    for index in range(2):
        root = tmp_path / f"pnr{index}"
        root.mkdir()
        backend, _routed = _make_pnr_power_backend(
            root,
            "create_clock -period 3 [get_ports clk]\n",
            pnr_run=f"pnr_run{index}",
        )
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        digests.append(recorded["config"]["options_sha256"])

    assert digests[0] is not None
    assert digests[0] != digests[1]


def test_the_upstream_identity_in_the_digest_is_not_an_absolute_path(
    tmp_path, monkeypatch
):
    """A digest that moved with the checkout would tell one run apart from
    itself, and the publish path cannot relativise this one: it rewrites the
    paths *inside* the config block, by which time the options mapping has
    already been hashed."""
    # The marker `project_root_for_dir` walks up for; without a project
    # around it every path is outside the tree and kept verbatim, which is
    # the documented fallback and not the case under test.
    (tmp_path / "root_config.yaml").write_text("")
    backend, _routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    _run_prepared_power(backend, monkeypatch)

    identity = backend._upstream_identity()

    assert identity["netlist_sha256"] is None
    assert identity["input_path"] is not None
    assert not Path(identity["input_path"]).is_absolute()
    assert identity["input_path"].endswith("demo_top.routed.odb")


def test_a_passing_power_run_publishes_the_phys_model(tmp_path, monkeypatch):
    from rtl_buddy.phys.manifest import load_manifest
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerPassResults

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    assert isinstance(result, PowerPassResults)
    model = load_model(result.results["phys_model"])
    assert model["design"]["top"] == "demo_top"
    assert model["modules"] is None
    assert [row["instance_path"] for row in model["instances"]] == [
        "_18_",
        "u_sub/_64_",
    ]
    assert model["instances"][1]["module"] == "DFF_X1"
    assert model["instances"][1]["total_uw"] == pytest.approx(2.42)
    # Watts on the way in, microwatts in the document.
    assert model["totals"]["total_uw"] == pytest.approx(28.3)
    # Bound to the netlist this run read, which is what a later `rb synth`
    # into the same directory tests its own output against (#560 review).
    assert (
        model["provenance"]["power"]["netlist_sha256"]
        == hashlib.sha256((tmp_path / "synth_netlist.v").read_bytes()).hexdigest()
    )

    manifest = load_manifest(Path(backend.artefact_dir) / "phys-manifest.json")
    assert manifest["command"] == "power"
    assert manifest["power"]["backend"] == "openroad"
    assert manifest["power"]["netlist_source"] == "synth"
    assert manifest["synth"]["backend"] is None


def test_the_manifest_names_the_netlist_behind_the_provenance_hash(
    tmp_path, monkeypatch
):
    """The finding (#560 round-16, Codex P2). `power_netlist.v` is kept
    precisely so the analyzed bytes survive the run, but a provenance hash
    with no path beside it in the manifest leaves an archived result nothing
    to verify against: the reader knows the analysis was pinned to one
    netlist and cannot find which."""
    from rtl_buddy.phys.manifest import POWER_KEYS, load_manifest
    from rtl_buddy.phys.model import load_model

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    manifest = load_manifest(Path(backend.artefact_dir) / "phys-manifest.json")
    assert "netlist_path" in POWER_KEYS
    recorded = manifest["power"]["netlist_path"]
    assert recorded.endswith("power_netlist.v")
    # The named file is the snapshot, and hashing it reproduces the
    # provenance hash — which is the whole point of naming it.
    snapshot = Path(backend._netlist_snapshot_path())
    assert snapshot.name == Path(recorded).name
    assert (
        load_model(result.results["phys_model"])["provenance"]["power"][
            "netlist_sha256"
        ]
        == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    )


def test_a_post_pnr_run_names_no_netlist_in_the_manifest(tmp_path):
    """Null, not absent: the key is always there, and a routed-database run
    has no snapshot to name (#560)."""
    from rtl_buddy.phys.manifest import POWER_KEYS, load_manifest
    from rtl_buddy.phys.publish import publish_power

    artefact_dir = tmp_path / "artefacts" / "demo_power"
    artefact_dir.mkdir(parents=True)
    published = publish_power(
        artefact_dir=str(artefact_dir),
        top="demo_top",
        backend="openroad",
        run="demo_power",
        netlist_source="pnr",
        netlist_sha256=None,
        netlist_path=None,
        total_w=1e-5,
    )

    manifest = load_manifest(Path(published["manifest"]))
    assert set(manifest["power"]) == set(POWER_KEYS)
    assert manifest["power"]["netlist_path"] is None
    assert manifest["power"]["netlist_source"] == "pnr"


RESYNTHESISED = "module demo_top(); // resynthesised\nendmodule\n"


def test_the_script_reads_this_runs_own_copy_of_the_netlist(tmp_path):
    """The finding (#560 round-11 review, Codex P1). Hashing the upstream
    netlist leaves a window however tightly it is drawn, so the analysis does
    not read the upstream netlist at all: it reads a copy in its own artefact
    directory, which is the file it hashes."""
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert f"read_verilog {backend._netlist_snapshot_path()}" in script
    assert f"read_verilog {tmp_path / 'synth_netlist.v'}" not in script


@pytest.mark.parametrize("swap_at", ["before_openroad", "during_openroad"])
def test_the_recorded_netlist_hash_is_of_the_bytes_openroad_was_given(
    tmp_path, monkeypatch, swap_at
):
    """The hash names the bytes OpenROAD parsed, whenever the swap lands.

    A `rb synth` into the upstream artefact directory can rewrite the netlist
    at any moment after the script is written: before the copy is taken, or
    while OpenROAD is reading it. Either way the run measures one file — its
    own copy — and records the hash of that file, so the merge cannot read a
    real mismatch as a match (#560 round-9, closed by construction in
    round-11)."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    netlist = tmp_path / "synth_netlist.v"
    snapshot = Path(backend._netlist_snapshot_path())
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    if swap_at == "before_openroad":
        # The clear runs between the script write and the copy, so a swap
        # here lands in the one gap left after `_write_script` named the
        # copy the script reads.
        clear = backend._clear_stale_report

        def _clear_then_resynthesise():
            clear()
            netlist.write_text(RESYNTHESISED)

        backend._clear_stale_report = _clear_then_resynthesise

    seen = {}

    def _fake_run(cmd, **kwargs):
        # What OpenROAD was handed: the script, and the file it names.
        seen["script"] = Path(cmd[-1]).read_text()
        seen["read"] = snapshot.read_bytes()
        if swap_at == "during_openroad":
            netlist.write_text(RESYNTHESISED)
        # Unmoved by the swap — the copy is inside this run's own artefact
        # directory, which no other command writes into.
        seen["read_after"] = snapshot.read_bytes()
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        Path(backend._instances_report_path()).write_text(_INSTANCE_RPT)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)

    result = backend.run()

    assert f"read_verilog {snapshot}" in seen["script"]
    assert seen["read_after"] == seen["read"]
    recorded = load_model(result.results["phys_model"])["provenance"]["power"]
    assert recorded["netlist_sha256"] == hashlib.sha256(seen["read"]).hexdigest()
    if swap_at == "during_openroad":
        # The upstream netlist has moved on; the hash still names what was
        # measured, so a later `rb synth` sees the mismatch.
        assert (
            recorded["netlist_sha256"]
            != hashlib.sha256(netlist.read_bytes()).hexdigest()
        )


def test_a_netlist_that_cannot_be_staged_fails_the_run(tmp_path, monkeypatch):
    """The generated script names the copy, so a copy that did not happen
    leaves `read_verilog` nothing to read: this is a failed run, not a
    by-product warning. The staging file goes with it — a half-written
    `power_netlist.v` must never be readable as a netlist."""
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _no_space(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(power_openroad.shutil, "copyfile", _no_space)

    def _unreachable(*_a, **_k):
        raise AssertionError("OpenROAD must not run without the netlist copy")

    monkeypatch.setattr(power_openroad.subprocess, "run", _unreachable)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert "could not stage the netlist" in res.results["desc"]
    assert not Path(backend._netlist_snapshot_path()).exists()
    assert not Path(backend._netlist_snapshot_path() + ".tmp").exists()


def test_a_netlist_rewritten_mid_copy_is_copied_again(tmp_path, monkeypatch):
    """The finding (#560 round-16, Codex P1). A private copy is immutable but
    not automatically *coherent*: when the upstream synthesis lives in another
    suite the two commands hold different artefact-tree locks, so a concurrent
    `rb synth` can rewrite the netlist under `copyfile`'s read and leave a torn
    prefix that the recorded sha256 would authenticate. The source is stat'd
    either side of the copy, and a copy that straddled a rewrite is taken
    again."""
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    # `_write_script` is what resolves the upstream netlist this run reads.
    backend._write_script()
    netlist = tmp_path / "synth_netlist.v"
    resynthesised = "module demo_top(); // rewritten by a concurrent synth\nendmodule\n"
    real_copyfile = power_openroad.shutil.copyfile
    copies = []

    def _copy_then_resynthesise(src, dst, **kwargs):
        copies.append(str(src))
        real_copyfile(src, dst)
        if len(copies) == 1:
            # Lands after the pre-copy stat and after the read: exactly the
            # window that makes the staging file a prefix of two netlists.
            netlist.write_text(resynthesised)

    monkeypatch.setattr(power_openroad.shutil, "copyfile", _copy_then_resynthesise)

    assert backend._snapshot_netlist() is None

    snapshot = Path(backend._netlist_snapshot_path())
    assert len(copies) == 2
    assert snapshot.read_text() == resynthesised
    assert backend._netlist_sha256 == hashlib.sha256(resynthesised.encode()).hexdigest()
    assert not Path(str(snapshot) + ".tmp").exists()


def test_a_netlist_that_never_holds_still_fails_the_run(tmp_path, monkeypatch):
    """Retrying is bounded, so a writer rewriting the netlist in a loop fails
    this run instead of pinning it. Refusing is the cheaper error: a rerun
    recovers a refusal, while watts measured over bytes that were never one
    netlist are not detectably wrong afterwards (#560)."""
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    netlist = tmp_path / "synth_netlist.v"
    real_copyfile = power_openroad.shutil.copyfile
    copies = []
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _never_settles(src, dst, **kwargs):
        real_copyfile(src, dst)
        copies.append(str(src))
        # A different length every time, so no retry can stat its way to a
        # matching pair.
        netlist.write_text(f"module demo_top(); {'/' * len(copies)}\nendmodule\n")

    monkeypatch.setattr(power_openroad.shutil, "copyfile", _never_settles)

    def _unreachable(*_a, **_k):
        raise AssertionError("OpenROAD must not run over an incoherent netlist")

    monkeypatch.setattr(power_openroad.subprocess, "run", _unreachable)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert len(copies) == power_openroad._SNAPSHOT_ATTEMPTS
    assert "changed underneath" in res.results["desc"]
    assert backend._netlist_sha256 is None
    assert not Path(backend._netlist_snapshot_path()).exists()
    assert not Path(backend._netlist_snapshot_path() + ".tmp").exists()


def test_a_previous_runs_netlist_copy_does_not_survive_a_failed_rerun(
    tmp_path, monkeypatch
):
    """The copy is the largest thing this flow writes and nothing reads it
    once OpenROAD has, so it is cleared like the reports beside it."""
    from unittest.mock import MagicMock
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        return MagicMock(returncode=1)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert not Path(backend._netlist_snapshot_path()).exists()


def test_a_post_pnr_run_snapshots_nothing_and_records_no_hash(tmp_path):
    """`netlist-source: pnr` reads a routed database, not a netlist. There
    are no bytes to copy and none to identify, which is what it recorded
    before the copy existed."""
    odb = tmp_path / "demo_top.routed.odb"
    odb.write_bytes(b"\x00routed\n")
    sdc = tmp_path / "constraints.sdc"

    backend = _make_power_backend(tmp_path)
    backend.power_cfg.netlist_source = "pnr"
    backend._resolve_inputs = lambda: {
        "netlist": None,
        "odb": str(odb),
        "sdc": str(sdc),
        "top": "demo_top",
    }

    script = Path(backend._write_script()).read_text()

    assert f"read_db {odb}" in script
    assert "read_verilog" not in script
    assert backend._snapshot_netlist() is None
    assert backend._netlist_sha256 is None
    assert not Path(backend._netlist_snapshot_path()).exists()


def test_a_power_run_without_the_per_instance_report_still_passes(
    tmp_path, monkeypatch
):
    """Same resilience rule as the synth half: the design totals are already
    parsed and reported by the time the model is built (#558)."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerPassResults

    _backend, result = _run_power_with(tmp_path, monkeypatch)

    assert isinstance(result, PowerPassResults)
    model = load_model(result.results["phys_model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] == pytest.approx(28.3)


def test_power_ignores_a_previous_runs_per_instance_report(tmp_path, monkeypatch):
    """The per-instance half is read back inside the same `run()`, so it
    takes the same stale-artefact treatment as `power.rpt` (#469)."""
    from rtl_buddy.phys.model import load_model

    backend = _make_power_backend(tmp_path)
    Path(backend._instances_report_path()).write_text(_INSTANCE_RPT)
    Path(backend._instances_cells_path()).write_text(_INSTANCE_CELLS)

    backend, result = _run_power_with(tmp_path, monkeypatch)

    assert not Path(backend._instances_report_path()).exists()
    assert load_model(result.results["phys_model"])["instances"] is None


def test_a_failed_power_rerun_withdraws_the_instances_half_it_published(
    tmp_path, monkeypatch
):
    """Publication happens only on a pass, so a rerun that fails leaves the
    last run's per-instance watts in `phys-model.json` with the report behind
    them already cleared. The clear withdraws them instead — and leaves the
    synthesis half, whose own artefacts are untouched, exactly as it was
    (#558)."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import publish_synth
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )
    model_path = Path(result.results["phys_model"])
    assert load_model(model_path)["instances"]

    # A synthesis into the same artefact directory, as a co-named `rb synth`
    # would have left it.
    publish_synth(
        artefact_dir=backend.artefact_dir,
        top="demo_top",
        backend="yosys",
        run="demo_synth",
        area_um2=5.586,
        gate_count=2,
    )

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        return MagicMock(returncode=1)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    rerun = backend.run()

    assert isinstance(rerun, PowerFailResults)
    model = load_model(model_path)
    assert model["instances"] is None
    assert model["totals"]["total_uw"] is None
    assert model["totals"]["area_um2"] == 5.586


def test_power_script_marks_where_the_by_product_begins(tmp_path):
    """The marker sits after the design-total report and before the walk, so
    the log gate can tell a fatal diagnostic from a by-product one (#558)."""
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    backend = _make_power_backend(tmp_path)

    lines = Path(backend._write_script()).read_text().splitlines()
    marker = lines.index(f'puts "{OpenRoadPower._DETAIL_MARKER}"')
    totals = next(i for i, ln in enumerate(lines) if ln.startswith("report_power >"))
    walk = lines.index("  set rb_insts [get_cells -hierarchical *]")

    assert totals < marker < walk


def test_an_error_in_the_by_product_half_costs_only_that_half(tmp_path, monkeypatch):
    """An OpenSTA without `report_power -instances` prints an `[ERROR ...]`
    before the `catch` swallows the failure. The totals are already on disk
    by then, so the run passes and loses its `instances` half (#558)."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerPassResults
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    _backend, result = _run_power_with(
        tmp_path,
        monkeypatch,
        log=(
            "[INFO ODB-0227] LEF file: nangate45.lef\n"
            f"{OpenRoadPower._DETAIL_MARKER}\n"
            "[ERROR STA-0001] report_power: unknown option -instances\n"
        ),
    )

    assert isinstance(result, PowerPassResults)
    assert result.results["total_w"] == pytest.approx(2.83e-05)
    assert load_model(result.results["phys_model"])["instances"] is None


def test_an_error_before_the_marker_still_fails_the_run(tmp_path, monkeypatch):
    """Everything up to the marker is the analysis itself: a diagnostic there
    means the watts cannot be trusted, and the report goes with it (#558)."""
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    backend, result = _run_power_with(
        tmp_path,
        monkeypatch,
        instances=_INSTANCE_RPT,
        cells=_INSTANCE_CELLS,
        log=(
            "[ERROR STA-0603] no clocks have been defined\n"
            f"{OpenRoadPower._DETAIL_MARKER}\n"
        ),
    )

    assert isinstance(result, PowerFailResults)
    assert "1 ERROR(s) in OpenROAD log" in result.results["desc"]
    assert not Path(backend._report_path()).exists()


def test_a_log_without_the_marker_is_scanned_whole(tmp_path, monkeypatch):
    """Backward compatibility: a log from a script that predates the marker
    has no by-product half to exempt, so every `[ERROR ...]` is fatal."""
    from rtl_buddy.runner.power_results import PowerFailResults

    _backend, result = _run_power_with(
        tmp_path,
        monkeypatch,
        log="[ERROR STA-0001] report_power: unknown option -instances\n",
    )

    assert isinstance(result, PowerFailResults)
    assert "1 ERROR(s) in OpenROAD log" in result.results["desc"]


# ---------------------------------------------------------------------------
# Publishing the per-instance reports only on success (#560 round-17)
# ---------------------------------------------------------------------------


def test_the_per_instance_block_writes_under_staging_names(tmp_path):
    """The finding (#560 round-17, Codex P2). Tcl's `>` creates the file
    before the command it redirects runs, so a `report_power -instances` that
    emits two thirds of the design and then raises leaves a nonempty report at
    the published path — and the `catch` around it hides that it failed. The
    block therefore redirects into a staging name and never into the published
    one."""
    backend = _make_power_backend(tmp_path)
    instances = backend._instances_report_path()
    cells = backend._instances_cells_path()

    script = Path(backend._write_script()).read_text()

    assert (
        f"report_power -instances $rb_insts > {backend._staging_path(instances)}"
        in script
    )
    assert f"report_power -instances $rb_insts > {instances}\n" not in script
    assert f"open {backend._staging_path(cells)} w" in script
    assert f"open {cells} w" not in script


def test_the_per_instance_reports_are_renamed_on_success_only(tmp_path):
    """The publication is two renames, and Tcl reaches them only when every
    command before them returned — an atomic publish-on-success. A block that
    raised leaves the staging files, which the trailing deletes remove."""
    backend = _make_power_backend(tmp_path)
    instances = backend._instances_report_path()
    cells = backend._instances_cells_path()
    instances_tmp = backend._staging_path(instances)
    cells_tmp = backend._staging_path(cells)

    lines = Path(backend._write_script()).read_text().splitlines()

    rename_cells = lines.index(f"    file rename -force {cells_tmp} {cells}")
    rename_insts = lines.index(f"    file rename -force {instances_tmp} {instances}")
    report = lines.index(f"    report_power -instances $rb_insts > {instances_tmp}")
    # Both renames follow the command that can fail, and both are inside the
    # `catch` block — the closing brace comes after them.
    assert report < rename_cells < rename_insts
    assert lines.index("}") > rename_insts
    # And the staging files a failed block leaves do not outlive the script.
    assert f"catch {{file delete -force {cells_tmp}}}" in lines
    assert f"catch {{file delete -force {instances_tmp}}}" in lines


def test_a_partial_per_instance_report_is_not_published_as_complete(
    tmp_path, monkeypatch
):
    """What the staging name buys. A block that emitted rows and then raised
    leaves them under the staging name and nothing at the published one, which
    is the state the publish already reads as "this run produced no
    breakdown" — a null half plus the existing warning, not two thirds of a
    design presented as all of it."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerPassResults
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _emits_then_raises(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        # The rows the redirection had already flushed when the Tcl error
        # unwound the block — under the staging name, never renamed.
        Path(backend._staging_path(backend._instances_report_path())).write_text(
            _INSTANCE_RPT
        )
        Path(backend._staging_path(backend._instances_cells_path())).write_text(
            _INSTANCE_CELLS
        )
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _emits_then_raises)
    result = backend.run()

    assert isinstance(result, PowerPassResults)
    assert load_model(result.results["phys_model"])["instances"] is None


def test_a_staging_report_left_by_a_killed_run_does_not_survive_the_clear(
    tmp_path, monkeypatch
):
    """An OpenROAD killed inside the block never reaches its trailing deletes,
    and a staging file left in the artefact directory would be the next run's
    to rename over its own."""
    backend = _make_power_backend(tmp_path)
    orphan = Path(backend._staging_path(backend._instances_report_path()))
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(_INSTANCE_RPT)

    _backend, _result = _run_power_with(tmp_path, monkeypatch)

    assert not orphan.exists()


# ---------------------------------------------------------------------------
# Publishing the top the script was generated from (#560 round-17)
# ---------------------------------------------------------------------------


def test_the_published_top_is_the_one_the_script_was_generated_from(
    tmp_path, monkeypatch
):
    """The finding (#560 round-17, Codex P2). Resolving a second time at
    publish re-reads the synth YAML this analysis references, minutes after
    OpenROAD was launched against the first answer: a `top:` edited in between
    would have these watts attributed to a design they do not describe, and
    merged against a co-named publication of another one."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    script_inputs = backend._resolve_inputs()
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        # The referenced synth.yaml is edited while OpenROAD works: the same
        # entry now names another design.
        backend._resolve_inputs = lambda: {**script_inputs, "top": "other_top"}
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    result = backend.run()

    model = load_model(result.results["phys_model"])
    assert model["design"]["top"] == "demo_top"


def test_a_referenced_entry_that_vanishes_mid_run_does_not_null_the_top(
    tmp_path, monkeypatch
):
    """The other half of the same finding: a resolution that *raises* at
    publish used to fall back to `{}` and record no top at all, so the run
    lost the design it had just measured."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)

        def _gone():
            raise FatalRtlBuddyError("synth entry 'demo_synth' not found")

        backend._resolve_inputs = _gone
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    result = backend.run()

    assert load_model(result.results["phys_model"])["design"]["top"] == "demo_top"


# ---------------------------------------------------------------------------
# A stale half that cannot be withdrawn stops the run (#560 round-17)
# ---------------------------------------------------------------------------


def _lock_the_publication(monkeypatch):
    """Make every `_publication_lock` acquisition time out, as a holder that
    outlives `PUBLISH_LOCK_TIMEOUT_SEC` does."""
    import contextlib as _contextlib
    from rtl_buddy.phys import publish as publish_mod

    @_contextlib.contextmanager
    def _held(_artefact_dir):
        raise TimeoutError("phys-publish.lock: another publish has held the lock")
        yield  # pragma: no cover - unreachable, keeps this a context manager

    monkeypatch.setattr(publish_mod, "_publication_lock", _held)


def test_a_half_that_cannot_be_withdrawn_stops_the_power_run(tmp_path, monkeypatch):
    """The finding (#560 round-17, Codex P2). The clear has already deleted
    the per-instance report, so a withdrawal that failed leaves the previous
    run's watts discoverable over nothing. Logging that at DEBUG and running
    anyway made the stale rows survive every failure after it; the run stops
    instead."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )
    model_path = Path(result.results["phys_model"])
    assert load_model(model_path)["instances"]

    _lock_the_publication(monkeypatch)

    def _never_reached(cmd, **kwargs):  # pragma: no cover - the point of the gate
        raise AssertionError("OpenROAD ran over a publication it could not withdraw")

    monkeypatch.setattr(power_openroad.subprocess, "run", _never_reached)
    rerun = backend.run()

    assert isinstance(rerun, PowerFailResults)
    desc = rerun.results["desc"]
    assert "could not be withdrawn" in desc
    assert "phys-publish.lock" in desc
    # And the rows are still there, which is exactly why the run stopped.
    assert load_model(model_path)["instances"]


def test_a_failed_withdrawal_is_named_in_a_post_openroad_failure(tmp_path, monkeypatch):
    """`_fail_after_openroad` runs the same clear. The run was over either
    way, but the user has to learn that the artefact directory still publishes
    rows over the report it just deleted."""
    from unittest.mock import MagicMock
    from rtl_buddy.runner.power_results import PowerFailResults
    from rtl_buddy.tools import power_openroad

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    _lock_the_publication(monkeypatch)

    def _dies(cmd, **kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        return MagicMock(returncode=3)

    monkeypatch.setattr(power_openroad.subprocess, "run", _dies)
    # The start-of-run gate fires first; reach the post-OpenROAD path by
    # letting that one through and failing the tool instead.
    monkeypatch.setattr(backend, "_clear_stale_report", _one_clean_clear(backend))
    rerun = backend.run()

    assert isinstance(rerun, PowerFailResults)
    assert "exited with code 3" in rerun.results["desc"]
    assert "could not be withdrawn" in rerun.results["desc"]


def _one_clean_clear(backend):
    """`_clear_stale_report` that succeeds once and fails thereafter.

    The start-of-run clear and the post-OpenROAD one are the same method, so
    a test that wants the second to report a failed withdrawal has to let the
    first through.
    """
    real = backend._clear_stale_report
    calls = {"n": 0}

    def _clear():
        result = real()
        calls["n"] += 1
        return None if calls["n"] == 1 else result

    return _clear


# ---------------------------------------------------------------------------
# The constraints hash is of the bytes the tool read (#570 round-17)
# ---------------------------------------------------------------------------


def test_a_routed_sdc_replaced_mid_run_records_no_constraints_hash(
    tmp_path, monkeypatch, caplog
):
    """The finding (#570 round-17, Codex P2). The digest used to be taken
    inside the publish, minutes after OpenROAD started — so a `<top>.routed.sdc`
    rewritten by a concurrent `rb pnr` was hashed as the constraints these
    watts were measured under, which is the exact substitution the hash exists
    to catch. Hashed at launch and confirmed on return instead; a file that
    moved has no identity this run can vouch for."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import sha256_of
    from rtl_buddy.tools import power_openroad

    backend, routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, **kwargs):
        # A concurrent `rb pnr` re-routing the same entry, landing
        # mid-analysis and rewriting the SDC in place.
        routed.write_text("create_clock -period 9 [get_ports clk]\n")
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        Path(backend._report_path()).write_text(_TOTAL_RPT)
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)

    with caplog.at_level("WARNING"):
        result = backend.run()

    config = load_model(result.results["phys_model"])["provenance"]["power"]["config"]
    # Neither hash: not the bytes at the start, not the ones on disk now.
    assert config["constraints_sha256"] is None
    assert sha256_of(routed) is not None
    # The path is still recorded — which file the run read is known, which
    # bytes it read is not.
    assert config["constraints"].endswith("demo_top.routed.sdc")
    assert "constraints_changed_during_run" in caplog.text


def test_an_unchanged_sdc_is_recorded_by_the_hash_taken_at_launch(
    tmp_path, monkeypatch
):
    """The success path is untouched: hash-before and confirm-after agree,
    and what is recorded is the file the analysis read."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.publish import sha256_of

    backend, routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    config = load_model(result.results["phys_model"])["provenance"]["power"]["config"]
    assert config["constraints_sha256"] == sha256_of(routed)


# ---------------------------------------------------------------------------
# The power fingerprint covers the technology it reads (#570 round-17)
# ---------------------------------------------------------------------------


def test_two_power_runs_over_two_libraries_do_not_share_a_fingerprint(
    tmp_path, monkeypatch
):
    """The finding (#570 round-17, Codex P2). `_write_script` emits
    `read_liberty` / `read_lef` from the resolved platform, and the options
    mapping recorded only the platform *name* — so a `cfg-pnr-platforms` entry
    repointed at another corner analysed a different technology under one
    fingerprint, and a run listing showed the two as one experiment."""
    from rtl_buddy.phys.model import load_model

    digests = []
    for corner in ("typ", "fast"):
        root = tmp_path / corner
        root.mkdir()
        backend = _make_power_backend(
            root,
            platform=_FakePlatform(
                liberty=f"/pdk/fake/nangate45_{corner}.lib",
                pdk=_FakePdk(f"/pdk/fake/tech_{corner}.lef"),
            ),
        )
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        digests.append(recorded["config"]["options_sha256"])

    assert digests[0] is not None
    assert digests[0] != digests[1]


def test_the_power_fingerprint_lists_the_technology_in_script_order(tmp_path):
    """`read_liberty` and `read_lef` are order-sensitive, so the list is the
    script's order and not a sort — the same rule the synthesis library
    fingerprints keep."""
    backend = _make_power_backend(
        tmp_path,
        platform=_FakePlatform(
            liberty="/pdk/fake/zz_last.lib",
            pdk=_FakePdk("/pdk/fake/aa_tech.lef", "/pdk/fake/mm_macro.lef"),
        ),
    )
    backend._write_script()

    assert backend._phys_technology() == [
        "/pdk/fake/zz_last.lib",
        "/pdk/fake/aa_tech.lef",
        "/pdk/fake/mm_macro.lef",
    ]


def test_a_platform_without_macro_lef_lists_only_what_the_script_reads(tmp_path):
    """The macro LEF line is conditional in the script, so it is conditional
    in the fingerprint: a null entry would be a file the run never read."""
    backend = _make_power_backend(tmp_path)
    backend._write_script()

    assert backend._phys_technology() == [
        "/pdk/fake/nangate45_typ.lib",
        "/pdk/fake/tech.lef",
    ]


# ---------------------------------------------------------------------------
# phys-run — where the power half is published (#589)
# ---------------------------------------------------------------------------


_STAT_JSON = (
    '{"modules": {"\\\\demo_top": {"num_cells": 4}}, "design": {"num_cells": 4}}'
)


def _synth_suite_yaml(entries) -> str:
    body = "".join(
        f'  - name: "{entry}"\n'
        '    desc: "demo"\n'
        '    model: "demo_top"\n'
        '    model_path: "models.yaml"\n'
        '    tool: "yosys"\n'
        "    reglvl: 0\n"
        for entry in entries
    )
    return f"rtl-buddy-filetype: synth_config\nsyntheses:\n{body}"


def _make_paired_power_backend(
    tmp_path, *, phys_run="demo_synth", entries=("demo_synth",), synth_in_project=True
):
    """A power backend in the layout `phys-run:` exists for (#589).

    The synthesis suite under `synth/demo/` and the power suite under
    `power/demo/`, so the two runs' artefact directories cannot coincide
    by accident and the co-location convention produces two half-filled
    models. Re-callable on one ``tmp_path``: a test that runs the same
    entry twice builds a second backend over the directories the first
    one left.

    Returns the backend, the synthesis run's artefact directory, and the
    netlist that directory holds — the file the analysis is stubbed to
    read, as an `rb synth` into it would have written.
    """
    from unittest.mock import MagicMock
    from rtl_buddy.config.power import PowerActivity
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    synth_suite = (root if synth_in_project else tmp_path / "elsewhere") / "synth/demo"
    synth_suite.mkdir(parents=True, exist_ok=True)
    (synth_suite / "models.yaml").write_text(
        'rtl-buddy-filetype: model_config\nmodels:\n  - name: "demo_top"\n'
        "    filelist: []\n"
    )
    (synth_suite / "synth.yaml").write_text(_synth_suite_yaml(entries))
    synth_artefacts = synth_suite / "artefacts" / "demo_synth"
    synth_artefacts.mkdir(parents=True, exist_ok=True)
    netlist = synth_artefacts / "synth_netlist.v"
    netlist.write_text("module demo_top(); endmodule\n")

    power_suite = root / "power" / "demo"
    power_suite.mkdir(parents=True, exist_ok=True)
    sdc = power_suite / "constraints.sdc"
    sdc.write_text("create_clock -period 10 [get_ports clk]\n")

    cfg = PowerConfig(
        name="demo_power",
        desc="demo",
        tool="openroad",
        mode="static",
        netlist_source="synth",
        synth_name="demo_synth",
        synth_suite_path=str(synth_suite / "synth.yaml"),
        pnr_name=None,
        pnr_suite_path=None,
        constraints=str(sdc),
        platform="nangate45_typ",
        activity=PowerActivity(
            saif=None,
            vcd=None,
            scope=None,
            default_toggle_rate=0.1,
            default_static_prob=0.5,
        ),
        _reglvl=None,
        tool_overrides=None,
        phys_run=phys_run,
    )
    backend = OpenRoadPower(
        name="demo/openroad",
        power_cfg=cfg,
        suite_dir=str(power_suite),
        root_cfg=MagicMock(),
    )
    backend._resolve_inputs = lambda: {
        "netlist": str(netlist),
        "odb": None,
        "sdc": str(sdc),
        "top": "demo_top",
    }
    backend._resolve_platform = lambda: _FakePlatform()
    return backend, synth_artefacts, netlist


def _publish_the_synthesis_half(artefacts, netlist):
    """What an `rb synth` into ``artefacts`` leaves for a power run to find."""
    from rtl_buddy.phys.publish import publish_synth

    (artefacts / "synth_stat.json").write_text(_STAT_JSON)
    return publish_synth(
        artefact_dir=str(artefacts),
        top="demo_top",
        backend="yosys",
        run="demo_synth",
        stats_path=str(artefacts / "synth_stat.json"),
        netlist_path=str(netlist),
        area_um2=5.586,
        gate_count=4,
    )


def test_phys_run_publishes_the_power_half_into_the_synthesis_directory(
    tmp_path, monkeypatch
):
    """The whole of the field: the model goes where the synthesis writes
    its own half, and the raw output stays with the run that produced it
    (#589)."""
    backend, synth_artefacts, _netlist = _make_paired_power_backend(tmp_path)

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    assert Path(result.results["phys_model"]).parent == synth_artefacts
    assert (synth_artefacts / "phys-manifest.json").exists()
    own = Path(backend.artefact_dir)
    assert not (own / "phys-model.json").exists()
    assert not (own / "phys-manifest.json").exists()
    # The reports, the log and the netlist copy are this run's own output
    # and are read back from its own directory.
    assert (own / "power.rpt").exists()
    assert (own / "power_instances.rpt").exists()
    assert (own / "power_netlist.v").exists()
    # And the manifest reaches them from where it now sits: the paths are
    # project-relative, so they join back onto the power run's directory
    # from the synthesis run's.
    manifest_path = synth_artefacts / "phys-manifest.json"
    manifest = load_manifest(str(manifest_path))
    report = manifest["power"]["report"]
    assert not os.path.isabs(report)
    assert Path(resolve(str(manifest_path), report)) == own / "power.rpt"


def test_without_phys_run_the_model_stays_in_the_runs_own_directory(
    tmp_path, monkeypatch
):
    """The convention `phys-run:` overrides, unchanged for a config that
    says nothing."""
    backend, synth_artefacts, _netlist = _make_paired_power_backend(
        tmp_path, phys_run=None
    )

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    assert Path(result.results["phys_model"]).parent == Path(backend.artefact_dir)
    assert not (synth_artefacts / "phys-model.json").exists()


def test_phys_run_completes_the_synthesis_half_already_published_there(
    tmp_path, monkeypatch
):
    """The half-filled model the field exists to end: the two suites are in
    two directories, so nothing but the name pairs them — and with the name
    given, one document comes out holding both halves (#589)."""
    from rtl_buddy.phys.model import load_model

    backend, synth_artefacts, netlist = _make_paired_power_backend(tmp_path)
    _publish_the_synthesis_half(synth_artefacts, netlist)

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    model = load_model(result.results["phys_model"])
    assert [row["module"] for row in model["modules"]] == ["demo_top"]
    assert len(model["instances"]) == 2
    assert model["totals"]["area_um2"] == pytest.approx(5.586)
    assert model["totals"]["total_uw"] == pytest.approx(28.3)


def test_a_phys_run_naming_no_synthesis_entry_fails_the_analysis(tmp_path, monkeypatch):
    """A typo or a rename would otherwise publish a merged model into a
    directory no run owns — harder to find than the half-filled pair."""
    from rtl_buddy.runner.power_results import PowerFailResults

    backend, _artefacts, _netlist = _make_paired_power_backend(
        tmp_path, phys_run="typo_synth"
    )

    result = _run_prepared_power(backend, monkeypatch, instances=_INSTANCE_RPT)

    assert isinstance(result, PowerFailResults)
    assert "typo_synth" in result.results["desc"]


def test_a_phys_run_outside_the_project_is_refused(tmp_path, monkeypatch):
    """`synth-path:` reaching into another checkout would put the merged
    model where this project's `rb phys` discovery never walks."""
    from rtl_buddy.runner.power_results import PowerFailResults

    backend, _artefacts, _netlist = _make_paired_power_backend(
        tmp_path, synth_in_project=False
    )

    result = _run_prepared_power(backend, monkeypatch, instances=_INSTANCE_RPT)

    assert isinstance(result, PowerFailResults)
    assert "outside the project" in result.results["desc"]


def test_a_paired_run_says_so_when_the_synthesis_half_read_another_netlist(
    tmp_path, monkeypatch, caplog
):
    """The pairing was asked for by name, so the gate refusing it is news.
    Without the field there is no expectation to disappoint and the same
    mismatch stays quiet."""
    from rtl_buddy.phys.model import load_model

    backend, synth_artefacts, _netlist = _make_paired_power_backend(tmp_path)
    stale = synth_artefacts / "an_older_netlist.v"
    stale.write_text("module demo_top(); wire other; endmodule\n")
    _publish_the_synthesis_half(synth_artefacts, stale)

    with caplog.at_level("WARNING"):
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )

    assert "phys_pair_mismatch" in caplog.text
    model = load_model(result.results["phys_model"])
    assert model["modules"] is None
    assert len(model["instances"]) == 2


def test_a_paired_run_is_quiet_when_the_two_halves_agree(tmp_path, monkeypatch, caplog):
    """The ordinary pair. A warning here would be noise on every run."""
    backend, synth_artefacts, netlist = _make_paired_power_backend(tmp_path)
    _publish_the_synthesis_half(synth_artefacts, netlist)

    with caplog.at_level("WARNING"):
        _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )

    assert "phys_pair_mismatch" not in caplog.text


def test_a_failed_paired_rerun_withdraws_its_half_from_the_synthesis_directory(
    tmp_path, monkeypatch
):
    """The withdrawal follows the publication. A rerun that fails has
    deleted the reports behind its rows, so the rows go too — out of the
    synthesis run's directory, leaving the synthesis' own half alone."""
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.tools import power_openroad

    backend, synth_artefacts, netlist = _make_paired_power_backend(tmp_path)
    _publish_the_synthesis_half(synth_artefacts, netlist)
    _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )
    assert load_model(str(synth_artefacts / "phys-model.json"))["instances"]

    rerun, _artefacts, _netlist = _make_paired_power_backend(tmp_path)
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    def _dies(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        return MagicMock(returncode=1)

    monkeypatch.setattr(power_openroad.subprocess, "run", _dies)

    rerun.run()

    model = load_model(str(synth_artefacts / "phys-model.json"))
    assert model["instances"] is None
    assert [row["module"] for row in model["modules"]] == ["demo_top"]


# ---------------------------------------------------------------------------
# Macro Liberty reaches the power run, and a macro with none is not a
# silent zero (#627)
# ---------------------------------------------------------------------------


#: What `_make_power_backend`'s fixture generates with no macro anywhere:
#: the script this flow emitted before #627, line for line. The paths are
#: the run's own, so the template is filled from the backend under test
#: rather than pinned absolutely.
_BASELINE_SCRIPT = """\
# Generated by rtl_buddy power flow
read_liberty /pdk/fake/nangate45_typ.lib
read_lef /pdk/fake/tech.lef
read_verilog {artefacts}/power_netlist.v
link_design demo_top
read_sdc {sdc}
report_power > {artefacts}/power.rpt
puts "RB_PHYS_DETAIL_BEGIN"
catch {{
  set rb_insts [get_cells -hierarchical *]
  if {{[llength $rb_insts] > 0}} {{
    set rb_fh [open {artefacts}/power_instances.cells.tmp w]
    foreach rb_inst $rb_insts {{
      puts $rb_fh "[get_full_name $rb_inst] [get_property $rb_inst ref_name]"
    }}
    close $rb_fh
    report_power -instances $rb_insts > {artefacts}/power_instances.rpt.tmp
    file rename -force {artefacts}/power_instances.cells.tmp \
{artefacts}/power_instances.cells
    file rename -force {artefacts}/power_instances.rpt.tmp \
{artefacts}/power_instances.rpt
  }}
}}
catch {{file delete -force {artefacts}/power_instances.cells.tmp}}
catch {{file delete -force {artefacts}/power_instances.rpt.tmp}}
exit
"""


def _capture_power_events(monkeypatch):
    """Record every `log_event` the power backend emits, with its level.

    Rather than caplog: `log_event` renders a human sentence into the
    message, so asserting on the text would pass on any wording that
    happened to carry the event id.
    """
    from rtl_buddy.tools import power_openroad

    events: list[tuple[int, str, dict]] = []
    real = power_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(power_openroad, "log_event", _record)
    return events


def _fields_of(events, name):
    """The fields of every recorded event called ``name``."""
    return [fields for _level, event, fields in events if event == name]


def _write_macro_liberty(path, cell="sram_32x256"):
    """A Liberty stub with one cell declaration in it."""
    path.write_text(
        dedent(f"""\
        library (macro) {{
          cell ({cell}) {{
            area : 1234.0;
          }}
        }}
        """)
    )
    return str(path)


def _write_upstream_suites(tmp_path, *, pnr_lib=(), lef=(), synth_lib=()):
    """A models/synth/pnr trio the power config resolves through for real.

    The inheritance rule is a statement about the *referenced run*, so the
    tests that assert it load that run out of YAML rather than stub it.
    ``lef`` goes on both entries, as a real macro's LEF does: P&R places
    it and synthesis links against it.
    """
    (tmp_path / "models.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: model_config
        models:
          - name: "demo_top"
            filelist: []
        """)
    )

    def _list(paths):
        return "[" + ", ".join(f'"{p}"' for p in paths) + "]"

    (tmp_path / "synth.yaml").write_text(
        dedent(f"""\
        rtl-buddy-filetype: synth_config
        syntheses:
          - name: "demo_synth"
            desc: "demo"
            model: "demo_top"
            model_path: "models.yaml"
            tool: "yosys"
            lib-paths: {_list(synth_lib)}
            lef-paths: {_list(lef)}
            reglvl: 0
        """)
    )
    (tmp_path / "pnr.yaml").write_text(
        dedent(f"""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: "demo_pnr"
            desc: "demo"
            tool: "openroad"
            synth: "demo_synth"
            synth-path: "synth.yaml"
            constraints: "constraints.sdc"
            platform: "nangate45_typ"
            lib-paths: {_list(pnr_lib)}
            lef-paths: {_list(lef)}
            reglvl: 0
        """)
    )


def _power_suite(tmp_path, body):
    """Load a one-run `power.yaml` written into ``tmp_path``."""
    path = tmp_path / "power.yaml"
    path.write_text(
        "rtl-buddy-filetype: power_config\nruns:\n" + dedent(body).rstrip() + "\n"
    )
    return PowerSuiteConfig(str(path))


# --- config: the new `lib-paths` key ---------------------------------------


def test_power_lib_paths_default_to_nothing(tmp_path):
    """Every `power.yaml` written before #627 declares no macro Liberty."""
    suite = _power_suite(
        tmp_path,
        """
          - name: "p"
            desc: "d"
            synth: "s"
            synth-path: "synth.yaml"
            platform: "nangate45_typ"
        """,
    )
    assert suite.get_runs("p")[0].get_lib_paths() == []


def test_power_lib_paths_resolve_against_the_power_yaml(tmp_path):
    """Relative like every other path on the entry — against the directory
    holding `power.yaml`, never the process cwd."""
    suite = _power_suite(
        tmp_path,
        """
          - name: "p"
            desc: "d"
            synth: "s"
            synth-path: "synth.yaml"
            platform: "nangate45_typ"
            lib-paths: ["../pdk/sram/sram.lib", "extra.lib"]
        """,
    )
    assert suite.get_runs("p")[0].get_lib_paths() == [
        os.path.normpath(str(tmp_path.parent / "pdk" / "sram" / "sram.lib")),
        str(tmp_path / "extra.lib"),
    ]


# --- inheritance ------------------------------------------------------------


def _inputs_with_upstream(tmp_path, *, source, pnr_lib=(), lef=(), synth_lib=()):
    """`_resolve_inputs()` of a backend whose upstream suites are real."""
    _write_upstream_suites(tmp_path, pnr_lib=pnr_lib, lef=lef, synth_lib=synth_lib)
    backend = _make_power_backend(tmp_path)
    del backend._resolve_inputs  # undo the fixture's stub
    cfg = backend.power_cfg
    cfg.netlist_source = source
    cfg.pnr_name = "demo_pnr"
    cfg.pnr_suite_path = str(tmp_path / "pnr.yaml")
    return backend


def test_a_pnr_power_run_inherits_the_pnr_runs_macro_liberty(tmp_path):
    """The macro's Liberty reaches `rb pnr` through that run's `lib-paths`;
    without inheriting it the power analysis reads only the platform corner
    and the macro contributes exactly zero (#627)."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    backend = _inputs_with_upstream(tmp_path, source="pnr", pnr_lib=["sram.lib"])

    inputs = backend._resolve_inputs()

    assert inputs["macro_libs"] == [lib]
    # The ODB carries every master the router placed, so no LEF is read.
    assert inputs["macro_lefs"] == []


def test_a_synth_power_run_inherits_the_synth_runs_macro_liberty_and_lef(tmp_path):
    """`read_verilog` + `link_design` builds the database out of LEF masters,
    so the synthesis source needs the LEF as well as the Liberty."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    lef = tmp_path / "sram.lef"
    lef.write_text("MACRO sram_32x256\nEND sram_32x256\n")
    backend = _inputs_with_upstream(
        tmp_path, source="synth", synth_lib=["sram.lib"], lef=["sram.lef"]
    )

    inputs = backend._resolve_inputs()

    assert inputs["macro_libs"] == [lib]
    assert inputs["macro_lefs"] == [str(lef)]


def test_an_explicit_lib_path_is_added_after_the_inherited_ones(tmp_path):
    """`read_liberty` is order-sensitive, and the inherited list is the base
    a `power.yaml` adds to."""
    inherited = _write_macro_liberty(tmp_path / "sram.lib")
    own = _write_macro_liberty(tmp_path / "pll.lib", cell="pll")
    backend = _inputs_with_upstream(tmp_path, source="pnr", pnr_lib=["sram.lib"])
    backend.power_cfg.lib_paths = [own]

    assert backend._resolve_inputs()["macro_libs"] == [inherited, own]


def test_a_library_named_on_both_sides_is_read_once(tmp_path):
    """De-duplicated on the resolved path, in first-named order: reading one
    Liberty twice makes OpenSTA re-register every cell in it."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    other = _write_macro_liberty(tmp_path / "pll.lib", cell="pll")
    backend = _inputs_with_upstream(
        tmp_path, source="pnr", pnr_lib=["sram.lib", "pll.lib"]
    )
    # The same file, reached by a path spelled differently.
    backend.power_cfg.lib_paths = [str(tmp_path / "." / "sram.lib")]

    assert backend._resolve_inputs()["macro_libs"] == [lib, other]


# --- the generated script ---------------------------------------------------


def _script_with_macros(tmp_path, *, libs=(), lefs=()):
    backend = _make_power_backend(tmp_path)
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {
        **inner(),
        "macro_libs": [str(p) for p in libs],
        "macro_lefs": [str(p) for p in lefs],
    }
    return backend, Path(backend._write_script()).read_text()


def test_the_script_reads_the_macro_liberty_after_the_platform_corner(tmp_path):
    """After, so a macro library can never shadow a standard cell."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    lef = tmp_path / "sram.lef"
    lef.write_text("MACRO sram_32x256\nEND sram_32x256\n")

    _backend, script = _script_with_macros(tmp_path, libs=[lib], lefs=[lef])
    lines = script.splitlines()

    assert lines[1] == "read_liberty /pdk/fake/nangate45_typ.lib"
    assert lines[2] == f"read_liberty {lib}"
    assert lines[3] == "read_lef /pdk/fake/tech.lef"
    assert lines[4] == f"read_lef {lef}"


def test_a_run_with_no_macros_generates_the_script_it_always_did(tmp_path):
    """Byte-identity with the pre-#627 flow. Both new lists are empty for a
    design with no macros, and every existing power run has to keep its
    compile-identical script — the whole fingerprint rests on it."""
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert script == _BASELINE_SCRIPT.format(
        artefacts=backend.artefact_dir, sdc=str(tmp_path / "constraints.sdc")
    )


def test_a_configured_macro_library_that_is_not_on_disk_stops_the_run(
    tmp_path, monkeypatch
):
    """Before OpenROAD is launched, and at ERROR, the way a stream-out judges
    its own inputs: a `read_liberty` of a path that is not there is a line in
    a log nobody reads and a macro back at 0 W (#627)."""
    from rtl_buddy.tools import power_openroad
    from rtl_buddy.runner.power_results import PowerFailResults

    launched = []
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(
        power_openroad.subprocess, "run", lambda *a, **k: launched.append(a)
    )

    backend = _make_power_backend(tmp_path)
    inner = backend._resolve_inputs
    missing = str(tmp_path / "gone.lib")
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": [missing]}
    events = _capture_power_events(monkeypatch)

    res = backend.run()

    assert isinstance(res, PowerFailResults)
    assert res.results["fail_stage"] == "setup"
    assert "configured macro input(s) not on disk" in res.results["desc"]
    assert missing in res.results["desc"]
    assert _fields_of(events, "power.missing_macro_inputs") == [
        {"power": "demo_power", "count": 1, "missing": [missing]}
    ]
    assert launched == []


def test_the_fingerprint_lists_the_macro_libraries_in_script_order(tmp_path):
    """A run that reads a macro's Liberty and one that does not measure the
    same netlist and report different watts — the before and after of #627 —
    so they must not digest identically (#570)."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")

    backend, _script = _script_with_macros(tmp_path, libs=[lib])

    assert backend._phys_technology() == [
        "/pdk/fake/nangate45_typ.lib",
        lib,
        "/pdk/fake/tech.lef",
    ]


# --- the macro with no library at all ---------------------------------------


_MACRO_INSTANCE_RPT = (
    "   Internal  Switching    Leakage      Total\n"
    "      Power      Power      Power      Power (Watts)\n"
    "--------------------------------------------\n"
    "   2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_\n"
    "   0.00e+00   0.00e+00   0.00e+00   0.00e+00 u_mem/u_sram\n"
)

_MACRO_INSTANCE_CELLS = "u_sub/_64_ DFF_X1\nu_mem/u_sram sram_32x256\n"


def _run_with_a_macro(tmp_path, monkeypatch, *, libs=()):
    backend = _make_power_backend(tmp_path)
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": [str(p) for p in libs]}
    return backend, _run_prepared_power(
        backend,
        monkeypatch,
        instances=_MACRO_INSTANCE_RPT,
        cells=_MACRO_INSTANCE_CELLS,
    )


def test_a_macro_with_no_liberty_is_named_rather_than_reported_as_zero(
    tmp_path, monkeypatch
):
    """The instance is in the design, in the report, and at 0.00e+00 in all
    four columns. Nothing in the report tells that apart from a cell that
    burns nothing, so the run says it (#627)."""
    events = _capture_power_events(monkeypatch)
    _backend, res = _run_with_a_macro(tmp_path, monkeypatch)

    assert res.results["result"] == "PASS"
    assert res.results["unpowered_cells"] == ["sram_32x256"]
    assert res.results["unpowered_cell_count"] == 1
    assert res.results["unpowered_instance_count"] == 1
    assert "no Liberty power data" in res.results["desc"]
    assert "sram_32x256" in res.results["desc"]
    assert _fields_of(events, "power.unpowered_instances") == [
        {"power": "demo_power", "count": 1, "cells": ["sram_32x256"]}
    ]


def test_the_verdict_is_unchanged_by_an_unpowered_macro(tmp_path, monkeypatch):
    """The watts reported are a real measurement of everything that had a
    library; refusing them would cost a user the standard-cell figure."""
    _backend, res = _run_with_a_macro(tmp_path, monkeypatch)

    assert res.is_pass()
    assert res.results["total_w"] == 2.83e-05


def test_a_macro_whose_liberty_the_run_read_is_not_flagged(tmp_path, monkeypatch):
    """The cell is declared in a Liberty the script named, so whatever the
    report says about it is a measurement."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    events = _capture_power_events(monkeypatch)

    _backend, res = _run_with_a_macro(tmp_path, monkeypatch, libs=[lib])

    assert "unpowered_cells" not in res.results
    assert res.results["desc"] == "Power analysis passed"
    assert _fields_of(events, "power.unpowered_instances") == []


def test_an_instance_at_zero_whose_cell_has_a_library_is_not_flagged(
    tmp_path, monkeypatch
):
    """Both conditions, not either: a standard cell that really does sit at
    zero has power data and is nobody's configuration error."""
    backend = _make_power_backend(tmp_path)
    liberty = tmp_path / "platform.lib"
    _write_macro_liberty(liberty, cell="DFF_X1")
    backend._resolve_platform = lambda: _FakePlatform(liberty=str(liberty))
    res = _run_prepared_power(
        backend,
        monkeypatch,
        instances=(
            "   Internal  Switching    Leakage      Total\n"
            "   0.00e+00   0.00e+00   0.00e+00   0.00e+00 u_sub/_64_\n"
        ),
        cells="u_sub/_64_ DFF_X1\n",
    )

    assert "unpowered_cells" not in res.results


def test_an_unpowered_macro_is_in_the_machine_row(tmp_path, monkeypatch):
    """`--machine` is the whole output for a consumer that cannot re-read the
    table, so the qualifier has to be a field and not only prose."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    _backend, res = _run_with_a_macro(tmp_path, monkeypatch)
    row = RtlBuddy._power_result_row(None, {"power_name": "demo_power", "results": res})

    assert row["unpowered_cells"] == ["sram_32x256"]
    assert row["unpowered_cell_count"] == 1
    assert row["unpowered_instance_count"] == 1


def test_the_published_model_carries_the_macros_own_watts(tmp_path, monkeypatch):
    """The roll-up `rb phys` does over `power_instances.rpt` is name-based, so
    a macro reaches a module total exactly when its row reaches the model with
    its Liberty cell in the module column (#558, #627)."""
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.phys.query import is_descendant, subtree_rollup

    backend = _make_power_backend(tmp_path)
    inner = backend._resolve_inputs
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": [lib]}
    res = _run_prepared_power(
        backend,
        monkeypatch,
        instances=(
            "   Internal  Switching    Leakage      Total\n"
            "   2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_\n"
            "   4.00e-05   1.00e-06   5.00e-07   4.15e-05 u_mem/u_sram\n"
        ),
        cells=_MACRO_INSTANCE_CELLS,
    )

    model = load_model(res.results["phys_model"])
    macro = next(
        row for row in model["instances"] if row["instance_path"] == "u_mem/u_sram"
    )
    assert macro["module"] == "sram_32x256"
    assert macro["total_uw"] == pytest.approx(41.5)

    under_mem = [
        row
        for row in model["instances"]
        if is_descendant(row["instance_path"], "u_mem")
    ]
    assert subtree_rollup(under_mem)["total_uw"] == pytest.approx(41.5)


def test_the_pdks_fill_cells_are_not_reported_as_unpowered(tmp_path, monkeypatch):
    """`rb pnr` ends with `filler_placement`, so a routed database holds
    tens of thousands of fill instances that are in the layout and not in
    the netlist. They have no Liberty and no power by construction; the
    PDK names them, and reporting them would bury the one macro this
    exists to find (#627). Measured on the sky130hd pipeclean: 26 076
    instances of four fill cells beside a single SRAM."""
    backend = _make_power_backend(
        tmp_path,
        platform=_FakePlatform(
            pdk=_FakePdk("/pdk/fake/tech.lef", fill_cells=["FILLCELL_X1"])
        ),
    )
    res = _run_prepared_power(
        backend,
        monkeypatch,
        instances=(
            "   Internal  Switching    Leakage      Total\n"
            "   0.00e+00   0.00e+00   0.00e+00   0.00e+00 FILLER_1\n"
            "   0.00e+00   0.00e+00   0.00e+00   0.00e+00 u_mem/u_sram\n"
        ),
        cells="FILLER_1 FILLCELL_X1\nu_mem/u_sram sram_32x256\n",
    )

    assert res.results["unpowered_cells"] == ["sram_32x256"]
    assert res.results["unpowered_instance_count"] == 1


# ---------------------------------------------------------------------------
# OpenROAD thread count (#654)
# ---------------------------------------------------------------------------


def _no_allocation(monkeypatch):
    from rtl_buddy.config import openroad_threads

    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setattr(openroad_threads, "_affinity_count", lambda: None)


def test_power_script_without_threads_is_unchanged(tmp_path, monkeypatch):
    _no_allocation(monkeypatch)
    backend = _make_power_backend(tmp_path)
    script = Path(backend._write_script()).read_text()
    assert "set_thread_count" not in script
    assert script.startswith("# Generated by rtl_buddy power flow\nread_liberty ")


def test_power_threads_precede_the_liberty_and_reach_the_results(tmp_path, monkeypatch):
    _no_allocation(monkeypatch)
    backend = _make_power_backend(tmp_path)
    backend.power_cfg.threads = 2
    script = Path(backend._write_script()).read_text()
    assert script.startswith(
        "# Generated by rtl_buddy power flow\nset_thread_count 2\nread_liberty "
    )

    backend = _make_power_backend(tmp_path)
    backend.power_cfg.threads = 2
    res = _run_prepared_power(backend, monkeypatch)
    assert res.results["result"] == "PASS"
    # The fake OpenROAD logged nothing, so the recorded count is the one set.
    assert res.results["openroad_threads"]["requested"] == 2
    assert res.results["openroad_threads"]["effective"] == 2


def test_power_threads_clamped_to_a_slurm_allocation(tmp_path, monkeypatch):
    from rtl_buddy.config import openroad_threads

    monkeypatch.setenv("SLURM_JOB_ID", "9")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "1")
    monkeypatch.setattr(openroad_threads, "_affinity_count", lambda: None)
    backend = _make_power_backend(tmp_path)
    backend.power_cfg.threads = 4
    assert "set_thread_count 1\n" in Path(backend._write_script()).read_text()


def test_power_threads_never_read_a_previous_runs_log(tmp_path, monkeypatch):
    """OpenROAD truncates `power.log` only once running; a launch that dies
    earlier must not report the previous run's ORD-0030 count (#654)."""
    from unittest.mock import MagicMock
    from rtl_buddy.tools import power_openroad

    _no_allocation(monkeypatch)
    backend = _make_power_backend(tmp_path)
    backend.power_cfg.threads = 2
    Path(backend._log_path()).write_text("[INFO ORD-0030] Using 8 thread(s).\n")
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        power_openroad.subprocess,
        "run",
        lambda cmd, **kw: MagicMock(returncode=134, stderr="dyld: Library not loaded"),
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["openroad_threads"]["effective"] == 2


# ---------------------------------------------------------------------------
# Extracted parasitics: reading the P&R run's routed SPEF (#101, #104 item 1)
# ---------------------------------------------------------------------------


def _age(path, seconds):
    """Set a file's mtime `seconds` into the past."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - int(seconds * 1e9)))


def _pnr_artefact_with_spef(
    root, *, extracted=True, spef=True, spef_age=0.0, script_age=60.0
):
    """A P&R artefact dir as `rb pnr` leaves it: script first, SPEF last.

    ``extracted`` says whether the flow script carries a `write_spef`
    command, i.e. whether the run that wrote the ODB was configured with
    `rcx-rules`. Ages are seconds into the past.
    """
    root.mkdir(parents=True, exist_ok=True)
    script = root / "pnr.tcl"
    script.write_text(
        "filler_placement $FILL_CELLS\n"
        + ("write_spef $OUT_DIR/${DESIGN}.routed.spef\n" if extracted else "")
        + "write_db $OUT_DIR/${DESIGN}.routed.odb\n"
    )
    _age(script, script_age)
    spef_path = root / "demo_top.routed.spef"
    if spef:
        spef_path.write_text('*SPEF "IEEE 1481-1998"\n')
        _age(spef_path, spef_age)
    return str(spef_path), str(script)


def test_a_spef_the_pnr_run_wrote_is_trusted(tmp_path):
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path)
    assert routed_spef_rejection(spef, script) is None


def test_no_spef_is_rejected_as_absent(tmp_path):
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path, spef=False)
    assert routed_spef_rejection(spef, script) == "no routed SPEF"


def test_a_spef_older_than_the_pnr_run_is_stale(tmp_path):
    """The case the clear list cannot cover: an rtl_buddy that predates the
    SPEF reruns P&R, rewrites the script and the ODB, and leaves the previous
    run's SPEF where it was."""
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path, spef_age=120.0)
    assert "older than the P&R run" in routed_spef_rejection(spef, script)


def test_a_spef_beside_a_run_that_did_not_extract_is_stale(tmp_path):
    """However new it is: the script that produced the ODB never wrote it."""
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path, extracted=False)
    assert "did not extract" in routed_spef_rejection(spef, script)


def test_a_spef_with_no_flow_script_is_not_vouched_for(tmp_path):
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path)
    os.unlink(script)
    assert "no P&R flow script" in routed_spef_rejection(spef, script)


def _make_spef_power_backend(tmp_path, **artefact):
    """`_make_pnr_power_backend`, with the SPEF and script resolved too."""
    backend, routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    inputs = backend._resolve_inputs()
    spef, script = _pnr_artefact_with_spef(Path(inputs["odb"]).parent, **artefact)
    backend._resolve_inputs = lambda: {**inputs, "spef": spef, "pnr_script": script}
    return backend, spef


def test_a_pnr_power_run_reads_the_trusted_spef_instead_of_estimating(tmp_path):
    backend, spef = _make_spef_power_backend(tmp_path)

    lines = Path(backend._write_script()).read_text().splitlines()

    assert f"read_spef {spef}" in lines
    assert "estimate_parasitics -global_routing" not in lines
    # After the database and the constraints it annotates, before power.
    read_db = next(i for i, ln in enumerate(lines) if ln.startswith("read_db "))
    read_sdc = next(i for i, ln in enumerate(lines) if ln.startswith("read_sdc "))
    read_spef = lines.index(f"read_spef {spef}")
    report = next(i for i, ln in enumerate(lines) if ln.startswith("report_power >"))
    assert read_db < read_sdc < read_spef < report
    assert backend._parasitics == "spef"


def test_a_pnr_power_run_without_a_spef_estimates_as_before(tmp_path):
    """The ODB-only fallback — Nangate45, which ships no rules — unchanged."""
    backend, spef = _make_spef_power_backend(tmp_path, spef=False)

    script = Path(backend._write_script()).read_text()

    assert "estimate_parasitics -global_routing\n" in script
    assert "read_spef" not in script
    assert backend._parasitics == "estimated"


def test_a_stale_spef_falls_back_to_the_estimate_and_says_so(tmp_path, monkeypatch):
    backend, spef = _make_spef_power_backend(tmp_path, spef_age=120.0)
    events = _capture_power_events(monkeypatch)

    script = Path(backend._write_script()).read_text()

    assert "read_spef" not in script
    assert "estimate_parasitics -global_routing\n" in script
    (rejected,) = _fields_of(events, "power.spef_rejected")
    assert rejected["spef"] == spef
    assert "older than" in rejected["reason"]
    (chosen,) = _fields_of(events, "power.parasitics")
    assert chosen["parasitics"] == "estimated"


def test_a_missing_spef_is_not_a_warning(tmp_path, monkeypatch):
    """No rules, no SPEF: the ordinary Nangate45 run has nothing to warn about."""
    backend, _spef = _make_spef_power_backend(tmp_path, spef=False)
    events = _capture_power_events(monkeypatch)

    backend._write_script()

    assert _fields_of(events, "power.spef_rejected") == []


def test_the_parasitics_source_reaches_the_results(tmp_path, monkeypatch):
    backend, _spef = _make_spef_power_backend(tmp_path)

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    assert result.results["parasitics"] == "spef"


def test_a_synth_source_run_records_no_parasitics(tmp_path, monkeypatch):
    _backend, result = _run_power_with(tmp_path, monkeypatch)

    assert "parasitics" not in result.results


def test_spef_and_estimated_runs_over_one_odb_do_not_share_a_fingerprint(
    tmp_path, monkeypatch
):
    """Same ODB, same SDC, same activity: timed on extracted parasitics and
    on the global-route estimate they are two measurements."""
    from rtl_buddy.phys.model import load_model

    digests = []
    # One directory for both, so the ODB path — the rest of the upstream
    # identity — is the same; only the SPEF beside it comes and goes.
    for spef in (True, False):
        backend, spef_path = _make_spef_power_backend(tmp_path)
        if not spef:
            os.unlink(spef_path)
        result = _run_prepared_power(
            backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
        )
        assert result.results["parasitics"] == ("spef" if spef else "estimated")
        recorded = load_model(result.results["phys_model"])["provenance"]["power"]
        digests.append(recorded["config"]["options_sha256"])

    assert digests[0] is not None and digests[0] != digests[1]
