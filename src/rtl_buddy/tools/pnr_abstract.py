"""Hardened-block abstracts: the `harden:` outputs of a P&R run (#95).

A run with `harden: true` finishes as a block another design can instance
as a hard macro. Next to its routed result it publishes the three views a
parent run consumes and a manifest that says what they were made from:

    <artefact-dir>/abstract/<top>.lef        write_abstract_lef
    <artefact-dir>/abstract/<top>.lib        write_timing_model (OpenSTA)
    <artefact-dir>/abstract/<top>.gds        the run's strict stream-out
    <artefact-dir>/abstract/abstract.manifest.json

The directory is all-or-nothing. OpenROAD writes the LEF and the Liberty
into a staging directory, the GDS and the manifest are added there, and the
staging directory is renamed into place only once all four exist. A partial
abstract is worse than none: a parent would read two fresh views beside a
stale one and could not tell (#469).

The manifest follows the `{path, size, sha256}` shape of
`export.provenance.json` (#618), with project-relative paths, so a later
reader can re-fingerprint every recorded input and decide whether the
abstract still describes the block's sources.
"""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from ..config.blocks import BlockRef
from .artifact_paths import project_relative, project_root_or_none

#: Where a hardened run publishes its abstract, under its artefact dir.
ABSTRACT_DIR_NAME = "abstract"
#: Where OpenROAD writes the views before they are published. Never read by
#: a parent; cleared by every run.
ABSTRACT_STAGING_NAME = "abstract.partial"
ABSTRACT_MANIFEST_NAME = "abstract.manifest.json"
#: Bumped when the manifest changes shape incompatibly.
ABSTRACT_MANIFEST_SCHEMA = 1
#: The three views, by manifest key; each is `<top>.<key>`.
ABSTRACT_VIEWS = ("lef", "lib", "gds")


def abstract_dir(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, ABSTRACT_DIR_NAME)


def staging_dir(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, ABSTRACT_STAGING_NAME)


def view_path(directory: str, design: str, view: str) -> str:
    return os.path.join(directory, f"{design}.{view}")


def clear_abstract(artefact_dir: str) -> list[str]:
    """Remove the published abstract and any staging leftover.

    A rerun of the block replaces the result the abstract was cut from, so
    the abstract goes with the other outputs whether or not the rerun
    hardens again — the rule every other output of the run follows (#469).
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
    """The Tcl the flow runs after `write_db` when the run hardens.

    Written in the P&R session itself rather than a second OpenROAD
    invocation: the routed design, the Liberty set it was timed against and
    the post-CTS constraints (propagated clocks) are all still loaded, so
    the timing model is characterised against exactly what produced the
    result.

    `-bloat_occupied_layers` reports every layer the block routes on as
    blocked over its whole footprint. It is the conservative choice: a
    parent that routes into a gap in a block's own metal is the failure a
    hardened block exists to rule out.
    """
    staging = f"$OUT_DIR/{ABSTRACT_STAGING_NAME}"
    return (
        '\nputs ">>> Abstract views (harden)"\n'
        f"file mkdir {staging}\n"
        f"write_abstract_lef -bloat_occupied_layers {staging}/${{DESIGN}}.lef\n"
        f"write_timing_model {staging}/${{DESIGN}}.lib\n"
    )


def file_fingerprint(path: str | None, root: str | None) -> dict | None:
    """`{path, size, sha256}`, the path project-relative where it can be.

    A file that cannot be read is recorded with a null size and digest, so
    a later comparison sees it as changed rather than silently skipping it.
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
    """The source files a synthesis filelist names, as absolute paths.

    Bare entries and `-v` library files; option lines (`+incdir+`,
    `+define+`, `-y`, nested `-f`) name no single source. Paths resolve
    against the filelist's own directory, the rule `VlogFilelist` writes by.
    An unreadable filelist has no sources.
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


def abstract_config(pnr_cfg, platform) -> dict:
    """The settings, beyond its input files, a hardened result depends on.

    Recorded in the manifest so a parent can tell a block whose
    configuration was edited since it was hardened (#95): the floorplan,
    the platform's placement, routing and cell choices, and which files the
    run is configured to read — a `lib-paths` entry added to the block is a
    different block even though no recorded file changed. Built from the
    loaded configuration alone, so the parent's check computes it the same
    way without running anything. Paths are project-relative.
    """
    root = project_root_or_none(os.path.dirname(pnr_cfg.get_synth_suite_path()))

    def _rel(path):
        return project_relative(path, root) if root and path else path

    fp = pnr_cfg.get_floorplan()
    return {
        "platform": pnr_cfg.get_platform(),
        "synth": {
            "name": pnr_cfg.get_synth_name(),
            "path": _rel(pnr_cfg.get_synth_suite_path()),
        },
        "constraints": _rel(pnr_cfg.get_constraints()),
        "pin_constraints": _rel(pnr_cfg.pin_constraints),
        "lef_paths": [_rel(p) for p in pnr_cfg.get_lef_paths()],
        "lib_paths": [_rel(p) for p in pnr_cfg.get_lib_paths()],
        "gds_paths": [_rel(p) for p in pnr_cfg.get_gds_paths()],
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
        },
        "placement": {
            "density": platform.get_placement_density(),
            "padding": platform.get_placement_padding(),
            "macro_halo": platform.get_placement_macro_halo(),
        },
        "routing": {
            "signal_layers": platform.get_signal_layers(),
            "clock_layers": platform.get_clock_layers(),
        },
        "cts_buffers": list(platform.get_cts_buffers()),
        "dont_use_cells": list(platform.get_dont_use_cells()),
    }


def config_digest(config: dict) -> str:
    """A stable SHA-256 over a JSON-able config record."""
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
    """Write `abstract.manifest.json` into the staging directory.

    ``inputs`` maps a role to one absolute path or a list of them (`None`
    for an input the run does not have); every one is fingerprinted here.
    ``technology`` names the technology LEF and the corner Liberty the
    block was built on — what a parent must share with it — and is
    fingerprinted the same way.
    Output paths are recorded where they will be once published, not where
    they are being staged — a manifest naming the staging directory would
    describe a directory that no longer exists.
    """
    root = project_root_or_none(artefact_dir)
    published = abstract_dir(artefact_dir)

    def _fp(value):
        if isinstance(value, list):
            return [_fp(v) for v in value]
        return file_fingerprint(value, root)

    outputs = {}
    for view in ABSTRACT_VIEWS:
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


# ---------------------------------------------------------------------------
# Consuming abstracts: `blocks:` (#95)
# ---------------------------------------------------------------------------


class BlockResolutionError(Exception):
    """A `blocks:` entry that cannot be satisfied; the message says why."""


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

    def result_row(self) -> dict:
        """What the machine output says about one consumed block."""
        return {
            "name": self.ref.name,
            "pnr_run": self.ref.pnr_run,
            "pnr_path": self.ref.pnr_suite_path,
            "abstract_dir": self.abstract_dir,
            "manifest": self.manifest_path,
        }


def read_manifest(directory: str) -> dict | None:
    """The abstract manifest in ``directory``, or ``None`` if there is no
    readable one of a schema this rtl_buddy knows."""
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
    """Resolve one `blocks:` entry to its published abstract, or raise.

    Fails fast rather than re-running anything: a block with no abstract
    means its `harden: true` run has not been run (or last failed), and the
    fix is to run it, which the message says how to do.
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
    if not suite.get_runs(ref.pnr_run)[0].get_harden():
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
    )


def check_technology(
    block: ResolvedBlock, *, liberty: str | None, tech_lef: str | None
) -> None:
    """Raise unless the consumer shares the block's technology and corner.

    Compared by content, not by path or platform name: a block hardened on
    a block-level platform (its own PDN, its own routing layers) is meant
    to go into a top on the full platform, and the two share exactly the
    technology LEF and the corner Liberty. A different corner or process is
    a model that does not describe the block in this run.
    """
    recorded = block.manifest.get("technology") or {}
    for role, path, what in (
        ("liberty", liberty, "corner Liberty"),
        ("tech_lef", tech_lef, "technology LEF"),
    ):
        if path is None:
            continue
        expected = (recorded.get(role) or {}).get("sha256")
        if expected is None:
            # An abstract from before the manifest recorded it: nothing to
            # compare, so nothing vouches that it fits this run.
            raise BlockResolutionError(
                f"block {block.ref.name!r}: its abstract records no {what} — "
                f"re-run `rb pnr {block.ref.pnr_run} -c {block.ref.pnr_suite_path}`"
            )
        actual = (file_fingerprint(path, None) or {}).get("sha256")
        if actual != expected:
            raise BlockResolutionError(
                f"block {block.ref.name!r}: platform/corner mismatch — hardened "
                f"against {what} {(recorded.get(role) or {}).get('path')!r}, "
                f"this run uses {path!r}"
            )


def resolve_blocks(refs: list[BlockRef]) -> list[ResolvedBlock]:
    return [resolve_block(ref) for ref in refs]
