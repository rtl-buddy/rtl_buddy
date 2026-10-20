"""Unknown keys in pnr.yaml, cfg-pnr-platforms and cfg-pdks are config errors (rtl_buddy#861)."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest
import yaml

from rtl_buddy.config.pdk import PDK_SPEC
from rtl_buddy.config.pnr import PNR_RUN_SPEC, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PNR_PLATFORM_SPEC
from rtl_buddy.config.root import RootConfig
from rtl_buddy.config.unknown_keys import (
    KeySpec,
    find_unknown_entry_keys,
    find_unknown_keys,
    serde_keys,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message

PLACEMENT_ALL = {
    "density": 0.5,
    "padding": 1,
    "macro-halo": 10,
    "macro-cell-halo": 1,
    "tie-separation": 0,
    "reference-hpwl": 1000,
    "routability-driven": True,
    "routability-use-grt": True,
    "timing-driven": True,
    "rotated-macro-footprint": "none",
    "macro-pin-limits": {
        "buffers": ["buf1", "buf8"],
        "margin": 0.05,
        "capacitance-margin": 0.5,
    },
}

# Sized by core-margin; the second run sizes with die-area, core-area and core-cutouts instead.
REPAIR_DESIGN_ALL = {"slew-margin": 20, "cap-margin": 10, "max-wire-length": 500}
GLOBAL_ROUTE_REPAIR_ALL = {
    "repair-design": dict(REPAIR_DESIGN_ALL),
    "setup-margin": 0.05,
    "hold-margin": 0.02,
    "reroute": "full",
}
POST_ROUTE_REPAIR_ALL = {
    "repair-design": {
        **REPAIR_DESIGN_ALL,
        "wire-rc": {"resistance": 0.06, "capacitance": 0.17},
        "after-timing": True,
    },
    "setup-margin": 0.05,
    "hold-margin": None,
    "rounds": 3,
    "margin-step": 0.01,
    "reroute": "full",
    "electrical-repair": {"slew-margin": 10, "cap-margin": None},
}
RUN_SIZED = {
    "name": "a",
    "desc": "every key",
    "tool": "openroad",
    "synth": "s",
    "synth-path": "synth.yaml",
    "constraints": "c.sdc",
    "pin-constraints": "pins.tcl",
    "pdn-config": "pdn.tcl",
    "pdn": {
        "ring": {"layers": ["m5", "m6"], "width": 1, "spacing": 1, "offset": 0},
        "stripes": [
            {"layer": "m1", "width": 0.5, "followpins": True},
            {
                "layer": "m4",
                "width": 1,
                "pitch": 20,
                "offset": 2,
                "spacing": 1,
            },
        ],
        "connect": [["m1", "m4"]],
    },
    "platform": "p",
    "floorplan": {
        "utilization": 0.5,
        "aspect": 1.0,
        "core-margin": 5.0,
        "macro-anchor": "lower-right",
        "macro-placement": "pack",
        "blockages": [
            {"rect": [0, 0, 10, 10]},
            {"rect": [0, 0, 10, 10], "type": "soft"},
            {
                "rect": [0, 0, 10, 10],
                "type": "partial",
                "max-density": 0.5,
                "lifetime": "global-placement",
            },
        ],
        "pins": [
            {
                "names": ["a*"],
                "side": "left",
                "start": 1,
                "end": 50,
                "group": True,
                "order": True,
            },
            {"names": "clk", "location": [0, 10], "layer": "m3", "size": [1, 1]},
        ],
        "macros": [
            {
                "instance": "u_a",
                "location": [10, 10],
                "orientation": "R90",
                "halo": [1, 1],
            },
            {"instance": "u_b", "halo": [2, 2]},
        ],
        "macros-file": "macros.yaml",
        "macro-snap": True,
        "pins-file": "pins.yaml",
        "pin-snap": True,
        "pins-complete": False,
    },
    "lef-paths": ["a.lef"],
    "lib-paths": ["a.lib"],
    "gds-paths": ["a.gds"],
    "gds-mode": "strict",
    "gds-allow-empty": ["x*"],
    "checkpoints": ["floorplan"],
    "hooks": {"session": "hook.tcl"},
    "inputs": ["helper.tcl"],
    "harden": True,
    "abstract": {"bloat": "occupied", "feedthrough-layers": ["m4"]},
    "buffer-ports": True,
    "fail-on-electrical": True,
    "blocks": [
        {"name": "blk", "pnr": "blk_run", "pnr-path": "blk/pnr.yaml"},
        {"name": "blk2", "pnr": "blk2_run"},
    ],
    "placement": PLACEMENT_ALL,
    "global-route-repair": GLOBAL_ROUTE_REPAIR_ALL,
    "post-route-repair": POST_ROUTE_REPAIR_ALL,
    "routing-layers": {"signal": "m2-m4", "clock": "m3-m4"},
    "routing-layer-adjustment": 0.2,
    "cts-buffer": "b2",
    "cts-latency-balancing": False,
    "cts-repair-clock-nets": True,
    "post-cts-setup-repair": False,
    "port-buffer": "buf2",
    "tapcell-tcl": "tap_run.tcl",
    "cts-dont-use-cells": ["*_L"],
    "keep-sizing-vt": True,
    "cts-clock-nets": ["clk", "clk_div[0]"],
    "mode": "closure",
    "reglvl": 1000,
    "tool_overrides": {"any": 1},
    "threads": 2,
    "resources": {
        "cpus": 4,
        "mem": "8G",
        "time": "02:00:00",
        "stages": {
            "floorplan": {"cpus": 1},
            "place": {"mem": "4G"},
            "cts": {"time": "00:30:00"},
            "global_route": {"cpus": 2, "mem": "4G", "time": "01:00:00"},
            "detailed_route": {"cpus": 16, "mem": "16G", "time": "12:00:00"},
            "post_route_repair": {"cpus": 8, "time": "04:00:00"},
            "finish": {"cpus": 2},
        },
    },
    "detailed-route-verbose": 1,
    "xfail": True,
    "xfail_strict": False,
    "xfail_match": "boom",
}
RUN_DIE = {
    "name": "b",
    "desc": "die and core",
    "synth": "s",
    "synth-path": "synth.yaml",
    "platform": "p",
    "floorplan": {
        "die-area": [0, 0, 100, 100],
        "core-area": [10, 10, 90, 90],
        "core-cutouts": [[20, 20, 30, 30]],
    },
    "stages": True,
    "global-route-hold-repair": True,
    "mode": "probe",
    "post-route-repair": {"repair-design": {"wire-rc": {"layer": "m5"}}},
}

PDK_ALL = {
    "name": "pdk1",
    "site": "site",
    "corners": {"typ": "lib/typ.lib", "my-fast_1": ["lib/a.lib", "lib/b.lib"]},
    "tech-lef": "t.lef",
    "macro-lef": "m.lef",
    "cell-gds": ["c.gds"],
    "klayout-tech": "k.lyt",
    "klayout-props": "k.lyp",
    "tie-hi": "H/Z",
    "tie-lo": "L/Z",
    "fill-cells": ["FILL1"],
    "pin-layers": {"horizontal": "m3", "vertical": "m2"},
    "placement": PLACEMENT_ALL,
    "dont-use-cells": ["x*"],
    "pdn-config": "pdn.tcl",
    "rcx-rules": "rcx.rules",
    "tracks-tcl": "tracks.tcl",
    "layer-rc-tcl": "rc.tcl",
    "tapcell-tcl": "tap.tcl",
    "platform-tcl": "plat.tcl",
}
PLATFORM_ONE_CORNER = {
    "name": "pl1",
    "pdk": "pdk1",
    "corner": "typ",
    "cts-buffer": ["b1", "b2"],
    "cts-sink-clustering": False,
    "cts-latency-balancing": False,
    "cts-repair-clock-nets": True,
    "post-cts-setup-repair": True,
    "global-route-repair": GLOBAL_ROUTE_REPAIR_ALL,
    "post-route-repair": POST_ROUTE_REPAIR_ALL,
    "routing-layer-adjustment": 0.1,
    "cts-apply-ndr": "half",
    "port-buffer": "buf",
    "max-fanout": 10,
    "routing-layers": {"signal": "m1-m5", "clock": "m3-m5"},
    "placement": PLACEMENT_ALL,
    "dont-use-cells": ["y*"],
    "cts-dont-use-cells": ["*_L", "*_SL"],
    "keep-sizing-vt": True,
    "tapcell-tcl": "tap_platform.tcl",
}
PLATFORM_CORNERS = {
    "name": "pl2",
    "pdk": "pdk1",
    "corners": ["typ", "my-fast_1"],
    "global-route-hold-repair": True,
    "post-route-repair": {"repair-design": {"wire-rc": {"layer": "m5"}}},
}


def _specs(spec: KeySpec):
    yield spec
    for child in spec.nested.values():
        yield from _specs(child)


def _used_keys(raw, spec: KeySpec, used: dict) -> None:
    if not isinstance(raw, dict):
        return
    used.setdefault(spec.cls, set()).update(raw)
    for key, child in spec.nested.items():
        value = raw.get(key)
        for item in value if child.many and isinstance(value, list) else [value]:
            _used_keys(item, child, used)


def _assert_every_valid_key_is_used(entries, spec: KeySpec, extra=()) -> None:
    """Fail when a serde class of `spec` has a key that no entry sets, so a new key must join the fixtures."""
    used: dict = {}
    for entry in entries:
        _used_keys(entry, spec, used)
    for other_entries, other_spec in extra:
        for entry in other_entries:
            _used_keys(entry, other_spec, used)
    for block in _specs(spec):
        assert set(serde_keys(block.cls)) <= used.get(block.cls, set()), block.cls


def _write_pnr(tmp_path: Path, *runs: dict) -> str:
    for run in runs:
        for name in run.get("inputs") or []:
            (tmp_path / name).write_text("")
    path = tmp_path / "pnr.yaml"
    path.write_text(
        yaml.safe_dump(
            {"rtl-buddy-filetype": "pnr_config", "runs": list(runs)}, sort_keys=False
        )
    )
    return str(path)


def _with_root_blocks(project: Path, pdks: list, platforms: list) -> None:
    cfg = project / "root_config.yaml"
    cfg.write_text(
        cfg.read_text()
        + "\n"
        + yaml.safe_dump(
            {"cfg-pdks": pdks, "cfg-pnr-platforms": platforms}, sort_keys=False
        )
    )


# -- pnr.yaml ------------------------------------------------------------------


def test_a_run_with_every_valid_key_loads(tmp_path):
    suite = PnrSuiteConfig(_write_pnr(tmp_path, RUN_SIZED, RUN_DIE))

    assert suite.get_run_names() == ["a", "b"]
    _assert_every_valid_key_is_used([RUN_SIZED, RUN_DIE], PNR_RUN_SPEC)


@pytest.mark.parametrize(
    ("where", "block_path", "valid"),
    [
        (
            "pnr run 'a'",
            (),
            "name, desc, tool, synth, synth-path, constraints, pin-constraints, "
            "pdn-config, pdn, platform, floorplan, lef-paths, lib-paths, gds-paths, "
            "gds-mode, gds-allow-empty, checkpoints, stages, hooks, inputs, harden, "
            "abstract, buffer-ports, fail-on-electrical, blocks, placement, "
            "global-route-repair, post-route-repair, routing-layers, "
            "routing-layer-adjustment, cts-buffer, cts-latency-balancing, "
            "cts-repair-clock-nets, post-cts-setup-repair, global-route-hold-repair, "
            "port-buffer, tapcell-tcl, cts-dont-use-cells, keep-sizing-vt, "
            "cts-clock-nets, mode, reglvl, tool_overrides, "
            "threads, resources, detailed-route-verbose, xfail, xfail_strict, "
            "xfail_match",
        ),
        (
            "pnr run 'a': floorplan",
            ("floorplan",),
            "utilization, aspect, core-margin, die-area, core-area, core-cutouts, "
            "macro-anchor, macro-placement, blockages, pins, macros, macros-file, "
            "macro-snap, pins-file, pin-snap, pins-complete",
        ),
        (
            "pnr run 'a': floorplan.blockages[2]",
            ("floorplan", "blockages", 2),
            "rect, type, max-density, lifetime",
        ),
        (
            "pnr run 'a': floorplan.pins[1]",
            ("floorplan", "pins", 1),
            "names, side, start, end, group, order, location, layer, size",
        ),
        (
            "pnr run 'a': floorplan.macros[0]",
            ("floorplan", "macros", 0),
            "instance, location, orientation, halo",
        ),
        ("pnr run 'a': pdn", ("pdn",), "ring, stripes, connect"),
        (
            "pnr run 'a': pdn.ring",
            ("pdn", "ring"),
            "layers, width, spacing, offset",
        ),
        (
            "pnr run 'a': pdn.stripes[1]",
            ("pdn", "stripes", 1),
            "layer, width, pitch, offset, spacing, followpins",
        ),
        (
            "pnr run 'a': abstract",
            ("abstract",),
            "bloat, feedthrough-layers",
        ),
        (
            "pnr run 'a': placement",
            ("placement",),
            "density, padding, macro-halo, macro-cell-halo, tie-separation, "
            "reference-hpwl, routability-driven, routability-use-grt, timing-driven, "
            "rotated-macro-footprint, macro-pin-limits",
        ),
        (
            "pnr run 'a': placement.macro-pin-limits",
            ("placement", "macro-pin-limits"),
            "buffers, margin, capacitance-margin",
        ),
        ("pnr run 'a': blocks[1]", ("blocks", 1), "name, pnr, pnr-path"),
        ("pnr run 'a': routing-layers", ("routing-layers",), "signal, clock"),
        (
            "pnr run 'a': global-route-repair",
            ("global-route-repair",),
            "repair-design, setup-margin, hold-margin, reroute",
        ),
        (
            "pnr run 'a': post-route-repair",
            ("post-route-repair",),
            "repair-design, setup-margin, hold-margin, rounds, margin-step, reroute, "
            "electrical-repair",
        ),
        (
            "pnr run 'a': post-route-repair.electrical-repair",
            ("post-route-repair", "electrical-repair"),
            "slew-margin, cap-margin",
        ),
        (
            "pnr run 'a': post-route-repair.repair-design",
            ("post-route-repair", "repair-design"),
            "slew-margin, cap-margin, max-wire-length, wire-rc, after-timing",
        ),
        (
            "pnr run 'a': post-route-repair.repair-design.wire-rc",
            ("post-route-repair", "repair-design", "wire-rc"),
            "resistance, capacitance, layer",
        ),
        (
            "pnr run 'a': global-route-repair.repair-design",
            ("global-route-repair", "repair-design"),
            "slew-margin, cap-margin, max-wire-length",
        ),
        ("pnr run 'a': resources", ("resources",), "cpus, mem, time, stages"),
        (
            "pnr run 'a': resources.stages",
            ("resources", "stages"),
            "floorplan, place, cts, global_route, detailed_route, "
            "post_route_repair, finish",
        ),
        (
            "pnr run 'a': resources.stages.detailed_route",
            ("resources", "stages", "detailed_route"),
            "cpus, mem, time",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and v.startswith("pnr run") else None,
)
def test_an_unknown_key_at_each_level_names_the_key_its_place_and_the_valid_keys(
    tmp_path, where, block_path, valid
):
    run = copy.deepcopy(RUN_SIZED)
    block = run
    for step in block_path:
        block = block[step]
    block["zzz"] = 1
    path = _write_pnr(tmp_path, run)

    with pytest.raises(FatalRtlBuddyError) as exc:
        PnrSuiteConfig(path)

    assert str(exc.value) == (
        f"{path}: {where}: unknown key 'zzz' (valid keys: {valid})"
    )
    assert exc.value.__cause__ is None


def test_a_misspelt_key_names_the_nearest_valid_one(tmp_path):
    run = copy.deepcopy(RUN_SIZED)
    run["floorplan"]["blockages"][0]["lifetme"] = "global-placement"
    path = _write_pnr(tmp_path, run)

    with pytest.raises(FatalRtlBuddyError) as exc:
        PnrSuiteConfig(path)

    assert (
        "floorplan.blockages[0]: unknown key 'lifetme', did you mean 'lifetime'? "
        "(valid keys: rect, type, max-density, lifetime)"
    ) in str(exc.value)


def test_a_python_style_spelling_suggests_the_yaml_spelling(tmp_path):
    run = copy.deepcopy(RUN_SIZED)
    run["floorplan"]["core_margin"] = run["floorplan"].pop("core-margin")
    path = _write_pnr(tmp_path, run)

    with pytest.raises(FatalRtlBuddyError, match="did you mean 'core-margin'\\?"):
        PnrSuiteConfig(path)


def test_every_unknown_key_in_the_file_is_reported_at_once(tmp_path):
    first = copy.deepcopy(RUN_SIZED)
    first["floorplan"]["pins"][0]["sdie"] = "left"
    first["colour"] = "red"
    second = copy.deepcopy(RUN_DIE)
    second["floorplan"]["die_area"] = [0, 0, 1, 1]
    path = _write_pnr(tmp_path, first, second)

    with pytest.raises(FatalRtlBuddyError) as exc:
        PnrSuiteConfig(path)

    lines = str(exc.value).splitlines()
    assert len(lines) == 3
    assert "pnr run 'a': unknown key 'colour'" in lines[0]
    assert "pnr run 'a': floorplan.pins[0]: unknown key 'sdie'" in lines[1]
    assert "pnr run 'b': floorplan: unknown key 'die_area'" in lines[2]


def test_an_unknown_key_is_reported_before_the_error_it_causes(tmp_path):
    run = copy.deepcopy(RUN_DIE)
    run["synth_path"] = run.pop("synth-path")
    path = _write_pnr(tmp_path, run)

    with pytest.raises(FatalRtlBuddyError) as exc:
        PnrSuiteConfig(path)

    assert "unknown key 'synth_path', did you mean 'synth-path'?" in str(exc.value)
    assert "missing 'synth-path'" not in str(exc.value)


def test_an_unknown_key_is_logged_at_error_with_its_fields(tmp_path, caplog):
    run = copy.deepcopy(RUN_SIZED)
    run["abstract"]["feedthrough_layers"] = ["m4"]
    path = _write_pnr(tmp_path, run)
    caplog.set_level(logging.ERROR)

    with pytest.raises(FatalRtlBuddyError):
        PnrSuiteConfig(path)

    [event] = [
        r.rtl_fields
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "config.unknown_key_rejected"
    ]
    assert event == {
        "path": path,
        "block": "pnr run 'a': abstract",
        "key": "feedthrough_layers",
        "suggestion": "feedthrough-layers",
        "known": ["bloat", "feedthrough-layers"],
    }
    assert _human_message("config.unknown_key_rejected", event) == (
        f"{path}: pnr run 'a': abstract: unknown key 'feedthrough_layers', "
        "did you mean 'feedthrough-layers'? (valid keys: bloat, feedthrough-layers)"
    )


def test_a_run_without_a_name_is_labelled_by_position(tmp_path):
    run = {"desc": "no name", "bogus": 1}
    path = _write_pnr(tmp_path, copy.deepcopy(RUN_DIE), run)

    with pytest.raises(FatalRtlBuddyError, match="pnr run #1: unknown key 'bogus'"):
        PnrSuiteConfig(path)


def test_a_malformed_suite_still_fails_with_the_load_error(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("rtl-buddy-filetype: [unclosed\n")
    with pytest.raises(FatalRtlBuddyError, match="failed to load") as exc:
        PnrSuiteConfig(str(bad_yaml))
    assert exc.value.__cause__ is not None

    dup = tmp_path / "dup.yaml"
    dup.write_text("rtl-buddy-filetype: pnr_config\nruns: []\nruns: []\n")
    with pytest.raises(FatalRtlBuddyError, match="failed to load") as exc:
        PnrSuiteConfig(str(dup))
    assert "duplicate key 'runs'" in str(exc.value.__cause__)

    wrong_type = _write_pnr(tmp_path, {**RUN_DIE, "floorplan": 3})
    with pytest.raises(FatalRtlBuddyError, match="failed to load"):
        PnrSuiteConfig(wrong_type)


def test_other_keys_of_a_run_are_not_checked_beyond_their_blocks(tmp_path):
    """Free-form mappings and the top-level keys stay open."""
    run = {**RUN_DIE, "reglvl": {"openroad": 1, "default": 0}}
    run["tool_overrides"] = {"anything": {"goes": True}}
    path = tmp_path / "pnr.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "rtl-buddy-filetype": "pnr_config",
                "x-shared": {"anchor": "holder"},
                "runs": [run],
            }
        )
    )

    assert PnrSuiteConfig(str(path)).get_run_names() == ["b"]


def test_a_non_string_key_is_reported_by_its_string_form():
    # YAML 1.1 reads an unquoted `on` key as True.
    found = find_unknown_keys(
        yaml.safe_load("on: 1\nname: x\n"),
        KeySpec(PDK_SPEC.cls),
        label="cfg-pdks 'x'",
    )

    assert [u.key for u in found] == ["True"]


def test_values_of_the_wrong_shape_are_left_to_the_typed_load():
    spec = PNR_RUN_SPEC
    for raw in (None, 3, "x", ["floorplan"], {"floorplan": 3, "blocks": {"a": 1}}):
        assert find_unknown_keys(raw, spec, label="run") == []
    assert find_unknown_entry_keys({"name": "x"}, spec, kind="pnr run") == []
    assert find_unknown_entry_keys([3, None], spec, kind="pnr run") == []
    assert find_unknown_keys({"blocks": [3, [1]]}, spec, label="run") == []


# -- cfg-pdks and cfg-pnr-platforms ---------------------------------------------


def test_pdk_and_platform_entries_with_every_valid_key_load(minimal_project: Path):
    _with_root_blocks(
        minimal_project, [PDK_ALL], [PLATFORM_ONE_CORNER, PLATFORM_CORNERS]
    )

    cfg = RootConfig(name="all keys")

    assert sorted(cfg.pdk_cfgs) == ["pdk1"]
    assert sorted(cfg.pnr_platform_cfgs) == ["pl1", "pl2"]
    assert cfg.get_pdk_cfg("pdk1").get_corners() == ["typ", "my-fast_1"]
    _assert_every_valid_key_is_used(
        [PDK_ALL],
        PDK_SPEC,
        extra=[([PLATFORM_ONE_CORNER, PLATFORM_CORNERS], PNR_PLATFORM_SPEC)],
    )
    _assert_every_valid_key_is_used(
        [PLATFORM_ONE_CORNER, PLATFORM_CORNERS],
        PNR_PLATFORM_SPEC,
        extra=[([PDK_ALL], PDK_SPEC)],
    )


def test_entries_built_from_anchors_and_merge_keys_load(minimal_project: Path):
    cfg = minimal_project / "root_config.yaml"
    cfg.write_text(
        cfg.read_text()
        + """
cfg-pdks:
  - &base
    name: pdk1
    corners: {typ: lib/typ.lib}
    placement: {density: 0.6}
  - <<: *base
    name: pdk2
cfg-pnr-platforms:
  - &plat {name: pl1, pdk: pdk1, corner: typ, routing-layers: {signal: m1-m5}}
  - <<: *plat
    name: pl2
"""
    )

    loaded = RootConfig(name="merge")

    assert sorted(loaded.pdk_cfgs) == ["pdk1", "pdk2"]
    assert sorted(loaded.pnr_platform_cfgs) == ["pl1", "pl2"]


@pytest.mark.parametrize(
    ("which", "block_path", "where", "valid"),
    [
        (
            "pdk",
            (),
            "cfg-pdks 'pdk1'",
            "name, site, corners, tech-lef, macro-lef, cell-gds, klayout-tech, "
            "klayout-props, tie-hi, tie-lo, fill-cells, pin-layers, placement, "
            "dont-use-cells, pdn-config, rcx-rules, tracks-tcl, layer-rc-tcl, "
            "tapcell-tcl, platform-tcl",
        ),
        (
            "pdk",
            ("pin-layers",),
            "cfg-pdks 'pdk1': pin-layers",
            "horizontal, vertical",
        ),
        (
            "pdk",
            ("placement",),
            "cfg-pdks 'pdk1': placement",
            "density, padding, macro-halo, macro-cell-halo, tie-separation, "
            "reference-hpwl, routability-driven, routability-use-grt, timing-driven, "
            "rotated-macro-footprint, macro-pin-limits",
        ),
        (
            "pdk",
            ("placement", "macro-pin-limits"),
            "cfg-pdks 'pdk1': placement.macro-pin-limits",
            "buffers, margin, capacitance-margin",
        ),
        (
            "platform",
            (),
            "cfg-pnr-platforms 'pl1'",
            "name, pdk, corner, corners, cts-buffer, cts-sink-clustering, "
            "cts-latency-balancing, cts-repair-clock-nets, "
            "post-cts-setup-repair, global-route-hold-repair, global-route-repair, "
            "post-route-repair, routing-layer-adjustment, cts-apply-ndr, "
            "port-buffer, max-fanout, routing-layers, placement, dont-use-cells, "
            "cts-dont-use-cells, keep-sizing-vt, tapcell-tcl",
        ),
        (
            "platform",
            ("global-route-repair", "repair-design"),
            "cfg-pnr-platforms 'pl1': global-route-repair.repair-design",
            "slew-margin, cap-margin, max-wire-length",
        ),
        (
            "platform",
            ("routing-layers",),
            "cfg-pnr-platforms 'pl1': routing-layers",
            "signal, clock",
        ),
        (
            "platform",
            ("placement",),
            "cfg-pnr-platforms 'pl1': placement",
            "density, padding, macro-halo, macro-cell-halo, tie-separation, "
            "reference-hpwl, routability-driven, routability-use-grt, timing-driven, "
            "rotated-macro-footprint, macro-pin-limits",
        ),
        (
            "platform",
            ("placement", "macro-pin-limits"),
            "cfg-pnr-platforms 'pl1': placement.macro-pin-limits",
            "buffers, margin, capacitance-margin",
        ),
    ],
)
def test_an_unknown_key_in_a_pdk_or_platform_names_the_key_its_place_and_the_valid_keys(
    minimal_project: Path, which, block_path, where, valid
):
    pdk, platform = copy.deepcopy(PDK_ALL), copy.deepcopy(PLATFORM_ONE_CORNER)
    block = pdk if which == "pdk" else platform
    for step in block_path:
        block = block[step]
    block["zzz"] = 1
    _with_root_blocks(minimal_project, [pdk], [platform])

    with pytest.raises(FatalRtlBuddyError) as exc:
        RootConfig(name="unknown")

    assert str(exc.value) == (
        f"{minimal_project / 'root_config.yaml'}: {where}: unknown key 'zzz' "
        f"(valid keys: {valid})"
    )
    assert exc.value.__cause__ is None


def test_a_misspelt_platform_key_names_the_nearest_valid_one(minimal_project: Path):
    platform = {**PLATFORM_CORNERS, "cts_buffer": "b1"}
    _with_root_blocks(minimal_project, [PDK_ALL], [platform])

    with pytest.raises(
        FatalRtlBuddyError,
        match=r"cfg-pnr-platforms 'pl2': unknown key 'cts_buffer', "
        r"did you mean 'cts-buffer'\?",
    ):
        RootConfig(name="typo")


def test_unknown_keys_in_pdks_and_platforms_are_reported_together(
    minimal_project: Path,
):
    pdk = {**PDK_ALL, "dont_use": ["x"]}
    platform = {**PLATFORM_CORNERS, "routing-layers": {"sginal": "m1-m2"}}
    _with_root_blocks(minimal_project, [pdk], [platform])

    with pytest.raises(FatalRtlBuddyError) as exc:
        RootConfig(name="both")

    lines = str(exc.value).splitlines()
    assert len(lines) == 2
    assert "cfg-pdks 'pdk1': unknown key 'dont_use'" in lines[0]
    assert (
        "cfg-pnr-platforms 'pl2': routing-layers: unknown key 'sginal', "
        "did you mean 'signal'?"
    ) in lines[1]
