# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Node-id-union merge of design-graph tiers (#377).

Every tier of the design knowledge graph emits the same NetworkX
node-link envelope, and the tiers are stitched together by *node id*:
``model:design/blk/models.yaml#blk_a`` links to ``module:blk_a``, and
``module:blk_a`` is a node the design tier owns. Merging is therefore a
plain union — no name matching, no heuristics, no tool required.

That last point is the reason this module exists at all. the extractor's
``merge-graphs`` would do the same job, but the extractor is an *optional*
dependency: without it the merged graph must still contain the design
and config tiers and still be queryable. So the union lives here and
runs always; the extractor's ``merge-graphs`` is used only as a
cross-check when it happens to be installed (see
:mod:`rtl_buddy.graph.extract`).

Both halves of the merge are deliberately lossless-by-content:

* nodes are unioned by ``id``; the first tier to introduce an attribute
  wins, later tiers only *fill in* what is missing, so a design-tier
  ``module`` node keeps its ``file``/``line`` even if a later tier
  mentions the same id with less detail.
* links are unioned by their whole content, not by
  ``(source, target, type)``. Two ``connects`` edges between the same
  instance and port that differ in ``formal``/``actual`` are different
  facts and both survive; byte-identical duplicates collapse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: Tier order used when merging. Earlier tiers win attribute conflicts:
#: the design tier is the authority on anything it can see (it parsed
#: the RTL), the config tier on what YAML declares, and the extractor's
#: binding tier fills the gaps.
TIER_ORDER = ("design", "config", "binding")

#: ``generator.tier`` stamped on a merged graph. The contract names the
#: three producing tiers; a union of them is none of those, so it gets
#: its own value plus a ``generator.tiers`` list naming the members.
MERGED_TIER = "merged"


def tier_sort_key(tier: str) -> tuple[int, str]:
    """Sort key placing known tiers in :data:`TIER_ORDER`, rest after."""
    try:
        return (TIER_ORDER.index(tier), tier)
    except ValueError:
        return (len(TIER_ORDER), tier)


def _link_key(link: dict) -> str:
    """Canonical form of a link, used as its dedup key."""
    return json.dumps(link, sort_keys=True, ensure_ascii=True)


def merge_graphs(
    tier_graphs: list[tuple[str, dict]],
    *,
    generator: dict,
    schema_version: int,
    project_root_rel: str = ".",
) -> dict:
    """Union ``tier_graphs`` into one node-link graph.

    Args:
      tier_graphs: ``(tier name, node-link graph)`` pairs. Processed in
        :data:`TIER_ORDER`; attribute conflicts resolve to the earlier
        tier.
      generator: ``{"tool", "version"}`` of the merging tool. ``tier``
        and ``tiers`` are filled in here.
      schema_version: Value for ``graph.schema_version``.
      project_root_rel: Project root relative to where the merged file
        will be written (``"../.."`` for the contracted
        ``artefacts/graph/graph.json``).

    Returns:
      dict: the merged NetworkX node-link payload. Nodes are sorted by
      id and links by their canonical form, so an unchanged project
      re-merges to byte-identical output.
    """
    ordered = sorted(tier_graphs, key=lambda item: tier_sort_key(item[0]))

    nodes: dict[str, dict] = {}
    node_tiers: dict[str, list[str]] = {}
    links: dict[str, dict] = {}
    provenance: list[dict] = []

    for tier, graph in ordered:
        block = graph.get("graph") or {}
        entry: dict = {"tier": tier}
        if block.get("generator"):
            entry["generator"] = block["generator"]
        # rtl-buddy-view records which top it elaborated; keeping it is
        # what lets a consumer of the merged file tell "this graph covers
        # blk_a and blk_b" without reopening the meta sidecar.
        if block.get("design"):
            entry["design"] = block["design"]
        provenance.append(entry)

        for node in graph.get("nodes") or []:
            node_id = node.get("id")
            if not node_id:
                continue
            merged = nodes.get(node_id)
            if merged is None:
                merged = dict(node)
                merged.setdefault("tier", tier)
                nodes[node_id] = merged
                node_tiers[node_id] = [tier]
                continue
            if tier not in node_tiers[node_id]:
                node_tiers[node_id].append(tier)
            incoming_type = node.get("type")
            if incoming_type and merged.get("type") != incoming_type:
                log_event(
                    logger,
                    logging.WARNING,
                    "graph_merge.node_type_conflict",
                    node=node_id,
                    first_type=merged.get("type"),
                    second_type=incoming_type,
                    tier=tier,
                )
            for key, value in node.items():
                if value is None:
                    continue
                merged.setdefault(key, value)

        for link in graph.get("links") or []:
            if not link.get("source") or not link.get("target"):
                continue
            links.setdefault(_link_key(link), link)

    # A node seen by more than one tier IS a stitch point; naming the
    # contributors makes that visible to a consumer without diffing the
    # per-tier files. Single-tier nodes stay clean.
    for node_id, tiers in node_tiers.items():
        if len(tiers) > 1:
            nodes[node_id]["tiers"] = sorted(tiers, key=tier_sort_key)

    merged_generator = dict(generator)
    merged_generator["tier"] = MERGED_TIER
    # One tier may contribute more than one graph (the binding tier has
    # two producers: the extractor, and rtl_buddy's post-merge binding stage),
    # so name each tier once. `graph.tiers` below keeps both provenance
    # entries — that is where "who produced what" belongs.
    merged_generator["tiers"] = list(dict.fromkeys(tier for tier, _ in ordered))

    return {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": schema_version,
            "generator": merged_generator,
            "project_root_rel": project_root_rel,
            "tiers": provenance,
        },
        "nodes": [nodes[key] for key in sorted(nodes)],
        "links": [links[key] for key in sorted(links)],
    }


def stitch_points(tier_graphs: list[tuple[str, dict]]) -> list[str]:
    """Node ids that actually join two tiers, sorted.

    Takes the **per-tier** graphs, not the merged one, because the
    joining evidence is destroyed by the union: once merged, a link no
    longer says which tier contributed it.

    An id qualifies when either

    * more than one tier defines a node with it (both tiers know the
      thing), or
    * one tier defines it and a *different* tier's link references it.

    The second case is the one that matters, and the reason a merged-graph-only
    implementation would report zero: the config tier never creates
    ``module:`` nodes, it only points its config->design stitches
    (``maps_to`` / ``elaborates_as`` / ``targets``) at them. The design
    tier defines them. That asymmetry *is* the stitch, so a count of
    "nodes both tiers emitted" would always be 0 on a healthy project.
    """
    defined: dict[str, set[str]] = {}
    referenced: dict[str, set[str]] = {}
    for tier, graph in tier_graphs:
        for node in graph.get("nodes") or []:
            node_id = node.get("id")
            if node_id:
                defined.setdefault(node_id, set()).add(tier)
        for link in graph.get("links") or []:
            for end in ("source", "target"):
                value = link.get(end)
                if value:
                    referenced.setdefault(value, set()).add(tier)
    return sorted(
        node_id
        for node_id, tiers in defined.items()
        if len(tiers) > 1 or (referenced.get(node_id, set()) - tiers)
    )


def dangling_targets(graph: dict) -> list[str]:
    """Link endpoints with no node of their own, sorted.

    A config-tier-only export leaves every config->design stitch's target dangling
    by design; after a merge with the design tier the list should be
    empty (or name a model whose ``module:`` never got exported), which
    makes it a cheap health signal for the merged file.
    """
    ids = {n.get("id") for n in graph.get("nodes") or []}
    missing = set()
    for link in graph.get("links") or []:
        for end in ("source", "target"):
            value = link.get(end)
            if value and value not in ids:
                missing.add(value)
    return sorted(missing)


# ---------------------------------------------------------------------------
# Input hashing / fingerprints
# ---------------------------------------------------------------------------


def rel_path(project_root: str | os.PathLike, path: str | os.PathLike) -> str:
    """Repo-relative, posix-separated path (absolute when outside root)."""
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def hash_inputs(
    project_root: str | os.PathLike, paths: list[str] | list[Path]
) -> list[dict]:
    """``[{"path", "sha256"}]`` for ``paths``, de-duplicated and sorted.

    An unreadable input hashes to ``None`` rather than raising: a
    filelist naming a file that vanished should surface in the meta
    sidecar, not abort a build whose other tiers are fine.
    """
    entries = []
    for path in sorted({os.path.realpath(str(p)) for p in paths}):
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            digest = None
        entries.append({"path": rel_path(project_root, path), "sha256": digest})
    return entries


def fingerprint(
    *,
    schema_version: int,
    tools: dict[str, str | None],
    tier_inputs: dict[str, list[dict]],
    selection: dict | None = None,
) -> str:
    """One hash covering every input, tool version and tier selection.

    This is what makes a re-run a no-op: if the fingerprint recorded in
    ``graph-meta.json`` still matches, nothing that could change
    ``graph.json`` **or the sidecar that describes it** has moved. Tool
    versions are part of it on purpose — upgrading rtl-buddy-view can
    change the design tier without any source file changing.

    ``selection`` is what a tier chose to cover, as opposed to what it
    read. The two are usually redundant — narrowing with ``--model``
    drops that model's sources out of ``tier_inputs`` — but not always:
    a tier whose every model opted out has no inputs at all, so the
    selectors that narrowed it move nothing here and a rerun would hand
    back a sidecar whose ``skipped`` list describes the previous
    invocation (#479). Callers must build it from repo-relative
    identities only, or the fingerprint stops reproducing across
    checkouts.
    """
    payload = {
        "schema_version": schema_version,
        "tools": {k: tools[k] for k in sorted(tools)},
        "inputs": {
            tier: [[e["path"], e["sha256"]] for e in tier_inputs[tier]]
            for tier in sorted(tier_inputs)
        },
        "selection": selection or {},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(blob).hexdigest()
