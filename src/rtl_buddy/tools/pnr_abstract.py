"""Hardened-block abstracts: the outputs of a P&R run with `harden: true`, and their use by `blocks:`.

A hardened run publishes `<artefact-dir>/abstract/` holding `<top>.lef`, `<top>.lib`, `<top>.gds` and `abstract.manifest.json`. The directory is published all at once, by renaming a staging directory, so a parent never reads a mix of fresh and stale views. The manifest records `{path, size, sha256}` for the inputs, outputs and configuration the abstract was built from, with project-relative paths.
"""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from ..config.blocks import BlockRef
from ..config.pdk import (
    DEFAULT_PLACEMENT_MACRO_CELL_HALO,
    DEFAULT_PLACEMENT_TIE_SEPARATION,
)
from ..config.pnr import MacroPlacement
from .artifact_paths import project_relative, project_root_or_none

ABSTRACT_DIR_NAME = "abstract"
#: OpenROAD writes the views here before `publish`; cleared by every run.
ABSTRACT_STAGING_NAME = "abstract.partial"
ABSTRACT_MANIFEST_NAME = "abstract.manifest.json"
#: Bumped on an incompatible manifest change.
ABSTRACT_MANIFEST_SCHEMA = 1
#: Manifest keys of the three views; each file is `<top>.<key>`.
ABSTRACT_VIEWS = ("lef", "lib", "gds")
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


def harden_tcl() -> str:
    """Return the Tcl that writes the LEF and timing model into the staging directory.

    It runs in the P&R session after `write_db`, so the timing model is characterised against the routed design and its propagated clocks. `-bloat_occupied_layers` marks every layer the block routes on as blocked over its whole footprint, so a parent never routes through the block's own metal.
    """
    staging = f"$OUT_DIR/{ABSTRACT_STAGING_NAME}"
    return (
        '\nputs ">>> Abstract views (harden)"\n'
        f"file mkdir {staging}\n"
        f"write_abstract_lef -bloat_occupied_layers {staging}/${{DESIGN}}.lef\n"
        f"write_timing_model {staging}/${{DESIGN}}.lib\n"
    )


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


def abstract_config(pnr_cfg, platform) -> dict:
    """Return the configuration a hardened result depends on, with project-relative paths.

    It covers the floorplan, the platform's placement, routing and cell settings, and the files the run is configured to read. It is built from loaded configuration alone, so a parent can recompute it without running anything.
    """
    root = project_root_or_none(os.path.dirname(pnr_cfg.get_synth_suite_path()))

    def _rel(path):
        return project_relative(path, root) if root and path else path

    fp = pnr_cfg.get_floorplan()
    hooks = dict(platform.get_pdk().get_tcl_hooks())
    return {
        "platform": pnr_cfg.get_platform(),
        "synth": {
            "name": pnr_cfg.get_synth_name(),
            "path": _rel(pnr_cfg.get_synth_suite_path()),
        },
        "constraints": _rel(pnr_cfg.get_constraints()),
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
            "macro_anchor": str(fp.macro_anchor),
            "blockages": [
                {
                    "rect": list(b.rect),
                    "type": str(b.type),
                    "max_density": b.max_density,
                }
                for b in fp.blockages
            ],
            # Emitted only when non-default so existing digests do not change.
            **(
                {"macro_placement": str(fp.macro_placement)}
                if fp.macro_placement is not MacroPlacement.PACK
                else {}
            ),
            **({"pins": [_pin_config(p) for p in fp.pins]} if fp.pins else {}),
            **({"macros": [_macro_config(m) for m in fp.macros]} if fp.macros else {}),
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
            "macro_halo": platform.get_placement_macro_halo(),
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
        },
        "cts_buffers": list(platform.get_cts_buffers()),
        "dont_use_cells": list(platform.get_dont_use_cells()),
        **(
            {"post_cts_setup_repair": True}
            if platform.get_post_cts_setup_repair() is True
            else {}
        ),
        **(
            {"global_route_hold_repair": True}
            if platform.get_global_route_hold_repair() is True
            else {}
        ),
        **(
            {"cts_apply_ndr": platform.get_cts_apply_ndr()}
            if isinstance(platform.get_cts_apply_ndr(), str)
            else {}
        ),
        **(
            {"max_fanout": platform.get_max_fanout()}
            if isinstance(platform.get_max_fanout(), int)
            else {}
        ),
        # Emitted only when on: abstracts hardened before `buffer-ports` existed have
        # unbuffered pins, so they go stale, and an explicit `false` keeps their digest.
        **({"buffer_ports": True} if pnr_cfg.get_buffer_ports() else {}),
        **(
            {"port_buffer": platform.get_port_buffer()}
            if pnr_cfg.get_buffer_ports() and platform.get_port_buffer()
            else {}
        ),
        # Emitted only when a PDK Tcl hook is set so existing digests do not change.
        **({"tcl_hooks": {k: _rel(v) for k, v in hooks.items()}} if hooks else {}),
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
) -> str:
    """Write `abstract.manifest.json` into the staging directory and return its path.

    `inputs` maps a role to an absolute path, a list of paths, or None. `technology` names the technology LEF and corner Liberty a parent must share; it is fingerprinted the same way. Output paths are recorded at their published location, not the staging one.
    """
    root = project_root_or_none(artefact_dir)
    published = abstract_dir(artefact_dir)

    def _fp(value):
        if isinstance(value, list):
            return [_fp(v) for v in value]
        return file_fingerprint(value, root)

    outputs = {}
    # The parameter record is optional: its probe can fail, and older abstracts have none.
    optional = [
        v for v in OPTIONAL_VIEWS if os.path.isfile(view_path(staging, design, v))
    ]
    for view in (*ABSTRACT_VIEWS, *optional):
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
    gds: str
    run_cfg: object = None
    changes: tuple[str, ...] = ()

    @property
    def stale(self) -> bool:
        return bool(self.changes)

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


def resolve_block(ref: BlockRef) -> ResolvedBlock:
    """Resolve one `blocks:` entry to its published abstract, or raise BlockResolutionError.

    Nothing is re-run; a missing abstract is reported with the `rb pnr` command that produces it.
    """
    from ..config.pnr import PnrSuiteConfig
    from ..errors import FatalRtlBuddyError

    where = f"block {ref.name!r} (pnr run {ref.pnr_run!r} in {ref.pnr_suite_path})"
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
    directory = abstract_dir(
        os.path.join(os.path.dirname(ref.pnr_suite_path), "artefacts", ref.pnr_run)
    )
    manifest_path = os.path.join(directory, ABSTRACT_MANIFEST_NAME)
    manifest = read_manifest(directory)
    if manifest is None:
        raise BlockResolutionError(
            f"{where}: no abstract at {directory} — run "
            f"`rb pnr {ref.pnr_run} -c {ref.pnr_suite_path}` first"
        )
    if manifest.get("block") != ref.name:
        raise BlockResolutionError(
            f"{where}: the abstract is of module {manifest.get('block')!r}, "
            f"not {ref.name!r}"
        )
    views = {view: view_path(directory, ref.name, view) for view in ABSTRACT_VIEWS}
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
        gds=views["gds"],
        run_cfg=run_cfg,
    )


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
                f"re-run `rb pnr {block.ref.pnr_run} -c {block.ref.pnr_suite_path}`"
            )
        actual = [(file_fingerprint(p, None) or {}).get("sha256") for p in paths]
        if actual != expected:
            hardened = [(r or {}).get("path") for r in records]
            raise BlockResolutionError(
                f"block {block.ref.name!r}: platform/corner mismatch — hardened "
                f"against {what} {one_or_many(hardened)!r}, "
                f"this run uses {one_or_many(list(paths))!r}"
            )


def resolve_blocks(refs: list[BlockRef]) -> list[ResolvedBlock]:
    return [resolve_block(ref) for ref in refs]


def _records(value):
    """Normalise a manifest input entry (one record, a list, or None) to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def block_changes(block: ResolvedBlock, root_cfg) -> list[str]:
    """Return what changed since the block was hardened; empty when current.

    Recorded inputs and the three published views are re-fingerprinted and compared by content, not timestamp. The block's configuration is rebuilt from its current `pnr.yaml` and platform and its digest compared.
    """
    root = project_root_or_none(block.abstract_dir)
    changes: list[str] = []

    def _compare(role: str, record: dict | None) -> None:
        if not record or not record.get("path"):
            return
        path = record["path"]
        on_disk = (
            path if os.path.isabs(path) or root is None else os.path.join(root, path)
        )
        now = (file_fingerprint(on_disk, None) or {}).get("sha256")
        if now is None:
            changes.append(f"{role} {path} is gone")
        elif now != record.get("sha256"):
            changes.append(f"{role} {path} changed")

    for role, value in (block.manifest.get("inputs") or {}).items():
        for record in _records(value):
            _compare(role, record)
    for view, record in (block.manifest.get("outputs") or {}).items():
        _compare(f"abstract {view}", record)

    recorded = block.manifest.get("config") or {}
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
            f"{more} — re-run `rb pnr {first.ref.pnr_run} -c "
            f"{first.ref.pnr_suite_path}`, or pass --accept-stale"
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
