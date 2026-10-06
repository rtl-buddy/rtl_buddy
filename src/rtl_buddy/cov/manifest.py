# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``cov_dir/manifest.json``: which coverage artefacts a run produced and where they are.

Every coverage run writes one manifest into its ``cov_dir``, rewritten whole each run. The blocks ``merged``, ``datasets``, ``descriptions``, ``tests`` and ``coverview`` are always present, with ``null`` for an artefact not produced. Paths are POSIX and relative to the project root.

``merge_failed`` is true when a requested merge produced nothing, and ``failed_metrics`` lists the metrics lost with it (``toggle``, ``expression`` and ``functional`` exist only in the merged database). ``totals`` is built from the per-test databases and is unaffected by a failed merge. ``model`` is ``null`` under ``coverage_model: "none"``; ``totals`` and ``source_totals`` are still written.

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "generator": "rtl-buddy 6.24.0",
      "generated_at": "2026-08-06T11:04:12+08:00",
      "command": "regression",           # or "test", "randtest"
      "suite": "verif/demo/regression.yaml",
      "builder": "verilator",
      "simulator_family": "verilator",
      "merge_mode": "raw"|"info_process"|null,
      "merge_failed": false,             # true: the requested merge died
      "failed_metrics": [],              # e.g. ["toggle", "expression"]
      "cov_dir": "artefacts/cov_dir",
      "coverage_model": "full"|"totals"|"none",  # --coverage-model
      "model": "artefacts/cov_dir/coverage-model.json"|null,
      "totals": {"line": {"found": .., "hit": .., "ratio": ..}, ...},
      "source_totals": {...}|null,     # same shape, module dropped from point identity
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

from ..fs_walk import artefact_layout_boundary, walk_unique

#: Bumped when the manifest's shape changes incompatibly.
MANIFEST_SCHEMA_VERSION = 1

# Defined in `tools.artifact_paths` (shared with the physical manifest) and re-exported here.
from ..tools.artifact_paths import (  # noqa: E402
    COV_MANIFEST_NAME as MANIFEST_FILENAME,
    joins_back,
    project_relative,
    project_root_or_none,
)

#: Name of the coverage artefact directory a run writes.
COV_DIR_NAME = "cov_dir"

#: Typed dataset keys, in report order.
DATASET_TYPES = ("line", "branch", "toggle", "expression")


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def build_manifest(
    *,
    project_root,
    cov_dir,
    command: str,
    suite: str | None = None,
    builder: str | None = None,
    simulator_family: str | None = None,
    merge_mode: str | None = None,
    merge_failed: bool = False,
    failed_metrics=None,
    model_path=None,
    coverage_model: str = "full",
    totals: dict | None = None,
    source_totals: dict | None = None,
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
        # Always written, so an absent key never means the merge was fine.
        "merge_failed": bool(merge_failed),
        "failed_metrics": list(failed_metrics or []),
        "cov_dir": rel(cov_dir),
        "coverage_model": coverage_model,
        "model": rel(model_path),
        "totals": totals,
        "source_totals": source_totals,
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
    """Turn a manifest path (relative to the project root, not to the manifest) into an absolute one."""
    if relative_path is None:
        return None
    if os.path.isabs(relative_path):
        return relative_path
    root = project_root_for(manifest_path)
    if root is None:
        return relative_path
    return str(Path(root) / relative_path)


def project_root_for(manifest_path) -> str | None:
    """Infer the project root a manifest's relative paths hang off.

    Counts up the logical path by the number of components in the manifest's ``cov_dir``, not the resolved path, so a symlinked ``artefacts/`` still works. The count is checked with :func:`~rtl_buddy.tools.artifact_paths.joins_back`, then a project-marker walk is tried; if neither joins back, the counted root is returned. Same rule as :func:`rtl_buddy.phys.manifest.project_root_for`.
    """
    manifest_path = Path(os.path.abspath(manifest_path))
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    cov_dir = manifest.get("cov_dir")
    if not cov_dir or os.path.isabs(cov_dir):
        return str(manifest_path.parent)
    counted = manifest_path.parent
    for _ in Path(cov_dir).parts:
        counted = counted.parent
    if joins_back(counted, cov_dir, manifest_path.parent):
        return str(counted)
    walked = project_root_or_none(manifest_path.parent)
    if walked is not None and joins_back(walked, cov_dir, manifest_path.parent):
        return walked
    return str(counted)


def discover_manifests(project_root) -> list[str]:
    """Return every ``cov_dir/manifest.json`` under a project, newest first (ties by path).

    Version-control and build directories are skipped. Symlinked directories are followed only inside the artefact layout (:func:`~rtl_buddy.fs_walk.may_follow_link`), so a ``cov_dir`` reachable only through a link outside an ``artefacts`` path is not found; pass ``--cov-dir`` or ``--manifest``. Each real directory is visited once.
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
