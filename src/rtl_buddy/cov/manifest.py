# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``cov_dir/manifest.json`` — the coverage artefact discovery contract (#399).

Every run that produces coverage writes one manifest beside its
artefacts. It answers the only two questions a later consumer has: *what
did this run produce*, and *where is it*. Without it, finding last
night's coverage meant knowing the suite basename, the merge mode and
the working directory the command happened to run from.

Rules the file keeps:

* **Stable keys.** The blocks (``merged``, ``datasets``,
  ``descriptions``, ``tests``, ``coverview``) are always present; a
  value is ``null`` when that artefact was not produced. Absent never
  means "not produced" — ``null`` does.
* **Project-relative paths.** Every path is POSIX and relative to the
  project root, so a manifest survives being read from somewhere else,
  archived, or attached to a CI artefact.
* **One per ``cov_dir``.** ``cov_dir`` is the run's coverage artefact
  directory; the manifest is its index, rewritten whole on each run.

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "generator": "rtl-buddy 6.24.0",
      "generated_at": "2026-08-06T11:04:12+08:00",
      "command": "regression",           # or "test"
      "suite": "verif/demo/regression.yaml",
      "builder": "verilator",
      "simulator_family": "verilator",
      "merge_mode": "raw"|"info_process"|"lcov"|null,
      "cov_dir": "artefacts/cov_dir",
      "model": "artefacts/cov_dir/coverage-model.json",
      "totals": {"line": {"found": .., "hit": .., "ratio": ..}, ...},
      "merged": {"info": .., "raw": .., "desc": .., "html_dir": ..},
      "datasets": {"line": .., "branch": .., "toggle": .., "expression": ..},
      "descriptions": {"line": .., "branch": .., "toggle": .., "expression": ..},
      "coverview": {"zip": .., "per_test_zip": ..},
      "tests": [{"name": .., "suite": .., "raw": .., "info": ..,
                 "html_dir": .., "coverview_zip": ..}]
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

#: Filename inside ``cov_dir``.
MANIFEST_FILENAME = "manifest.json"

#: Name of the coverage artefact directory a run writes.
COV_DIR_NAME = "cov_dir"

#: Typed dataset keys, in report order.
DATASET_TYPES = ("line", "branch", "toggle", "expression")


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def project_relative(path, project_root) -> str | None:
    """POSIX path relative to the project root, or the path unchanged.

    A path outside the project (an absolute artefact directory on a
    scratch filesystem, say) is kept verbatim rather than turned into a
    ``../..`` chain nothing can join on.
    """
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return str(path)


def build_manifest(
    *,
    project_root,
    cov_dir,
    command: str,
    suite: str | None = None,
    builder: str | None = None,
    simulator_family: str | None = None,
    merge_mode: str | None = None,
    model_path=None,
    totals: dict | None = None,
    merged: dict | None = None,
    datasets: dict | None = None,
    descriptions: dict | None = None,
    coverview: dict | None = None,
    tests=None,
) -> dict:
    """Assemble a manifest document with every path project-relative."""

    def rel(path):
        return project_relative(path, project_root)

    merged = merged or {}
    datasets = datasets or {}
    descriptions = descriptions or {}
    coverview = coverview or {}
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": _generator(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command,
        "suite": rel(suite),
        "builder": builder,
        "simulator_family": simulator_family,
        "merge_mode": merge_mode,
        "cov_dir": rel(cov_dir),
        "model": rel(model_path),
        "totals": totals,
        "merged": {
            "info": rel(merged.get("info")),
            "raw": rel(merged.get("raw")),
            "desc": rel(merged.get("desc")),
            "html_dir": rel(merged.get("html_dir")),
        },
        "datasets": {key: rel(datasets.get(key)) for key in DATASET_TYPES},
        "descriptions": {key: rel(descriptions.get(key)) for key in DATASET_TYPES},
        "coverview": {
            "zip": rel(coverview.get("zip")),
            "per_test_zip": rel(coverview.get("per_test_zip")),
        },
        "tests": [
            {
                "name": entry.get("name"),
                "suite": rel(entry.get("suite")),
                "raw": rel(entry.get("raw")),
                "info": rel(entry.get("info")),
                "html_dir": rel(entry.get("html_dir")),
                "coverview_zip": rel(entry.get("coverview_zip")),
            }
            for entry in (tests or [])
        ],
    }


def write_manifest(manifest: dict, cov_dir) -> str:
    """Write ``manifest.json`` into ``cov_dir`` and return its path."""
    cov_dir = Path(cov_dir)
    cov_dir.mkdir(parents=True, exist_ok=True)
    path = cov_dir / MANIFEST_FILENAME
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return str(path)


def load_manifest(path) -> dict:
    """Read a manifest document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve(manifest_path, relative_path) -> str | None:
    """Turn a manifest-relative path into an absolute one.

    Paths are relative to the *project root*, not to the manifest, so
    resolution walks up from ``cov_dir`` using the manifest's own
    ``cov_dir`` value. That keeps a manifest joinable after the tree has
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
    cov_dir = manifest.get("cov_dir")
    if not cov_dir or os.path.isabs(cov_dir):
        return str(manifest_path.parent)
    root = manifest_path.parent
    for _ in Path(cov_dir).parts:
        root = root.parent
    return str(root)


def discover_manifests(project_root) -> list[str]:
    """Every ``cov_dir/manifest.json`` under a project, newest first.

    Coverage artefacts land wherever the command ran, so discovery is a
    bounded walk rather than one fixed path. Version-control and build
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
        if os.path.basename(dirpath) != COV_DIR_NAME:
            continue
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
