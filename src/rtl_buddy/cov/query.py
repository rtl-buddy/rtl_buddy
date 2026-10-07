# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Payload builders for ``rb cov summary`` and ``rb cov module <name>``.

They read ``cov_dir/manifest.json`` and its model without running a simulator. Each builder takes a context and returns a dict, which both the CLI and the MCP tools emit verbatim.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import FatalRtlBuddyError
from . import manifest as manifest_mod
from .model import cover_records, load_model, source_totals
from .raw import METRICS

#: Bumped when a payload's shape changes incompatibly; carried on every payload.
COV_QUERY_SCHEMA_VERSION = 1

#: Files reported by ``rb cov summary`` before truncation, coldest first.
DEFAULT_FILE_LIMIT = 20


class CovQueryError(FatalRtlBuddyError):
    """A coverage question that cannot be answered as asked; `candidates` lists near-miss module names for an unknown module."""

    def __init__(self, message: str, *, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class CovContext:
    """One run's coverage artefacts, loaded."""

    project_root: str
    manifest_path: str
    manifest: dict
    model: dict
    model_path: str | None


def resolve_manifest_path(project_root, *, cov_dir=None, manifest=None) -> str:
    """Locate the manifest to read, most explicit request first."""
    if manifest is not None:
        candidate = Path(manifest)
        if candidate.is_dir():
            candidate = candidate / manifest_mod.MANIFEST_FILENAME
        if not candidate.exists():
            raise CovQueryError(f"cov: no coverage manifest at {candidate}")
        return str(candidate)

    if cov_dir is not None:
        candidate = Path(cov_dir) / manifest_mod.MANIFEST_FILENAME
        if not candidate.exists():
            raise CovQueryError(
                f"cov: no {manifest_mod.MANIFEST_FILENAME} in {cov_dir}; "
                "run a coverage command there first"
            )
        return str(candidate)

    found = manifest_mod.discover_manifests(project_root)
    if not found:
        raise CovQueryError(
            f"cov: no {manifest_mod.COV_DIR_NAME}/"
            f"{manifest_mod.MANIFEST_FILENAME} under {project_root}; "
            "run `rb regression --coverage-merge` (or any coverage flag) first"
        )
    return found[0]


def load_context(project_root, *, cov_dir=None, manifest=None) -> CovContext:
    """Load the manifest and its model.

    Raises :class:`CovQueryError` when the model is missing or the run used ``--coverage-model none``.
    """
    manifest_path = resolve_manifest_path(
        project_root, cov_dir=cov_dir, manifest=manifest
    )
    try:
        document = manifest_mod.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise CovQueryError(f"cov: cannot read {manifest_path}: {exc}")

    root = manifest_mod.project_root_for(manifest_path) or str(project_root)
    model_path = manifest_mod.resolve(manifest_path, document.get("model"))
    if document.get("coverage_model") == "none":
        raise CovQueryError(
            f"cov: {manifest_path} was written with --coverage-model none and "
            "has no coverage model; re-run without it (or with "
            "--coverage-model totals) to query points"
        )
    if model_path is None or not os.path.exists(model_path):
        raise CovQueryError(
            f"cov: {manifest_path} names no coverage model "
            f"({document.get('model')}); re-run the coverage command"
        )
    try:
        model = load_model(model_path)
    except (OSError, ValueError) as exc:
        raise CovQueryError(f"cov: cannot read {model_path}: {exc}")

    return CovContext(
        project_root=root,
        manifest_path=manifest_path,
        manifest=document,
        model=model,
        model_path=model_path,
    )


def artefacts_block(ctx: CovContext) -> dict:
    """Every artefact path this run produced, project-relative."""
    document = ctx.manifest
    merged = document.get("merged") or {}
    coverview = document.get("coverview") or {}
    return {
        "manifest": manifest_mod.project_relative(ctx.manifest_path, ctx.project_root),
        "cov_dir": document.get("cov_dir"),
        "model": document.get("model"),
        "merged_info": merged.get("info"),
        "merged_raw": merged.get("raw"),
        "merged_desc": merged.get("desc"),
        "html_dir": merged.get("html_dir"),
        "datasets": dict(document.get("datasets") or {}),
        "descriptions": dict(document.get("descriptions") or {}),
        "coverview_zip": coverview.get("zip"),
        "coverview_per_test_zip": coverview.get("per_test_zip"),
    }


def _run_block(ctx: CovContext) -> dict:
    document = ctx.manifest
    return {
        "schema_version": COV_QUERY_SCHEMA_VERSION,
        "manifest": manifest_mod.project_relative(ctx.manifest_path, ctx.project_root),
        "generated_at": document.get("generated_at"),
        # Not `command`: the machine envelope uses that key.
        "run_command": document.get("command"),
        "suite": document.get("suite"),
        "builder": document.get("builder"),
        "simulator": document.get("simulator_family") or ctx.model.get("simulator"),
        "merge_mode": document.get("merge_mode"),
        "merge_failed": bool(document.get("merge_failed")),
        "failed_metrics": list(document.get("failed_metrics") or []),
    }


def _file_summary(file_row: dict) -> dict:
    summary = {
        "path": file_row["path"],
        "modules": file_row.get("modules", []),
        "totals": file_row.get("totals", {}),
    }
    # Omitted when the model has no source totals.
    collapsed = source_totals(file_row)
    if collapsed is not None:
        summary["source_totals"] = collapsed
    return summary


def coldest_first(file_rows, limit=None):
    """Order files coldest first: lowest line ratio, then most misses, then path.

    Files with no line points go last. `limit` (when positive) truncates. The ``/cov`` pane uses the same ordering. ``--by-source`` keeps this per-elaboration order and changes only the figures shown.
    """

    def sort_key(row):
        totals = row.get("totals", {}).get("line", {})
        ratio = totals.get("ratio")
        found = totals.get("found", 0)
        hit = totals.get("hit", 0)
        return (
            0 if found else 1,
            ratio if ratio is not None else 1.0,
            -(found - hit),
            row["path"],
        )

    ordered = sorted(file_rows, key=sort_key)
    return ordered if limit is None or limit <= 0 else ordered[:limit]


def _payload_around_files(ctx: CovContext, files: list) -> dict:
    """Build the payload shared by summary and detail around a caller-built ``files`` list."""
    model = ctx.model
    payload = _run_block(ctx)
    payload.update(
        {
            "totals": model.get("totals", {}),
            "counts": model.get("counts", {}),
            "tests": [_test_summary(row) for row in model.get("tests", [])],
            "files": files,
            "modules": sorted((model.get("modules") or {}).keys()),
            "artefacts": artefacts_block(ctx),
        }
    )
    # Reported beside `totals`, never instead of it.
    collapsed = source_totals(model)
    if collapsed is not None:
        payload["source_totals"] = collapsed
    covers = cover_records(model)
    if covers:
        payload["covers"] = covers
    return payload


def _test_summary(test_row: dict) -> dict:
    summary = {
        "name": test_row.get("name"),
        "suite": test_row.get("suite"),
        "totals": test_row.get("totals", {}),
    }
    collapsed = source_totals(test_row)
    if collapsed is not None:
        summary["source_totals"] = collapsed
    return summary


def summary_payload(ctx: CovContext, *, limit: int = DEFAULT_FILE_LIMIT) -> dict:
    """Run-level totals, per-test totals and the coldest `limit` files.

    Every scope carries ``totals`` (per elaboration) and ``source_totals`` (collapsed on the source point); ``--by-source`` only changes rendering.
    """
    return _payload_around_files(
        ctx,
        [
            _file_summary(row)
            for row in coldest_first(ctx.model.get("files", []), limit)
        ],
    )


def detail_payload(ctx: CovContext, *, limit: int | None = None) -> dict:
    """:func:`summary_payload` with every file's points included and no truncation unless `limit` is given."""
    return _payload_around_files(ctx, coldest_first(ctx.model.get("files", []), limit))


def module_names(ctx: CovContext) -> list[str]:
    """Every module the model knows about."""
    return sorted((ctx.model.get("modules") or {}).keys())


def resolve_module_name(model: dict, module: str, *, where=None) -> str:
    """Map a user's module name (case-insensitive) to the model's spelling.

    Raises :class:`CovQueryError` with near misses if there is no such module.
    """
    modules = model.get("modules") or {}
    if module in modules:
        return module
    lowered = {name.lower(): name for name in modules}
    if module.lower() in lowered:
        return lowered[module.lower()]
    raise CovQueryError(
        f"cov: no module {module!r} in {where or 'the coverage model'}",
        candidates=difflib.get_close_matches(module, sorted(modules), n=10, cutoff=0.4)
        or sorted(modules)[:10],
    )


def module_coverage(model: dict, module: str) -> dict:
    """One module's files, points, totals and per-test hit counts.

    Shared by ``rb cov module`` and the graph coverage overlay. `module` must be spelled as the model spells it (see :func:`resolve_module_name`).
    """
    joined = modules_coverage(model, [module])
    joined.pop("modules", None)
    return {"module": module, **joined}


def modules_coverage(model: dict, modules) -> dict:
    """:func:`module_coverage` over a set of elaborated module names (e.g. one source module built with two parameterisations).

    Do not sum separate :func:`module_coverage` results: points with no module (an ``.info`` fallback, a model written before line points carried one) would be counted once per name. Returns the same shape with ``module`` replaced by a sorted ``modules`` list.
    """
    names = frozenset(str(name) for name in modules)
    known = model.get("modules") or {}
    paths = {path for name in names for path in known.get(name, ())}
    totals = {metric: {"found": 0, "hit": 0, "ratio": None} for metric in METRICS}
    tests: dict[str, int] = {}
    files = []
    for row in model.get("files", []):
        if row["path"] not in paths:
            continue
        file_entry = _module_file(row, names, tests)
        files.append(file_entry)
        for metric in METRICS:
            entry = file_entry["totals"][metric]
            totals[metric]["found"] += entry["found"]
            totals[metric]["hit"] += entry["hit"]
    for metric in METRICS:
        entry = totals[metric]
        entry["ratio"] = None if entry["found"] == 0 else entry["hit"] / entry["found"]
    return {
        "modules": sorted(names),
        "totals": totals,
        "files": files,
        "tests": dict(sorted(tests.items())),
    }


def module_payload(ctx: CovContext, module: str) -> dict:
    """Per-file, per-point coverage for one module's sources."""
    resolved = resolve_module_name(ctx.model, module, where=ctx.model_path)
    joined = module_coverage(ctx.model, resolved)

    payload = _run_block(ctx)
    payload.update(
        {
            "module": resolved,
            "totals": joined["totals"],
            "files": joined["files"],
            "tests": sorted(joined["tests"]),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


def _module_file(file_row: dict, modules: frozenset[str], tests: dict) -> dict:
    """One file's points restricted to ``modules``; points with no module (``.info`` fallback, older models' line points) are kept once."""
    entry = {
        "path": file_row["path"],
        "modules": file_row.get("modules", []),
        "totals": {},
    }
    for metric in METRICS:
        points = [
            point
            for point in file_row.get(metric, [])
            if point.get("module") is None or point.get("module") in modules
        ]
        entry[metric] = points
        found = len(points)
        hit = sum(1 for point in points if point.get("hits", 0) > 0)
        entry["totals"][metric] = {
            "found": found,
            "hit": hit,
            "ratio": None if found == 0 else hit / found,
        }
        for point in points:
            for test, hits in (point.get("tests") or {}).items():
                tests[test] = tests.get(test, 0) + hits
    return entry
