"""Tests for the power-analysis config schema."""

import hashlib
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


def _make_power_backend(tmp_path):
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
    backend._resolve_platform = lambda: MagicMock()
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
    the walk writes the mapping the model's module column needs."""
    backend = _make_power_backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert f"open {backend._instances_cells_path()} w" in script
    assert "get_property $rb_inst ref_name" in script


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


def _make_pnr_power_backend(tmp_path, routed_sdc_text):
    """A `netlist-source: pnr` backend with no explicit `constraints:`.

    Which is the ordinary spelling: the routed SDC is an artefact of the
    `rb pnr` run this reads, so nobody names it in `power.yaml`.
    `_resolve_inputs` is the thing that knows where it is, and it is
    stubbed here exactly as the synth fixture stubs it.
    """
    backend = _make_power_backend(tmp_path)
    backend.power_cfg.netlist_source = "pnr"
    backend.power_cfg.constraints = None
    pnr_artefact = tmp_path / "pnr_artefacts" / "demo_pnr"
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
