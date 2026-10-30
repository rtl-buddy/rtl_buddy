"""Tests for `placement.repair-design`, the options of the place stage's `repair_design`.

The block takes the keys the other `repair-design` blocks take and is read by the same
code; it merges field by field from the PDK through the platform to the run.
"""

import os
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from serde.yaml import from_yaml
from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _platform, _render_flow
from test_pnr_checkpoints import _pnr_cfg as _ckpt_pnr_cfg

from rtl_buddy.config.pdk import PlacementFile, validate_placement
from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.config.pnr_repair import RepairDesign
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

_PLACE_REPAIR = "estimate_parasitics -placement\nrepair_design{}\n"


def _raw(**block) -> PlacementFile:
    """A `placement:` block as loaded from YAML; the PDK and the platform validate it."""
    return PlacementFile(repair_design=block)


def _placement(where: str, **block) -> PlacementFile:
    """A run's `placement:` block, validated as the run loader does."""
    return validate_placement(_raw(**block), where)


def _on(tmp_path, **block):
    return _platform(_make_pdk_cfg(tmp_path), placement=_raw(**block))


# --- configuration ------------------------------------------------------------


def test_unset_leaves_every_option_to_openroad(tmp_path):
    assert (
        _platform(_make_pdk_cfg(tmp_path)).get_placement_repair_design()
        == RepairDesign()
    )


def test_yaml_keys_load_as_the_other_repair_design_blocks_do(tmp_path):
    platform = PnrPlatformConfig(
        from_yaml(
            PnrPlatformConfigFile,
            dedent("""\
                name: "p"
                pdk: "p"
                placement:
                  repair-design: {slew-margin: 10, cap-margin: "20", max-wire-length: 600}
            """),
        ),
        lambda _n: _make_pdk_cfg(tmp_path),
    )
    assert platform.get_placement_repair_design() == RepairDesign(
        slew_margin=10.0, cap_margin=20.0, max_wire_length=600.0
    )


def test_levels_merge_field_by_field_and_null_clears_the_levels_below(tmp_path):
    pdk = _make_pdk_cfg(
        tmp_path, placement=_raw(**{"slew-margin": 5, "max-wire-length": 800})
    )
    platform = _platform(pdk, placement=_raw(**{"cap-margin": 10}))
    assert platform.get_placement_repair_design() == RepairDesign(
        slew_margin=5.0, cap_margin=10.0, max_wire_length=800.0
    )

    run = platform.with_run_placement(
        _placement("pnr run 'r'", **{"max-wire-length": 300, "slew-margin": None}),
        "r",
    )
    assert run.get_placement_repair_design() == RepairDesign(
        cap_margin=10.0, max_wire_length=300.0
    )

    cleared = platform.with_run_placement(
        validate_placement(PlacementFile(repair_design=None), "pnr run 'r'"), "r"
    )
    assert cleared.get_placement_repair_design() == RepairDesign()

    # A run block without the key keeps the platform's.
    kept = platform.with_run_placement(PlacementFile(density=0.5), "r")
    assert kept.get_placement_repair_design() == platform.get_placement_repair_design()


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"slew-margin": 101}, "placement.repair-design.slew-margin must be a number"),
        ({"cap-margin": -1}, "placement.repair-design.cap-margin must be a number"),
        (
            {"max-wire-length": 0},
            "placement.repair-design.max-wire-length must be a positive length",
        ),
        ({"max-wire-length": "far"}, "placement.repair-design.max-wire-length"),
    ],
)
def test_a_bad_value_is_refused_naming_the_key_and_the_level(block, message):
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'r': " + message):
        _placement("pnr run 'r'", **block)


def test_a_block_that_is_not_a_mapping_is_refused():
    with pytest.raises(FatalRtlBuddyError, match="placement.repair-design must be"):
        validate_placement(PlacementFile(repair_design=500), "pnr run 'r'")


def test_the_block_joins_the_hardened_config_digest_only_when_set(tmp_path):
    pnr_cfg = _make_pnr_cfg(tmp_path, harden=True)
    plain = pnr_abstract.abstract_config(pnr_cfg, _platform(_make_pdk_cfg(tmp_path)))
    assert "repair_design" not in plain["placement"]

    on = pnr_abstract.abstract_config(
        pnr_cfg, _on(tmp_path, **{"max-wire-length": 400})
    )
    assert on["placement"]["repair_design"] == {"max_wire_length": 400.0}
    assert pnr_abstract.config_digest(on) != pnr_abstract.config_digest(plain)


# --- rendered flow ------------------------------------------------------------


def test_the_options_reach_the_place_stage_repair_design_only(tmp_path):
    text = _render_flow(
        tmp_path,
        _on(tmp_path, **{"slew-margin": 10, "cap-margin": 20, "max-wire-length": 600}),
    )
    assert (
        _PLACE_REPAIR.format(" -slew_margin 10 -cap_margin 20 -max_wire_length 600")
        in text
    )
    assert text.count("-max_wire_length 600") == 1


def _scripts(tmp_path, platform, *, stages: bool, template=None) -> dict[str, str]:
    """`pnr.tcl` and, for a staged run, each stage script, rendered for `platform`."""
    pnr_cfg = _ckpt_pnr_cfg(tmp_path, None, stages=stages)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    run_dir = tmp_path / ("ck-base" if template else "ck") / "run"
    if stages:
        backend._ckpt_run_dir = str(run_dir)
        os.makedirs(run_dir, exist_ok=True)
    if template is not None:
        backend._load_template = lambda: template
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
    written = {"pnr.tcl": script.read_text().replace(str(run_dir), "RUN")}
    if stages:
        for path in sorted(run_dir.glob("*.tcl")):
            written[path.name] = path.read_text().replace(str(run_dir), "RUN")
    return written


@pytest.mark.parametrize("stages", [False, True])
def test_an_unset_block_renders_every_script_as_the_bare_command_did(tmp_path, stages):
    """Without the block, `pnr.tcl` and every stage script are the template's render with
    the place stage's bare `repair_design`."""
    from test_pnr_repair import _real_platform

    platform = _real_platform(tmp_path)
    template = OpenRoadPnr("x", MagicMock(), str(tmp_path), MagicMock())
    template = template._load_template()
    placeholder = "repair_design{{ place_repair_design_options }}\n"
    assert template.count(placeholder) == 1
    base = template.replace(placeholder, "repair_design\n")

    now = _scripts(tmp_path, platform, stages=stages)
    before = _scripts(tmp_path, platform, stages=stages, template=base)
    assert now == before
    assert len(now) == (len(CHECKPOINT_STAGES) + 1 if stages else 1)
    assert _PLACE_REPAIR.format("") in "".join(now.values())
