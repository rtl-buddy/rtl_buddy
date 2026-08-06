# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Read verbs over coverage artefacts already on disk (#399).

``rb cov summary`` and ``rb cov module <name>`` answer the two questions
that previously required re-running a regression: *how covered is this
run*, and *what is cold inside this block*. Neither runs a simulator;
both read ``cov_dir/manifest.json`` and the model it points at.

Every payload builder here is a **plain function taking a context and
returning a dict**. The CLI hands the dict straight to
``_emit_machine_result``, and the MCP tools (phase 3) wrap the same dict
verbatim — the payload *is* the contract, so it may not be assembled
inside a command body where only one of the two surfaces would see it.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import FatalRtlBuddyError
from . import manifest as manifest_mod
from .model import cover_records, load_model
from .raw import METRICS

#: Bumped when a payload's shape changes incompatibly. Rides on every
#: payload so an agent surface can tell.
COV_QUERY_SCHEMA_VERSION = 1

#: Files reported by ``rb cov summary`` before truncation. Coldest
#: first — a summary that leads with the fully covered files is a
#: summary nobody reads to the end.
DEFAULT_FILE_LIMIT = 20


class CovQueryError(FatalRtlBuddyError):
    """A coverage question that cannot be answered as asked.

    Carries ``candidates`` when the failure was an unknown module name,
    so the CLI and the MCP server can show near misses instead of only
    the miss.
    """

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

    A manifest with no model is an error rather than an empty answer:
    the model is written by the same code path that writes the manifest,
    so its absence means the artefacts were truncated, not that nothing
    was covered.
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


# ---------------------------------------------------------------------------
# shared payload pieces
# ---------------------------------------------------------------------------


def artefacts_block(ctx: CovContext) -> dict:
    """Every artefact path this run produced, project-relative.

    The block machine consumers gate on: paths, not the display lines
    (``Merged LCOV: <path>``) the summary table prints.
    """
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
        # Not `command`: the machine envelope already spends that key on the
        # verb being run, and `**payload` would collide with it.
        "run_command": document.get("command"),
        "suite": document.get("suite"),
        "builder": document.get("builder"),
        "simulator": document.get("simulator_family") or ctx.model.get("simulator"),
        "merge_mode": document.get("merge_mode"),
    }


def _file_summary(file_row: dict) -> dict:
    return {
        "path": file_row["path"],
        "modules": file_row.get("modules", []),
        "totals": file_row.get("totals", {}),
    }


def coldest_first(file_rows, limit=None):
    """Files ordered coldest first: lowest line ratio, then most misses.

    Files with no line points at all go last. They are not cold, they
    are silent — a header, a package, a file whose lines the database
    never recorded — and reading their ``null`` ratio as 1.0 filed them
    among the fully covered ones, where a reader scanning up from the
    bottom for "what is left" met them first.

    Public because the ``/cov`` pane orders its file list the same way
    the summary does — two orderings for "which file should I look at
    first" would be one too many. The pane applies this same rule to
    whichever metric its picker has selected; on ``line`` the two agree
    exactly.
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


# ---------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------


def _payload_around_files(ctx: CovContext, files: list) -> dict:
    """Everything both payloads share, wrapped around a ``files`` list.

    The summary and the detail differ only in the depth of ``files``, so
    the caller builds that list and this builds the rest — the detail
    used to call the summary and throw its file rows away.
    """
    model = ctx.model
    payload = _run_block(ctx)
    payload.update(
        {
            "totals": model.get("totals", {}),
            "counts": model.get("counts", {}),
            "tests": [
                {
                    "name": row.get("name"),
                    "suite": row.get("suite"),
                    "totals": row.get("totals", {}),
                }
                for row in model.get("tests", [])
            ],
            "files": files,
            "modules": sorted((model.get("modules") or {}).keys()),
            "artefacts": artefacts_block(ctx),
        }
    )
    covers = cover_records(model)
    if covers:
        payload["covers"] = covers
    return payload


def summary_payload(ctx: CovContext, *, limit: int = DEFAULT_FILE_LIMIT) -> dict:
    """Run-level scalars, per-test scalars and the coldest files."""
    return _payload_around_files(
        ctx,
        [
            _file_summary(row)
            for row in coldest_first(ctx.model.get("files", []), limit)
        ],
    )


def detail_payload(ctx: CovContext, *, limit: int | None = None) -> dict:
    """:func:`summary_payload`, but with every file's points included.

    The summary truncates its file list and reports only each file's
    totals, because a terminal reading 40 000 points is a terminal
    nobody reads. A pane is the other case: it renders the points, so
    dropping them would force a second request per file and put the
    "which tests hit this line" join on the client.

    Same run block, same ``artefacts`` block, same coldest-first
    ordering — the only difference is the depth of ``files``.
    """

    return _payload_around_files(ctx, coldest_first(ctx.model.get("files", []), limit))


def module_names(ctx: CovContext) -> list[str]:
    """Every module the model knows about."""
    return sorted((ctx.model.get("modules") or {}).keys())


def resolve_module_name(model: dict, module: str, *, where=None) -> str:
    """A user's module name -> the name the model spells it with.

    Raises :class:`CovQueryError` with near misses when there is no such
    module — an unknown name is a typo far more often than it is a
    coverage hole, and the near-miss list is the cheaper fix.
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

    The join every module-scoped consumer needs: ``rb cov module``
    prints it, and the graph's coverage overlay (#402) keys it to
    ``module:<name>`` nodes. One implementation, so a module's ratio on
    the graph pane cannot disagree with the same module's ratio in the
    coverage verbs.

    ``module`` must already be spelled the model's way — see
    :func:`resolve_module_name`.
    """
    paths = set((model.get("modules") or {}).get(module, ()))
    totals = {metric: {"found": 0, "hit": 0, "ratio": None} for metric in METRICS}
    tests: dict[str, int] = {}
    files = []
    for row in model.get("files", []):
        if row["path"] not in paths:
            continue
        file_entry = _module_file(row, module, tests)
        files.append(file_entry)
        for metric in METRICS:
            entry = file_entry["totals"][metric]
            totals[metric]["found"] += entry["found"]
            totals[metric]["hit"] += entry["hit"]
    for metric in METRICS:
        entry = totals[metric]
        entry["ratio"] = None if entry["found"] == 0 else entry["hit"] / entry["found"]
    return {
        "module": module,
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


def _module_file(file_row: dict, module: str, tests: dict) -> dict:
    """One file's points, keeping only the points that belong to ``module``.

    A header included into several modules records its points once per
    containing module; reporting the whole file would attribute another
    block's misses to this one. Points with no module recorded (an
    ``.info``-only fallback) are kept, since dropping them would report
    a file with no lines at all.
    """
    entry = {
        "path": file_row["path"],
        "modules": file_row.get("modules", []),
        "totals": {},
    }
    for metric in METRICS:
        points = [
            point
            for point in file_row.get(metric, [])
            if point.get("module") in (None, module)
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
