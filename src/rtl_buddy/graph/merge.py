# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Merge design-graph tiers by node-id union.

Tiers share one node-link envelope and join by node id, so the merge needs no name
matching and no optional tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from ..logging_utils import log_event

logger = logging.getLogger(__name__)

# Earlier tiers win attribute conflicts.
TIER_ORDER = ("design", "config", "binding")

# `generator.tier` of a merged graph; `generator.tiers` lists the members.
MERGED_TIER = "merged"


def tier_sort_key(tier: str) -> tuple[int, str]:
    """Sort key placing known tiers in `TIER_ORDER` and the rest after."""
    try:
        return (TIER_ORDER.index(tier), tier)
    except ValueError:
        return (len(TIER_ORDER), tier)


def _link_key(link: dict) -> str:
    """Canonical JSON form of a link, used as its dedup key."""
    return json.dumps(link, sort_keys=True, ensure_ascii=True)


def merge_graphs(
    tier_graphs: list[tuple[str, dict]],
    *,
    generator: dict,
    schema_version: int,
    project_root_rel: str = ".",
) -> dict:
    """Union `tier_graphs` into one node-link graph.

    - Nodes are unioned by `id`. The first tier to set an attribute keeps it; later
      tiers only fill in missing ones.
    - Links are unioned by whole content, not `(source, target, type)`, so `connects`
      edges that differ in `formal`/`actual` both survive and only identical duplicates
      collapse.

    Args:
      tier_graphs: `(tier name, node-link graph)` pairs, processed in `TIER_ORDER`.
      generator: `{"tool", "version"}` of the merging tool; `tier` and `tiers` are filled
        in here.
      schema_version: Value for `graph.schema_version`.
      project_root_rel: Project root relative to the written file (`"../.."` for
        `artefacts/graph/graph.json`).

    Returns:
      The merged payload, with nodes sorted by id and links by canonical form so an
      unchanged project re-merges byte-identically.
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
        # Keep the elaborated top so the merged file says which designs it covers.
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

    # Name the contributing tiers on nodes seen by more than one tier.
    for node_id, tiers in node_tiers.items():
        if len(tiers) > 1:
            nodes[node_id]["tiers"] = sorted(tiers, key=tier_sort_key)

    merged_generator = dict(generator)
    merged_generator["tier"] = MERGED_TIER
    # One tier can contribute several graphs (extractor and binding stage); name each
    # tier once. `graph.tiers` keeps every provenance entry.
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
    """Return sorted ids of nodes that join two tiers.

    Takes the per-tier graphs because the merged graph does not record which tier
    contributed a link. An id qualifies when more than one tier defines a node with it,
    or when one tier defines it and a different tier's link references it. The second
    case covers config-tier `maps_to`/`elaborates_as`/`targets` links pointing at
    design-tier `module:` nodes.
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
    """Return sorted link endpoints that have no node.

    A config-tier-only graph leaves its config-to-design targets dangling; after merging
    with the design tier the list should be empty.
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
    """Return a repo-relative posix path (absolute when outside the root)."""
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def hash_inputs(
    project_root: str | os.PathLike, paths: list[str] | list[Path]
) -> list[dict]:
    """Return `[{"path", "sha256"}]` for `paths`, de-duplicated and sorted.

    An unreadable input hashes to `None` instead of raising.
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
    """Return one hash over every input, tool version and tier selection.

    If it matches the fingerprint in `graph-meta.json`, a re-run is a no-op. `selection`
    is what a tier chose to cover, as opposed to what it read: selectors that leave a
    tier with no inputs change nothing else. It must be built from repo-relative
    identities only, or the fingerprint stops reproducing across checkouts.
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
