# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Joins the coverage model onto graph ids: declared `covers:` items against observed cover
points.

The result rides in `results-overlay.json`, not `graph.json`, and is built from files
already on disk; no coverage tool is re-run.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..cov.model import TestArtefacts, build_model, cover_points
from ..cov.query import (
    CovQueryError,
    load_context as load_cov_context,
    modules_coverage,
)
from ..logging_utils import log_event
from .build import QUALIFIER_SEP
from .config_tier import MAPS_TO


logger = logging.getLogger(__name__)

# Bumped when the overlay's `coverage` block changes incompatibly. Independent of
# `OVERLAY_SCHEMA_VERSION` because the block is optional.
COVERAGE_SCHEMA_VERSION = 1

# Sources `rb graph results --coverage` accepts by name; anything else is a path to a
# merged LCOV `.info` file.
COVERAGE_SOURCE_AUTO = "auto"
COVERAGE_SOURCE_MODEL = "model"

# Per-item verdicts, rendered by the pane, `rb graph explain` and the MCP `test_status`
# tool.
# `exercised`: a declared item correlated with a cover point that fired.
# `declared-only`: a declared item with no observed cover point, or one that never
# fired; `observed` empty tells the two apart.
# `observed-but-undeclared`: a cover point no `covers:` entry claims; it has no node, so
# it is listed separately.
STATUS_EXERCISED = "exercised"
STATUS_DECLARED_ONLY = "declared-only"
STATUS_OBSERVED_UNDECLARED = "observed-but-undeclared"

# Rungs by which a declared item and an observed cover point were correlated, strongest
# first; each match records its rung.
MATCH_TIERS = ("exact", "nocase", "normalized", "affix")

# Metric driving the design-column tint; line coverage is the only one every simulator
# family reports.
TINT_METRIC = "line"

# Node types that carry the module ratio directly (`model:` nodes get it through
# `maps_to`). Ports and parameters are excluded because a per-port tint is not
# actionable.
_DESIGN_TYPES = frozenset({"module", "instance"})

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_COV_AFFIX = ("cov", "cvr", "c")

# One trailing `__<alnum>` group; see `base_module_name`.
_ELABORATION_SUFFIX = re.compile(r"__[A-Za-z0-9]+$")


def base_module_name(name: str) -> str:
    """Return the source-level module name behind an elaborated one.

    Verilator appends a mangled suffix to parameterised modules (`ip_async_fifo__DB13`),
    while the graph uses source names. One trailing `__<alnum>` group is stripped, and
    only when a non-empty base remains. Callers must try exact equality first (see
    `_design_entries`), because a real `axi__lite` would strip to `axi`.

    The JS counterpart is the `module-names` block in `rtl_buddy/hub/cov_page.html`
    (`baseModuleName`, `resolveModuleName`); keep the two in step.
    """
    text = str(name)
    return _ELABORATION_SUFFIX.sub("", text) or text


def _normalize(name: str) -> str:
    """Return the casefolded, punctuation-free form: `A-COV-1` -> `acov1`."""
    return _NON_ALNUM.sub("", str(name).lower())


def _affix_variants(name: str) -> set[str]:
    """Return normalized forms of `name` with a `cov`-like affix removed.

    Applied only to the observed label; stripping both sides would let `SHARED-COV` and
    `SHARED` collide.
    """
    variants: set[str] = set()
    tail = str(name).rsplit(".", 1)[-1]
    for candidate in {str(name), tail}:
        parts = [p for p in re.split(r"[^0-9A-Za-z]+", candidate) if p]
        if len(parts) > 1:
            if parts[0].lower() in _COV_AFFIX:
                variants.add(_normalize("_".join(parts[1:])))
            if parts[-1].lower() in _COV_AFFIX:
                variants.add(_normalize("_".join(parts[:-1])))
        if candidate != name:
            variants.add(_normalize(candidate))
    variants.discard("")
    return variants


@dataclass
class CoverageJoin:
    """One coverage join, ready to be folded into the overlay.

    Attributes:
      block: The overlay's `coverage` block.
      per_test: Test node id -> that test's coverage scalars.
    problems: Why coverage could not be joined. Empty problems with `block is None`
    means the tree has no coverage artefacts, which is not an error.
    """

    block: dict | None = None
    per_test: dict = dc_field(default_factory=dict)
    problems: list[dict] = dc_field(default_factory=list)

    def available(self) -> bool:
        return self.block is not None


# ---------------------------------------------------------------------------
# graph-side inputs
# ---------------------------------------------------------------------------


def _declared_items(graph: dict | None) -> tuple[dict, dict]:
    """Return `(items, declarers)`: `covitem` nodes by id, and node id -> sorted ids of the
    tests reaching it backwards along `covers`.
    """
    items: dict[str, dict] = {}
    declarers: dict[str, list[str]] = {}
    if not graph:
        return items, declarers
    for node in graph.get("nodes") or []:
        if node.get("type") == "coverage_item" and node.get("id"):
            items[str(node["id"])] = node
    for link in graph.get("links") or []:
        if link.get("type") != "covers":
            continue
        target = str(link.get("target", ""))
        if target in items:
            declarers.setdefault(target, []).append(str(link.get("source", "")))
    return items, {key: sorted(set(value)) for key, value in declarers.items()}


def _module_nodes(graph: dict | None) -> dict[str, list[str]]:
    """Return design module name -> the graph node ids that carry its coverage.

    One module can carry several nodes: suite-qualified ids (`module:blk_a@verif/x`),
    instances, and its `model:` node.
    """
    by_module: dict[str, list[str]] = {}
    if not graph:
        return by_module
    for node in graph.get("nodes") or []:
        node_id, node_type = str(node.get("id", "")), node.get("type")
        if not node_id or node_type not in _DESIGN_TYPES:
            continue
        if node_type == "module":
            name = node_id[len("module:") :].split(QUALIFIER_SEP)[0]
        else:
            name = str(node.get("module") or "")
        if not name:
            continue
        by_module.setdefault(name, []).append(node_id)
    for link in graph.get("links") or []:
        source, target = str(link.get("source", "")), str(link.get("target", ""))
        if link.get("type") != MAPS_TO or not source.startswith("model:"):
            continue
        if not target.startswith("module:"):
            continue
        name = target[len("module:") :].split(QUALIFIER_SEP)[0]
        by_module.setdefault(name, []).append(source)
    return {name: sorted(set(ids)) for name, ids in by_module.items()}


def _test_node_ids(entries: dict) -> tuple[dict, dict]:
    """Index overlay entries by test name and by `(suite, name)`."""
    by_name: dict[str, list[str]] = {}
    by_suite: dict[tuple[str, str], str] = {}
    for node_id, entry in (entries or {}).items():
        name = str(entry.get("test") or "")
        if not name:
            continue
        by_name.setdefault(name, []).append(node_id)
        suite = str(entry.get("suite") or "")
        by_suite[(suite, name)] = node_id
    return by_name, by_suite


def _resolve_test_node(row: dict, by_name: dict, by_suite: dict) -> str | None:
    """Return the overlay id for one model test row, or None.

    The model records the suite file, the overlay keys on the suite directory. A unique
    test name settles it; otherwise the suite directory disambiguates.
    """
    name = str(row.get("name") or "")
    if not name:
        return None
    candidates = by_name.get(name) or []
    if len(candidates) == 1:
        return candidates[0]
    suite = row.get("suite")
    if not suite:
        return None
    # The model may record an absolute suite path while the overlay keys on the
    # project-relative directory, so peel path segments from the left until a known
    # suite matches.
    parts = [
        p for p in os.path.dirname(str(suite)).replace(os.sep, "/").split("/") if p
    ]
    for start in range(len(parts)):
        node_id = by_suite.get(("/".join(parts[start:]), name))
        if node_id is not None:
            return node_id
    return None


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def _match_observed(items: dict, observed: list[dict]) -> tuple[dict, list[dict]]:
    """Correlate observed cover points with declared coverage items.

    Returns `(hits, undeclared)`: item node id -> matching observed records (each
    stamped with its `MATCH_TIERS` rung), and the records matching nothing.
    """
    exact: dict[str, list[str]] = {}
    nocase: dict[str, list[str]] = {}
    normalized: dict[str, list[str]] = {}
    for node_id, node in items.items():
        label = str(node.get("label") or node_id.rsplit("#", 1)[-1])
        exact.setdefault(label, []).append(node_id)
        nocase.setdefault(label.lower(), []).append(node_id)
        normalized.setdefault(_normalize(label), []).append(node_id)

    hits: dict[str, list[dict]] = {}
    undeclared: list[dict] = []
    for record in observed:
        name = record.get("name")
        if not name:
            undeclared.append(record)
            continue
        matched: list[str] = []
        tier = None
        for candidate_tier, table, keys in (
            ("exact", exact, [str(name)]),
            ("nocase", nocase, [str(name).lower()]),
            ("normalized", normalized, [_normalize(name)]),
            ("affix", normalized, sorted(_affix_variants(name))),
        ):
            for key in keys:
                matched.extend(table.get(key, ()))
            if matched:
                tier = candidate_tier
                break
        if not matched:
            undeclared.append(record)
            continue
        # A shared item id has several nodes; the observation lands on all of them.
        for node_id in sorted(set(matched)):
            hits.setdefault(node_id, []).append({**record, "match": tier})
    return hits, undeclared


def _item_entry(
    node_id: str,
    node: dict,
    observed: list[dict],
    declared_by: list[str],
    entries: dict,
) -> dict:
    """Return one `covitem:` node's verdict."""
    total = sum(int(record.get("hits") or 0) for record in observed)
    by_test: dict[str, int] = {}
    for record in observed:
        for test, count in (record.get("tests") or {}).items():
            by_test[test] = by_test.get(test, 0) + int(count or 0)
    statuses: dict[str, int] = {}
    for test_id in declared_by:
        status = str((entries.get(test_id) or {}).get("status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
    entry = {
        "kind": "item",
        "status": STATUS_EXERCISED if total > 0 else STATUS_DECLARED_ONLY,
        "item": str(node.get("label") or node_id.rsplit("#", 1)[-1]),
        "block": node.get("block"),
        "hits": total,
        "declared_by": declared_by,
        "declared_by_status": dict(sorted(statuses.items())),
        "hit_by": sorted(name for name, count in by_test.items() if count > 0),
        "observed": [
            {
                "name": record.get("name"),
                "module": record.get("module"),
                "file": record.get("file"),
                "line": record.get("line"),
                "hits": record.get("hits", 0),
                "match": record.get("match"),
            }
            for record in sorted(
                observed,
                key=lambda r: (str(r.get("file") or ""), r.get("line") or -1),
            )
        ],
    }
    return entry


def _undeclared_entry(record: dict) -> dict:
    return {
        "kind": "cover",
        "status": STATUS_OBSERVED_UNDECLARED,
        "name": record.get("name"),
        "module": record.get("module"),
        "file": record.get("file"),
        "line": record.get("line"),
        "hits": record.get("hits", 0),
        "hit_by": sorted(
            name for name, count in (record.get("tests") or {}).items() if count
        ),
    }


def _resolve_module_node(name: str, node_ids: dict, by_base: dict) -> str | None:
    """Return the graph's name for one elaborated model module, or None.

    Exact match first, stripped second, as `resolveModuleName` in `cov_page.html` does.
    A model can hold both `ip_cdc_sync` and `ip_cdc_sync__W4`, and only exact-first
    keeps the plain one off the parameterised one's node. Ties among stripped candidates
    go to the first name in sorted order.
    """
    if name in node_ids:
        return name
    return by_base.get(base_module_name(name))


def _design_entries(model: dict, graph: dict | None) -> tuple[dict, list[str]]:
    """Return node id -> module coverage for every module the model knows.

    Several elaborations of one module land on one node and are aggregated there by
    `cov.query.modules_coverage` over the whole set (summed counts, recomputed ratios,
    unioned file and test lists). A module with no graph node is keyed by the
    `module:<name>` id the design tier would have emitted, under its elaborated name, so
    a missing design tier is visible.
    """
    node_ids = _module_nodes(graph)
    by_base: dict[str, str] = {}
    for graph_name in sorted(node_ids):
        by_base.setdefault(base_module_name(graph_name), graph_name)

    groups: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for name in sorted(model.get("modules") or {}):
        resolved = _resolve_module_node(name, node_ids, by_base)
        if resolved is None:
            unmatched.append(name)
        groups.setdefault(resolved if resolved is not None else name, []).append(name)

    attached: dict[str, dict] = {}
    for group, names in sorted(groups.items()):
        joined = modules_coverage(model, names)
        totals = joined["totals"]
        entry = {
            "kind": "design",
            "module": group,
            "elaborations": joined["modules"],
            "ratio": (totals.get(TINT_METRIC) or {}).get("ratio"),
            "totals": totals,
            "files": [row["path"] for row in joined["files"]],
            "tests": sorted(joined["tests"]),
        }
        for node_id in node_ids.get(group) or [f"module:{group}"]:
            attached[node_id] = dict(entry)
    return attached, unmatched


def join_coverage(
    project_root: str | os.PathLike,
    *,
    entries: dict | None = None,
    graph: dict | None = None,
    cov_dir: str | os.PathLike | None = None,
    manifest: str | os.PathLike | None = None,
    required: bool = False,
    source: str = COVERAGE_SOURCE_AUTO,
) -> CoverageJoin:
    """Join a run's coverage onto the graph's ids.

    Sources, most structured first:

    - `model`: `cov_dir/manifest.json` and the coverage model it names, as written by a
      coverage-mode run.
    - `artefacts`: no manifest, but the overlay's test entries recorded per-test
      `coverage.dat` databases; a model is synthesized from them. This is the `auto`
      fallback.
    - `info`: an explicitly named merged LCOV `.info` (`rb graph results --coverage
      merged.info`), joined by file because LCOV has no module names.

    Args:
      project_root: Project the overlay is refreshed for.
    entries: The overlay's `tests` block, for per-test scalars keyed by test node id and
    for item pass status.
    graph: A loaded `graph.json`. Without one, module coverage is keyed by
    `module:<name>` and no declared items are known.
    cov_dir, manifest: Where to read coverage from. Default to the newest
    `cov_dir/manifest.json` under the project.
    required: When true, a missing or unreadable manifest is reported in `problems`
    instead of meaning "no coverage".
    source: `COVERAGE_SOURCE_AUTO`, `COVERAGE_SOURCE_MODEL`, or a path to a merged
    `.info`.

    Returns:
      A `CoverageJoin` with `block` set when coverage was found.
    """
    entries = entries or {}
    if source not in (COVERAGE_SOURCE_AUTO, COVERAGE_SOURCE_MODEL):
        return _safe_join(
            _join_from_info,
            project_root,
            source,
            entries=entries,
            graph=graph,
            required=required,
        )
    try:
        ctx = load_cov_context(project_root, cov_dir=cov_dir, manifest=manifest)
    except CovQueryError as exc:
        # No manifest: fall back to the per-test `coverage.dat` files the overlay
        # already found.
        if (
            source == COVERAGE_SOURCE_AUTO
            and cov_dir is None
            and manifest is None
            and _artefact_tests(project_root, entries)
        ):
            return _safe_join(
                _join_from_artefacts,
                project_root,
                entries=entries,
                graph=graph,
                required=required,
            )
        problems = (
            [{"scope": "coverage", "error": str(exc)}]
            if required or cov_dir is not None or manifest is not None
            else []
        )
        log_event(
            logger,
            logging.DEBUG,
            "graph_coverage.unavailable",
            error=str(exc),
            required=required,
        )
        return CoverageJoin(problems=problems)

    meta = {
        "source": "model",
        "manifest": _relative(ctx.project_root, ctx.manifest_path),
        "model": ctx.manifest.get("model"),
        "generated_at": ctx.manifest.get("generated_at"),
        "run_command": ctx.manifest.get("command"),
        "suite": ctx.manifest.get("suite"),
        "simulator": ctx.manifest.get("simulator_family") or ctx.model.get("simulator"),
        "cov_dir": ctx.manifest.get("cov_dir"),
    }
    return _safe_join(
        _joined, ctx.model, meta, entries=entries, graph=graph, required=required
    )


def _safe_join(join, *args, required: bool = False, **kwargs) -> CoverageJoin:
    """Run one join body, degrading any exception to a problems row.

    A source that loads but has the wrong shape must not take down the whole overlay,
    since coverage is optional. `required` only annotates the log line and is not
    forwarded to `join`.
    """
    try:
        return join(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - any shape of broken source
        error = f"{type(exc).__name__}: {exc}"
        log_event(
            logger,
            logging.WARNING,
            "graph_coverage.unavailable",
            error=error,
            required=required,
        )
        return CoverageJoin(problems=[{"scope": "coverage", "error": error}])


def _joined(model: dict, meta: dict, *, entries: dict, graph: dict | None):
    """The join proper, once a coverage model exists.

    `meta` says where the model came from: the manifest header fields, or `{"source":
    "artefacts"}`. The block keeps the manifest keys either way, with `null` meaning no
    manifest.
    """
    per_test, unjoined = _per_test_rows(model, meta, entries)

    items, declarers = _declared_items(graph)
    observed = cover_points(model)
    matched, undeclared = _match_observed(items, observed)

    nodes, unmatched_modules = _design_entries(model, graph)
    counts = _fold_item_nodes(nodes, items, declarers, matched, entries)

    block = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "source": meta["source"],
        "manifest": meta.get("manifest"),
        "model": meta.get("model"),
        "generated_at": meta.get("generated_at"),
        "run_command": meta.get("run_command"),
        "suite": meta.get("suite"),
        "simulator": meta.get("simulator") or model.get("simulator"),
        "totals": model.get("totals", {}),
        "tint_metric": TINT_METRIC,
        "summary": {
            "source": meta["source"],
            "tests": len(per_test),
            "modules": len(model.get("modules") or {}),
            "items": len(items),
            STATUS_EXERCISED: counts[STATUS_EXERCISED],
            STATUS_DECLARED_ONLY: counts[STATUS_DECLARED_ONLY],
            STATUS_OBSERVED_UNDECLARED: len(undeclared),
            "unjoined_tests": sorted(name for name in unjoined if name),
            "unmatched_modules": unmatched_modules,
        },
        "nodes": {key: nodes[key] for key in sorted(nodes)},
        "undeclared": [
            _undeclared_entry(record)
            for record in sorted(
                undeclared,
                key=lambda r: (
                    str(r.get("file") or ""),
                    r.get("line") or -1,
                    str(r.get("name") or ""),
                ),
            )
        ],
    }
    log_event(
        logger,
        logging.DEBUG,
        "graph_coverage.joined",
        source=meta["source"],
        manifest=block["manifest"],
        tests=len(per_test),
        items=len(items),
        exercised=counts[STATUS_EXERCISED],
        undeclared=len(undeclared),
    )
    return CoverageJoin(block=block, per_test=per_test)


def _per_test_rows(model: dict, meta: dict, entries: dict) -> tuple[dict, list[str]]:
    """Return test node id -> coverage scalars, plus the unjoined rows."""
    by_name, by_suite = _test_node_ids(entries)
    per_test: dict[str, dict] = {}
    unjoined: list[str] = []
    for row in model.get("tests", []):
        node_id = _resolve_test_node(row, by_name, by_suite)
        scalars = {
            "totals": row.get("totals", {}),
            "manifest": meta.get("cov_dir"),
            "raw": row.get("raw"),
            "info": row.get("info"),
        }
        if node_id is None:
            unjoined.append(str(row.get("name") or ""))
            continue
        per_test[node_id] = {k: v for k, v in scalars.items() if v is not None}
    return per_test, unjoined


def _fold_item_nodes(
    nodes: dict, items: dict, declarers: dict, matched: dict, entries: dict
) -> dict:
    """Add every `covitem:` verdict to `nodes`; return the tally."""
    counts = {STATUS_EXERCISED: 0, STATUS_DECLARED_ONLY: 0}
    for node_id in sorted(items):
        entry = _item_entry(
            node_id,
            items[node_id],
            matched.get(node_id, []),
            declarers.get(node_id, []),
            entries,
        )
        counts[entry["status"]] += 1
        nodes[node_id] = entry
    return counts


# ---------------------------------------------------------------------------
# manifest-less sources
# ---------------------------------------------------------------------------


def _artefact_tests(
    project_root: str | os.PathLike, entries: dict
) -> list[TestArtefacts]:
    """Return one `TestArtefacts` per overlay test entry that recorded a `coverage.dat`.

    `suite` gets a `/tests.yaml` tail because the model's test rows record the suite
    file and the resolver peels the directory back off.
    """
    root = str(project_root)
    tests: list[TestArtefacts] = []
    for entry in (entries or {}).values():
        raw = (entry.get("artefacts") or {}).get("coverage")
        name = entry.get("test")
        if not raw or not name:
            continue
        raw_abs = os.path.join(root, raw)
        suite = entry.get("suite")
        hints = [os.path.dirname(raw_abs)]
        if suite:
            hints.append(os.path.join(root, suite))
        tests.append(
            TestArtefacts(
                name=str(name),
                raw=raw_abs,
                suite=f"{suite}/tests.yaml" if suite else None,
                source_roots=tuple(hints),
            )
        )
    return tests


def _synthesized_model(project_root, entries: dict) -> dict | None:
    """Build a coverage model in memory from per-test raw databases, or None.

    None when no entry recorded one or none parsed into a point. Output is deterministic
    in the databases' bytes, so refreshes stay byte-identical.
    """
    tests = _artefact_tests(project_root, entries)
    if not tests:
        return None
    model = build_model(tests, project_root=project_root)
    return model if model.get("files") else None


def _join_from_artefacts(
    project_root, *, entries: dict, graph: dict | None
) -> CoverageJoin:
    """The `auto` fallback: join a model synthesized from raw databases."""
    model = _synthesized_model(project_root, entries)
    if model is None:
        return CoverageJoin()
    return _joined(model, {"source": "artefacts"}, entries=entries, graph=graph)


# ---------------------------------------------------------------------------
# merged LCOV .info ingestion
# ---------------------------------------------------------------------------


def _absolutized_sf(root: Path, info_dir: str, sf: str) -> tuple[str | None, int]:
    """Return `(project-relative path or None, trim depth)` for one `SF:` record.

    Depth is 0 for a path believed as written (absolute, or relative to the `.info`'s
    directory) and `n` when `n` leading segments were dropped to re-anchor it on the
    project root.

    There is deliberately no basename rung, unlike `SourcePathResolver`: repo-wide
    basename matching mis-attributes duplicate basenames across suites. Trimming stops
    while at least two segments survive. A record that reaches nothing is reported,
    never guessed at.
    """
    text = str(sf).strip().replace("\\", "/")
    candidates: list[tuple[Path, int]] = []
    path = Path(text)
    if path.is_absolute():
        candidates.append((path, 0))
    else:
        candidates.append((Path(info_dir) / text, 0))
        parts = [part for part in text.split("/") if part not in ("", ".")]
        # Stops before the single-segment candidate: a bare basename is never evidence.
        for idx in range(max(len(parts) - 1, 0)):
            candidates.append((root / Path(*parts[idx:]), idx))
    for candidate, depth in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_file():
                continue
            return resolved.relative_to(root).as_posix(), depth
        except (OSError, ValueError):
            # Outside the project root or unreadable; graph module files are
            # project-relative, so it cannot match.
            continue
    return None, 0


def _parse_info(
    info_path: Path, project_root: str
) -> tuple[dict, list[str], list[str]]:
    """Return per-file line/branch points from a merged LCOV `.info`.

    Returns `(files, unresolved, reanchored)`: project-relative path -> `{"line": {key:
    hits}, "branch": {key: hits}}` (summed across duplicate `SF:` blocks), the `SF:`
    records that resolved nowhere, and those that resolved only after trimming, which is
    an inferred match. Both lists are verbatim and sorted.
    """
    files: dict[str, dict] = {}
    unresolved: set[str] = set()
    reanchored: set[str] = set()
    # Each distinct `SF:` record is resolved once, since resolving costs several stat
    # calls.
    root = Path(project_root).resolve()
    seen: dict[str, tuple[str | None, int]] = {}
    info_dir = str(info_path.parent)
    current: dict | None = None
    with open(info_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if line.startswith("SF:"):
                recorded = line[3:].strip()
                if recorded not in seen:
                    seen[recorded] = _absolutized_sf(root, info_dir, recorded)
                resolved, depth = seen[recorded]
                if resolved is None:
                    unresolved.add(recorded)
                    current = None
                    continue
                if depth:
                    reanchored.add(recorded)
                current = files.setdefault(resolved, {"line": {}, "branch": {}})
            elif current is None:
                continue
            elif line.startswith("DA:"):
                payload = line[3:].split(",")
                try:
                    line_no, hits = int(payload[0]), int(payload[1])
                except (IndexError, ValueError):
                    continue
                bucket = current["line"]
                bucket[line_no] = bucket.get(line_no, 0) + hits
            elif line.startswith("BRDA:"):
                payload = line[5:].split(",")
                if len(payload) < 4:
                    continue
                try:
                    line_no = int(payload[0])
                except ValueError:
                    continue
                taken = payload[3]
                hits = 0 if taken in ("-", "") else int(taken)
                key = (line_no, payload[1], payload[2])
                bucket = current["branch"]
                bucket[key] = bucket.get(key, 0) + hits
            elif line == "end_of_record":
                current = None
    return files, sorted(unresolved), sorted(reanchored)


def _totals_of(points: dict) -> dict:
    found = len(points)
    hit = sum(1 for hits in points.values() if hits > 0)
    return {"found": found, "hit": hit, "ratio": (hit / found) if found else None}


def _file_totals(file_points: dict) -> dict:
    return {metric: _totals_of(file_points[metric]) for metric in ("line", "branch")}


def _sum_file_totals(files_totals: list[dict]) -> dict:
    totals: dict = {}
    for metric in ("line", "branch"):
        found = sum(t[metric]["found"] for t in files_totals)
        hit = sum(t[metric]["hit"] for t in files_totals)
        totals[metric] = {
            "found": found,
            "hit": hit,
            "ratio": (hit / found) if found else None,
        }
    return totals


def _info_design_entries(
    files: dict[str, dict], graph: dict | None
) -> tuple[dict, dict]:
    """Return node id -> module coverage joined by file, plus bookkeeping.

    A `module:` node is covered by the `.info` rows whose resolved path equals the file
    the node claims. An unambiguous module name fans out to its instances and `model:`
    alias; a name two files claimed stays on its own qualified node. Modules sharing one
    source file share its numbers (`joined_by: "file"`).
    """
    module_nodes: list[tuple[str, str, str]] = []  # (node id, name, file)
    name_count: dict[str, int] = {}
    for node in (graph or {}).get("nodes") or []:
        if node.get("type") != "module":
            continue
        node_id, file = str(node.get("id", "")), node.get("file")
        if not node_id.startswith("module:"):
            continue
        name = node_id[len("module:") :].split(QUALIFIER_SEP)[0]
        # The ambiguity guard counts every `module:` node, including those without a
        # `file`, because the fan-out covers the same population.
        name_count[name] = name_count.get(name, 0) + 1
        if file:
            module_nodes.append((node_id, name, str(file)))

    fanout = _module_nodes(graph)
    attached: dict[str, dict] = {}
    matched_files: set[str] = set()
    for node_id, name, file in sorted(module_nodes):
        points = files.get(file)
        if points is None:
            continue
        matched_files.add(file)
        totals = _file_totals(points)
        entry = {
            "kind": "design",
            "module": name,
            "joined_by": "file",
            "ratio": totals.get(TINT_METRIC, {}).get("ratio"),
            "totals": totals,
            "files": [file],
        }
        targets = fanout.get(name) if name_count.get(name) == 1 else None
        for target in targets or [node_id]:
            attached[target] = dict(entry)

    module_files = {file for _, _, file in module_nodes}
    unmatched = sorted(set(files) - matched_files)
    bookkeeping = {
        "design_files": len(module_files),
        "module_basenames": {os.path.basename(file) for file in module_files},
        "matched_files": sorted(matched_files),
        "unmatched_files": unmatched,
    }
    return attached, bookkeeping


def _join_from_info(
    project_root, info_path: str, *, entries: dict, graph: dict | None
) -> CoverageJoin:
    """Join one merged LCOV `.info` onto the graph's ids.

    The `.info` is authoritative for design-column heat. Per-test scalars and `covitem:`
    verdicts still come from per-test raw databases when the overlay found any. Files
    that resolved only after trimming are listed in `summary.reanchored_files`.
    """
    root = str(Path(project_root).resolve())
    info = Path(info_path)
    if not info.is_file():
        return CoverageJoin(
            problems=[
                {"scope": "coverage", "error": f"coverage: no .info file at {info}"}
            ]
        )
    rel_info = _relative(root, info)
    files, unresolved, reanchored = _parse_info(info, root)
    problems: list[dict] = []
    if not files and not unresolved:
        return CoverageJoin(
            problems=[
                {
                    "scope": "coverage",
                    "error": f"coverage: no SF: record in {rel_info}",
                }
            ]
        )

    design_nodes, bookkeeping = _info_design_entries(files, graph)
    # A record whose basename names a design file but whose path reaches elsewhere
    # signals a repo-scope merge that rewrote duplicate basenames against another suite;
    # report it.
    basenames = bookkeeping["module_basenames"]
    suspects = sorted(
        {
            path
            for path in bookkeeping["unmatched_files"] + unresolved
            if os.path.basename(str(path).replace("\\", "/")) in basenames
        }
    )
    if bookkeeping["design_files"] == 0:
        problems.append(
            {
                "scope": "coverage",
                "error": (
                    f"coverage: the graph has no design-tier module files to "
                    f"join {rel_info} against; run `rb graph build` with the "
                    "design tier"
                ),
            }
        )
    elif not design_nodes:
        skipped = len(files) + len(unresolved)
        problems.append(
            {
                "scope": "coverage",
                "error": (
                    f"coverage: none of the {skipped} SF record(s) in "
                    f"{rel_info} matches a design file the graph knows — a "
                    "different suite's elaboration (duplicate-basename SF "
                    "rewriting), or paths that did not absolutize; nothing "
                    "was attributed"
                ),
            }
        )
    elif suspects:
        shown = ", ".join(suspects[:5])
        problems.append(
            {
                "scope": "coverage",
                "error": (
                    f"coverage: {len(suspects)} SF record(s) in "
                    f"{rel_info} share a basename with a design file but "
                    f"resolve elsewhere (duplicate-basename SF rewriting?): "
                    f"{shown}"
                ),
            }
        )

    # The `.info` has no per-test column or cover points; the per-test raw databases
    # supply them, as for the `artefacts` source.
    model = _synthesized_model(root, entries)
    per_test, unjoined = _per_test_rows(model or {}, {"source": "info"}, entries)
    items, declarers = _declared_items(graph)
    nodes = dict(design_nodes)
    counts = {STATUS_EXERCISED: 0, STATUS_DECLARED_ONLY: 0}
    undeclared: list[dict] = []
    if model is not None:
        matched_items, undeclared = _match_observed(items, cover_points(model))
        counts = _fold_item_nodes(nodes, items, declarers, matched_items, entries)
    # `items` counts declared items; `items_scored` counts those this source could reach
    # a verdict on. Without per-test databases they differ, and `N items, 0 exercised`
    # would misread as "none hit".
    items_scored = len(items) if model is not None else 0

    block = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "source": "info",
        "info": rel_info,
        "manifest": None,
        "model": None,
        "generated_at": None,
        "run_command": None,
        "suite": None,
        "simulator": None,
        "totals": _sum_file_totals([_file_totals(files[path]) for path in files]),
        "tint_metric": TINT_METRIC,
        "summary": {
            "source": "info",
            "info": rel_info,
            "tests": len(per_test),
            "modules": len({e["module"] for e in design_nodes.values()}),
            "items": len(items),
            "items_scored": items_scored,
            STATUS_EXERCISED: counts[STATUS_EXERCISED],
            STATUS_DECLARED_ONLY: counts[STATUS_DECLARED_ONLY],
            STATUS_OBSERVED_UNDECLARED: len(undeclared),
            "unjoined_tests": sorted(name for name in unjoined if name),
            "matched_files": len(bookkeeping["matched_files"]),
            "unmatched_files": bookkeeping["unmatched_files"],
            "unresolved_files": unresolved,
            "reanchored_files": reanchored,
        },
        "nodes": {key: nodes[key] for key in sorted(nodes)},
        "undeclared": [
            _undeclared_entry(record)
            for record in sorted(
                undeclared,
                key=lambda r: (
                    str(r.get("file") or ""),
                    r.get("line") or -1,
                    str(r.get("name") or ""),
                ),
            )
        ],
    }
    log_event(
        logger,
        logging.DEBUG,
        "graph_coverage.joined",
        source="info",
        info=rel_info,
        tests=len(per_test),
        matched_files=len(bookkeeping["matched_files"]),
        unresolved=len(unresolved),
        reanchored=len(reanchored),
    )
    return CoverageJoin(block=block, per_test=per_test, problems=problems)


def _relative(project_root, path) -> str:
    try:
        return os.path.relpath(str(path), str(project_root)).replace(os.sep, "/")
    except ValueError:  # pragma: no cover - different drives on Windows
        return str(path)


# ---------------------------------------------------------------------------
# join hooks — the mirror of results.overlay_for_node / annotate_graph
# ---------------------------------------------------------------------------


def coverage_block(overlay: dict | None) -> dict | None:
    """Return the overlay's `coverage` block, or None."""
    if not overlay:
        return None
    block = overlay.get("coverage")
    return block if isinstance(block, dict) else None


def coverage_for_node(overlay: dict | None, node_id: str) -> dict | None:
    """Return the coverage entry for one node id, or None.

    Counterpart of `results.overlay_for_node`: modules and instances get a ratio,
    `covitem:` nodes a verdict, others nothing.
    """
    block = coverage_block(overlay)
    if block is None:
        return None
    return (block.get("nodes") or {}).get(node_id)


def annotate_coverage(graph: dict, overlay: dict | None) -> int:
    """Attach coverage entries to a graph's nodes in memory; return the count.

    The caller's dict is mutated, the file is not; `graph.json` stays hash-stable.
    """
    block = coverage_block(overlay)
    if block is None:
        return 0
    nodes = block.get("nodes") or {}
    annotated = 0
    for node in graph.get("nodes") or []:
        entry = nodes.get(node.get("id"))
        if entry is None:
            continue
        node["coverage"] = entry
        annotated += 1
    return annotated


__all__ = [
    "COVERAGE_SCHEMA_VERSION",
    "COVERAGE_SOURCE_AUTO",
    "COVERAGE_SOURCE_MODEL",
    "MATCH_TIERS",
    "STATUS_DECLARED_ONLY",
    "STATUS_EXERCISED",
    "STATUS_OBSERVED_UNDECLARED",
    "TINT_METRIC",
    "CoverageJoin",
    "annotate_coverage",
    "base_module_name",
    "coverage_block",
    "coverage_for_node",
    "join_coverage",
]
