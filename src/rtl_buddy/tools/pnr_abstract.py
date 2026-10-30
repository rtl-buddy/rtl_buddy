"""Hardened-block abstracts: the outputs of a P&R run with `harden: true`, and their use by `blocks:`.

A hardened run publishes `<artefact-dir>/abstract/` holding `<top>.lef`, `<top>.lib`, `<top>.gds` (left out with `abstract: {gds: false}`) and `abstract.manifest.json`. The directory is published all at once, by renaming a staging directory, so a parent never reads a mix of fresh and stale views. The manifest records `{path, size, sha256}` for the inputs, outputs and configuration the abstract was built from, with project-relative paths.
"""

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from importlib.metadata import version
from pathlib import Path

from ..config.blocks import BlockRef
from ..config.pdk import (
    DEFAULT_PLACEMENT_MACRO_CELL_HALO,
    DEFAULT_ROTATED_MACRO_FOOTPRINT,
    DEFAULT_PLACEMENT_TIE_SEPARATION,
    MacroPinLimitsFile,
)
from ..config.pnr_cts import CtsSinkClustering
from ..config.pnr_repair import GlobalRouteRepair
from ..config.pnr_signoff import effective_gates
from ..config.pnr import (
    ABSTRACT_BLOAT_OCCUPIED,
    BlockageLifetime,
    PnrAbstract,
    PortBuffers,
)
from .artifact_paths import project_relative, project_root_or_none, run_artifact_root

ABSTRACT_DIR_NAME = "abstract"
#: OpenROAD writes the views here before `publish`; cleared by every run.
ABSTRACT_STAGING_NAME = "abstract.partial"
ABSTRACT_MANIFEST_NAME = "abstract.manifest.json"
#: In a copy of an abstract (see `pnr_tag_inputs`): the abstract it was copied from, whose project root its manifest's relative paths are against.
COPY_SOURCE_NAME = ".rb-copy-source.json"
#: Bumped on an incompatible manifest change.
ABSTRACT_MANIFEST_SCHEMA = 1
#: Enters every config digest; bumped when the flow's own steps or defaults change what a hardened block's views hold, so every abstract hardened before goes stale.
ABSTRACT_FLOW_GENERATION = 8
#: Manifest keys of the three views; each file is `<top>.<key>`.
ABSTRACT_VIEWS = ("lef", "lib", "gds")
#: The real-shape LEF a run with `abstract.feedthrough-layers` writes beside `<top>.lef`; spliced in and removed before publishing.
FEEDTHROUGH_VIEW = "feedthrough.lef"
#: Views published when they could be produced: `<top>.params.json`, the parameter values the top was elaborated with (see `block_params`).
OPTIONAL_VIEWS = ("params.json",)


def abstract_dir(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, ABSTRACT_DIR_NAME)


def staging_dir(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, ABSTRACT_STAGING_NAME)


def view_path(directory: str, design: str, view: str) -> str:
    return os.path.join(directory, f"{design}.{view}")


def clear_abstract(artefact_dir: str) -> list[str]:
    """Remove the published abstract and any staging leftover, returning the removed paths.

    Every `rb pnr` run calls this, including runs that do not harden.
    """
    removed = []
    for path in (abstract_dir(artefact_dir), staging_dir(artefact_dir)):
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed.append(path)
    return removed


def _bloat_flag(bloat) -> str:
    """The `write_abstract_lef` option for an `abstract.bloat` value."""
    if bloat == ABSTRACT_BLOAT_OCCUPIED:
        return "-bloat_occupied_layers"
    return f"-bloat_factor {float(bloat):g}"


def harden_tcl(abstract: PnrAbstract | None = None) -> str:
    """Return the Tcl that writes the LEF and timing model into the staging directory.

    It runs in the P&R session after `write_db`, so the timing model is characterised against the routed design and its propagated clocks. By default `-bloat_occupied_layers` marks every layer the block routes on as blocked over its whole footprint, so a parent never routes through the block's own metal; `abstract.bloat` can name a `-bloat_factor` instead. With `abstract.feedthrough-layers`, a second LEF is written with `-bloat_factor 0`, the block's real shapes, for :func:`splice_feedthrough_obs` (#768).
    """
    abstract = abstract or PnrAbstract()
    staging = f"$OUT_DIR/{ABSTRACT_STAGING_NAME}"
    feedthrough = (
        f"write_abstract_lef -bloat_factor 0 {staging}/${{DESIGN}}.{FEEDTHROUGH_VIEW}\n"
        if abstract.feedthrough_layers
        else ""
    )
    return (
        '\nputs ">>> Abstract views (harden)"\n'
        f"file mkdir {staging}\n"
        f"write_abstract_lef {_bloat_flag(abstract.bloat)} {staging}/${{DESIGN}}.lef\n"
        f"{feedthrough}"
        f"write_timing_model {staging}/${{DESIGN}}.lib\n"
    )


def _obs_groups(
    lines: list[str],
) -> tuple[int, int, list[tuple[str, list[str]]]] | None:
    """Return the `OBS` section of a one-macro LEF as `(first, last, [(layer, lines)])`, or None without one.

    `first` and `last` index the `OBS` and closing `END` lines. Each group is a `LAYER` statement and the geometry lines under it.
    """
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "OBS"), None)
    if start is None:
        return None
    groups: list[tuple[str, list[str]]] = []
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "END":
            return start, i, groups
        words = stripped.split()
        if words and words[0] == "LAYER" and len(words) > 1:
            groups.append((words[1].rstrip(";"), [lines[i]]))
        elif groups:
            groups[-1][1].append(lines[i])
    return None


def splice_feedthrough_obs(
    lef_path: str, feedthrough_path: str, layers: tuple[str, ...]
) -> list[str]:
    """Rewrite `lef_path`'s obstructions on `layers` with the real shapes from `feedthrough_path`, then remove that file.

    Every other layer keeps `lef_path`'s obstruction, which `abstract.bloat` chose. A feedthrough layer the block does not use ends up with no obstruction. Returns the `layers` found in neither LEF, which are most likely misspelt.
    """
    base = Path(lef_path).read_text().splitlines()
    shapes = Path(feedthrough_path).read_text().splitlines()
    base_obs = _obs_groups(base)
    shape_obs = _obs_groups(shapes)
    wanted = set(layers)
    real = {name: body for name, body in (shape_obs[2] if shape_obs else [])}
    seen = set(real)
    if base_obs is not None:
        first, last, groups = base_obs
        seen |= {name for name, _ in groups}
        body: list[str] = []
        placed: set[str] = set()
        for name, group in groups:
            if name in wanted:
                body.extend(real.get(name, []))
                placed.add(name)
            else:
                body.extend(group)
        for name, group in real.items():
            if name in wanted and name not in placed:
                body.extend(group)
        base = base[: first + 1] + body + base[last:]
        Path(lef_path).write_text("\n".join(base) + "\n")
    os.unlink(feedthrough_path)
    return [name for name in layers if name not in seen]


def file_fingerprint(path: str | None, root: str | None) -> dict | None:
    """Return `{path, size, sha256}` for a file, or None without a path.

    The path is project-relative when `root` is given. An unreadable file gets a null size and digest, which later compares as changed.
    """
    if not path:
        return None
    digest = hashlib.sha256()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        size, sha = None, None
    else:
        sha = digest.hexdigest()
    shown = project_relative(path, root) if root else path
    return {"path": shown, "size": size, "sha256": sha}


def filelist_sources(filelist: str) -> list[str]:
    """Return the absolute paths of the sources a synthesis filelist names.

    Bare entries and `-v` files count; option lines (`+incdir+`, `+define+`, `-y`, `-f`) do not. Paths resolve against the filelist's directory. An unreadable filelist has none.
    """
    base = os.path.dirname(os.path.abspath(filelist))
    skip = ("+incdir+", "+libext+", "+define+", "-y ", "-F ", "-f ")
    sources = []
    try:
        with open(filelist) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith(skip):
            continue
        if line.startswith("-v "):
            line = line[3:].strip()
        sources.append(os.path.normpath(os.path.join(base, line)))
    return sources


def _pin_config(pin) -> dict:
    """One `floorplan.pins` entry for the config digest, without its unset keys."""
    entry = {
        "names": list(pin.names),
        "side": str(pin.side) if pin.side is not None else None,
        "start": pin.start,
        "end": pin.end,
        "group": pin.group or None,
        "order": pin.order or None,
        "location": list(pin.location) if pin.location is not None else None,
        "layer": pin.layer,
        "size": list(pin.size) if pin.size is not None else None,
    }
    return {key: value for key, value in entry.items() if value is not None}


def _macro_config(macro) -> dict:
    """One `floorplan.macros` entry for the config digest, without its unset keys."""
    entry = {
        "instance": macro.instance,
        "location": list(macro.location) if macro.location is not None else None,
        "orientation": macro.orientation,
        "halo": list(macro.halo) if macro.halo is not None else None,
    }
    return {key: value for key, value in entry.items() if value is not None}


def _pdn_config(pdn) -> dict:
    """A run's `pdn:` block for the config digest."""
    stripes = []
    for stripe in pdn.stripes:
        entry = {
            "layer": stripe.layer,
            "width": stripe.width,
            "pitch": stripe.pitch,
            "offset": stripe.offset,
            "spacing": stripe.spacing,
            "followpins": stripe.followpins or None,
        }
        stripes.append({k: v for k, v in entry.items() if v is not None})
    ring = pdn.ring
    return {
        **(
            {
                "ring": {
                    "layers": list(ring.layers),
                    "width": ring.width,
                    "spacing": ring.spacing,
                    "offset": ring.offset,
                }
            }
            if ring is not None
            else {}
        ),
        "stripes": stripes,
        "connect": [list(pair) for pair in pdn.connect],
    }


def _cts_controls_digest(platform) -> dict:
    """The CTS controls of `cts-repair-clock-nets`, `cts-sink-clustering`, `cts-root-buffer` and `cts-obstruction-aware`, each emitted only when set or non-default so existing digests do not change."""
    digest: dict = {}
    if platform.get_cts_repair_clock_nets() is True:
        max_wire_length = platform.get_cts_repair_max_wire_length()
        digest["cts_repair_clock_nets"] = (
            True if max_wire_length is None else {"max_wire_length": max_wire_length}
        )
    clustering = CtsSinkClustering(
        platform.get_cts_sink_clustering(),
        platform.get_cts_sink_clustering_size(),
        platform.get_cts_sink_clustering_max_diameter(),
    ).digest()
    if clustering is not None:
        digest["cts_sink_clustering"] = clustering
    if platform.get_cts_root_buffer() is not None:
        digest["cts_root_buffer"] = platform.get_cts_root_buffer()
    if platform.get_cts_obstruction_aware() is not None:
        digest["cts_obstruction_aware"] = platform.get_cts_obstruction_aware()
    if platform.get_cts_repair_clock_inverters() is False:
        digest["cts_repair_clock_inverters"] = False
    return digest


def _flow_steps_digest(platform) -> dict:
    """The ORFS steps rb runs by default, each emitted only when off, its non-default."""
    return {
        key: False
        for key, on in (
            ("floorplan_setup_repair", platform.get_floorplan_setup_repair()),
            ("global_route_antenna_repair", platform.get_global_route_antenna_repair()),
            (
                "detailed_route_antenna_repair",
                platform.get_detailed_route_antenna_repair(),
            ),
        )
        if on is False
    }


def _repair_digest(platform) -> dict:
    """The timing repairs after global and detailed route, each emitted only when set so existing digests do not change.

    A hold-only global-route repair with no margin is what `global-route-hold-repair: true` always ran, so it keeps that key.
    """
    digest = {}
    gr = platform.get_global_route_repair()
    if gr == GlobalRouteRepair(hold_margin=0.0):
        digest["global_route_hold_repair"] = True
    elif gr is not None:
        digest["global_route_repair"] = _jsonable(asdict(gr))
    pr = platform.get_post_route_repair()
    if pr is not None:
        digest["post_route_repair"] = _jsonable(asdict(pr))
    for block in digest.values():
        design = block.get("repair_design") if isinstance(block, dict) else None
        if design:
            # Emitted only when set so existing digests do not change.
            if design["wire_rc"] is None:
                del design["wire_rc"]
            else:
                design["wire_rc"] = {
                    k: v for k, v in design["wire_rc"].items() if v is not None
                }
            if not design["after_timing"]:
                del design["after_timing"]
    post_route = digest.get("post_route_repair")
    if post_route is not None:
        electrical = post_route.pop("electrical_repair")
        if electrical is not None:
            post_route["electrical_repair"] = {
                "slew_margin": electrical["slew_margin"],
                "cap_margin": electrical["cap_margin"],
            }
    return digest


def _jsonable(value):
    """`value` with every enum as its string, for a digest."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, Enum):
        return value.value
    return value


def abstract_config(pnr_cfg, platform) -> dict:
    """Return the configuration a hardened result depends on, with project-relative paths.

    It covers the floorplan, the platform's placement, routing and cell settings, and the files the run is configured to read. It is built from loaded configuration alone, so a parent can recompute it without running anything.
    """
    root = project_root_or_none(os.path.dirname(pnr_cfg.get_synth_suite_path()))

    def _rel(path):
        return project_relative(path, root) if root and path else path

    # A one-corner mapping means its one SDC, as a plain path does.
    constraints = pnr_cfg.get_constraints()
    if isinstance(constraints, dict):
        constraints = (
            _rel(next(iter(constraints.values())))
            if len(constraints) == 1
            else {c: _rel(p) for c, p in constraints.items()}
        )
    else:
        constraints = _rel(constraints)
    fp = pnr_cfg.get_floorplan()
    platform = platform.with_run_placement(pnr_cfg.get_placement(), pnr_cfg.get_name())
    platform = platform.with_run_repairs(
        pnr_cfg.global_route_repair, pnr_cfg.post_route_repair, pnr_cfg.get_name()
    )
    platform = platform.with_run_overrides(pnr_cfg.get_overrides())
    hooks = platform.get_tcl_hooks()
    return {
        "flow": ABSTRACT_FLOW_GENERATION,
        "platform": pnr_cfg.get_platform(),
        "synth": {
            "name": pnr_cfg.get_synth_name(),
            "path": _rel(pnr_cfg.get_synth_suite_path()),
        },
        "constraints": constraints,
        "pin_constraints": _rel(pnr_cfg.pin_constraints),
        # Emitted only when set so existing digests do not change.
        **(
            {"pdn_config": _rel(pnr_cfg.get_pdn_config())}
            if pnr_cfg.get_pdn_config()
            else {}
        ),
        **({"pdn": _pdn_config(pnr_cfg.get_pdn())} if pnr_cfg.get_pdn() else {}),
        "lef_paths": [_rel(p) for p in pnr_cfg.get_lef_paths()],
        "lib_paths": [_rel(p) for p in pnr_cfg.get_lib_paths()],
        "gds_paths": [_rel(p) for p in pnr_cfg.get_gds_paths()],
        "blocks": [
            {"name": b.name, "pnr": b.pnr_run, "pnr_path": _rel(b.pnr_suite_path)}
            for b in pnr_cfg.get_blocks()
        ],
        "floorplan": {
            "utilization": fp.utilization,
            "aspect": fp.aspect,
            "core_margin": fp.core_margin,
            "blockages": [
                {
                    "rect": list(b.rect),
                    "type": str(b.type),
                    "max_density": b.max_density,
                    # Emitted only when non-default so existing digests do not change.
                    **(
                        {"lifetime": str(b.lifetime)}
                        if b.lifetime is not BlockageLifetime.FLOW
                        else {}
                    ),
                }
                for b in fp.blockages
            ],
            **({"pins": [_pin_config(p) for p in fp.pins]} if fp.pins else {}),
            **({"macros": [_macro_config(m) for m in fp.macros]} if fp.macros else {}),
            # Planner files by path; their contents are fingerprinted as inputs.
            **({"macros_file": _rel(fp.macros_file)} if fp.macros_file else {}),
            **({"macro_snap": False} if not fp.macro_snap else {}),
            **({"pins_file": _rel(fp.pins_file)} if fp.pins_file else {}),
            **({"pin_snap": False} if not fp.pin_snap else {}),
            **({"pins_complete": True} if fp.pins_complete else {}),
            **(
                {"die_area": list(fp.die_area), "core_area": list(fp.core_area)}
                if fp.die_area is not None
                else {}
            ),
            **(
                {"core_cutouts": [list(c) for c in fp.core_cutouts]}
                if fp.core_cutouts
                else {}
            ),
        },
        "placement": {
            "density": platform.get_placement_density(),
            "padding": platform.get_placement_padding(),
            # RTL-MP's `-target_util` is the density above; its channel and pin-aware flag count only when a channel is set.
            **(
                {
                    "macro_channel": channel,
                    "macro_pin_aware_channels": (
                        platform.get_placement_macro_pin_aware_channels()
                    ),
                }
                if (channel := platform.get_placement_macro_channel()) is not None
                else {}
            ),
            # Emitted only when non-default so existing digests do not change.
            **(
                {"macro_cell_halo": platform.get_placement_macro_cell_halo()}
                if platform.get_placement_macro_cell_halo()
                != DEFAULT_PLACEMENT_MACRO_CELL_HALO
                else {}
            ),
            **(
                {"tie_separation": platform.get_placement_tie_separation()}
                if platform.get_placement_tie_separation()
                != DEFAULT_PLACEMENT_TIE_SEPARATION
                else {}
            ),
            **(
                {"reference_hpwl": platform.get_placement_reference_hpwl()}
                if isinstance(platform.get_placement_reference_hpwl(), float)
                else {}
            ),
            **(
                {"routability_driven": True}
                if platform.get_placement_routability_driven() is True
                else {}
            ),
            **(
                {"routability_use_grt": True}
                if platform.get_placement_routability_use_grt() is True
                else {}
            ),
            **(
                {"timing_driven": True}
                if platform.get_placement_timing_driven() is True
                else {}
            ),
            **(
                {
                    "rotated_macro_footprint": str(
                        platform.get_placement_rotated_macro_footprint()
                    )
                }
                if platform.get_placement_rotated_macro_footprint()
                != DEFAULT_ROTATED_MACRO_FOOTPRINT
                else {}
            ),
            **(
                {"improve_placement": False}
                if platform.get_placement_improve_placement() is False
                else {}
            ),
            **(
                {"optimize_mirroring": False}
                if platform.get_placement_optimize_mirroring() is False
                else {}
            ),
            **(
                {
                    "macro_pin_limits": {
                        "buffers": list(macro_pin_limits.buffers),
                        "margin": macro_pin_limits.margin,
                        "capacitance_margin": macro_pin_limits.capacitance_margin,
                    }
                }
                if isinstance(
                    macro_pin_limits := platform.get_placement_macro_pin_limits(),
                    MacroPinLimitsFile,
                )
                else {}
            ),
            **(
                {"repair_design": options}
                if (options := platform.get_placement_repair_design().option_values())
                else {}
            ),
        },
        "routing": {
            "signal_layers": platform.get_signal_layers(),
            "clock_layers": platform.get_clock_layers(),
            # Emitted only when set so existing digests do not change.
            **(
                {"layer_adjustment": platform.get_routing_layer_adjustment()}
                if isinstance(platform.get_routing_layer_adjustment(), float)
                else {}
            ),
            **(
                {"global_route": digest}
                if (digest := platform.get_global_route().digest())
                else {}
            ),
        },
        "cts_buffers": list(platform.get_cts_buffers()),
        "dont_use_cells": list(platform.get_dont_use_cells()),
        # Emitted only when off, its non-default.
        **(
            {"post_cts_setup_repair": False}
            if platform.get_post_cts_setup_repair() is False
            else {}
        ),
        **_repair_digest(platform),
        **_flow_steps_digest(platform),
        # Emitted only when set so existing digests do not change: the corners the post-route rounds judge each side at decide the round kept. `fail_on` is the effective gates, so equivalent configs share a digest.
        **(
            {
                "signoff": {
                    "setup_corners": list(signoff.setup_corners),
                    "hold_corners": list(signoff.hold_corners),
                    "fail_on": list(
                        effective_gates(signoff, pnr_cfg.get_fail_on_electrical())
                    ),
                }
            }
            if (signoff := platform.get_signoff()) is not None
            else {}
        ),
        # The CTS knobs below are emitted only when set so existing digests do not change.
        **(
            {"cts_clock_nets": list(platform.get_cts_clock_nets())}
            if platform.get_cts_clock_nets()
            else {}
        ),
        **(
            {"cts_latency_balancing": False}
            if platform.get_cts_latency_balancing() is False
            else {}
        ),
        **_cts_controls_digest(platform),
        **(
            {"cts_apply_ndr": platform.get_cts_apply_ndr()}
            if isinstance(platform.get_cts_apply_ndr(), str)
            else {}
        ),
        # The threshold-voltage keys, likewise only when set.
        **(
            {"cts_dont_use_cells": cts_dont_use}
            if (cts_dont_use := list(platform.get_cts_dont_use_cells()))
            else {}
        ),
        **({"keep_sizing_vt": True} if platform.get_keep_sizing_vt() is True else {}),
        **(
            {"match_cell_footprint": True}
            if platform.get_match_cell_footprint() is True
            else {}
        ),
        **(
            {"max_fanout": platform.get_max_fanout()}
            if isinstance(platform.get_max_fanout(), int)
            else {}
        ),
        # Emitted only when on: abstracts hardened before `buffer-ports` existed have
        # unbuffered pins, so they go stale, and an explicit `false` keeps their digest.
        # Every port with the defaults is `true`, so a mapping that writes them keeps it.
        **(
            {"buffer_ports": True if ports == PortBuffers() else asdict(ports)}
            if (ports := pnr_cfg.get_port_buffers())
            else {}
        ),
        **(
            {"port_buffer": platform.get_port_buffer()}
            if pnr_cfg.get_buffer_ports() and platform.get_port_buffer()
            else {}
        ),
        # Emitted only when a Tcl hook is set so existing digests do not change; `tapcell-tcl` is the run's, the platform's or the PDK's.
        **({"tcl_hooks": {k: _rel(v) for k, v in hooks.items()}} if hooks else {}),
        # Emitted only when a run hook is set so existing digests do not change.
        **(
            {"hooks": {k: [_rel(p) for p in v] for k, v in pnr_cfg.get_hooks().items()}}
            if pnr_cfg.get_hooks()
            else {}
        ),
        # Emitted only when `inputs:` is set so existing digests do not change.
        **(
            {"inputs": [_rel(p) for p in pnr_cfg.get_inputs()]}
            if pnr_cfg.get_inputs()
            else {}
        ),
        # Emitted only when non-default so existing digests do not change; a change regenerates.
        **(
            {
                "abstract": {
                    "bloat": pnr_cfg.get_abstract().bloat,
                    "feedthrough_layers": list(
                        pnr_cfg.get_abstract().feedthrough_layers
                    ),
                    **({"gds": False} if not pnr_cfg.get_abstract().gds else {}),
                }
            }
            if not pnr_cfg.get_abstract().is_default()
            else {}
        ),
    }


def config_digest(config: dict) -> str:
    """Return a stable SHA-256 of a JSON-serialisable config record."""
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def write_manifest(
    staging: str,
    *,
    artefact_dir: str,
    design: str,
    run: str,
    platform: str,
    pdk: str,
    openroad: dict,
    technology: dict,
    inputs: dict,
    config: dict,
    gds: bool = True,
    blocks: list | None = None,
) -> str:
    """Write `abstract.manifest.json` into the staging directory and return its path.

    `inputs` maps a role to an absolute path, a list of paths, or None. `technology` names the technology LEF and corner Liberty a parent must share; it is fingerprinted the same way. Output paths are recorded at their published location, not the staging one. Without `gds` the GDS output is recorded as null. `blocks` are the `ResolvedBlock`s the run consumed; each is recorded with the tag its abstract came from and the abstract manifest's digest.
    """
    root = project_root_or_none(artefact_dir)
    published = abstract_dir(artefact_dir)

    def _fp(value):
        if isinstance(value, list):
            return [_fp(v) for v in value]
        if isinstance(value, dict):
            # A fingerprinted record (the netlist) as is; a per-corner `constraints:` mapping corner by corner.
            if "sha256" in value:
                return value
            return {key: _fp(v) for key, v in value.items()}
        return file_fingerprint(value, root)

    outputs = {}
    # The parameter record is optional: its probe can fail, and older abstracts have none.
    optional = [
        v for v in OPTIONAL_VIEWS if os.path.isfile(view_path(staging, design, v))
    ]
    for view in (*ABSTRACT_VIEWS, *optional):
        if view == "gds" and not gds:
            outputs[view] = None
            continue
        record = file_fingerprint(view_path(staging, design, view), root) or {}
        record["path"] = (
            project_relative(view_path(published, design, view), root)
            if root
            else view_path(published, design, view)
        )
        outputs[view] = record

    document = {
        "schema_version": ABSTRACT_MANIFEST_SCHEMA,
        "generator": f"rtl-buddy {version('rtl-buddy')}",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "block": design,
        "pnr_run": run,
        "platform": platform,
        "pdk": pdk,
        "tool": {"name": "openroad", **openroad},
        "technology": {role: _fp(value) for role, value in technology.items()},
        "inputs": {role: _fp(value) for role, value in inputs.items()},
        "config": {**config, "sha256": config_digest(config)},
        "outputs": outputs,
        **({"blocks": [b.manifest_entry() for b in blocks]} if blocks else {}),
    }
    path = os.path.join(staging, ABSTRACT_MANIFEST_NAME)
    Path(path).write_text(json.dumps(document, indent=2) + "\n")
    return path


def publish(artefact_dir: str) -> str:
    """Move the staging directory into place as the run's abstract."""
    final = abstract_dir(artefact_dir)
    if os.path.lexists(final):
        shutil.rmtree(final)
    os.replace(staging_dir(artefact_dir), final)
    return final


class BlockResolutionError(Exception):
    """Raised when a `blocks:` entry cannot be satisfied; the message says why."""


@dataclass(frozen=True)
class ResolvedBlock:
    """A `blocks:` entry resolved to a published abstract on disk."""

    ref: BlockRef
    abstract_dir: str
    manifest_path: str
    manifest: dict
    lef: str
    lib: str
    #: None when the block was hardened with `abstract: {gds: false}`.
    gds: str | None
    run_cfg: object = None
    changes: tuple[str, ...] = ()
    #: The `--run-tag` whose block run published the abstract; None for the untagged one.
    run_tag: str | None = None
    manifest_sha256: str | None = None
    #: For a copy, the published abstract it was copied from; None when `abstract_dir` is the published one.
    source_dir: str | None = None

    @property
    def stale(self) -> bool:
        return bool(self.changes)

    def rerun_hint(self) -> str:
        return rerun_command(self.ref, self.run_tag)

    def manifest_entry(self) -> dict:
        """Return this block's entry in the consuming run's own abstract manifest."""
        return {
            "name": self.ref.name,
            "pnr": self.ref.pnr_run,
            "run_tag": self.run_tag,
            "manifest_sha256": self.manifest_sha256,
        }

    def result_row(self) -> dict:
        """Return the machine-output row for this block: abstract location, view fingerprints and staleness."""
        return {
            "name": self.ref.name,
            "pnr_run": self.ref.pnr_run,
            "pnr_path": self.ref.pnr_suite_path,
            "abstract_dir": self.abstract_dir,
            "manifest": self.manifest_path,
            "fingerprints": {
                view: (self.manifest.get("outputs") or {}).get(view)
                for view in ABSTRACT_VIEWS
            },
            "stale": self.stale,
            "changes": list(self.changes),
            "run_tag": self.run_tag,
            "manifest_sha256": self.manifest_sha256,
        }


def read_manifest(directory: str) -> dict | None:
    """Return the manifest in `directory`, or None if it is missing, unreadable or of an unknown schema."""
    try:
        data = json.loads(
            Path(directory, ABSTRACT_MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != ABSTRACT_MANIFEST_SCHEMA:
        return None
    return data


def has_gds(manifest: dict) -> bool:
    """Whether an abstract has a GDS: one hardened with `abstract: {gds: false}` records its GDS output as null."""
    outputs = manifest.get("outputs") or {}
    return not ("gds" in outputs and outputs["gds"] is None)


def rerun_command(ref: BlockRef, run_tag: str | None = None) -> str:
    """The `rb pnr` command that (re)publishes `ref`'s abstract, under `run_tag` when given."""
    tag = f" --run-tag {run_tag}" if run_tag else ""
    return f"`rb pnr {ref.pnr_run} -c {ref.pnr_suite_path}{tag}`"


def resolve_block(ref: BlockRef, *, source=None) -> ResolvedBlock:
    """Resolve one `blocks:` entry to its published abstract, or raise BlockResolutionError.

    Nothing is re-run; a missing abstract is reported with the `rb pnr` command that produces it. With a `source` (`pnr_tag_inputs.TagInputs`), the abstract read is the one it returns; for a saved run's export, the copy it records, whether or not the block run is still defined.
    """
    where = f"block {ref.name!r} (pnr run {ref.pnr_run!r} in {ref.pnr_suite_path})"
    pinned = source.pinned_abstract(ref, where) if source is not None else None
    try:
        run_cfg = _hardening_run_cfg(ref, where)
    except BlockResolutionError:
        # A saved run's copy needs no block run to define it now; only its staleness check does.
        if pinned is None:
            raise
        run_cfg = None
    if pinned is not None:
        directory, run_tag = pinned
    elif source is None:
        directory, run_tag = (
            abstract_dir(
                str(
                    run_artifact_root(os.path.dirname(ref.pnr_suite_path)) / ref.pnr_run
                )
            ),
            None,
        )
    else:
        directory, run_tag = source.abstract_dir(ref, where)
    manifest_path = os.path.join(directory, ABSTRACT_MANIFEST_NAME)
    manifest = read_manifest(directory)
    if manifest is None:
        raise BlockResolutionError(
            f"{where}: no abstract at {directory} — run "
            f"{rerun_command(ref, run_tag)} first"
        )
    if manifest.get("block") != ref.name:
        raise BlockResolutionError(
            f"{where}: the abstract is of module {manifest.get('block')!r}, "
            f"not {ref.name!r}"
        )
    views = {
        view: view_path(directory, ref.name, view)
        for view in ABSTRACT_VIEWS
        if view != "gds" or has_gds(manifest)
    }
    missing = [os.path.basename(p) for p in views.values() if not os.path.isfile(p)]
    if missing:
        raise BlockResolutionError(
            f"{where}: abstract at {directory} is missing {', '.join(missing)}"
        )
    return ResolvedBlock(
        ref=ref,
        abstract_dir=directory,
        manifest_path=manifest_path,
        manifest=manifest,
        lef=views["lef"],
        lib=views["lib"],
        gds=views.get("gds"),
        run_cfg=run_cfg,
        run_tag=run_tag,
        manifest_sha256=(file_fingerprint(manifest_path, None) or {}).get("sha256"),
        source_dir=copy_source(directory),
    )


def _hardening_run_cfg(ref: BlockRef, where: str):
    """Return the run config `ref` names, or raise BlockResolutionError when it is not a hardening run."""
    from ..config.pnr import PnrSuiteConfig
    from ..errors import FatalRtlBuddyError

    if not os.path.isfile(ref.pnr_suite_path):
        raise BlockResolutionError(f"{where}: pnr-path does not exist")
    try:
        suite = PnrSuiteConfig(ref.pnr_suite_path)
    except FatalRtlBuddyError as e:
        raise BlockResolutionError(f"{where}: {e}") from None
    if ref.pnr_run not in suite.get_run_names():
        raise BlockResolutionError(f"{where}: no such run in that pnr.yaml")
    run_cfg = suite.get_runs(ref.pnr_run)[0]
    if not run_cfg.get_harden():
        raise BlockResolutionError(
            f"{where}: that run does not set harden: true, so it publishes no abstract"
        )
    return run_cfg


def copy_source(directory: str) -> str | None:
    """Return the published abstract `directory` is a copy of, or None when it is not a copy."""
    try:
        record = json.loads(Path(directory, COPY_SOURCE_NAME).read_text())
    except (OSError, ValueError):
        return None
    source = record.get("abstract_dir") if isinstance(record, dict) else None
    return source if isinstance(source, str) else None


def one_or_many(paths: list[str]) -> str | list[str]:
    """Return a one-file list as its path, so a single-file corner records as it always has."""
    return paths[0] if len(paths) == 1 else list(paths)


def check_technology(
    block: ResolvedBlock, *, liberty: list[str] | None, tech_lef: str | None
) -> None:
    """Raise unless the consumer's technology LEF and corner Liberty files match the block's.

    Files are compared by content, in order, not by path or platform name, so a block built on a block-level platform can go into a top on the full platform.
    """
    recorded = block.manifest.get("technology") or {}
    for role, paths, what in (
        ("liberty", liberty, "corner Liberty"),
        ("tech_lef", None if tech_lef is None else [tech_lef], "technology LEF"),
    ):
        if paths is None:
            continue
        records = _records(recorded.get(role))
        expected = [(r or {}).get("sha256") for r in records]
        if not expected or None in expected:
            raise BlockResolutionError(
                f"block {block.ref.name!r}: its abstract records no {what} — "
                f"re-run {block.rerun_hint()}"
            )
        actual = [(file_fingerprint(p, None) or {}).get("sha256") for p in paths]
        if actual != expected:
            hardened = [(r or {}).get("path") for r in records]
            raise BlockResolutionError(
                f"block {block.ref.name!r}: platform/corner mismatch — hardened "
                f"against {what} {one_or_many(hardened)!r}, "
                f"this run uses {one_or_many(list(paths))!r}"
            )


def resolve_blocks(refs: list[BlockRef], *, source=None) -> list[ResolvedBlock]:
    return [resolve_block(ref, source=source) for ref in refs]


def _records(value):
    """Normalise a manifest input entry (one record, a list, a per-corner mapping of either, or None) to a list of records."""
    if value is None:
        return []
    if isinstance(value, list):
        return [r for v in value for r in _records(v)]
    if isinstance(value, dict) and "path" not in value:
        return [r for v in value.values() for r in _records(v)]
    return [value]


def block_changes(block: ResolvedBlock, root_cfg) -> list[str]:
    """Return what changed since the block was hardened; empty when current.

    Recorded inputs and the three published views are re-fingerprinted and compared by content, not timestamp. The block's configuration is rebuilt from its current `pnr.yaml` and platform and its digest compared.
    """
    # A copy's manifest records paths against the project of the abstract it was copied from.
    root = project_root_or_none(block.source_dir or block.abstract_dir)
    changes: list[str] = []

    def _compare(role: str, record: dict | None) -> None:
        if not record or not record.get("path"):
            return
        path = record["path"]
        on_disk = (
            path if os.path.isabs(path) or root is None else os.path.join(root, path)
        )
        path = record.get("shown", path)
        now = (file_fingerprint(on_disk, None) or {}).get("sha256")
        if now is None:
            changes.append(f"{role} {path} is gone")
        elif now != record.get("sha256"):
            changes.append(f"{role} {path} changed")

    for role, value in (block.manifest.get("inputs") or {}).items():
        for record in _records(value):
            _compare(role, record)
    design = block.manifest.get("block")
    for view, record in (block.manifest.get("outputs") or {}).items():
        if record and record.get("path"):
            # The views read, which for a `BlockSource` copy are not at the recorded path.
            record = {
                **record,
                "path": view_path(block.abstract_dir, design, view),
                "shown": record["path"],
            }
        _compare(f"abstract {view}", record)

    recorded = block.manifest.get("config") or {}
    if block.run_cfg is None:
        # A saved run's copy whose block run is gone from its pnr.yaml: no config to compare.
        return changes
    try:
        platform = root_cfg.get_pnr_platform_cfg(block.run_cfg.get_platform())
        current = abstract_config(block.run_cfg, platform)
        digest = config_digest(current)
    except Exception as e:  # an unloadable platform is itself a change
        changes.append(f"config cannot be rebuilt ({e})")
    else:
        if digest != recorded.get("sha256"):
            edited = sorted(
                key
                for key in set(current) | (set(recorded) - {"sha256"})
                if current.get(key) != recorded.get(key)
            )
            changes.append(f"config changed ({', '.join(edited) or 'digest'})")
    return changes


def assess_blocks(
    resolved: list[ResolvedBlock], root_cfg, *, accept_stale: bool
) -> list[ResolvedBlock]:
    """Attach each block's changes and raise on a stale block unless `accept_stale` is set.

    With `--accept-stale` the run proceeds, qualifies its result, and marks each stale block's row `stale: true`.
    """
    assessed = [replace(b, changes=tuple(block_changes(b, root_cfg))) for b in resolved]
    stale = [b for b in assessed if b.stale]
    if stale and not accept_stale:
        first = stale[0]
        more = f" (and {len(stale) - 1} more stale block(s))" if len(stale) > 1 else ""
        raise BlockResolutionError(
            f"block {first.ref.name!r} is stale: {describe_changes(list(first.changes))}"
            f"{more} — re-run {first.rerun_hint()}, or pass --accept-stale"
        )
    return assessed


def stale_qualifier(blocks: list[ResolvedBlock]) -> str:
    """Return the result qualifier naming accepted stale abstracts, or an empty string."""
    names = [b.ref.name for b in blocks if b.stale]
    if not names:
        return ""
    return f"stale block abstract(s) accepted: {', '.join(names)}"


def describe_changes(changes: list[str], limit: int = 3) -> str:
    shown = "; ".join(changes[:limit])
    if len(changes) > limit:
        shown += f"; +{len(changes) - limit} more"
    return shown
