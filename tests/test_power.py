"""Tests for the power-analysis config schema and the OpenRoadPower backend."""

import hashlib
import json
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
from rtl_buddy.config.blocks import BlockRef
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.phys.manifest import load_manifest, resolve
from rtl_buddy.tools import pnr_abstract


def test_power_tool_cfg_exposes_name_and_executable():
    cfg = PowerToolConfig(PowerToolConfigFile(name="openroad", tool="openroad"))
    assert cfg.get_name() == "openroad"
    assert cfg.get_executable() == "openroad"


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
    """Static mode wins over a trace: no activity command is emitted."""
    cfg = _make_power_cfg_with("static", _activity(saif="/tmp/x.saif"))
    assert cfg.get_activity_source() == "default"


def test_power_backends_registry_contains_openroad():
    from rtl_buddy.runner.power_runner import _POWER_BACKENDS
    from rtl_buddy.tools.power_base import BasePower
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    assert "openroad" in _POWER_BACKENDS
    assert _POWER_BACKENDS["openroad"] is OpenRoadPower
    assert issubclass(OpenRoadPower, BasePower)


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
    """A yaml without netlist-source defaults to synth."""
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
    """Without `phys-run:` the power half is published into the run's own directory."""
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
    """`phys-run` is a run name, not a path; a separator is refused so the join onto the
    synth suite's `artefacts/` cannot reach elsewhere.
    """
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
    """`phys-run` requires a synth netlist source: a `netlist-source: pnr` half records no
    netlist hash, so it could not merge with a synthesis.
    """
    p = tmp_path / "power.yaml"
    p.write_text(_phys_run_yaml("demo_synth", source="pnr"))
    with pytest.raises(FatalRtlBuddyError, match="netlist-source"):
        PowerSuiteConfig(str(p))


def test_power_synth_source_still_requires_synth_fields(tmp_path):
    """The synth source keeps its required-field validation."""
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
    """get_top() picks the resolver from netlist_source."""
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


class _FakePdk:
    """A `cfg-pdks` corner as the power script reads it: two LEF paths."""

    def __init__(
        self, tech_lef, macro_lef=None, fill_cells=(), layer_rc_tcl="", platform_tcl=""
    ):
        self._tech_lef = tech_lef
        self._macro_lef = macro_lef
        self._layer_rc_tcl = layer_rc_tcl
        self._platform_tcl = platform_tcl
        # `filler_placement` adds tens of thousands of these to a routed database; they
        # have no Liberty and no power.
        self._fill_cells = list(fill_cells)

    def get_name(self):
        return "fake_pdk"

    def get_fill_cells(self):
        return list(self._fill_cells)

    def get_tech_lef(self):
        return self._tech_lef

    def get_macro_lef(self):
        return self._macro_lef

    def get_layer_rc_tcl(self):
        return self._layer_rc_tcl

    def get_platform_tcl(self):
        return self._platform_tcl


class _FakePlatform:
    """A `cfg-pnr-platforms` entry: one Liberty, over one PDK corner.

    Named paths rather than a `MagicMock`, because the power fingerprint digests the
    technology and a mock answers every call with a new object.
    """

    def __init__(
        self,
        liberty="/pdk/fake/nangate45_typ.lib",
        pdk=None,
        corners=None,
        max_fanout=None,
    ):
        self._liberty = liberty
        self._max_fanout = max_fanout
        self._pdk = pdk or _FakePdk("/pdk/fake/tech.lef")
        # corner -> Liberty, primary first, for a multi-corner platform; `None` is the
        # single-corner platform other tests use.
        self._corners = corners

    def get_sta_lib_paths(self):
        return [self._liberty]

    def is_multi_corner(self):
        return bool(self._corners) and len(self._corners) > 1

    def get_sta_corner_lib_paths(self):
        return {c: [lib] for c, lib in (self._corners or {}).items()}

    def get_pdk(self):
        return self._pdk

    def get_max_fanout(self):
        return self._max_fanout


def _make_power_backend(tmp_path, platform=None):
    """An OpenRoadPower over a synthetic netlist, with input and platform resolution
    stubbed.
    """
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
    """A report left by an earlier run is not quoted as this run's, even if OpenROAD exits
    0 without [ERROR].
    """
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
    """`report_power` writes before the script ends, so OpenROAD can exit non-zero with
    `power.rpt` on disk; a FAIL publishes nothing.
    """
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
    """A report with no parseable `Total` row publishes nothing."""
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
    """A missing input without OpenROAD is a config error, not "openroad not found".

    `_write_script` validates the configuration, so it runs before the availability
    check; a failed run also clears the previous report.
    """
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
    """A valid config on a box without OpenROAD keeps an existing report, since that box
    never ran the tool.
    """
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
    """One `report_power -instances` call covers the whole cell list; a call per cell
    reruns propagation each time.
    """
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert "set rb_insts [get_cells -hierarchical *]" in script
    assert (
        f"report_power -instances $rb_insts > {backend._instances_report_path()}"
        in (script)
    )
    # The design-total report is written first, so a failure in the walk cannot cost the
    # headline numbers.
    assert script.index("report_power >") < script.index("report_power -instances")


def test_power_script_wraps_the_walk_in_a_catch(tmp_path):
    """The walk is wrapped in a `catch`, so a Tcl error does not abort the script and its
    exit code.
    """
    backend = _make_power_backend(tmp_path)

    lines = Path(backend._write_script()).read_text().splitlines()
    walk = lines.index("  set rb_insts [get_cells -hierarchical *]")

    assert lines[walk - 1] == "catch {"
    assert "}" in lines[walk:]


def test_power_script_records_the_liberty_cell_of_each_instance(tmp_path):
    """The walk records the Liberty cell of each instance, since `report_power` never
    prints the master.

    The mapping is written under a staging name so a `foreach` that raises part-way
    leaves no half mapping at the published path.
    """
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

    The routed SDC is an artefact of the `rb pnr` run, so `power.yaml` does not name it;
    `_resolve_inputs` locates it and is stubbed as in the synth fixture. ``pnr_run``
    names the upstream `rb pnr` entry, so two backends can differ only in the routed
    database.
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
    """A `pnr` run records the routed SDC it read.

    With no explicit `constraints:`, the analysis reads `<pnr
    artefact>/<top>.routed.sdc`, so the config block alone would fingerprint runs
    against different routed SDCs identically.
    """
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
    """Different routed SDCs give different hashes."""
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
    """A trace rewritten in place gives the run a new identity.

    `rb test` and `rb saif` rewrite `dump.saif` in place, so the path names the trace
    but does not identify it. Two captures of one trace are two measurements.
    """
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
    """The trace is hashed as OpenROAD is launched, not at publish, so a trace re-captured
    mid-run is not identified by its replacement.
    """
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
    """A trace rewritten during the run is recorded as unknown.

    The hash is taken at launch and confirmed when OpenROAD returns. If the trace moved,
    neither hash is trustworthy, so `null` (unknown) is recorded and a warning stops it
    reading as "no trace".
    """
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
    # Neither hash is recorded: not the bytes at the start, not the ones on disk now.
    assert activity["trace_sha256"] is None
    assert sha256_of(trace) is not None
    # The path is still recorded: which file was read is known, which bytes is not.
    assert activity["trace"].endswith("dump.saif")
    assert "trace_changed_during_run" in caplog.text


def test_a_static_run_hashes_no_trace_and_reads_none(tmp_path, monkeypatch):
    """A static run hashes no trace and reads none.

    The fixture names a trace with `mode: static`; the Tcl emits no `read_saif`, and a
    VCD is large enough that reading an ignored one is wasteful.
    """
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
    """The options digest ignores `tool_overrides`, which `power.yaml` accepts but no
    backend reads (`PowerConfig.get_tool_overrides()` has no caller).
    """
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
    """Two power runs over two netlists have different fingerprints.

    `netlist_source` names the kind of upstream, not which one, so entries differing
    only in `synth:`/`synth-path:` must still differ, or `rb phys runs` would list them
    as one experiment.
    """
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
    """Two `netlist-source: pnr` runs over two databases have different fingerprints.

    Such a run records no netlist hash, so the resolved database path distinguishes
    them.
    """
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
    """The upstream identity in the digest is not an absolute path, so the digest does not
    move with the checkout.

    The publish path cannot relativise it because it rewrites paths inside the config
    block after the options mapping is hashed.
    """
    # The marker `project_root_for_dir` walks up for; without a project every path is
    # outside the tree and kept verbatim, which is not the case under test.
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
    # Bound to the netlist this run read, which a later `rb synth` into the same
    # directory tests its own output against.
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
    """The manifest names the netlist behind the provenance hash.

    `power_netlist.v` is kept so the analyzed bytes survive the run; a hash with no path
    beside it leaves an archived result nothing to verify against.
    """
    from rtl_buddy.phys.manifest import POWER_KEYS, load_manifest
    from rtl_buddy.phys.model import load_model

    backend, result = _run_power_with(
        tmp_path, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    manifest = load_manifest(Path(backend.artefact_dir) / "phys-manifest.json")
    assert "netlist_path" in POWER_KEYS
    recorded = manifest["power"]["netlist_path"]
    assert recorded.endswith("power_netlist.v")
    # The named file is the snapshot, and hashing it reproduces the provenance hash.
    snapshot = Path(backend._netlist_snapshot_path())
    assert snapshot.name == Path(recorded).name
    assert (
        load_model(result.results["phys_model"])["provenance"]["power"][
            "netlist_sha256"
        ]
        == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    )


def test_a_post_pnr_run_names_no_netlist_in_the_manifest(tmp_path):
    """A post-pnr run names no netlist in the manifest: the key is present and null."""
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
    """The script reads this run's own copy of the netlist, in its own artefact directory,
    and that copy is what gets hashed.
    """
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert f"read_verilog {backend._netlist_snapshot_path()}" in script
    assert f"read_verilog {tmp_path / 'synth_netlist.v'}" not in script


@pytest.mark.parametrize("swap_at", ["before_openroad", "during_openroad"])
def test_the_recorded_netlist_hash_is_of_the_bytes_openroad_was_given(
    tmp_path, monkeypatch, swap_at
):
    """The recorded netlist hash is of the bytes OpenROAD was given, whenever an upstream
    swap lands.

    A concurrent `rb synth` can rewrite the upstream netlist before the copy or during
    the read. The run measures its own copy and records that file's hash, so the merge
    cannot read a real mismatch as a match.
    """
    from unittest.mock import MagicMock
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.tools import power_openroad

    backend = _make_power_backend(tmp_path)
    netlist = tmp_path / "synth_netlist.v"
    snapshot = Path(backend._netlist_snapshot_path())
    monkeypatch.setattr(power_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(power_openroad, "task_status", lambda *a, **k: nullcontext())

    if swap_at == "before_openroad":
        # The clear runs between the script write and the copy, so a swap here lands in
        # the one gap left after `_write_script` named the copy.
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
        # Unmoved by the swap: the copy is inside this run's own artefact directory.
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
        # The upstream netlist has moved on; the hash still names what was measured, so
        # a later `rb synth` sees the mismatch.
        assert (
            recorded["netlist_sha256"]
            != hashlib.sha256(netlist.read_bytes()).hexdigest()
        )


def test_a_netlist_that_cannot_be_staged_fails_the_run(tmp_path, monkeypatch):
    """A netlist that cannot be staged fails the run.

    The script names the copy, so a failed copy leaves `read_verilog` nothing to read.
    The staging file is removed so a half-written `power_netlist.v` is never readable.
    """
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
    """A netlist rewritten mid-copy is copied again.

    When the upstream synthesis is in another suite the two commands hold different
    artefact-tree locks, so a concurrent `rb synth` can leave a torn copy that the
    sha256 would authenticate. The source is stat'd either side of the copy, and a copy
    that straddled a rewrite is retaken.
    """
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
            # Lands after the pre-copy stat and after the read: the window that makes
            # the staging file a prefix of two netlists.
            netlist.write_text(resynthesised)

    monkeypatch.setattr(power_openroad.shutil, "copyfile", _copy_then_resynthesise)

    assert backend._snapshot_netlist() is None

    snapshot = Path(backend._netlist_snapshot_path())
    assert len(copies) == 2
    assert snapshot.read_text() == resynthesised
    assert backend._netlist_sha256 == hashlib.sha256(resynthesised.encode()).hexdigest()
    assert not Path(str(snapshot) + ".tmp").exists()


def test_a_netlist_that_never_holds_still_fails_the_run(tmp_path, monkeypatch):
    """Retries are bounded: a netlist rewritten in a loop fails the run rather than pinning
    it, since a rerun recovers a refusal but bad watts are undetectable.
    """
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
        # A different length every time, so no retry can stat its way to a matching
        # pair.
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
    """A previous run's netlist copy is cleared after a failed rerun, like the reports
    beside it.
    """
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
    """`netlist-source: pnr` reads a routed database, so there is nothing to copy or hash."""
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
    """A missing per-instance report does not fail the run; the design totals are already
    parsed and reported.
    """
    from rtl_buddy.phys.model import load_model
    from rtl_buddy.runner.power_results import PowerPassResults

    _backend, result = _run_power_with(tmp_path, monkeypatch)

    assert isinstance(result, PowerPassResults)
    model = load_model(result.results["phys_model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] == pytest.approx(28.3)


def test_power_ignores_a_previous_runs_per_instance_report(tmp_path, monkeypatch):
    """A previous run's per-instance report is ignored, like `power.rpt`."""
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
    """A failed rerun withdraws the instances half it published.

    Publication happens only on a pass, so without the withdrawal `phys-model.json`
    would keep the last run's per-instance watts after their report was cleared. The
    synthesis half is left as it was.
    """
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

    # A synthesis into the same artefact directory, as a co-named `rb synth` would have
    # left it.
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
    """The script marks where the by-product begins: after the design-total report and
    before the walk, so the log gate can tell fatal diagnostics from by-product ones.
    """
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    backend = _make_power_backend(tmp_path)

    lines = Path(backend._write_script()).read_text().splitlines()
    marker = lines.index(f'puts "{OpenRoadPower._DETAIL_MARKER}"')
    totals = next(i for i, ln in enumerate(lines) if ln.startswith("report_power >"))
    walk = lines.index("  set rb_insts [get_cells -hierarchical *]")

    assert totals < marker < walk


def test_an_error_in_the_by_product_half_costs_only_that_half(tmp_path, monkeypatch):
    """An OpenSTA without `report_power -instances` prints an `[ERROR ...]` before the
    `catch` swallows it. The totals are on disk, so the run passes and loses its
    `instances` half.
    """
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
    """An `[ERROR ...]` before the marker fails the run and removes the report, since the
    watts cannot be trusted.
    """
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
    """A log without the marker is scanned whole: every `[ERROR ...]` is fatal."""
    from rtl_buddy.runner.power_results import PowerFailResults

    _backend, result = _run_power_with(
        tmp_path,
        monkeypatch,
        log="[ERROR STA-0001] report_power: unknown option -instances\n",
    )

    assert isinstance(result, PowerFailResults)
    assert "1 ERROR(s) in OpenROAD log" in result.results["desc"]


def test_the_per_instance_block_writes_under_staging_names(tmp_path):
    """The per-instance block redirects into staging names, never the published ones.

    Tcl's `>` creates the file before the redirected command runs, so a `report_power
    -instances` that raises part-way would leave a nonempty report at the published
    path.
    """
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
    """The per-instance reports are renamed into place only on success.

    Tcl reaches the two renames only if every earlier command returned. A block that
    raised leaves staging files, which the trailing deletes remove.
    """
    backend = _make_power_backend(tmp_path)
    instances = backend._instances_report_path()
    cells = backend._instances_cells_path()
    instances_tmp = backend._staging_path(instances)
    cells_tmp = backend._staging_path(cells)

    lines = Path(backend._write_script()).read_text().splitlines()

    rename_cells = lines.index(f"    file rename -force {cells_tmp} {cells}")
    rename_insts = lines.index(f"    file rename -force {instances_tmp} {instances}")
    report = lines.index(f"    report_power -instances $rb_insts > {instances_tmp}")
    # Both renames follow the command that can fail, and both are inside the `catch`
    # block.
    assert report < rename_cells < rename_insts
    assert lines.index("}") > rename_insts
    # The staging files a failed block leaves do not outlive the script.
    assert f"catch {{file delete -force {cells_tmp}}}" in lines
    assert f"catch {{file delete -force {instances_tmp}}}" in lines


def test_a_partial_per_instance_report_is_not_published_as_complete(
    tmp_path, monkeypatch
):
    """A partial per-instance report is not published as complete.

    A block that emitted rows and then raised leaves them under the staging name and
    nothing at the published one, which the publish reads as a null half plus the
    existing warning.
    """
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
        # The rows flushed before the Tcl error unwound the block: under the staging
        # name, never renamed.
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
    """A staging report left by a killed OpenROAD does not survive the clear, or the next
    run would rename over its own.
    """
    backend = _make_power_backend(tmp_path)
    orphan = Path(backend._staging_path(backend._instances_report_path()))
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(_INSTANCE_RPT)

    _backend, _result = _run_power_with(tmp_path, monkeypatch)

    assert not orphan.exists()


def test_the_published_top_is_the_one_the_script_was_generated_from(
    tmp_path, monkeypatch
):
    """The published top is the one the script was generated from.

    Resolving again at publish would re-read a synth YAML that may have been edited
    minutes into the run, attributing the watts to another design.
    """
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
        # The referenced synth.yaml is edited while OpenROAD works: the same entry now
        # names another design.
        backend._resolve_inputs = lambda: {**script_inputs, "top": "other_top"}
        return MagicMock(returncode=0)

    monkeypatch.setattr(power_openroad.subprocess, "run", _fake_run)
    result = backend.run()

    model = load_model(result.results["phys_model"])
    assert model["design"]["top"] == "demo_top"


def test_a_referenced_entry_that_vanishes_mid_run_does_not_null_the_top(
    tmp_path, monkeypatch
):
    """A referenced entry that vanishes mid-run does not null the top: a resolution that
    raises at publish must not record no top.
    """
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


def _lock_the_publication(monkeypatch):
    """Make every `_publication_lock` acquisition time out, as a holder that outlives
    `PUBLISH_LOCK_TIMEOUT_SEC` does.
    """
    import contextlib as _contextlib
    from rtl_buddy.phys import publish as publish_mod

    @_contextlib.contextmanager
    def _held(_artefact_dir):
        raise TimeoutError("phys-publish.lock: another publish has held the lock")
        yield  # pragma: no cover - unreachable, keeps this a context manager

    monkeypatch.setattr(publish_mod, "_publication_lock", _held)


def test_a_half_that_cannot_be_withdrawn_stops_the_power_run(tmp_path, monkeypatch):
    """A half that cannot be withdrawn stops the power run.

    The clear has already deleted the per-instance report, so a failed withdrawal would
    leave the previous run's watts discoverable over nothing.
    """
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
    # The rows are still there, which is why the run stopped.
    assert load_model(model_path)["instances"]


def test_a_failed_withdrawal_is_named_in_a_post_openroad_failure(tmp_path, monkeypatch):
    """`_fail_after_openroad` runs the same clear; a failed withdrawal is named in the
    failure so the user learns the artefact directory still publishes rows over the
    deleted report.
    """
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
    # The start-of-run gate fires first; reach the post-OpenROAD path by letting it
    # through and failing the tool instead.
    monkeypatch.setattr(backend, "_clear_stale_report", _one_clean_clear(backend))
    rerun = backend.run()

    assert isinstance(rerun, PowerFailResults)
    assert "exited with code 3" in rerun.results["desc"]
    assert "could not be withdrawn" in rerun.results["desc"]


def _one_clean_clear(backend):
    """`_clear_stale_report` that succeeds once and fails thereafter.

    The start-of-run clear and the post-OpenROAD one are the same method, so the first
    must pass for the second to report a failed withdrawal.
    """
    real = backend._clear_stale_report
    calls = {"n": 0}

    def _clear():
        result = real()
        calls["n"] += 1
        return None if calls["n"] == 1 else result

    return _clear


def test_a_routed_sdc_replaced_mid_run_records_no_constraints_hash(
    tmp_path, monkeypatch, caplog
):
    """A routed SDC replaced mid-run records no constraints hash.

    The hash is taken at launch and confirmed on return, so a `<top>.routed.sdc`
    rewritten by a concurrent `rb pnr` has no identity this run can vouch for.
    """
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
        # A concurrent `rb pnr` re-routing the same entry, landing mid-analysis and
        # rewriting the SDC in place.
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
    # The path is still recorded: which file was read is known, which bytes is not.
    assert config["constraints"].endswith("demo_top.routed.sdc")
    assert "constraints_changed_during_run" in caplog.text


def test_an_unchanged_sdc_is_recorded_by_the_hash_taken_at_launch(
    tmp_path, monkeypatch
):
    """An unchanged SDC is recorded by the hash taken at launch, and the file recorded is
    the one the analysis read.
    """
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


def test_two_power_runs_over_two_libraries_do_not_share_a_fingerprint(
    tmp_path, monkeypatch
):
    """Two power runs over two libraries have different fingerprints.

    `_write_script` emits `read_liberty` and `read_lef` from the resolved platform, so a
    `cfg-pnr-platforms` entry repointed at another corner must not share a fingerprint.
    """
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
    """The fingerprint lists the technology in script order, since `read_liberty` and
    `read_lef` are order-sensitive.
    """
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
    """The macro LEF line is conditional in the script and in the fingerprint; a null entry
    would name a file never read.
    """
    backend = _make_power_backend(tmp_path)
    backend._write_script()

    assert backend._phys_technology() == [
        "/pdk/fake/nangate45_typ.lib",
        "/pdk/fake/tech.lef",
    ]


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
    """A power backend in the layout `phys-run:` is for.

    The synthesis suite is under `synth/demo/` and the power suite under `power/demo/`,
    so their artefact directories cannot coincide by accident. It can be called
    repeatedly on one ``tmp_path``.

    Returns the backend, the synthesis run's artefact directory, and the netlist that
    directory holds, as an `rb synth` into it would have written.
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
    """`phys-run` publishes the model into the synthesis run's directory, where the
    synthesis writes its own half; the raw output stays with the power run.
    """
    backend, synth_artefacts, _netlist = _make_paired_power_backend(tmp_path)

    result = _run_prepared_power(
        backend, monkeypatch, instances=_INSTANCE_RPT, cells=_INSTANCE_CELLS
    )

    assert Path(result.results["phys_model"]).parent == synth_artefacts
    assert (synth_artefacts / "phys-manifest.json").exists()
    own = Path(backend.artefact_dir)
    assert not (own / "phys-model.json").exists()
    assert not (own / "phys-manifest.json").exists()
    # The reports, the log and the netlist copy are this run's own output and are read
    # from its own directory.
    assert (own / "power.rpt").exists()
    assert (own / "power_instances.rpt").exists()
    assert (own / "power_netlist.v").exists()
    # The manifest paths are project-relative, so they join back onto the power run's
    # directory from the synthesis run's.
    manifest_path = synth_artefacts / "phys-manifest.json"
    manifest = load_manifest(str(manifest_path))
    report = manifest["power"]["report"]
    assert not os.path.isabs(report)
    assert Path(resolve(str(manifest_path), report)) == own / "power.rpt"


def test_without_phys_run_the_model_stays_in_the_runs_own_directory(
    tmp_path, monkeypatch
):
    """Without `phys-run:` the model stays in the run's own directory."""
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
    """`phys-run` completes the synthesis half already published there: with the name
    given, one document holds both halves.
    """
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
    """A `phys-run` naming no synthesis entry fails the analysis, rather than publishing a
    merged model into a directory no run owns.
    """
    from rtl_buddy.runner.power_results import PowerFailResults

    backend, _artefacts, _netlist = _make_paired_power_backend(
        tmp_path, phys_run="typo_synth"
    )

    result = _run_prepared_power(backend, monkeypatch, instances=_INSTANCE_RPT)

    assert isinstance(result, PowerFailResults)
    assert "typo_synth" in result.results["desc"]


def test_a_phys_run_outside_the_project_is_refused(tmp_path, monkeypatch):
    """A `phys-run` outside the project is refused, since the merged model would be where
    `rb phys` discovery never walks.
    """
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
    """A paired run warns when the synthesis half read another netlist; without `phys-run:`
    the same mismatch stays quiet.
    """
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
    """The ordinary pair is quiet."""
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
    """A failed paired rerun withdraws its half from the synthesis run's directory and
    leaves the synthesis half alone.
    """
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


# What `_make_power_backend`'s fixture generates with no macro anywhere. The paths are
# the run's own, so the template is filled from the backend under test.
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

    Asserting on rendered text would pass on any wording that carried the event id.
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


def _write_upstream_suites(
    tmp_path, *, pnr_lib=(), lef=(), synth_lib=(), blocks_yaml=""
):
    """A models/synth/pnr trio the power config resolves through for real.

    The inheritance rule is about the referenced run, so tests load that run from YAML.
    ``lef`` goes on both entries, as a real macro's LEF does: P&R places it and
    synthesis links against it.
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
        + blocks_yaml
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
        + blocks_yaml
    )


def _power_suite(tmp_path, body):
    """Load a one-run `power.yaml` written into ``tmp_path``."""
    path = tmp_path / "power.yaml"
    path.write_text(
        "rtl-buddy-filetype: power_config\nruns:\n" + dedent(body).rstrip() + "\n"
    )
    return PowerSuiteConfig(str(path))


def test_power_lib_paths_default_to_nothing(tmp_path):
    """A `power.yaml` with no `lib-paths` declares no macro Liberty."""
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
    """`lib-paths` resolve against the directory holding `power.yaml`, not the process cwd."""
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


def _inputs_with_upstream(
    tmp_path, *, source, pnr_lib=(), lef=(), synth_lib=(), blocks_yaml=""
):
    """`_resolve_inputs()` of a backend whose upstream suites are real."""
    _write_upstream_suites(
        tmp_path,
        pnr_lib=pnr_lib,
        lef=lef,
        synth_lib=synth_lib,
        blocks_yaml=blocks_yaml,
    )
    backend = _make_power_backend(tmp_path)
    del backend._resolve_inputs  # undo the fixture's stub
    cfg = backend.power_cfg
    cfg.netlist_source = source
    cfg.pnr_name = "demo_pnr"
    cfg.pnr_suite_path = str(tmp_path / "pnr.yaml")
    return backend


def test_a_pnr_power_run_inherits_the_pnr_runs_macro_liberty(tmp_path):
    """A `pnr` power run inherits the P&R run's macro Liberty (its `lib-paths`); otherwise
    the macro contributes exactly zero.
    """
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    backend = _inputs_with_upstream(tmp_path, source="pnr", pnr_lib=["sram.lib"])

    inputs = backend._resolve_inputs()

    assert inputs["macro_libs"] == [lib]
    # The ODB carries every master the router placed, so no LEF is read.
    assert inputs["macro_lefs"] == []


def test_a_synth_power_run_inherits_the_synth_runs_macro_liberty_and_lef(tmp_path):
    """A synth power run inherits the synth run's macro Liberty and LEF, since
    `read_verilog` + `link_design` builds the database from LEF masters.
    """
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
    """An explicit `lib-path` is added after the inherited ones: `read_liberty` is
    order-sensitive.
    """
    inherited = _write_macro_liberty(tmp_path / "sram.lib")
    own = _write_macro_liberty(tmp_path / "pll.lib", cell="pll")
    backend = _inputs_with_upstream(tmp_path, source="pnr", pnr_lib=["sram.lib"])
    backend.power_cfg.lib_paths = [own]

    assert backend._resolve_inputs()["macro_libs"] == [inherited, own]


def test_a_library_named_on_both_sides_is_read_once(tmp_path):
    """A library named on both sides is read once, de-duplicated on the resolved path in
    first-named order.
    """
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    other = _write_macro_liberty(tmp_path / "pll.lib", cell="pll")
    backend = _inputs_with_upstream(
        tmp_path, source="pnr", pnr_lib=["sram.lib", "pll.lib"]
    )
    # The same file, reached by a path spelled differently.
    backend.power_cfg.lib_paths = [str(tmp_path / "." / "sram.lib")]

    assert backend._resolve_inputs()["macro_libs"] == [lib, other]


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
    """The macro Liberty is read after the platform corner, so it cannot shadow a standard
    cell.
    """
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
    """A run with no macros generates a byte-identical script, on which the fingerprint
    depends.
    """
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert script == _BASELINE_SCRIPT.format(
        artefacts=backend.artefact_dir, sdc=str(tmp_path / "constraints.sdc")
    )


def test_a_configured_macro_library_that_is_not_on_disk_stops_the_run(
    tmp_path, monkeypatch
):
    """A configured macro library missing from disk stops the run at ERROR before OpenROAD
    launches, instead of a `read_liberty` failure in an unread log.
    """
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
    """The fingerprint lists the macro libraries in script order, so runs that read a
    macro's Liberty and those that do not digest differently.
    """
    lib = _write_macro_liberty(tmp_path / "sram.lib")

    backend, _script = _script_with_macros(tmp_path, libs=[lib])

    assert backend._phys_technology() == [
        "/pdk/fake/nangate45_typ.lib",
        lib,
        "/pdk/fake/tech.lef",
    ]


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
    """A macro with no Liberty is named, not reported as zero.

    The instance is in the report at 0.00e+00 in all four columns, which is
    indistinguishable from a cell that burns nothing.
    """
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
    """An unpowered macro does not change the verdict: the watts for everything with a
    library are a real measurement.
    """
    _backend, res = _run_with_a_macro(tmp_path, monkeypatch)

    assert res.is_pass()
    assert res.results["total_w"] == 2.83e-05


def test_a_macro_whose_liberty_the_run_read_is_not_flagged(tmp_path, monkeypatch):
    """A macro whose Liberty the run read is not flagged."""
    lib = _write_macro_liberty(tmp_path / "sram.lib")
    events = _capture_power_events(monkeypatch)

    _backend, res = _run_with_a_macro(tmp_path, monkeypatch, libs=[lib])

    assert "unpowered_cells" not in res.results
    assert res.results["desc"] == "Power analysis passed"
    assert _fields_of(events, "power.unpowered_instances") == []


def test_an_instance_at_zero_whose_cell_has_a_library_is_not_flagged(
    tmp_path, monkeypatch
):
    """An instance at zero whose cell has a Liberty is not flagged: a standard cell at zero
    has power data.
    """
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
    """An unpowered macro is in the `--machine` row as a field, not only prose."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    _backend, res = _run_with_a_macro(tmp_path, monkeypatch)
    row = RtlBuddy._power_result_row(None, {"power_name": "demo_power", "results": res})

    assert row["unpowered_cells"] == ["sram_32x256"]
    assert row["unpowered_cell_count"] == 1
    assert row["unpowered_instance_count"] == 1


def test_the_published_model_carries_the_macros_own_watts(tmp_path, monkeypatch):
    """The published model carries the macro's own watts.

    The `rb phys` roll-up over `power_instances.rpt` is name-based, so a macro reaches a
    module total when its row has its Liberty cell in the module column.
    """
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
    """The PDK's fill cells are not reported as unpowered.

    `rb pnr` ends with `filler_placement`, so a routed database holds tens of thousands
    of fill instances with no Liberty and no power. The PDK names them, and reporting
    them would bury the macro this check exists to find.
    """
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
    """A launch that dies before OpenROAD truncates `power.log` does not report the
    previous run's ORD-0030 count.
    """
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


def _age(path, seconds):
    """Set a file's mtime `seconds` into the past."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - int(seconds * 1e9)))


def _pnr_artefact_with_spef(
    root, *, extracted=True, spef=True, spef_age=0.0, script_age=60.0
):
    """A P&R artefact dir as `rb pnr` leaves it: script first, SPEF last.

    ``extracted`` says whether the flow script carries a `write_spef` command, meaning
    the run was configured with `rcx-rules`. Ages are seconds into the past.
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
    """A SPEF older than the P&R run is stale.

    The clear list cannot cover the case where an older rtl_buddy reran P&R and rewrote
    the script and ODB, leaving the previous SPEF.
    """
    from rtl_buddy.tools.power_openroad import routed_spef_rejection

    spef, script = _pnr_artefact_with_spef(tmp_path, spef_age=120.0)
    assert "older than the P&R run" in routed_spef_rejection(spef, script)


def test_a_spef_beside_a_run_that_did_not_extract_is_stale(tmp_path):
    """A SPEF beside a run that did not extract is stale, however new: the script never
    wrote it.
    """
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


def test_a_synth_power_run_sets_the_platform_max_fanout_after_read_sdc(tmp_path):
    backend = _make_power_backend(tmp_path, platform=_FakePlatform(max_fanout=100000))

    lines = Path(backend._write_script()).read_text().splitlines()

    read_sdc = next(i for i, ln in enumerate(lines) if ln.startswith("read_sdc "))
    assert lines[read_sdc + 1] == "set_max_fanout 100000 [current_design]"
    assert sum(ln.startswith("set_max_fanout") for ln in lines) == 1


def test_a_pnr_power_run_sets_the_platform_max_fanout_after_read_sdc(tmp_path):
    backend, _routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    backend._resolve_platform = lambda: _FakePlatform(max_fanout=64)

    lines = Path(backend._write_script()).read_text().splitlines()

    read_sdc = next(i for i, ln in enumerate(lines) if ln.startswith("read_sdc "))
    assert lines[read_sdc + 1] == "set_max_fanout 64 [current_design]"
    assert sum(ln.startswith("set_max_fanout") for ln in lines) == 1


def test_a_pnr_power_run_without_a_spef_estimates_as_before(tmp_path):
    """A `pnr` power run without a SPEF estimates parasitics; this is the ODB-only fallback
    (Nangate45 ships no rules).
    """
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
    """A missing SPEF is not a warning: no rules, no SPEF, nothing to warn about."""
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
    """SPEF-timed and estimate-timed runs over one ODB have different fingerprints."""
    from rtl_buddy.phys.model import load_model

    digests = []
    # One directory for both, so the ODB path is the same; only the SPEF beside it comes
    # and goes.
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


_BLOCKS_YAML = (
    "    blocks:\n      - {name: blk_top, pnr: blk_pnr, pnr-path: blk/pnr.yaml}\n"
)


def _publish_block_abstract(tmp_path, *, module="blk_top"):
    """A block pnr.yaml whose `harden: true` run published an abstract.

    Its Liberty is what `write_timing_model` writes: the block as a cell, with timing
    and no power tables.
    """
    suite = tmp_path / "blk" / "pnr.yaml"
    suite.parent.mkdir(parents=True, exist_ok=True)
    suite.write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk_pnr
            desc: block
            synth: s
            synth-path: synth.yaml
            platform: p
            harden: true
        """)
    )
    out = tmp_path / "blk" / "artefacts" / "blk_pnr" / "abstract"
    out.mkdir(parents=True)
    _write_macro_liberty(out / f"{module}.lib", cell=module)
    (out / f"{module}.lef").write_text(f"MACRO {module}\nEND {module}\n")
    (out / f"{module}.gds").write_text("gds\n")
    (out / "abstract.manifest.json").write_text(
        json.dumps({"schema_version": 1, "block": module, "outputs": {}})
    )
    return out


def test_a_pnr_power_run_resolves_blocks_but_reads_no_block_liberty(tmp_path):
    """A `pnr` power run resolves blocks but reads no block Liberty.

    A `write_timing_model` Liberty has no output `function`, so OpenSTA would propagate
    zero activity out of every block output and the logic it drives would read as
    static. The macro set is the P&R run's `lib-paths`, then the power run's.
    """
    sram = _write_macro_liberty(tmp_path / "sram.lib")
    own = _write_macro_liberty(tmp_path / "pll.lib", cell="pll")
    out = _publish_block_abstract(tmp_path)
    backend = _inputs_with_upstream(
        tmp_path, source="pnr", pnr_lib=["sram.lib"], blocks_yaml=_BLOCKS_YAML
    )
    backend.power_cfg.lib_paths = [own]

    inputs = backend._resolve_inputs()

    assert inputs["macro_libs"] == [sram, own]
    assert str(out / "blk_top.lib") not in inputs["macro_libs"]
    assert inputs["macro_lefs"] == []
    assert [b.ref.name for b in backend._blocks] == ["blk_top"]


def test_a_synth_power_run_reads_the_block_lef_and_not_its_liberty(tmp_path):
    """A synth power run reads the block LEF, since `link_design` needs a master for the
    blackbox, but not its Liberty.
    """
    out = _publish_block_abstract(tmp_path)
    backend = _inputs_with_upstream(tmp_path, source="synth", blocks_yaml=_BLOCKS_YAML)

    inputs = backend._resolve_inputs()

    assert inputs["macro_libs"] == []
    assert inputs["macro_lefs"] == [str(out / "blk_top.lef")]


def test_a_block_whose_abstract_is_gone_stops_the_run_naming_it(tmp_path):
    """A block whose abstract is gone stops the run naming it; reading on would report the
    partition at zero watts.
    """
    out = _publish_block_abstract(tmp_path)
    (out / "abstract.manifest.json").unlink()
    backend = _inputs_with_upstream(tmp_path, source="pnr", blocks_yaml=_BLOCKS_YAML)

    with pytest.raises(RuntimeError, match="block 'blk_top'.*no abstract"):
        backend._resolve_inputs()


def test_a_run_with_no_blocks_resolves_none(tmp_path):
    backend = _inputs_with_upstream(tmp_path, source="pnr")

    backend._resolve_inputs()

    assert backend._blocks == []
    assert backend._blocks_fields() == {}


_BLOCK_INSTANCE_RPT = (
    "   Internal  Switching    Leakage      Total\n"
    "      Power      Power      Power      Power (Watts)\n"
    "--------------------------------------------\n"
    "   2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_\n"
    "   0.00e+00   0.00e+00   0.00e+00   0.00e+00 u_blk\n"
)


def _run_with_a_block(tmp_path, monkeypatch):
    """A run whose inputs came from an upstream with one `blocks:` entry."""
    out = _publish_block_abstract(tmp_path)
    block = pnr_abstract.resolve_block(
        BlockRef(
            name="blk_top",
            pnr_run="blk_pnr",
            pnr_suite_path=str(tmp_path / "blk" / "pnr.yaml"),
        )
    )
    backend = _make_power_backend(tmp_path)
    inner = backend._resolve_inputs

    def _inputs():
        backend._blocks = [block]
        return {**inner(), "macro_libs": [block.lib]}

    backend._resolve_inputs = _inputs
    res = _run_prepared_power(
        backend,
        monkeypatch,
        instances=_BLOCK_INSTANCE_RPT,
        cells="u_sub/_64_ DFF_X1\nu_blk blk_top\n",
    )
    return out, res


def test_a_block_at_zero_watts_is_named_although_its_abstract_was_read(
    tmp_path, monkeypatch
):
    """A block at zero watts is named although its abstract was read.

    A `power.yaml` can name the abstract in its own `lib-paths`, but a timing model
    carries no power tables, so the partition is still an instance the analysis said
    nothing about.
    """
    _out, res = _run_with_a_block(tmp_path, monkeypatch)

    assert res.is_pass()
    assert res.results["unpowered_cells"] == ["blk_top"]


def test_the_blocks_the_run_read_are_in_its_result_and_machine_row(
    tmp_path, monkeypatch
):
    """The result and machine row carry the blocks the run read, as `rb pnr` reports them,
    without the staleness this run does not assess.
    """
    from rtl_buddy.rtl_buddy import RtlBuddy

    out, res = _run_with_a_block(tmp_path, monkeypatch)

    [row] = res.results["blocks"]
    assert row["name"] == "blk_top"
    assert row["pnr_run"] == "blk_pnr"
    assert row["abstract_dir"] == str(out)
    assert "stale" not in row and "changes" not in row
    machine = RtlBuddy._power_result_row(
        None, {"power_name": "demo_power", "results": res}
    )
    assert machine["blocks"] == [row]


def _hook(tmp_path, name, text="# hook\n"):
    path = tmp_path / "pdk" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_pnr_power_run_sources_the_pdk_layer_rc_after_the_constraints(tmp_path):
    """Layer RC is session state the ODB does not carry, so the estimate needs the PDK's
    `layer-rc-tcl` again; it follows `read_sdc`, as in `rb pnr`.
    """
    backend, _routed = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    rc = _hook(tmp_path, "set rc.tcl")
    backend._resolve_platform = lambda: _FakePlatform(
        pdk=_FakePdk("/pdk/fake/tech.lef", layer_rc_tcl=str(rc))
    )

    lines = Path(backend._write_script()).read_text().splitlines()

    source = lines.index(f'source "{rc}"')
    read_sdc = next(i for i, ln in enumerate(lines) if ln.startswith("read_sdc "))
    estimate = lines.index("estimate_parasitics -global_routing")
    assert read_sdc < source < estimate


def test_a_synth_power_run_does_not_source_the_layer_rc(tmp_path):
    """Without a routed database there is no parasitics estimate to feed, and a missing
    file there is not an error.
    """
    rc = tmp_path / "pdk" / "missing_setRC.tcl"
    backend = _make_power_backend(
        tmp_path,
        platform=_FakePlatform(
            pdk=_FakePdk("/pdk/fake/tech.lef", layer_rc_tcl=str(rc))
        ),
    )

    assert "source" not in Path(backend._write_script()).read_text()
    assert "layer_rc_tcl_sha256" not in backend._upstream_identity()


@pytest.mark.parametrize("pnr_source", [False, True])
def test_power_sources_the_platform_tcl_before_any_liberty(tmp_path, pnr_source):
    tcl = _hook(tmp_path, "liberty_suppressions.tcl")
    platform = _FakePlatform(pdk=_FakePdk("/pdk/fake/tech.lef", platform_tcl=str(tcl)))
    if pnr_source:
        backend, _ = _make_pnr_power_backend(
            tmp_path, "create_clock -period 3 [get_ports clk]\n"
        )
        backend._resolve_platform = lambda: platform
    else:
        backend = _make_power_backend(tmp_path, platform=platform)

    lines = Path(backend._write_script()).read_text().splitlines()

    source = lines.index(f'source "{tcl}"')
    first_lib = next(i for i, ln in enumerate(lines) if ln.startswith("read_liberty"))
    assert source < first_lib


@pytest.mark.parametrize("key", ["platform-tcl", "layer-rc-tcl"])
def test_power_fails_at_setup_on_a_missing_tcl_hook(tmp_path, monkeypatch, key):
    from rtl_buddy.tools import power_openroad

    backend, _ = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    missing = tmp_path / "pdk" / "missing.tcl"
    field = key.replace("-", "_")
    backend._resolve_platform = lambda: _FakePlatform(
        pdk=_FakePdk("/pdk/fake/tech.lef", **{field: str(missing)})
    )
    events = _capture_power_events(monkeypatch)
    launched = []
    monkeypatch.setattr(
        power_openroad.subprocess, "run", lambda *a, **k: launched.append(a)
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert f"{key} not found: {missing}" in res.results["desc"]
    assert not launched
    (fields,) = _fields_of(events, "power.tcl_hook_missing")
    assert fields["key"] == key
    assert fields["path"] == str(missing)


def test_an_estimating_pnr_power_run_digests_the_layer_rc_contents(tmp_path):
    """Editing `setRC.tcl` changes the estimate, so it is a new experiment."""
    backend, _ = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    rc = _hook(
        tmp_path, "setRC.tcl", "set_wire_rc -signal -resistance 1 -capacitance 1\n"
    )
    backend._resolve_platform = lambda: _FakePlatform(
        pdk=_FakePdk("/pdk/fake/tech.lef", layer_rc_tcl=str(rc))
    )

    backend._write_script()
    first = backend._upstream_identity()["layer_rc_tcl_sha256"]
    rc.write_text("set_wire_rc -signal -resistance 2 -capacitance 2\n")
    backend._write_script()
    second = backend._upstream_identity()["layer_rc_tcl_sha256"]

    assert first and second and first != second


def test_a_pnr_power_run_without_layer_rc_keeps_its_digest_keys(tmp_path):
    backend, _ = _make_pnr_power_backend(
        tmp_path, "create_clock -period 3 [get_ports clk]\n"
    )
    backend._write_script()
    assert "layer_rc_tcl_sha256" not in backend._upstream_identity()


def test_a_spef_timed_power_run_does_not_digest_the_layer_rc(tmp_path):
    """With a trusted SPEF the layer RC never reaches the numbers."""
    backend, _spef = _make_spef_power_backend(tmp_path)
    rc = _hook(tmp_path, "setRC.tcl")
    backend._resolve_platform = lambda: _FakePlatform(
        pdk=_FakePdk("/pdk/fake/tech.lef", layer_rc_tcl=str(rc))
    )

    backend._write_script()

    identity = backend._upstream_identity()
    assert identity["parasitics"] == "spef"
    assert "layer_rc_tcl_sha256" not in identity
