# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Coverage on the design knowledge graph (#402, #390).

The graph has always known **declared** coverage intent — a
``tests.yaml`` ``covers:`` entry becomes a
``test:<suite>#<name> --covers--> covitem:<block>#<id>`` edge — and the
coverage model (#399) has always known what the simulator **observed**
— SVA cover points with hits and per-test attribution. Nothing joined
them, so "is this spec item actually exercised, by which tests, and did
they pass?" was answerable from data already on disk and answered by
nothing.

This module is that join. Three rules shape it:

**The overlay carries it, not ``graph.json``.** Coverage is a property
of last night's run, not of the design, so it rides in
``results-overlay.json`` beside the statuses (#379) — the file that is
already re-read by every consumer and already excluded from the build
fingerprint. A sidecar was the alternative and was rejected: it would
have been a third file with a third staleness question, and both
readers (the query verbs and the ``/graph`` pane) already load the
overlay through one hook.

**Nothing re-runs.** The numbers come from ``cov_dir/manifest.json``
and the model it names — never from ``verilator_coverage``. Every value
written is a property of files on disk, so refreshing the overlay with
nothing re-run rewrites identical bytes.

**The name match is a ladder, and it is recorded.** A spec item id
(``A-COV-1``) and an SVA cover label (``cov_a_cov_1``) are written by
different people in different files, so the correlation is heuristic.
Rather than hide that, each match records *which rung* it came off
(:data:`MATCH_TIERS`), so a wrong join is visible instead of merely
wrong. The module join has a two-rung ladder of its own
(:func:`base_module_name`), because the model speaks the simulator's
elaborated names and the graph speaks the source's.

Per item the join emits one of three statuses:

``exercised``
    A declared item correlated with an observed cover point that fired.
``declared-only``
    A declared item with no observed cover point, or one that never
    fired. The two are told apart by whether ``observed`` is empty —
    "the RTL has no such cover" and "the cover never hit" are different
    bugs, and collapsing them into two statuses would have needed a
    fourth word for the same three questions.
``observed-but-undeclared``
    A cover point in the RTL that no ``covers:`` entry claims. These
    have no node in the graph, so they are listed separately rather
    than keyed by an id nothing can look up.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field as dc_field

from ..cov.model import cover_points
from ..cov.query import (
    CovQueryError,
    load_context as load_cov_context,
    modules_coverage,
)
from ..logging_utils import log_event
from .build import QUALIFIER_SEP
from .config_tier import MAPS_TO


logger = logging.getLogger(__name__)

#: Bumped when the overlay's ``coverage`` block changes incompatibly.
#: Independent of :data:`~rtl_buddy.graph.results.OVERLAY_SCHEMA_VERSION`:
#: the block is optional, so a consumer that does not read it is not
#: broken by a change here.
COVERAGE_SCHEMA_VERSION = 1

#: Per-item verdicts. The vocabulary the pane, ``rb graph explain`` and
#: the MCP ``test_status`` tool all render.
STATUS_EXERCISED = "exercised"
STATUS_DECLARED_ONLY = "declared-only"
STATUS_OBSERVED_UNDECLARED = "observed-but-undeclared"

#: How a declared item and an observed cover point were correlated,
#: strongest first. Recorded per match so a suspicious join can be seen.
MATCH_TIERS = ("exact", "nocase", "normalized", "affix")

#: Metric whose ratio drives the design-column tint. Line coverage is
#: the only metric every simulator family reports, and the only one an
#: ``.info``-only fallback carries at all.
TINT_METRIC = "line"

#: Node types the module ratio is attached to directly (a ``model:``
#: node picks it up through its ``maps_to`` stitch instead). Ports and
#: parameters are deliberately excluded: a per-port tint says nothing a
#: person can act on and would drown the columns it decorates.
_DESIGN_TYPES = frozenset({"module", "instance"})

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_COV_AFFIX = ("cov", "cvr", "c")

#: One trailing ``__<alnum>`` group — see :func:`base_module_name`.
_ELABORATION_SUFFIX = re.compile(r"__[A-Za-z0-9]+$")


def base_module_name(name: str) -> str:
    """The source-level module name behind an elaborated one.

    The coverage model keys modules on the name the **simulator**
    elaborated: verilator appends a mangled parameterisation suffix, so
    ``ip_async_fifo`` compiled once is ``ip_async_fifo__DB13`` and
    compiled twice is ``ip_cdc_handshake__W13`` and
    ``ip_cdc_handshake__Wc``. The design graph keys on the **source**
    name (``module:ip_async_fifo``), which is also the name a person
    reads in ``design/common/*.sv``. One trailing ``__<alnum>`` group is
    the entire difference between the two vocabularies, so it is
    stripped exactly once, and only when a non-empty base survives —
    ``__A8`` on its own is a whole name, not a suffix.

    A module a project really did call ``axi__lite`` strips to ``axi``,
    which is why every caller must try exact equality first (see
    :func:`_design_entries`); a real name then beats any stripped near
    miss.

    This is the python end of a wire whose other end is the
    ``module-names`` marker block in ``rtl_buddy/hub/cov_page.html``
    (``baseModuleName`` / ``resolveModuleName``). The two must agree: if
    one changes, the other changes with it.
    """
    text = str(name)
    return _ELABORATION_SUFFIX.sub("", text) or text


def _normalize(name: str) -> str:
    """Casefolded, punctuation-free form: ``A-COV-1`` -> ``acov1``."""
    return _NON_ALNUM.sub("", str(name).lower())


def _affix_variants(name: str) -> set[str]:
    """Normalized forms of ``name`` with a ``cov``-ish affix removed.

    Applied to the **observed** label only. A declared id is what a
    human wrote in ``specs.yaml`` and is matched verbatim; it is the SVA
    label that conventionally wears a ``cov_``/``_cov`` decoration, and
    stripping affixes off both sides would let ``SHARED-COV`` and
    ``SHARED`` collide as if they were the same item.
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
      block (dict): the overlay's ``coverage`` block.
      per_test (dict): test node id -> that test's coverage scalars.
      problems (list[dict]): why coverage could not be joined, if it
        could not. Empty and ``block is None`` together mean "no
        coverage artefacts on this tree", which is not an error.
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
    """``covitem`` nodes and the tests that declare they cover them.

    Returns ``(items, declarers)``: node id -> node, and node id ->
    sorted test node ids reached backwards along ``covers``.
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
    """Design module name -> the graph node ids that carry its coverage.

    A module id is suite-qualified when two files claimed the same name
    (``module:blk_a@verif/x``), every instance of a module records the
    module it instantiates, and a ``model:`` node *is* its module under
    another name (the ``maps_to`` stitch is an identity) — so one model
    module can carry several nodes, and the mapping is built once here
    rather than re-derived by each consumer.
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
    """Overlay entries indexed by test name and by ``(suite, name)``."""
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
    """The overlay id for one model test row, or ``None``.

    The model records the test's name and the suite *file* it ran from;
    the overlay is keyed by the suite *directory*. A unique name settles
    it outright — which is every project that does not run the same test
    name in two suites — and the suite directory disambiguates the rest.
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
    # The model may record the suite file absolutely (a run from a
    # scratch filesystem) while the overlay keys on the project-relative
    # directory, so the tail is tried first and the path is peeled from
    # the left until one of its suffixes is a suite the overlay knows.
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

    Returns ``(hits, undeclared)``: item node id -> the observed records
    that correlate with it (each stamped with the :data:`MATCH_TIERS`
    rung it came off), and the records that correlate with nothing.
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
        # A shared item id is declared by several blocks and so has
        # several nodes; the covers edges fan out the same way, so the
        # observation lands on all of them rather than on an arbitrary one.
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
    """One ``covitem:`` node's verdict."""
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
    """The graph's name for one elaborated model module, or ``None``.

    **Exact first, stripped second** — the same order, for the same
    reason, as ``resolveModuleName`` in the ``module-names`` block of
    ``rtl_buddy/hub/cov_page.html``. It matters: the project template
    has both an ``ip_cdc_sync`` and an ``ip_cdc_sync__W4`` in one model,
    and only exact-first keeps the plain one off the parameterised one's
    node. Ties among stripped candidates go to the first name in sorted
    order, which is what ``by_base`` was built with.
    """
    if name in node_ids:
        return name
    return by_base.get(base_module_name(name))


def _design_entries(model: dict, graph: dict | None) -> tuple[dict, list[str]]:
    """Node id -> module coverage, for every module the model knows.

    The model spells a module the way the simulator elaborated it and
    the graph spells it the way the source does; :func:`base_module_name`
    is the whole difference, and :func:`_resolve_module_node` is the
    ladder across it. Several elaborations of one module therefore land
    on one node and are **aggregated** there — counts summed, ratios
    recomputed from the sums, file and test lists unioned — because the
    graph has one node for what the simulator compiled twice. The
    aggregate is computed by
    :func:`~rtl_buddy.cov.query.modules_coverage` over the whole set at
    once rather than by adding up per-elaboration totals, which would
    count each file's module-less line points once per elaboration.

    A module the graph has no node for still gets an entry, keyed by the
    ``module:<name>`` id the design tier would have emitted: a graph
    built with ``--no-design`` still carries that id as a dangling
    ``maps_to`` target, and the pane draws those. The entry is inert
    when nothing claims the id, and the modules in that position are
    reported under their **elaborated** name — the name that is in the
    coverage model and so the name a person can grep for — so "the
    design tier was never built" is visible rather than silently absent
    coverage.
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
) -> CoverageJoin:
    """Join a run's coverage model onto the graph's ids.

    Args:
      project_root: the project the overlay is being refreshed for.
      entries: the overlay's ``tests`` block, so per-test scalars can be
        keyed by test node id and an item can report whether the tests
        declaring it passed.
      graph: an already-loaded ``graph.json``. Without one, module
        coverage is keyed by the ``module:<name>`` id the config tier
        would emit and no declared items are known.
      cov_dir / manifest: where to read coverage from. Defaults to the
        newest ``cov_dir/manifest.json`` under the project.
      required: when true, a missing or unreadable manifest is reported
        in ``problems`` instead of being the ordinary "this tree has no
        coverage" answer.

    Returns:
      CoverageJoin: with ``block`` set when coverage was found.
    """
    entries = entries or {}
    try:
        ctx = load_cov_context(project_root, cov_dir=cov_dir, manifest=manifest)
    except CovQueryError as exc:
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

    # Past the load, every walk indexes into a document read off disk.
    # An unreadable manifest already degrades to a problems row; a model
    # that *loads* and is then the wrong shape — truncated writer, hand
    # edit, a schema from a future build — must degrade the same way.
    # `rb graph results` joins coverage by default, so anything raising
    # here would take the whole overlay down with it, statuses included,
    # and coverage is the optional tier.
    try:
        return _joined(ctx, entries=entries, graph=graph)
    except Exception as exc:  # noqa: BLE001 - any shape of broken model
        error = f"{type(exc).__name__}: {exc}"
        log_event(
            logger,
            logging.WARNING,
            "graph_coverage.unavailable",
            error=error,
            required=required,
        )
        return CoverageJoin(problems=[{"scope": "coverage", "error": error}])


def _joined(ctx, *, entries: dict, graph: dict | None) -> CoverageJoin:
    """The join proper, once the coverage model has loaded.

    Split out of :func:`join_coverage` so that function can wrap it: the
    body below is all model-shaped indexing, and the wrapper is what
    turns a broken model into a reported problem instead of a traceback.
    """
    model = ctx.model
    by_name, by_suite = _test_node_ids(entries)
    per_test: dict[str, dict] = {}
    unjoined: list[str] = []
    for row in model.get("tests", []):
        node_id = _resolve_test_node(row, by_name, by_suite)
        scalars = {
            "totals": row.get("totals", {}),
            "manifest": ctx.manifest.get("cov_dir"),
            "raw": row.get("raw"),
            "info": row.get("info"),
        }
        if node_id is None:
            unjoined.append(str(row.get("name") or ""))
            continue
        per_test[node_id] = {k: v for k, v in scalars.items() if v is not None}

    items, declarers = _declared_items(graph)
    observed = cover_points(model)
    matched, undeclared = _match_observed(items, observed)

    nodes, unmatched_modules = _design_entries(model, graph)
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

    block = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "manifest": _relative(ctx.project_root, ctx.manifest_path),
        "model": ctx.manifest.get("model"),
        "generated_at": ctx.manifest.get("generated_at"),
        "run_command": ctx.manifest.get("command"),
        "suite": ctx.manifest.get("suite"),
        "simulator": ctx.manifest.get("simulator_family") or model.get("simulator"),
        "totals": model.get("totals", {}),
        "tint_metric": TINT_METRIC,
        "summary": {
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
        manifest=block["manifest"],
        tests=len(per_test),
        items=len(items),
        exercised=counts[STATUS_EXERCISED],
        undeclared=len(undeclared),
    )
    return CoverageJoin(block=block, per_test=per_test)


def _relative(project_root, path) -> str:
    try:
        return os.path.relpath(str(path), str(project_root)).replace(os.sep, "/")
    except ValueError:  # pragma: no cover - different drives on Windows
        return str(path)


# ---------------------------------------------------------------------------
# join hooks — the mirror of results.overlay_for_node / annotate_graph
# ---------------------------------------------------------------------------


def coverage_block(overlay: dict | None) -> dict | None:
    """The overlay's ``coverage`` block, or ``None``."""
    if not overlay:
        return None
    block = overlay.get("coverage")
    return block if isinstance(block, dict) else None


def coverage_for_node(overlay: dict | None, node_id: str) -> dict | None:
    """The coverage entry for one node id, or ``None``.

    The counterpart of
    :func:`~rtl_buddy.graph.results.overlay_for_node`: a module or
    instance node gets its ratio, a ``covitem:`` node gets its verdict,
    and everything else gets nothing.
    """
    block = coverage_block(overlay)
    if block is None:
        return None
    return (block.get("nodes") or {}).get(node_id)


def annotate_coverage(graph: dict, overlay: dict | None) -> int:
    """Attach coverage entries to a graph's nodes **in memory**.

    Same contract as
    :func:`~rtl_buddy.graph.results.annotate_graph`: the caller's dict
    is mutated, the file is not. ``graph.json`` stays hash-stable across
    coverage runs because coverage never enters it.
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
