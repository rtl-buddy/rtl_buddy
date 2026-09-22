"""Tests for the P&R config schema, OpenRoadPnr backend, and rb pnr wiring."""

import json
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


def _touch(*paths):
    """Materialize configured input files as empty placeholders."""
    for path in paths:
        if not path:
            continue
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


def _make_stream_pdk(tmp_path, **overrides):
    """A PDK whose stream-out inputs all exist on disk.

    `_run_def2stream` refuses to launch KLayout when an input the config
    names is missing (#617), so a test that wants to reach the tool has to
    put those files there.
    """
    base = dict(klayout_tech="pdk/klayout/tech.lyt", cell_gds="pdk/gds/cells.gds")
    base.update(overrides)
    pdk = _make_pdk_cfg(tmp_path, **base)
    _touch(
        pdk.get_klayout_tech(),
        pdk.get_tech_lef(),
        pdk.get_macro_lef(),
        *pdk.get_cell_gds_paths(),
    )
    return pdk


_GDS_BYTES = b"\x00\x06\x00\x02\x00\x07"


def _capture_pnr_events(monkeypatch):
    """Record every `log_event` the P&R backend emits, with its level.

    `caplog` cannot be used past the first console write: `task_status`
    initialises rtl_buddy's own logging, which clears the root handlers
    pytest installed. Recording at the call site tests the same contract —
    which event, at which level, carrying which fields.
    """
    from rtl_buddy.tools import pnr_openroad

    events: list[tuple[int, str, dict]] = []
    real = pnr_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _record)
    return events


def _one_event(events, name):
    """The single recorded event called `name`, as `(level, fields)`."""
    matches = [(level, fields) for level, event, fields in events if event == name]
    assert len(matches) == 1, f"{name}: {[e[1] for e in events]}"
    return matches[0]


def _fake_klayout(
    backend,
    design,
    *,
    missing=(),
    allowed_empty=(),
    orphans=(),
    gds=_GDS_BYTES,
    report=True,
    returncode=None,
):
    """A `subprocess.run` stand-in that does what `def2stream.py` does.

    It writes the GDS, then the JSON report that says which cells came out
    empty, and exits with the helper's error count (#619). `report=False`
    writes none and `report=<str>` writes that text verbatim, which is how
    a helper that died mid-stream and a corrupt report are simulated.
    """
    out_gds = Path(backend.artefact_dir) / f"{design}.gds"
    report_path = Path(backend.artefact_dir) / "def2stream.report.json"

    def _run(_cmd, **_kwargs):
        errors = len(missing) + len(orphans)
        if gds is not None:
            out_gds.write_bytes(gds)
        if report is True:
            report_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "design": design,
                        "out_file": str(out_gds),
                        "complete": not missing,
                        "missing_cells": list(missing),
                        "allowed_empty_cells": list(allowed_empty),
                        "orphan_cells": list(orphans),
                        "other_errors": len(orphans),
                        "errors": errors,
                    }
                )
            )
        elif isinstance(report, str):
            report_path.write_text(report)
        result = MagicMock()
        result.returncode = errors if returncode is None else returncode
        result.stdout = ""
        result.stderr = ""
        return result

    return _run


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
    assert pdk.get_pin_layer_horizontal() == "metal3"
    assert pdk.get_pin_layer_vertical() == "metal2"


def test_pdk_exposes_configured_pin_layers(tmp_path):
    from rtl_buddy.config.pdk import PdkPinLayersFile

    pdk = _make_pdk_cfg(
        tmp_path,
        pin_layers=PdkPinLayersFile(horizontal="met3", vertical="met2"),
    )
    assert pdk.get_pin_layer_horizontal() == "met3"
    assert pdk.get_pin_layer_vertical() == "met2"


# ---------------------------------------------------------------------------
# PdkConfig — placement, don't-use cells, PDN (#101)
# ---------------------------------------------------------------------------


def test_pdk_leaves_the_new_process_keys_unset_by_default(tmp_path):
    """A PDK that names none of them says so, rather than guessing."""
    pdk = _make_pdk_cfg(tmp_path)
    assert pdk.get_placement_density() is None
    assert pdk.get_placement_padding() is None
    assert pdk.get_placement_macro_halo() is None
    assert pdk.get_dont_use_cells() == []
    assert pdk.get_pdn_config() == ""


def test_pdk_exposes_configured_placement(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(density=0.55, padding=2))
    assert pdk.get_placement_density() == 0.55
    assert pdk.get_placement_padding() == 2


def test_pdk_exposes_a_configured_macro_halo(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(macro_halo=20.0))
    assert pdk.get_placement_macro_halo() == 20.0


@pytest.mark.parametrize("halo", [-1.0, -0.005])
def test_pdk_rejects_a_negative_macro_halo(tmp_path, halo):
    from rtl_buddy.config.pdk import PlacementFile

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _make_pdk_cfg(tmp_path, placement=PlacementFile(macro_halo=halo))
    assert "placement.macro-halo" in str(excinfo.value)
    assert "nangate45" in str(excinfo.value)


def test_pdk_accepts_a_macro_halo_of_zero(tmp_path):
    """Zero is a legal, if PDN-hostile, request: macros may abut."""
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(macro_halo=0.0))
    assert pdk.get_placement_macro_halo() == 0.0


def test_pdk_resolves_pdn_config_against_the_root_config(tmp_path):
    pdk = _make_pdk_cfg(tmp_path, pdn_config="pdk/nangate45/pdn.tcl")
    assert pdk.get_pdn_config() == str(tmp_path / "pdk/nangate45/pdn.tcl")


@pytest.mark.parametrize("density", [0.0, -0.5, 1.5])
def test_pdk_rejects_a_placement_density_outside_zero_to_one(tmp_path, density):
    from rtl_buddy.config.pdk import PlacementFile

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _make_pdk_cfg(tmp_path, placement=PlacementFile(density=density))
    assert "placement.density" in str(excinfo.value)
    assert "nangate45" in str(excinfo.value)


@pytest.mark.parametrize("padding", [-1, True])
def test_pdk_rejects_a_placement_padding_that_is_not_a_natural_number(
    tmp_path, padding
):
    from rtl_buddy.config.pdk import PlacementFile

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _make_pdk_cfg(tmp_path, placement=PlacementFile(padding=padding))
    assert "placement.padding" in str(excinfo.value)


def test_pdk_accepts_a_density_of_one_and_a_padding_of_zero(tmp_path):
    """The range is half-open at zero and closed at one; padding may be 0."""
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(density=1.0, padding=0))
    assert pdk.get_placement_density() == 1.0
    assert pdk.get_placement_padding() == 0


def test_pdk_keeps_dont_use_cell_patterns_in_config_order(tmp_path):
    pdk = _make_pdk_cfg(tmp_path, dont_use_cells=["*_X32", "AND2_X1"])
    assert pdk.get_dont_use_cells() == ["*_X32", "AND2_X1"]


def test_new_pdk_and_platform_keys_are_spelled_in_kebab_case(tmp_path):
    """Pin the YAML spellings a `root_config.yaml` has to use."""
    from serde.yaml import from_yaml

    pdk_file = from_yaml(
        PdkConfigFile,
        dedent("""\
            name: "nangate45"
            corners:
              typ: "pdk/lib/typ.lib"
            placement:
              density: 0.55
              padding: 2
              macro-halo: 20
            dont-use-cells: ["AND2_X1", "*_X32"]
            pdn-config: "pdk/nangate45/pdn.tcl"
        """),
    )
    pdk = PdkConfig(pdk_file, str(tmp_path / "root_config.yaml"))
    assert pdk.get_placement_density() == 0.55
    assert pdk.get_placement_padding() == 2
    assert pdk.get_placement_macro_halo() == 20.0
    assert pdk.get_dont_use_cells() == ["AND2_X1", "*_X32"]
    assert pdk.get_pdn_config() == str(tmp_path / "pdk/nangate45/pdn.tcl")

    platform_file = from_yaml(
        PnrPlatformConfigFile,
        dedent("""\
            name: "nangate45_typ"
            pdk: "nangate45"
            cts-buffer: ["BUF_X4", "BUF_X8"]
            placement:
              density: 0.62
              macro-halo: 30
        """),
    )
    platform = PnrPlatformConfig(platform_file, lambda _name: pdk)
    assert platform.get_cts_buffers() == ["BUF_X4", "BUF_X8"]
    assert platform.get_placement_density() == 0.62
    assert platform.get_placement_padding() == 2
    assert platform.get_placement_macro_halo() == 30.0


@pytest.mark.parametrize("cell", ["", "   ", "AND2_X1 OR2_X1"])
def test_pdk_rejects_an_unusable_dont_use_entry(tmp_path, cell):
    """One pattern per entry: a whitespace-carrying entry would silently
    become two arguments on a Yosys command line."""
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _make_pdk_cfg(tmp_path, dont_use_cells=[cell])
    assert "dont-use-cells" in str(excinfo.value)


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


def _platform(pdk, **overrides):
    base = dict(name="nangate45_typ", pdk="nangate45")
    base.update(overrides)
    return PnrPlatformConfig(PnrPlatformConfigFile(**base), lambda _name: pdk)


def test_pnr_platform_placement_falls_back_to_the_flow_defaults(tmp_path):
    """Neither block says anything: the values the flow always emitted."""
    cfg = _platform(_make_pdk_cfg(tmp_path))
    assert cfg.get_placement_density() == 0.7
    assert cfg.get_placement_padding() == 1
    assert cfg.get_placement_macro_halo() == 20.0


def test_pnr_platform_placement_takes_the_pdk_values(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(density=0.55, padding=2))
    cfg = _platform(pdk)
    assert cfg.get_placement_density() == 0.55
    assert cfg.get_placement_padding() == 2


def test_pnr_platform_placement_overrides_the_pdk_field_by_field(tmp_path):
    """The platform wins where it says something, the PDK where it does not."""
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(
        tmp_path, placement=PlacementFile(density=0.55, padding=2, macro_halo=20.0)
    )
    cfg = _platform(pdk, placement=PlacementFile(density=0.6))
    assert cfg.get_placement_density() == 0.6
    assert cfg.get_placement_padding() == 2
    assert cfg.get_placement_macro_halo() == 20.0


def test_pnr_platform_rejects_its_own_out_of_range_placement(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        _platform(_make_pdk_cfg(tmp_path), placement=PlacementFile(density=1.2))
    assert "pnr platform 'nangate45_typ'" in str(excinfo.value)
    assert "placement.density" in str(excinfo.value)


def test_pnr_platform_cts_buffer_takes_a_name_or_a_list(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    single = _platform(pdk, cts_buffer="BUF_X4")
    assert single.get_cts_buffer() == "BUF_X4"
    assert single.get_cts_buffers() == ["BUF_X4"]

    listed = _platform(pdk, cts_buffer=["BUF_X4", "BUF_X8", "BUF_X16"])
    # The root buffer is the first entry; the buffer list is all of them.
    assert listed.get_cts_buffer() == "BUF_X4"
    assert listed.get_cts_buffers() == ["BUF_X4", "BUF_X8", "BUF_X16"]

    unset = _platform(pdk)
    assert unset.get_cts_buffer() == ""
    assert unset.get_cts_buffers() == []


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


def _make_pnr_cfg(tmp_path, **overrides):
    from rtl_buddy.config.pnr import PnrFloorplan

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
    )
    base.update(overrides)
    return PnrConfig(**base)


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
    assert "-sink_clustering_enable" in text
    assert "set PIN_LAYER_H     metal3" in text
    assert "set PIN_LAYER_V     metal2" in text
    assert "place_pins -hor_layers $PIN_LAYER_H -ver_layers $PIN_LAYER_V" in text
    # No leftover placeholders
    assert "{{" not in text
    assert "}}" not in text


def test_openroad_pnr_can_disable_cts_sink_clustering(tmp_path):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pdk = _make_pdk_cfg(tmp_path)
    platform = PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            cts_buffer="BUF_X4",
            cts_sink_clustering=False,
        ),
        lambda _name: pdk,
    )
    pnr_cfg = _make_pnr_cfg(tmp_path)
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

    text = Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()

    assert "-sink_clustering_enable" not in text
    assert "{{" not in text


# ---------------------------------------------------------------------------
# Tcl rendering — placement, PDN, don't-use, CTS buffer list (#101)
# ---------------------------------------------------------------------------


def _render_flow(tmp_path, platform, suite_dir=None, **overrides):
    """The `pnr.tcl` this platform renders, as text."""
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pnr_cfg = _make_pnr_cfg(tmp_path, **overrides)
    resolved_synth = MagicMock()
    resolved_synth.get_top.return_value = "demo_top"
    resolved_synth.get_name.return_value = "demo_synth"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=resolved_synth)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=pnr_cfg,
        suite_dir=str(suite_dir or tmp_path),
        root_cfg=MagicMock(),
    )
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


def test_pin_constraints_resolve_against_suite(tmp_path):
    from rtl_buddy.config.pnr import PnrConfigFile

    cfg = PnrConfigFile(
        name="pins",
        desc="pins",
        synth="demo",
        synth_path="synth.yaml",
        platform="pdk",
        pin_constraints="pins/plan.tcl",
    ).initialise(str(tmp_path))
    assert cfg.pin_constraints == str(tmp_path / "pins/plan.tcl")


def test_pin_constraints_follow_floorplan_and_precede_placement(tmp_path):
    path = tmp_path / 'pins $[x] "test".tcl'
    path.write_text("# constraints\n")
    pdk = _make_pdk_cfg(tmp_path)
    platform = PnrPlatformConfig(
        PnrPlatformConfigFile(name="p", pdk="p"), lambda _: pdk
    )
    text = _render_flow(tmp_path, platform, pin_constraints=str(path))
    source = text.index('source "')
    assert text.index("make_tracks") < source < text.index("place_pins -hor_layers")
    assert r"\$\[x\] \"test\".tcl" in text


def test_pin_constraints_missing_file_fails(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    platform = PnrPlatformConfig(
        PnrPlatformConfigFile(name="p", pdk="p"), lambda _: pdk
    )
    with pytest.raises(RuntimeError, match="pin-constraints file does not exist"):
        _render_flow(tmp_path, platform, pin_constraints=str(tmp_path / "missing.tcl"))


def test_pnr_flow_is_unchanged_when_no_new_key_is_set(tmp_path):
    """Back-compat pin: a config that sets none of the #101 keys renders the
    Tcl the flow rendered before they existed — same placement line, same
    CTS invocation, and no trace of the conditional stages."""
    text = _render_flow(
        tmp_path, _platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    )

    assert "global_placement -density 0.7 -pad_left 1 -pad_right 1\n" in text
    assert "set CTS_BUF         BUF_X4\n" in text
    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf $CTS_BUF \\\n"
        "    -buf_list $CTS_BUF \\\n"
        "    -sink_clustering_enable\n"
    ) in text
    assert 'read_sdc $SDC_FILE\n\nputs ">>> Initializing floorplan"' in text
    assert "pdngen" not in text
    assert "set_dont_use" not in text
    assert "{{" not in text


def test_pnr_flow_substitutes_placement_density_and_padding(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(density=0.55, padding=2))
    text = _render_flow(tmp_path, _platform(pdk, cts_buffer="BUF_X4"))

    assert "global_placement -density 0.55 -pad_left 2 -pad_right 2\n" in text


def test_pnr_flow_renders_the_default_macro_halo(tmp_path):
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))

    assert "set MACRO_HALO      20\n" in text


def test_pnr_flow_renders_a_configured_macro_halo(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(macro_halo=27.5))
    text = _render_flow(tmp_path, _platform(pdk))

    assert "set MACRO_HALO      27.5\n" in text


def test_pnr_flow_embeds_the_macro_packer(tmp_path):
    """The packer file is substituted verbatim, so pnr.tcl is self-contained
    and the run does not depend on a second file surviving a dispatch."""
    from importlib.resources import files

    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    packer = files("rtl_buddy.pnr").joinpath("macro_pack.tcl").read_text()

    assert packer.strip() in text
    assert "rb::macro_pack::solve" in text
    assert "{{" not in text


def test_pnr_flow_placement_honours_the_platform_override(tmp_path):
    from rtl_buddy.config.pdk import PlacementFile

    pdk = _make_pdk_cfg(tmp_path, placement=PlacementFile(density=0.55, padding=2))
    platform = _platform(
        pdk, cts_buffer="BUF_X4", placement=PlacementFile(density=0.62)
    )
    text = _render_flow(tmp_path, platform)

    assert "global_placement -density 0.62 -pad_left 2 -pad_right 2\n" in text


def test_pnr_flow_emits_dont_use_before_any_optimisation(tmp_path):
    """`set_dont_use` has to land before the first pass that may pick a
    cell — placement, repair, CTS — so the exclusions actually hold."""
    pdk = _make_pdk_cfg(tmp_path, dont_use_cells=["AND2_X1", "*_X32"])
    text = _render_flow(tmp_path, _platform(pdk, cts_buffer="BUF_X4"))

    assert "set_dont_use [list AND2_X1 *_X32]\n" in text
    dont_use_at = text.index("set_dont_use")
    for later in ("global_placement", "repair_design", "clock_tree_synthesis"):
        assert dont_use_at < text.index(later)


def test_pnr_flow_sources_the_pdn_snippet_and_runs_pdngen(tmp_path):
    """ORFS convention: the snippet declares the grid, the flow calls
    `pdngen`, after macro placement and before global placement."""
    pdk = _make_pdk_cfg(tmp_path, pdn_config="pdk/pdn.tcl")
    text = _render_flow(tmp_path, _platform(pdk, cts_buffer="BUF_X4"))

    assert f"source {tmp_path / 'pdk/pdn.tcl'}\npdngen\n" in text
    assert text.index("Macro placement") < text.index("source ")
    assert text.index("pdngen") < text.index("global_placement")
    assert text.index("pdngen") < text.index("global_route")


def test_pnr_flow_renders_a_cts_buffer_list(tmp_path):
    """Every entry reaches `-buf_list`; the first is the root buffer."""
    platform = _platform(
        _make_pdk_cfg(tmp_path), cts_buffer=["BUF_X4", "BUF_X8", "BUF_X16"]
    )
    text = _render_flow(tmp_path, platform)

    assert "set CTS_BUF         {BUF_X4 BUF_X8 BUF_X16}\n" in text
    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf [lindex $CTS_BUF 0] \\\n"
        "    -buf_list $CTS_BUF \\\n"
    ) in text


def test_pnr_flow_renders_a_one_entry_list_exactly_like_a_name(tmp_path):
    """`cts-buffer: [BUF_X4]` and `cts-buffer: BUF_X4` are one config."""
    pdk = _make_pdk_cfg(tmp_path)
    as_name = _render_flow(
        tmp_path, _platform(pdk, cts_buffer="BUF_X4"), suite_dir=tmp_path / "a"
    )
    as_list = _render_flow(
        tmp_path, _platform(pdk, cts_buffer=["BUF_X4"]), suite_dir=tmp_path / "a"
    )
    assert as_name == as_list


def test_pnr_run_rejects_a_missing_pdn_config_before_launching_openroad(
    tmp_path, monkeypatch
):
    """A snippet the config names and the disk does not have is a setup
    failure, not a Tcl `source` error minutes into the run."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    launched = []
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", lambda *a, **kw: launched.append(a)
    )

    pdk = _make_pdk_cfg(tmp_path, pdn_config="pdk/nangate45/pdn.tcl")
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk, cts_buffer="BUF_X4")
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    events = _capture_pnr_events(monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "setup"
    assert "pdn-config not found" in res.results["desc"]
    assert not launched
    assert not Path(backend._script_path()).exists()
    _one_event(events, "pnr.pdn_config_missing")


def test_pnr_run_accepts_a_pdn_config_that_is_on_disk(tmp_path, monkeypatch):
    """The same run, with the snippet present, reaches OpenROAD."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

    pdk = _make_pdk_cfg(tmp_path, pdn_config="pdk/nangate45/pdn.tcl")
    _touch(pdk.get_pdn_config())
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk, cts_buffer="BUF_X4")
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )

    launched = []

    def _fake_run(cmd, **_kwargs):
        launched.append(cmd)
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    backend.run()

    assert launched


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


def test_def2stream_preview_keeps_an_incomplete_gds_and_names_the_cells(
    tmp_path, monkeypatch
):
    """KLayout's def2stream exits non-zero when a macro has no GDS body, but
    the streamout still produces a valid GDS with the macro as an empty
    placeholder. `preview` keeps that GDS (so `--png` can still render it)
    and reports which cells have no layout — a picture with a hole in it is
    still useful, as long as nobody is told it is complete (#619)."""
    import logging

    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path)
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
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, design, missing=["sram_32x64"]),
    )
    events = _capture_pnr_events(monkeypatch)

    export = backend._run_def2stream(platform, design)

    assert export.status == "incomplete"
    assert export.gds_path == str(out_gds), (
        "preview keeps a non-empty GDS even on non-zero exit"
    )
    assert out_gds.exists() and out_gds.stat().st_size > 0
    assert export.missing_cells == ["sram_32x64"]
    assert "sram_32x64" in export.desc

    level, fields = _one_event(events, "pnr.gds_incomplete")
    assert level == logging.WARNING, "preview reports, it does not fail"
    assert fields["cells"] == ["sram_32x64"]
    assert fields["count"] == 1


def test_def2stream_treats_empty_gds_as_failure(tmp_path, monkeypatch):
    """If KLayout fails before producing any GDS bytes the export is a
    failure, so the downstream PNG render is skipped."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path)
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        # No GDS and no report written, exit non-zero.
        _fake_klayout(backend, "demo_top", gds=None, report=False, returncode=1),
    )

    export = backend._run_def2stream(platform, "demo_top")
    assert export.status == "failed"
    assert export.gds_path is None


def test_def2stream_ignores_a_previous_runs_gds(tmp_path, monkeypatch):
    """A failed streamout must not return the GDS an earlier run left behind
    — it would then be rendered and reported as this run's layout (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path)
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
    stale_gds.write_bytes(_GDS_BYTES)

    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        # KLayout dies before writing anything.
        _fake_klayout(backend, "demo_top", gds=None, report=False, returncode=1),
    )

    export = backend._run_def2stream(platform, "demo_top")
    assert export.status == "failed"
    assert export.gds_path is None
    assert not stale_gds.exists()


# ---------------------------------------------------------------------------
# Stream-out inputs: multi-GDS and the macro LEF handoff (#617)
# ---------------------------------------------------------------------------


def test_pdk_cell_gds_accepts_a_single_path(tmp_path):
    """The key took one string before the list form and still does."""
    pdk = _make_pdk_cfg(tmp_path, cell_gds="pdk/gds/cells.gds")
    assert pdk.get_cell_gds() == str(tmp_path / "pdk" / "gds" / "cells.gds")
    assert pdk.get_cell_gds_paths() == [str(tmp_path / "pdk" / "gds" / "cells.gds")]


def test_pdk_cell_gds_unset_is_empty(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    assert pdk.get_cell_gds() == ""
    assert pdk.get_cell_gds_paths() == []


def test_pdk_cell_gds_accepts_a_list_resolved_entry_by_entry(tmp_path):
    pdk = _make_pdk_cfg(
        tmp_path,
        cell_gds=["pdk/gds/cells.gds", "../shared/sram.gds"],
    )
    assert pdk.get_cell_gds_paths() == [
        str(tmp_path / "pdk" / "gds" / "cells.gds"),
        str(tmp_path.parent / "shared" / "sram.gds"),
    ]
    # The single-valued getter still answers, with the first entry.
    assert pdk.get_cell_gds() == str(tmp_path / "pdk" / "gds" / "cells.gds")


def test_pdk_cell_gds_path_with_spaces_is_one_path(tmp_path):
    pdk = _make_pdk_cfg(tmp_path, cell_gds=["pdk/gds lib/std cells.gds"])
    assert pdk.get_cell_gds_paths() == [
        str(tmp_path / "pdk" / "gds lib" / "std cells.gds")
    ]


def test_pdk_cell_gds_from_yaml(tmp_path):
    """Both spellings have to survive deserialization, not just the ctor."""
    from serde.yaml import from_yaml

    one = from_yaml(PdkConfigFile, "name: sky130hd\ncell-gds: pdk/cells.gds\n")
    many = from_yaml(
        PdkConfigFile,
        "name: sky130hd\ncell-gds: [pdk/cells.gds, pdk/sram.gds]\n",
    )
    root = str(tmp_path / "root_config.yaml")
    assert PdkConfig(one, root).get_cell_gds_paths() == [
        str(tmp_path / "pdk/cells.gds")
    ]
    assert PdkConfig(many, root).get_cell_gds_paths() == [
        str(tmp_path / "pdk/cells.gds"),
        str(tmp_path / "pdk/sram.gds"),
    ]


def test_pnr_run_gds_paths_resolve_against_the_pnr_yaml(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(
        dedent("""\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: "demo_pnr"
                desc: "Demo run"
                synth: "demo_synth"
                synth-path: "../synth/synth.yaml"
                platform: "nangate45_typ"
                lef-paths: ["../../pdk/sram/sram.lef"]
                gds-paths: ["../../pdk/sram/sram.gds", "macros/an odd name.gds"]
        """)
    )
    run = PnrSuiteConfig(str(pnr_yaml)).get_runs("demo_pnr")[0]
    root = tmp_path.parent.parent
    assert run.get_lef_paths() == [str(root / "pdk" / "sram" / "sram.lef")]
    assert run.get_gds_paths() == [
        str(root / "pdk" / "sram" / "sram.gds"),
        str(tmp_path / "macros" / "an odd name.gds"),
    ]


def test_pnr_run_without_gds_paths_has_none(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(_PNR_YAML)
    assert PnrSuiteConfig(str(pnr_yaml)).get_runs("demo_pnr")[0].get_gds_paths() == []


def test_def2stream_inputs_are_ordered_and_deduplicated(tmp_path):
    """Standard cells then macros; tech LEF, PDK macro LEF, then run LEFs.

    The reader order is OpenROAD's own, and a macro the PDK and the run both
    name is one input — handing it twice re-registers every master in it.
    """
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pdk = _make_stream_pdk(tmp_path, cell_gds=["pdk/gds/cells.gds", "pdk/gds/fill.gds"])
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    sram_lef = str(tmp_path / "pdk" / "sram" / "sram.lef")
    sram_gds = str(tmp_path / "pdk" / "sram" / "sram.gds")
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(
            tmp_path,
            # The PDK's macro LEF repeated on purpose, and one of its GDS.
            lef_paths=[sram_lef, pdk.get_macro_lef()],
            gds_paths=[sram_gds, str(tmp_path / "pdk" / "gds" / "fill.gds")],
        ),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )
    _touch(sram_lef, sram_gds)

    inputs = backend.gather_def2stream_inputs(platform)
    assert inputs.tech == pdk.get_klayout_tech()
    assert inputs.gds == [
        str(tmp_path / "pdk" / "gds" / "cells.gds"),
        str(tmp_path / "pdk" / "gds" / "fill.gds"),
        sram_gds,
    ]
    assert inputs.lef == [
        pdk.get_tech_lef(),
        pdk.get_macro_lef(),
        sram_lef,
    ]
    assert inputs.missing == []


def test_def2stream_reports_every_missing_input_and_skips_klayout(
    tmp_path, monkeypatch, caplog
):
    """A configured input that is not on disk stops the export up front.

    KLayout would otherwise stream a GDS with the unresolvable masters left
    empty, which is a layout that looks produced (#617)."""
    import logging

    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path)
    Path(pdk.get_cell_gds()).unlink()
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    missing_lef = str(tmp_path / "pdk" / "sram" / "sram.lef")
    missing_gds = str(tmp_path / "pdk" / "sram" / "sram.gds")
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(
            tmp_path, lef_paths=[missing_lef], gds_paths=[missing_gds]
        ),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    def _must_not_run(cmd, **_kwargs):
        raise AssertionError(f"KLayout was launched with a missing input: {cmd}")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _must_not_run)

    with caplog.at_level(logging.ERROR):
        assert backend._run_def2stream(platform, "demo_top").status == "failed"

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.gds_missing_inputs"
    )
    assert record.levelno == logging.ERROR
    assert record.rtl_fields["missing"] == [
        pdk.get_cell_gds(),
        missing_gds,
        missing_lef,
    ]
    assert record.rtl_fields["count"] == 3


def test_def2stream_hands_klayout_a_json_manifest_that_survives_spaces(
    tmp_path, monkeypatch
):
    """Paths reach the helper as a list, not a whitespace-joined string."""
    from rtl_buddy.pnr.klayout.def2stream import load_inputs
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path, cell_gds=["pdk/gds lib/std cells.gds"])
    platform = MagicMock()
    platform.get_pdk.return_value = pdk

    sram_lef = str(tmp_path / "macro dir" / "sram macro.lef")
    sram_gds = str(tmp_path / "macro dir" / "sram macro.gds")
    _touch(sram_lef, sram_gds)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path, lef_paths=[sram_lef], gds_paths=[sram_gds]),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )

    out_gds = Path(backend.artefact_dir) / "demo_top.gds"
    seen = {}
    streamout = _fake_klayout(backend, "demo_top")

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return streamout(cmd, **kwargs)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    assert backend._run_def2stream(platform, "demo_top").gds_path == str(out_gds)

    manifest = next(
        arg.split("=", 1)[1] for arg in seen["cmd"] if arg.startswith("inputs_json=")
    )
    assert not any(arg.startswith("in_files=") for arg in seen["cmd"])
    inputs = load_inputs(manifest)
    assert inputs["gds"] == [
        str(tmp_path / "pdk" / "gds lib" / "std cells.gds"),
        sram_gds,
    ]
    assert inputs["lef"] == [pdk.get_tech_lef(), pdk.get_macro_lef(), sram_lef]


def test_merge_lef_files_appends_after_the_technologys_own(tmp_path):
    """The `.lyt`'s LEF list is kept, and its entries are not re-added.

    Replacing it would strip the masters the existing flow relies on; a
    relative entry in it resolves against the `.lyt`'s directory, which is
    where KLayout reads it from."""
    from rtl_buddy.pnr.klayout.def2stream import merge_lef_files

    tech_file = str(tmp_path / "pdk" / "tech.lyt")
    _touch(tech_file, str(tmp_path / "pdk" / "lef" / "tech.lef"))

    merged = merge_lef_files(
        ["lef/tech.lef"],
        [
            str(tmp_path / "pdk" / "lef" / "tech.lef"),
            str(tmp_path / "macro dir" / "sram macro.lef"),
        ],
        tech_file,
    )
    assert merged == [
        "lef/tech.lef",
        str(tmp_path / "macro dir" / "sram macro.lef"),
    ]


def test_merge_gds_reads_every_input_and_extends_the_lef_list(tmp_path):
    """The helper takes a GDS *list* and hands the extra LEFs to the reader."""
    from rtl_buddy.pnr.klayout import def2stream

    read: list[str] = []

    class _FakeCell:
        def __init__(self, name):
            self.name = name

        def cell_index(self):
            return 0

        def clear(self):
            pass

        def is_empty(self):
            return False

        def parent_cells(self):
            return 0

        def copy_tree(self, _other):
            pass

    class _FakeLayout:
        dbu = 0.001

        def __init__(self):
            self.written = None

        def each_cell(self):
            return iter([_FakeCell("demo_top")])

        def read(self, path, _options=None):
            read.append(path)

        def cell(self, _name):
            return _FakeCell("demo_top")

        def create_cell(self, name):
            return _FakeCell(name)

        def top_cells(self):
            return [_FakeCell("demo_top")]

        def write(self, path):
            self.written = path

    lefdef = MagicMock()
    lefdef.lef_files = ["lef/tech.lef"]
    options = MagicMock()
    options.lefdef_config = lefdef
    tech = MagicMock()
    tech.load_layout_options = options
    pya_mod = MagicMock()
    pya_mod.Technology.return_value = tech
    pya_mod.Layout.side_effect = [_FakeLayout(), _FakeLayout()]

    tech_file = str(tmp_path / "pdk" / "tech.lyt")
    errors = def2stream.merge_gds(
        pya_mod=pya_mod,
        tech_file=tech_file,
        layer_map="",
        in_def=str(tmp_path / "demo top.def"),
        design_name="demo_top",
        in_files=[str(tmp_path / "std cells.gds"), str(tmp_path / "sram macro.gds")],
        seal_file="",
        out_file=str(tmp_path / "demo_top.gds"),
        lef_files=[str(tmp_path / "sram macro.lef")],
    )
    assert errors == 0
    assert read == [
        str(tmp_path / "demo top.def"),
        str(tmp_path / "std cells.gds"),
        str(tmp_path / "sram macro.gds"),
    ]
    assert lefdef.lef_files == [
        "lef/tech.lef",
        str(tmp_path / "sram macro.lef"),
    ]


# ---------------------------------------------------------------------------
# Stream-out completeness: strict vs preview (#619)
# ---------------------------------------------------------------------------


def test_classify_empty_cells_splits_the_intentional_from_the_missing():
    """`gds-allow-empty` takes names or globs; the legacy regex still counts.

    A cell the run declared abstract is not a shortfall — it is layout the
    design says it does not have. Everything else is a cell whose GDS the
    stream-out could not find, which is what makes an export incomplete."""
    from rtl_buddy.pnr.klayout.def2stream import classify_empty_cells

    allowed, missing = classify_empty_cells(
        ["fakeram45_64x32", "fakeram45_256x8", "sram_32x64", "SRAM_32x64"],
        ["fakeram45_*", "sram_32x64"],
    )
    assert allowed == ["fakeram45_64x32", "fakeram45_256x8", "sram_32x64"]
    # Matched case-sensitively: GDS cell names are.
    assert missing == ["SRAM_32x64"]

    allowed, missing = classify_empty_cells(
        ["fakeram45_64x32", "sram_32x64"], (), "fakeram45_.*"
    )
    assert (allowed, missing) == (["fakeram45_64x32"], ["sram_32x64"])


def _fake_pya_for(empty_cells, design_name="demo_top"):
    """A `pya` stand-in whose final layout holds `empty_cells` as empty."""

    class _FakeCell:
        def __init__(self, name, empty=False):
            self.name = name
            self._empty = empty

        def cell_index(self):
            return 0

        def clear(self):
            pass

        def is_empty(self):
            return self._empty

        def parent_cells(self):
            return 1

        def copy_tree(self, _other):
            pass

    class _FakeLayout:
        dbu = 0.001

        def __init__(self):
            self.written = None

        def each_cell(self):
            return iter(
                [_FakeCell(design_name)]
                + [_FakeCell(name, empty=True) for name in empty_cells]
            )

        def read(self, _path, _options=None):
            pass

        def cell(self, name):
            return _FakeCell(name)

        def create_cell(self, name):
            return _FakeCell(name)

        def top_cells(self):
            return [_FakeCell(design_name)]

        def write(self, path):
            self.written = path

    lefdef = MagicMock()
    lefdef.lef_files = []
    options = MagicMock()
    options.lefdef_config = lefdef
    tech = MagicMock()
    tech.load_layout_options = options
    pya_mod = MagicMock()
    pya_mod.Technology.return_value = tech
    pya_mod.Layout.side_effect = [_FakeLayout(), _FakeLayout()]
    return pya_mod


def test_merge_gds_reports_missing_and_allowed_empty_cells(tmp_path):
    """The helper writes its verdict as JSON rather than leaving the caller
    to scrape cell names out of KLayout's stdout (#619)."""
    from rtl_buddy.pnr.klayout import def2stream

    report_file = tmp_path / "def2stream.report.json"
    errors = def2stream.merge_gds(
        pya_mod=_fake_pya_for(["fakeram45_64x32", "sram_32x64"]),
        tech_file=str(tmp_path / "tech.lyt"),
        layer_map="",
        in_def=str(tmp_path / "demo_top.def"),
        design_name="demo_top",
        in_files=[],
        seal_file="",
        out_file=str(tmp_path / "demo_top.gds"),
        allow_empty_patterns=["fakeram45_*"],
        report_file=str(report_file),
    )

    # One error for the cell with no layout; the declared one is not an error.
    assert errors == 1
    report = json.loads(report_file.read_text())
    assert report["schema"] == def2stream.REPORT_SCHEMA
    assert report["complete"] is False
    assert report["missing_cells"] == ["sram_32x64"]
    assert report["allowed_empty_cells"] == ["fakeram45_64x32"]
    assert report["other_errors"] == 0
    assert report["errors"] == 1


def test_merge_gds_reports_a_complete_export(tmp_path):
    from rtl_buddy.pnr.klayout import def2stream

    report_file = tmp_path / "def2stream.report.json"
    errors = def2stream.merge_gds(
        pya_mod=_fake_pya_for([]),
        tech_file=str(tmp_path / "tech.lyt"),
        layer_map="",
        in_def=str(tmp_path / "demo_top.def"),
        design_name="demo_top",
        in_files=[],
        seal_file="",
        out_file=str(tmp_path / "demo_top.gds"),
        report_file=str(report_file),
    )

    assert errors == 0
    report = json.loads(report_file.read_text())
    assert report["complete"] is True
    assert report["missing_cells"] == []


def _stream_backend(tmp_path, monkeypatch, *, run_overrides=None, **backend_kwargs):
    """An `OpenRoadPnr` whose stream-out inputs are all on disk."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")
    pdk = _make_stream_pdk(tmp_path, klayout_props="pdk/klayout/props.lyp")
    platform = MagicMock()
    platform.get_pdk.return_value = pdk
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path, **(run_overrides or {})),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
        **backend_kwargs,
    )
    return backend, platform


def test_export_of_a_complete_gds_is_unqualified(tmp_path, monkeypatch):
    """Every cell has layout: nothing to qualify, in either mode."""
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top")
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "complete"
    assert export.delivered is True
    assert export.desc == ""
    assert export.missing_cells == []
    assert export.result_fields()["gds_status"] == "complete"


def test_export_reports_an_allow_listed_macro_as_intentionally_empty(
    tmp_path, monkeypatch
):
    """A macro the run declares abstract is complete-as-intended.

    The declaration travels in the input manifest, not in an environment
    variable the helper reads behind the caller's back (#619)."""
    from rtl_buddy.pnr.klayout.def2stream import load_inputs
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(
        tmp_path, monkeypatch, run_overrides={"gds_allow_empty": ["fakeram45_*"]}
    )
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", allowed_empty=["fakeram45_64x32"]),
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "complete"
    assert export.delivered is True
    assert export.allowed_empty_cells == ["fakeram45_64x32"]
    fields = export.result_fields()
    assert fields["gds_allowed_empty_cells"] == ["fakeram45_64x32"]
    assert fields["gds_missing_cell_count"] is None

    manifest = load_inputs(os.path.join(backend.artefact_dir, "def2stream.inputs.json"))
    assert manifest["allow_empty"] == ["fakeram45_*"]
    assert manifest["report"] == os.path.join(
        backend.artefact_dir, "def2stream.report.json"
    )


def test_strict_export_of_a_missing_macro_publishes_nothing(tmp_path, monkeypatch):
    """A design that simply forgot its SRAM GDS gets a failure, not a
    plausible picture with a hole in it (#619)."""
    import logging

    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(
        tmp_path, monkeypatch, run_overrides={"gds_mode": "strict"}, emit_png=True
    )
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", missing=["sram_32x64"]),
    )
    events = _capture_pnr_events(monkeypatch)

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "incomplete"
    assert export.delivered is False
    assert export.missing_cells == ["sram_32x64"]
    assert export.gds_path is None and export.png_path is None
    artefacts = Path(backend.artefact_dir)
    assert not (artefacts / "demo_top.gds").exists()
    assert not (artefacts / "demo_top.png").exists()
    assert not (artefacts / "def2stream.report.json").exists()

    level, fields = _one_event(events, "pnr.gds_incomplete")
    assert level == logging.ERROR
    assert fields["cells"] == ["sram_32x64"]
    assert _one_event(events, "pnr.gds_export_rejected")[1]["missing"] == ["sram_32x64"]


def test_strict_export_fails_when_klayout_is_missing(tmp_path, monkeypatch):
    import logging

    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(
        tmp_path, monkeypatch, run_overrides={"gds_mode": "strict"}
    )
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: None)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("KLayout was launched without an executable"),
    )
    events = _capture_pnr_events(monkeypatch)

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert "KLayout not found" in export.desc
    assert _one_event(events, "pnr.no_klayout")[0] == logging.ERROR


def test_preview_export_without_klayout_only_warns(tmp_path, monkeypatch):
    """KLayout stays optional: preview reports its absence and moves on."""
    import logging

    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: None)
    events = _capture_pnr_events(monkeypatch)

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert _one_event(events, "pnr.no_klayout")[0] == logging.WARNING


def test_strict_export_fails_without_a_klayout_technology(tmp_path, monkeypatch):
    import logging

    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    platform = MagicMock()
    platform.get_pdk.return_value = _make_pdk_cfg(tmp_path)  # no klayout-tech
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path, gds_mode="strict"),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )
    events = _capture_pnr_events(monkeypatch)

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert "klayout-tech" in export.desc
    assert _one_event(events, "pnr.gds_no_klayout_tech")[0] == logging.ERROR


def test_export_fails_when_the_render_fails(tmp_path, monkeypatch):
    """A complete GDS whose PNG never rendered is not what `--png` asked
    for; strict publishes neither, preview keeps the GDS and says so."""
    from rtl_buddy.tools import pnr_openroad

    for mode, keeps_gds in (("preview", True), ("strict", False)):
        backend, platform = _stream_backend(
            tmp_path / mode,
            monkeypatch,
            run_overrides={"gds_mode": mode},
            emit_png=True,
        )
        streamout = _fake_klayout(backend, "demo_top")
        calls = {"n": 0}

        def _run(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return streamout(cmd, **kwargs)
            # The render leaves a half-written PNG and fails.
            Path(backend.artefact_dir, "demo_top.png").write_bytes(b"\x89PNG")
            result = MagicMock()
            result.returncode = 1
            result.stderr = ""
            result.stdout = result.stderr = ""
            return result

        monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

        export = backend.export_layout(platform, "demo_top")

        assert export.status == "complete"
        assert export.delivered is False, mode
        assert "PNG render failed" in export.desc
        assert export.png_path is None
        assert not Path(backend.artefact_dir, "demo_top.png").exists()
        assert (export.gds_path is not None) is keeps_gds, mode


@pytest.mark.parametrize(
    "report",
    [False, "{not json", json.dumps({"schema": 99, "missing_cells": []})],
    ids=["absent", "corrupt", "wrong-schema"],
)
def test_export_fails_when_the_report_says_nothing(tmp_path, monkeypatch, report):
    """No readable report means nobody vouched for this layout, so it is a
    failed export rather than a complete one — in either mode (#619)."""
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", report=report, returncode=0),
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert export.gds_path is None
    assert not Path(backend.artefact_dir, "demo_top.gds").exists()


def test_export_ignores_a_previous_runs_report(tmp_path, monkeypatch):
    """A stale report is the #469 failure in its purest form: it is the file
    that says a layout is complete."""
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    stale = Path(backend.artefact_dir) / "def2stream.report.json"
    stale.write_text(
        json.dumps({"schema": 1, "complete": True, "missing_cells": [], "errors": 0})
    )
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", report=False, returncode=0),
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert not stale.exists()


def test_export_fails_on_errors_the_missing_cells_do_not_account_for(
    tmp_path, monkeypatch
):
    """Preview covers cells without layout, nothing else. An orphan cell is
    a different failure and fails the export in both modes (#619)."""
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", orphans=["orphan_buf"]),
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert "beyond missing cells" in export.desc
    assert not Path(backend.artefact_dir, "demo_top.gds").exists()


def test_export_fails_when_klayout_dies_after_streaming(tmp_path, monkeypatch):
    """An exit code the report does not explain is not a preview."""
    from rtl_buddy.tools import pnr_openroad

    backend, platform = _stream_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", returncode=139),
    )

    export = backend.export_layout(platform, "demo_top")

    assert export.status == "failed"
    assert "139" in export.desc


def test_cli_gds_mode_overrides_the_run_config(tmp_path):
    """`--gds-mode` decides for the invocation; the run key is the default."""
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    def _backend(**kwargs):
        return OpenRoadPnr(
            name="demo/openroad",
            pnr_cfg=_make_pnr_cfg(tmp_path, **kwargs.pop("run", {})),
            suite_dir=str(tmp_path),
            root_cfg=MagicMock(),
            **kwargs,
        )

    assert _backend().gds_mode == "preview"
    assert _backend(run={"gds_mode": "strict"}).gds_mode == "strict"
    assert _backend(gds_mode="strict").gds_mode == "strict"
    assert _backend(run={"gds_mode": "strict"}, gds_mode="preview").gds_mode == (
        "preview"
    )


_PNR_GDS_MODE_YAML = dedent("""\
    rtl-buddy-filetype: pnr_config

    runs:
      - name: "gds_default"
        desc: "no gds-mode"
        tool: "openroad"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
      - name: "gds_strict"
        desc: "signoff stream-out"
        tool: "openroad"
        synth: "demo_synth"
        synth-path: "../synth/synth.yaml"
        platform: "nangate45_typ"
        gds-mode: strict
        gds-allow-empty:
          - "fakeram45_*"
""")


def test_pnr_suite_loads_gds_mode_and_allow_empty(tmp_path):
    from rtl_buddy.config.pnr import GdsMode

    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(_PNR_GDS_MODE_YAML)
    suite = PnrSuiteConfig(path=str(pnr_yaml))

    default = suite.get_runs("gds_default")[0]
    assert default.get_gds_mode() == GdsMode.PREVIEW
    assert default.get_gds_allow_empty() == []

    strict = suite.get_runs("gds_strict")[0]
    assert strict.get_gds_mode() == GdsMode.STRICT
    assert strict.get_gds_allow_empty() == ["fakeram45_*"]


def test_pnr_suite_rejects_an_unknown_gds_mode(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(
        _PNR_GDS_MODE_YAML.replace("gds-mode: strict", "gds-mode: signoff")
    )
    with pytest.raises(FatalRtlBuddyError, match="gds-mode"):
        PnrSuiteConfig(path=str(pnr_yaml))


def _run_backend_with_export(tmp_path, monkeypatch, *, mode, missing=()):
    """A full `run()` over a clean OpenROAD and a fake KLayout stream-out."""
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
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path, klayout_props="pdk/klayout/props.lyp")
    platform = MagicMock()
    platform.get_pdk.return_value = pdk
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path, gds_mode=mode),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        emit_gds=True,
    )
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)

    artefacts = Path(backend.artefact_dir)
    streamout = _fake_klayout(backend, "demo_top", missing=missing)

    def _run(cmd, **kwargs):
        if "-log" in cmd:
            Path(cmd[cmd.index("-log") + 1]).write_text(
                "Design area 123.45 um^2 1% utilization\n"
                "Number of instances:          42\n"
            )
            (artefacts / "demo_top.routed.odb").write_bytes(b"\x00odb\x00")
            (artefacts / "demo_top.def").write_text("DESIGN demo_top ;\n")
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result
        return streamout(cmd, **kwargs)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    return backend, artefacts


def test_strict_export_failure_fails_the_run_but_keeps_the_routed_database(
    tmp_path, monkeypatch
):
    """The P&R verdict is a pass and its outputs are trustworthy — OpenROAD
    finished cleanly — but the export the user asked for was not delivered,
    so the run fails and says which cells (#619). The stage is named so an
    `xfail:` marker on the design's timing cannot excuse it (#553)."""
    backend, artefacts = _run_backend_with_export(
        tmp_path, monkeypatch, mode="strict", missing=["sram_32x64"]
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "export"
    assert "sram_32x64" in res.results["desc"]
    assert res.results["gds_status"] == "incomplete"
    assert res.results["gds_missing_cells"] == ["sram_32x64"]
    assert res.results["gds_missing_cell_count"] == 1
    assert res.results["gds_mode"] == "strict"
    # The measurements P&R did make are still reported beside the failure.
    assert res.results["area_um2"] == 123.45
    assert res.results["cell_count"] == 42
    # No layout published; the routed database stays for `rb power`.
    assert not (artefacts / "demo_top.gds").exists()
    assert not (artefacts / "def2stream.report.json").exists()
    assert (artefacts / "demo_top.routed.odb").exists()


def test_preview_export_qualifies_an_otherwise_passing_run(tmp_path, monkeypatch):
    """Preview keeps the incomplete layout, and every place the result is
    read says it is incomplete — including the `desc` a table shows."""
    backend, artefacts = _run_backend_with_export(
        tmp_path, monkeypatch, mode="preview", missing=["sram_32x64"]
    )

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert res.is_pass()
    assert res.results["desc"].startswith("P&R passed; GDS incomplete")
    assert "sram_32x64" in res.results["desc"]
    assert res.results["gds_status"] == "incomplete"
    assert res.results["gds_missing_cells"] == ["sram_32x64"]
    assert res.results["gds_path"] == str(artefacts / "demo_top.gds")
    assert (artefacts / "demo_top.gds").exists()


def test_complete_export_leaves_the_pass_unqualified(tmp_path, monkeypatch):
    backend, artefacts = _run_backend_with_export(tmp_path, monkeypatch, mode="strict")

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert res.results["desc"] == "P&R passed"
    assert res.results["gds_status"] == "complete"
    assert "gds_missing_cells" not in res.results
    assert (artefacts / "demo_top.gds").exists()


def test_pnr_result_row_carries_the_export_status():
    """The machine output names the cells, not just a count (#619)."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    results = PnrPassResults(
        name="demo/results",
        desc="P&R passed; GDS incomplete: no layout for 1 cell (sram_32x64)",
        area_um2=123.45,
        fields={
            "gds_path": "/a/demo_top.gds",
            "gds_mode": "preview",
            "gds_status": "incomplete",
            "gds_missing_cells": ["sram_32x64"],
            "gds_missing_cell_count": 1,
            "gds_allowed_empty_cells": [],
        },
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})

    assert row["gds_status"] == "incomplete"
    assert row["gds_missing_cells"] == ["sram_32x64"]
    assert row["gds_missing_cell_count"] == 1
    assert row["gds_path"] == "/a/demo_top.gds"
    # An empty list is "nothing to report", not a field worth carrying.
    assert "gds_allowed_empty_cells" not in row
    assert "sram_32x64" in row["desc"]


def test_pnr_outputs_column_qualifies_an_incomplete_export():
    """The human table says it too, in the Outputs column (#619)."""
    from rtl_buddy.rtl_buddy import _pnr_outputs_cell

    complete = {"gds_path": "a.gds", "png_path": "a.png", "gds_status": "complete"}
    assert _pnr_outputs_cell(complete) == "gds+png"
    assert (
        _pnr_outputs_cell(
            {**complete, "gds_status": "incomplete", "gds_missing_cell_count": 2}
        )
        == "gds+png (incomplete: 2 missing)"
    )
    assert (
        _pnr_outputs_cell({**complete, "gds_allowed_empty_cells": ["fakeram45_64x32"]})
        == "gds+png (1 empty by design)"
    )
    assert _pnr_outputs_cell({"gds_status": "failed"}) == "export failed"
    # A strict run publishes nothing, so the note stands on its own.
    assert (
        _pnr_outputs_cell({"gds_status": "incomplete", "gds_missing_cell_count": 3})
        == "incomplete: 3 missing"
    )


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


def test_pnr_template_legalizes_after_every_cell_inserting_repair():
    """Every pass that inserts or moves cells must be followed by a
    `detailed_placement` before `global_route`, or the router meets cells at
    unlegalized locations and fails with DRT-0073 "no access point" on
    exactly the inserted instances (#591). `repair_timing -hold` after CTS
    was the one that shipped without it."""
    from importlib.resources import files

    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    commands = [
        line.strip()
        for line in template.splitlines()
        if line.strip() and not line.strip().startswith(("#", "puts "))
    ]
    route_at = commands.index(
        "set_routing_layers -signal $SIGNAL_LAYERS -clock $CLOCK_LAYERS"
    )
    pre_route = commands[:route_at]
    inserters = [
        i
        for i, cmd in enumerate(pre_route)
        if cmd.startswith(("repair_design", "repair_timing", "clock_tree_synthesis"))
    ]
    assert inserters, "no cell-inserting passes found; did the template move?"
    for i in inserters:
        tail = pre_route[i + 1 :]
        assert "detailed_placement" in tail, (
            f"`{pre_route[i]}` is not followed by a legalization pass before "
            "routing (#591)"
        )

    # The legalization that closes placement is also checked, so a repair
    # that legalization cannot absorb fails there — with a named cell —
    # rather than as a router mystery.
    last_dp = len(pre_route) - 1 - pre_route[::-1].index("detailed_placement")
    # `-verbose` is what names the cell (#639).
    assert "check_placement -verbose" in pre_route[last_dp + 1 :]


def test_pnr_template_packs_macros_by_their_own_size():
    """The flow hands every macro's own footprint to the packer and places
    the result FIRM. The packing itself is tested in test_pnr_macro_pack.py."""
    from importlib.resources import files

    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    assert "[[$inst getMaster] isBlock]" in template
    assert (
        "lappend footprints [list [$inst getName] [$master getWidth] [$master getHeight]]"
        in template
    )
    assert "rb::macro_pack::solve \\" in template
    assert "$inst setPlacementStatus FIRM" in template
    # No slot is sized for the largest macro any more (#626).
    assert "max_macro_w" not in template
    assert "slot_w" not in template


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
        result.stderr = ""
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
    # The generated flow script goes with them: a run that never reaches
    # `_write_script` must not leave the previous script beside its absent
    # outputs, where it reads as the script this run used (#527).
    stale_script = Path(backend._script_path())
    stale_script.write_text("# previous run's flow\n")

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: None)

    res = backend.run()

    # The tool-missing message is unchanged...
    assert "not found" in res.results["desc"]
    # ...but nothing is left for `rb power` to pick up.
    assert not stale_odb.exists()
    assert not stale_drc.exists()
    assert not stale_script.exists()


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


def test_pnr_openroad_writes_odb_then_fails_removes_it(tmp_path, monkeypatch):
    """The flow's `write_db` runs before the script ends, so OpenROAD can be
    killed or exit non-zero with a complete or partial `<top>.routed.odb` on
    disk. `rb power` accepts that ODB by existence, so a FAIL must not leave
    it behind (#469)."""
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

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

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
    odb = artefacts / "demo_top.routed.odb"
    routed_v = artefacts / "demo_top.routed.v"

    def _writes_then_dies(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        odb.write_bytes(b"\x00partial odb\x00")
        routed_v.write_text("module demo_top(); endmodule\n")
        result = MagicMock()
        result.returncode = 1
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _writes_then_dies)

    res = backend.run()

    assert "exited with code 1" in res.results["desc"]
    assert not odb.exists()
    assert not routed_v.exists()


def test_pnr_openroad_stderr_reaches_the_log_and_the_verdict(tmp_path, monkeypatch):
    """A Tcl error — the macro packer refusing a floorplan, say — is written
    to stderr, which OpenROAD's own `-log` does not carry. It has to survive
    the run: the whole diagnostic in the log, its first line in the verdict
    (#626)."""
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

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

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

    diagnostic = (
        "Error: pnr.tcl, 272 3 macros do not fit the 492.660 x 489.600 um "
        "floorplan core\n"
        "  smallest core at this aspect ratio that would fit: 559.917 x 556.440 um\n"
    )

    def _dies_with_a_tcl_error(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text(">>> Macro placement\n")
        result = MagicMock()
        result.returncode = 1
        result.stderr = diagnostic
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _dies_with_a_tcl_error)

    res = backend.run()

    assert (
        "3 macros do not fit the 492.660 x 489.600 um floorplan core"
        in (res.results["desc"])
    )
    log_text = Path(backend._log_path()).read_text()
    assert "smallest core at this aspect ratio that would fit" in log_text


def test_pnr_post_openroad_failure_keeps_the_flow_script(tmp_path, monkeypatch):
    """A FAIL past OpenROAD publishes no outputs but keeps `pnr.tcl`.

    The script on disk at that point is the one OpenROAD really ran, so it is
    what someone reading `pnr.log` needs; only the up-front clear touches it
    (#527). The outputs still go — that contract is unchanged.
    """
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

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

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
    script = Path(backend._script_path())

    def _fake_write_script(*_args, **_kwargs):
        script.write_text("# this run's flow\n")
        return str(script)

    monkeypatch.setattr(backend, "_write_script", _fake_write_script)
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)

    artefacts = Path(backend.artefact_dir)
    odb = artefacts / "demo_top.routed.odb"
    # A leftover script from the previous run is replaced, not merely kept.
    script.write_text("# previous run's flow\n")

    def _writes_then_dies(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        odb.write_bytes(b"\x00partial odb\x00")
        result = MagicMock()
        result.returncode = 1
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _writes_then_dies)

    res = backend.run()

    assert "exited with code 1" in res.results["desc"]
    assert not odb.exists()
    assert script.read_text() == "# this run's flow\n"


def test_pnr_error_line_after_writing_removes_the_odb(tmp_path, monkeypatch):
    """Same for the other post-run gate: an `[ERROR ...]` line fails the run,
    so the ODB written before it must go (#469)."""
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

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())

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

    odb = Path(backend.artefact_dir) / "demo_top.routed.odb"

    def _writes_then_errors(cmd, **_kwargs):
        Path(cmd[cmd.index("-log") + 1]).write_text(
            "[ERROR GRT-0012] detailed route failed\n"
        )
        odb.write_bytes(b"\x00partial odb\x00")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _writes_then_errors)

    res = backend.run()

    assert "ERROR(s) in OpenROAD log" in res.results["desc"]
    assert not odb.exists()


def test_def2stream_removes_a_zero_length_gds(tmp_path, monkeypatch):
    """A zero-length GDS is what the size check rejects, and it is still a
    file — leaving it means the next run's `isfile` sees a layout where none
    was produced (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_stream_pdk(tmp_path)
    platform = MagicMock()
    platform.get_pdk.return_value = pdk
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
    )
    out_gds = Path(backend.artefact_dir) / "demo_top.gds"

    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", gds=b"", report=False, returncode=1),
    )

    assert backend._run_def2stream(platform, "demo_top").status == "failed"
    assert not out_gds.exists()


def test_gds2png_removes_a_partial_png(tmp_path, monkeypatch):
    """KLayout can render part of the image and then fail; a partial PNG left
    here is reported as this run's layout by the next one (#469)."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/opt/klayout")

    pdk = _make_pdk_cfg(tmp_path, klayout_props="pdk/klayout/props.lyp")
    platform = MagicMock()
    platform.get_pdk.return_value = pdk
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
        emit_gds=True,
        emit_png=True,
    )
    out_png = Path(backend.artefact_dir) / "demo_top.png"
    gds = Path(backend.artefact_dir) / "demo_top.gds"
    gds.write_bytes(b"\x00\x06\x00\x02\x00\x07")

    def _writes_partial(cmd, **_kwargs):
        out_png.write_bytes(b"\x89PNG partial")
        result = MagicMock()
        result.returncode = 1
        result.stderr = ""
        result.stdout = "render aborted"
        result.stderr = ""
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _writes_partial)

    assert backend._run_gds2png(platform, str(gds), "demo_top") is None
    assert not out_png.exists()
