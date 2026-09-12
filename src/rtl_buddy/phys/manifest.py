# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``phys-manifest.json`` — the physical artefact discovery contract (#558).

Every run that produces physical metrics writes one manifest beside its
artefacts. It answers the only two questions a later consumer has: *what
did this run produce*, and *where is it*. Without it, finding last
night's synthesis meant knowing the suite, the run name, and which of
the two synth backends had written the log.

Rules the file keeps, unchanged from the coverage manifest they are
copied from:

* **Stable keys.** The blocks (``synth``, ``power``) are always present,
  and so is every key inside them; a value is ``null`` when that
  artefact was not produced. Absent never means "not produced" — ``null``
  does. ``backend`` is the one to read first: it is ``null`` exactly when
  that half did not run here.
* **Project-relative paths.** Every path is POSIX and relative to the
  project root, so a manifest survives being read from somewhere else,
  archived, or attached to a CI artefact.
* **One per artefact directory**, rewritten whole on each run.

The one place it diverges from ``cov_dir/manifest.json`` is *where* it
lives, and the divergence follows from the flows. Coverage has a
``cov_dir`` — one directory per run, named, that discovery can walk for.
Synthesis and power do not: each writes into the run's own
``artefacts/<name>/``, which it shares with every other command that
happens to carry that name. So the manifest is named
``phys-manifest.json`` rather than ``manifest.json`` (discovery is a
filename match, and it must not collide with a coverage manifest a user
has pointed at the same directory), and there is no ``phys_dir``
convention to key on.

That layout also means a synth run and a power run *can* land in one
directory when they share a name, so the manifest merges the same way
the model does: a run rewrites its own block and carries the other's
forward when the model kept the other half (:func:`merge_manifest`).

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
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
                "netlist": .., "log": ..},
      "power": {"backend": "openroad"|null, "run": .., "netlist_source": ..,
                "report": .., "instances": .., "cells": .., "log": ..}
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: Bumped when the manifest's shape changes incompatibly.
MANIFEST_SCHEMA_VERSION = 1

#: Filename inside the producing run's artefact directory.
# Defined in `tools.artifact_paths` — the bottom of the import graph, and
# where the artefact-clearing helpers protect it from a co-named run's
# suffix clear (#469). Re-exported here, where consumers already look.
from ..tools.artifact_paths import (  # noqa: E402
    PHYS_MANIFEST_NAME as MANIFEST_FILENAME,
)

#: Keys of the ``synth`` block, so a power-only run still writes them all.
SYNTH_KEYS = ("backend", "run", "stats", "netlist", "log")

#: Keys of the ``power`` block, likewise.
POWER_KEYS = ("backend", "run", "netlist_source", "report", "instances", "cells", "log")

#: Which block keys hold a path and so need making project-relative. The
#: rest are plain strings a `rel()` would mangle into a filename.
_PATH_KEYS = frozenset({"stats", "netlist", "log", "report", "instances", "cells"})


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def project_relative(path, project_root) -> str | None:
    """POSIX path relative to the project root, or the path unchanged.

    A path outside the project (an artefact directory on a scratch
    filesystem, say) is kept verbatim rather than turned into a ``../..``
    chain nothing can join on.
    """
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return str(path)


#: What marks a project root, walking up from an artefact directory. Same
#: two markers :func:`rtl_buddy.config.root.discover_project_root` uses,
#: in the same order.
_ROOT_MARKERS = ("root_config.yaml", ".git")


def project_root_for_dir(artefact_dir) -> str:
    """The project root an artefact directory's paths should hang off.

    Resolved by walking up from the artefact directory rather than taken
    from the root config, because the backends that call this hold a
    suite directory and, in the OpenROAD synthesis case, a ``root_cfg``
    that may legitimately be absent.

    Deliberately not :func:`rtl_buddy.config.root.discover_project_root`,
    close as the walk is: that one logs at ERROR before falling back, and
    a synthesis run outside a project is not an error *here* — it is a
    manifest whose paths are bare filenames, which is still joinable and
    still worth writing.
    """
    start = Path(artefact_dir).resolve()
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return str(candidate)
    return str(start)


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

    ``synth`` and ``power`` are the two producer blocks; pass only the
    one this run wrote. The other is still emitted, with every key
    ``null`` — that is the stable-keys rule, and it is what lets a
    consumer read ``manifest["power"]["report"]`` without first asking
    whether a power run ever happened here.
    """

    def rel(path):
        return project_relative(path, project_root)

    def block(values, keys):
        values = values or {}
        return {
            key: rel(values.get(key)) if key in _PATH_KEYS else values.get(key)
            for key in keys
        }

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
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


def merge_manifest(existing: dict | None, new: dict) -> dict:
    """Carry an ``existing`` manifest's other-half block onto ``new``.

    The mirror of :func:`rtl_buddy.phys.model.merge_model`, and it must
    stay in step with it: the model's merge is what makes both halves
    describable from one directory, and a manifest that then pointed at
    only the last run's reports would leave half the document
    unattributable to the tool output behind it.

    A block is inherited only when the new run left it empty
    (``backend`` null) and the existing manifest describes the same top,
    for the same reason the model's merge checks the top: a
    same-directory artefact from a different design is not evidence
    about this one.
    """
    if not isinstance(existing, dict):
        return new
    if existing.get("schema_version") != new.get("schema_version"):
        return new
    if existing.get("top") != new.get("top"):
        return new
    merged = dict(new)
    merged["totals"] = dict(new.get("totals") or {})
    for half, keys in (("synth", SYNTH_KEYS), ("power", POWER_KEYS)):
        block = merged.get(half) or {}
        if block.get("backend") is not None:
            continue
        inherited = existing.get(half)
        if isinstance(inherited, dict) and inherited.get("backend") is not None:
            merged[half] = {key: inherited.get(key) for key in keys}
    for key, value in (existing.get("totals") or {}).items():
        if merged["totals"].get(key) is None:
            merged["totals"][key] = value
    return merged


def write_manifest(manifest: dict, phys_dir) -> str:
    """Write ``phys-manifest.json`` into ``phys_dir`` and return its path."""
    phys_dir = Path(phys_dir)
    phys_dir.mkdir(parents=True, exist_ok=True)
    path = phys_dir / MANIFEST_FILENAME
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return str(path)


def load_manifest(path) -> dict:
    """Read a manifest document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest_or_none(phys_dir) -> dict | None:
    """The manifest already in ``phys_dir``, or ``None``.

    The merge's read side; see
    :func:`rtl_buddy.phys.model.load_model_or_none` for why a bad read is
    "nothing to merge" rather than an error.
    """
    try:
        manifest = load_manifest(Path(phys_dir) / MANIFEST_FILENAME)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def resolve(manifest_path, relative_path) -> str | None:
    """Turn a manifest-relative path into an absolute one.

    Paths are relative to the *project root*, not to the manifest, so
    resolution walks up from ``phys_dir`` using the manifest's own
    ``phys_dir`` value. That keeps a manifest joinable after the tree has
    been moved, which a project-root field baked in at write time would
    not.
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
    """Infer the project root a manifest's relative paths hang off."""
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    phys_dir = manifest.get("phys_dir")
    if not phys_dir or os.path.isabs(phys_dir):
        return str(manifest_path.parent)
    root = manifest_path.parent
    for _ in Path(phys_dir).parts:
        root = root.parent
    return str(root)


def discover_manifests(project_root) -> list[str]:
    """Every ``phys-manifest.json`` under a project, newest first.

    A bounded walk rather than one fixed path, and — unlike the coverage
    equivalent — it cannot shortcut on the directory name: a physical
    manifest lives in whatever ``artefacts/<run>/`` the run was named
    into, so the filename is the only marker. Version-control and build
    directories are skipped; ties break on the path so the order is
    deterministic on a tree with identical timestamps.
    """
    root = Path(project_root)
    found: list[tuple[float, str]] = []
    skip = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
    for dirpath, dirnames, filenames in os.walk(root):
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
