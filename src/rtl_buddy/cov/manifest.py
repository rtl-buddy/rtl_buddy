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

A **failed merge** is stated, not implied (#638). Under ``merge_mode:
"raw"``, ``merged.raw`` is the path to the merged database when
``verilator_coverage --write`` succeeded and ``null`` when it was asked
for and produced nothing — the mode says it was requested, so ``null``
there is a failure and not an absence. That inference is now unnecessary:
``merge_failed`` is the explicit fact and ``failed_metrics`` names what it
cost (``toggle``, ``expression`` and ``functional`` come only from the
merged database; an LCOV ``.info`` cannot represent them).

``totals`` is deliberately **left intact** when that happens. It is built
from the per-test databases by :mod:`rtl_buddy.cov.model`, not from the
merged one, so it is a real measurement of exactly what it says and
blanking a metric there would destroy data the run did produce. What a
failed merge costs is the *merged summary* number — the one the console
prints — so the failure is reported beside ``totals`` rather than written
into it. A consumer that needs one number per metric keeps reading
``totals``; a consumer comparing it against the console summary reads
``merge_failed`` first.

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "generator": "rtl-buddy 6.24.0",
      "generated_at": "2026-08-06T11:04:12+08:00",
      "command": "regression",           # or "test"
      "suite": "verif/demo/regression.yaml",
      "builder": "verilator",
      "simulator_family": "verilator",
      "merge_mode": "raw"|"info_process"|null,
      "merge_failed": false,             # true: the requested merge died
      "failed_metrics": [],              # e.g. ["toggle", "expression"]
      "cov_dir": "artefacts/cov_dir",
      "model": "artefacts/cov_dir/coverage-model.json",
      "totals": {"line": {"found": .., "hit": .., "ratio": ..}, ...},
      "source_totals": {...}|null,     # same shape, module dropped (#637)
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

#: Filename inside ``cov_dir``, and the path rules its contents keep.
# All defined in `tools.artifact_paths` — the bottom of the import graph:
# the filename because that is where the artefact-clearing helpers protect
# it from a co-named run's suffix clear (#469), and the path helpers
# because the physical manifest keeps the same rules and this module kept
# a resolve-both copy of `project_relative` that broke on a symlinked
# `artefacts/` (rtl-buddy/rtl_buddy#564). Re-exported here, where
# consumers already look.
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
        # Always written, both of them: a consumer must never have to read
        # the absence of a key as "the merge was fine" (#638).
        "merge_failed": bool(merge_failed),
        "failed_metrics": list(failed_metrics or []),
        "cov_dir": rel(cov_dir),
        "model": rel(model_path),
        "totals": totals,
        # The same run scored with the elaborated module dropped from a
        # point's identity (#637) — null when the model carried no such
        # figure. Beside `totals`, not instead of it.
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
    """Infer the project root a manifest's relative paths hang off.

    Counted back up the *logical* path, not the resolved one, for the
    reason :func:`~rtl_buddy.tools.artifact_paths.project_relative`
    spells out: the ``cov_dir`` the count consumes is relative to the
    project root as the writer saw it, and a ``cov_dir`` behind an
    ``artefacts/`` symlinked to scratch resolves to a path with none of
    those components above it. Counting them off *that* path climbs out
    of scratch entirely and returns a root no manifest path joins onto —
    `rb cov` then reporting a missing model that is sitting right there.

    The count alone is only right when the manifest is *read* through the
    same route it was written through, and one ordinary layout breaks
    that: an ``artefacts/`` link whose target is itself inside the
    project. :func:`discover_manifests` admits each directory once by its
    real path, so whichever of the two routes the walk reaches first wins
    — and when that is the target the count climbs off the wrong stem.
    So the count is *checked* against the directory the manifest actually
    sits in (:func:`~rtl_buddy.tools.artifact_paths.joins_back`), and the
    marker walk gets a second try when it fails. Failing both, the
    counted root stands: it is no worse than before, and a manifest read
    from outside any project has no better answer available. The same
    rule, and the same two helpers, as the physical manifest's
    :func:`rtl_buddy.phys.manifest.project_root_for`.
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
    """Every ``cov_dir/manifest.json`` under a project, newest first.

    Coverage artefacts land wherever the command ran, so discovery is a
    bounded walk rather than one fixed path. Version-control and build
    directories are skipped; ties break on the path so the order is
    deterministic on a tree with identical timestamps.

    **Symlinked directories are followed only inside the artefact
    layout**, the boundary the physical walk draws and for the same
    reasons (:func:`~rtl_buddy.fs_walk.may_follow_link`). A ``cov_dir``
    defaults to ``artefacts/cov_dir``, so a suite whose ``artefacts/`` is
    a link onto scratch storage keeps its coverage behind that link and a
    walk stopping there answered "no coverage found" for a run sitting
    right in front of it (rtl-buddy/rtl_buddy#564). Following *every*
    link is the other error: a ``vendor/`` link, or one to ``$HOME``,
    drags an unrelated tree into the walk and reports someone else's
    coverage as this project's. ``cov_dir`` is configurable, though, so
    the boundary is a real limit and not only a safety rail: a
    ``cov_dir`` pointed somewhere else and reached *only* through a link
    with no ``artefacts`` component on its path below the root is not
    discovered. Name it with ``--cov-dir`` (or ``--manifest``) and it is
    read directly, discovery unneeded.

    Each directory is also admitted once by its real path, so a cycle
    terminates and a ``cov_dir`` reachable two ways is reported once.
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
