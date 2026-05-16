"""Tests for the power-analysis config schema."""

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
        synth_name="demo_synth",
        synth_suite_path="/tmp/synth.yaml",
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
        synth_name="demo_synth",
        synth_suite_path="/tmp/synth.yaml",
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
