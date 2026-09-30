# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``phys-manifest.json``: which physical artefacts a run produced, and where.

The file keeps these rules:

* Stable keys. The ``synth`` and ``power`` blocks and every key in them
  are always present. ``null`` means "not produced"; ``backend`` is
  ``null`` exactly when that half did not run here.
* Every path is POSIX and relative to the project root.
* One manifest per artefact directory, rewritten whole on each run. A
  synth run and a power run that share a directory merge: a run rewrites
  its own block and inherits the other's when the model kept that half
  (:func:`merge_manifest`). A rerun that fails before publishing
  withdraws its own block (:func:`blank_block`).
* The name differs from the coverage ``manifest.json`` so discovery, a
  filename match, cannot collide with it.
* Each write carries the ``publication`` token of the model written with
  it (:func:`rtl_buddy.phys.model.new_publication`).

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "publication": "9f2c…",           # shared with the model of this write
      "generator": "rtl-buddy 6.44.0",
      "generated_at": "2026-09-12T11:04:12+08:00",
      "command": "synth",               # or "power" — this write's producer
      "run": "demo_synth",              # the run name the artefact dir is keyed on
      "top": "demo_top",
      "phys_dir": "verif/demo/artefacts/demo_synth",
      "model": "verif/demo/artefacts/demo_synth/phys-model.json",
      "totals": {"area_um2": .., "cell_count": .., "internal_uw": ..,
                 "switching_uw": .., "leakage_uw": .., "total_uw": ..},
      "synth": {"backend": "yosys"|"openroad"|null, "run": .., "stats": ..,
                "netlist": .., "log": .., "config": {..}|null},
      "power": {"backend": "openroad"|null, "run": .., "netlist_source": ..,
                "netlist_path": .., "report": .., "instances": ..,
                "cells": .., "log": ..,
                "mode": "static"|"dynamic"|null, "activity": {..}|null,
                "config": {..}|null}
    }

``config``, ``mode`` and ``activity`` identify the run (shapes in
:mod:`rtl_buddy.phys.provenance`) and are copied from the model's
provenance so a run listing needs no model read. A manifest without them
reads as ``null``. Paths inside them are made project-relative by
:func:`rtl_buddy.phys.publish._publish`, once, for both documents.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Re-exported: the hub's `?dir=` route reads `may_follow_link` off this module.
from ..fs_walk import (
    artefact_layout_boundary,
    may_follow_link,  # noqa: F401 - re-export
    walk_unique,
)

#: Bumped when the manifest's shape changes incompatibly.
MANIFEST_SCHEMA_VERSION = 1

# Defined in `tools.artifact_paths`; re-exported for consumers.
from ..tools.artifact_paths import (  # noqa: E402
    PHYS_MANIFEST_NAME as MANIFEST_FILENAME,
    joins_back,
    project_relative,
    project_root_or_none,
)

# The model's block-to-totals pairing, shared so both merges agree.
from .model import _POWER_TOTALS, _SYNTH_TOTALS  # noqa: E402

#: Keys of the ``synth`` block.
SYNTH_KEYS = ("backend", "run", "stats", "netlist", "log", "config")

#: Keys of the ``power`` block.
POWER_KEYS = (
    "backend",
    "run",
    "netlist_source",
    # The run's own netlist copy; null for `netlist-source: pnr`.
    "netlist_path",
    "report",
    "instances",
    "cells",
    "log",
    "mode",
    "activity",
    "config",
)

#: Block keys that hold a path and so are made project-relative.
PATH_KEYS = frozenset(
    {"stats", "netlist", "log", "report", "instances", "cells", "netlist_path"}
)

#: Each producer block, its keys and the totals it owns; walked by
#: :func:`merge_manifest` and :func:`blank_block`.
_BLOCKS = (
    ("synth", SYNTH_KEYS, _SYNTH_TOTALS),
    ("power", POWER_KEYS, _POWER_TOTALS),
)


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def project_root_for_dir(artefact_dir) -> str:
    """The project root that an artefact directory's paths hang off.

    Walks up from ``artefact_dir`` on the logical path first, then on the
    resolved one, so an ``artefacts/`` symlinked to scratch still finds
    its project. Outside any project it answers with the directory itself,
    which makes every path a bare filename. Unlike
    :func:`rtl_buddy.config.root.discover_project_root` it does not log an
    error in that case. Use :func:`project_root_or_none` to tell "no
    project" apart.
    """
    return project_root_or_none(artefact_dir) or str(
        Path(os.path.abspath(artefact_dir))
    )


def build_manifest(
    *,
    project_root,
    phys_dir,
    command: str,
    run: str | None = None,
    top: str | None = None,
    model_path=None,
    totals: dict | None = None,
    synth: dict | None = None,
    power: dict | None = None,
) -> dict:
    """Assemble a manifest document with every path project-relative.

    Pass only the producer block this run wrote. The other is emitted with
    every key ``null``, so a consumer can read
    ``manifest["power"]["report"]`` without checking that a power run
    happened.
    """

    def rel(path):
        return project_relative(path, project_root)

    def block(values, keys):
        values = values or {}
        return {
            key: rel(values.get(key)) if key in PATH_KEYS else values.get(key)
            for key in keys
        }

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        # `phys.publish._publish` stamps the token it also puts on the model.
        "publication": None,
        "generator": _generator(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command,
        "run": run,
        "top": top,
        "phys_dir": rel(phys_dir),
        "model": rel(model_path),
        "totals": totals,
        "synth": block(synth, SYNTH_KEYS),
        "power": block(power, POWER_KEYS),
    }


def merge_manifest(existing: dict | None, new: dict, *, own_block: str | None) -> dict:
    """Carry the other half's block from ``existing`` onto ``new``.

    Mirrors :func:`rtl_buddy.phys.model.merge_model` and must stay in step
    with it. ``own_block`` (``"synth"`` or ``"power"``) is the block this
    run produced and is never inherited. The other block is inherited
    only when ``new`` left it empty (``backend`` null) and ``existing``
    describes the same top, with the totals that belong to it. A block
    this run produced keeps its own totals, nulls included.

    ``existing`` is ignored when it is not a dict or differs in
    ``schema_version`` or ``top``. The result keeps ``new``'s
    ``publication`` token.
    """
    if not isinstance(existing, dict):
        return new
    if existing.get("schema_version") != new.get("schema_version"):
        return new
    if existing.get("top") != new.get("top"):
        return new
    merged = dict(new)
    merged["totals"] = dict(new.get("totals") or {})
    for half, keys, totals_keys in _BLOCKS:
        if half == own_block:
            continue
        block = merged.get(half) or {}
        if block.get("backend") is not None:
            continue
        inherited = existing.get(half)
        if not isinstance(inherited, dict) or inherited.get("backend") is None:
            continue
        merged[half] = {key: inherited.get(key) for key in keys}
        for key in totals_keys:
            value = (existing.get("totals") or {}).get(key)
            if value is not None and merged["totals"].get(key) is None:
                merged["totals"][key] = value
    return merged


def blank_block(manifest: dict, own_block: str) -> dict:
    """``manifest`` with ``own_block``'s keys and totals set to ``null``.

    The manifest side of :func:`rtl_buddy.phys.model.blank_half`. The
    header (``command``, ``run``, ``generated_at``) is left as it was.
    """
    blanked = dict(manifest)
    blanked["totals"] = dict(manifest.get("totals") or {})
    for block, keys, totals_keys in _BLOCKS:
        if block != own_block:
            continue
        blanked[block] = {key: None for key in keys}
        for key in totals_keys:
            blanked["totals"][key] = None
    return blanked


def write_manifest(manifest: dict, phys_dir) -> str:
    """Write ``phys-manifest.json`` into ``phys_dir`` and return its path.

    Written through a temp file and :func:`os.replace`, so a reader never
    sees a partial file.
    """
    phys_dir = Path(phys_dir)
    phys_dir.mkdir(parents=True, exist_ok=True)
    path = phys_dir / MANIFEST_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def load_manifest(path) -> dict:
    """Read a manifest document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest_or_none(phys_dir) -> dict | None:
    """The manifest in ``phys_dir``, or ``None`` if absent or unreadable."""
    try:
        manifest = load_manifest(Path(phys_dir) / MANIFEST_FILENAME)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def resolve(manifest_path, relative_path) -> str | None:
    """Turn a project-relative path from a manifest into an absolute one.

    Paths are relative to the project root, not to the manifest; the root
    comes from :func:`project_root_for`. Absolute paths and ``None`` pass
    through.
    """
    if relative_path is None:
        return None
    if os.path.isabs(relative_path):
        return relative_path
    root = project_root_for(manifest_path)
    if root is None:
        return relative_path
    return str(Path(root) / relative_path)


def project_root_for(manifest_path) -> str | None:
    """The project root that a manifest's relative paths hang off.

    Counts back up the logical path by the number of components in the
    manifest's ``phys_dir``. That count is used only if the root it gives
    joins back onto the manifest's directory, which can fail when
    ``artefacts/`` is a symlink to a target inside the project and the
    manifest was reached through the target. Otherwise the marker walk
    (:func:`project_root_for_dir`) is tried under the same test, and the
    counted root is the last resort. ``None`` when the manifest cannot be
    read.
    """
    manifest_path = Path(os.path.abspath(manifest_path))
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    phys_dir = manifest.get("phys_dir")
    if not phys_dir or os.path.isabs(phys_dir):
        return str(manifest_path.parent)
    counted = manifest_path.parent
    for _ in Path(phys_dir).parts:
        counted = counted.parent
    if joins_back(counted, phys_dir, manifest_path.parent):
        return str(counted)
    walked = project_root_for_dir(manifest_path.parent)
    if joins_back(walked, phys_dir, manifest_path.parent):
        return walked
    return str(counted)


def discover_manifests(project_root) -> list[str]:
    """Every ``phys-manifest.json`` under a project, newest first.

    Ties break on path. The walk matches on the filename alone, since a
    run can be named into any ``artefacts/<run>/``, and skips VCS, build
    and ``obj_dir*`` directories.

    Symlinked directories are followed only inside the artefact layout,
    so a linked ``artefacts/`` is walked and a linked ``vendor/`` or
    ``$HOME`` is not. A link to the project root or an ancestor is always
    refused, and each real directory is walked once. Both rules are
    :func:`~rtl_buddy.fs_walk.may_follow_link` and
    :func:`~rtl_buddy.fs_walk.walk_unique`, shared with the coverage walk.
    """
    root = Path(project_root)
    found: list[tuple[float, str]] = []
    skip = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
    for dirpath, dirnames, filenames in walk_unique(
        root, may_follow=artefact_layout_boundary(root)
    ):
        dirnames[:] = [
            d for d in dirnames if d not in skip and not d.startswith("obj_dir")
        ]
        if MANIFEST_FILENAME not in filenames:
            continue
        path = os.path.join(dirpath, MANIFEST_FILENAME)
        try:
            mtime = os.path.getmtime(path)
        except OSError:  # pragma: no cover - raced deletion
            continue
        found.append((mtime, path))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in found]
