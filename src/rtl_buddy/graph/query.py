# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Query verbs over the merged design knowledge graph (#380).

``rb graph build`` writes the graph; this module is how anything reads
it back. Three verbs, deliberately the same three an agent asks in
prose:

* ``query`` — "where is X?" Keyword match over node ids, labels, types
  and files, then a bounded neighbourhood expansion so the answer
  arrives with its context attached.
* ``path`` — "how are A and B related?" The shortest chain of edges
  between two nodes, which is the question a hierarchy dump cannot
  answer without being read end to end.
* ``explain`` — "what is this node?" One node's attributes, every edge
  on it with the far endpoint resolved, and its last regression result.

Three rules hold this module together — plus one about size.

**Payloads are lean by default, expanded on request (#388).** The #381
measurement found the graph route losing to grep on every task for one
reason: an ``explain`` payload embedded a complete node summary for
every edge endpoint, so it cost ~1k tokens whatever it described.
``explain`` and the ``query`` neighbourhood therefore report an edge as
its type plus the peer's id/label/type — enough to decide whether to
hop — and ``expand=True`` (CLI ``--expand``) restores the full peer
summaries for the caller who wants one round-trip. A node's *own*
type-specific attributes, by contrast, are cheap (tens of tokens) and
were previously only reachable through a whole ``explain`` call, so
every full node summary now carries them.

**Deterministic, never a model.** Matching is keyword scoring with a
fixed rubric and ties broken on the node id, so the same question over
the same graph returns the same bytes on every machine. The point of the
graph is to cost fewer tokens than reading the tree — spending an LLM
call to search it would defeat the exercise.

**The overlay is joined, never merged.** Volatile results live in
``results-overlay.json`` (#379) and are attached to the *answer*, never
written back into ``graph.json``. Every node in every payload here
carries a ``results`` key when the overlay knows about it, which is what
lets "which tests cover coverage-item X, and what is their last status"
be one round-trip instead of two. Since #402 the same overlay carries
the coverage join, so a node also carries ``coverage`` when the run's
coverage model has something to say about it — a module's ratio, or a
coverage item's exercised / declared-only verdict.

**Every answer is citable.** Nodes carry ``file``/``line`` from their
tier, and instance nodes additionally carry the exact
``rb hier-query <top> source-snippet <path>`` command that quotes them.
Locating in the graph and citing from source is the documented agent
workflow (``docs/agents.md``); the payload hands over the second half
rather than leaving the agent to reconstruct it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .config_tier import GRAPH_JSON_NAME, default_graph_dir
from .coverage import coverage_block, coverage_for_node
from .merge import rel_path
from .results import RESULTS_OVERLAY_NAME, load_overlay, overlay_for_node

logger = logging.getLogger(__name__)

#: Bumped when a payload's shape changes incompatibly. Rides on every
#: envelope so an agent surface (``--machine``, ``rb mcp``) can tell.
#: 2: lean edges/neighbours by default — peer id/label/type instead of a
#: full embedded summary (#388); full node summaries carry their own
#: ``attributes``; neighbour truncation reports what it dropped.
QUERY_SCHEMA_VERSION = 2

#: Matches returned by ``rb graph query`` before truncation.
DEFAULT_LIMIT = 10

#: Hops of neighbourhood expansion around each match.
DEFAULT_DEPTH = 1

#: Hard ceiling on ``--depth``. Three hops already crosses test ->
#: testbench -> model -> module; beyond that a neighbourhood is the whole
#: graph and the answer stops being an answer.
MAX_DEPTH = 3

#: Neighbours reported per match before truncation.
DEFAULT_MAX_NEIGHBORS = 25

#: Shortest paths reported by ``rb graph path``.
DEFAULT_MAX_PATHS = 3

_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:#/\\-]*")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")

#: Question words and connectives. Dropped before scoring so "which
#: tests cover A-COV-1" scores on ``a-cov-1`` alone and not on every node
#: whose file path happens to contain "for".
_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "find",
        "for",
        "from",
        "get",
        "give",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "last",
        "list",
        "me",
        "my",
        "of",
        "on",
        "or",
        "show",
        "status",
        "than",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "up",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

#: Words an agent naturally writes -> the node type they mean. A hit is
#: a *preference*, never a filter: "which tests cover A-COV-1" must still
#: find the coverage item (the tests hang off it), so the word only
#: promotes nodes of that type among things that matched on their own.
_TYPE_WORDS = {
    "block": "spec_block",
    "blocks": "spec_block",
    "covitem": "coverage_item",
    "coverage": "coverage_item",
    "coverage-item": "coverage_item",
    "coverage-items": "coverage_item",
    "doc": "spec_doc",
    "docs": "spec_doc",
    "golden": "golden_model",
    "iface": "interface",
    "instance": "instance",
    "instances": "instance",
    "interface": "interface",
    "interfaces": "interface",
    "model": "model",
    "models": "model",
    "modport": "modport",
    "module": "module",
    "modules": "module",
    "param": "parameter",
    "parameter": "parameter",
    "parameters": "parameter",
    "params": "parameter",
    "port": "port",
    "ports": "port",
    "python": "python_module",
    "spec": "spec_block",
    "specs": "spec_block",
    "suite": "suite",
    "suites": "suite",
    "tb": "testbench",
    "test": "test",
    "testbench": "testbench",
    "testbenches": "testbench",
    "tests": "test",
}


class GraphQueryError(FatalRtlBuddyError):
    """A question the graph cannot answer as asked.

    Carries ``candidates`` when the failure was an ambiguous or unknown
    node reference, so a caller (the CLI, the MCP server) can show the
    near misses instead of only the miss.
    """

    def __init__(self, message: str, *, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def resolve_graph_path(
    project_root: str | os.PathLike, graph_path: str | os.PathLike | None = None
) -> Path:
    """Where ``graph.json`` is, given whatever the caller knows.

    Accepts the file itself, the directory holding it, or nothing at all
    (``<project root>/artefacts/graph/graph.json``) — the same latitude
    :func:`~rtl_buddy.graph.results.load_overlay` gives, so a caller that
    only knows the project root never has to build the path.
    """
    if graph_path is None:
        return default_graph_dir(project_root) / GRAPH_JSON_NAME
    candidate = Path(graph_path)
    if candidate.is_dir():
        return candidate / GRAPH_JSON_NAME
    return candidate


def load_graph(path: str | os.PathLike) -> dict:
    """Read a node-link graph, or fail with the command that makes one."""
    candidate = Path(path)
    try:
        payload = json.loads(candidate.read_text())
    except FileNotFoundError:
        raise GraphQueryError(
            f"graph: no graph at {candidate}; run `rb graph build` first"
        )
    except OSError as exc:
        raise GraphQueryError(f"graph: cannot read {candidate}: {exc}")
    except json.JSONDecodeError as exc:
        raise GraphQueryError(
            f"graph: {candidate} is not valid JSON ({exc}); "
            f"re-run `rb graph build --force`"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise GraphQueryError(
            f"graph: {candidate} is not node-link JSON (no 'nodes' list); "
            f"re-run `rb graph build --force`"
        )
    payload.setdefault("links", [])
    return payload


@dataclass
class GraphIndex:
    """Node/adjacency lookup over one loaded graph.

    Built once per command and shared by all three verbs. Adjacency is
    kept per direction because the direction *is* information: a ``test``
    node with an incoming ``declares`` belongs to a suite, and the same
    edge read the other way says the suite owns the test.
    """

    graph: dict
    nodes: dict[str, dict] = dc_field(default_factory=dict)
    out: dict[str, list[dict]] = dc_field(default_factory=dict)
    inc: dict[str, list[dict]] = dc_field(default_factory=dict)

    @classmethod
    def build(cls, graph: dict) -> "GraphIndex":
        index = cls(graph=graph)
        for node in graph.get("nodes") or []:
            node_id = node.get("id")
            if isinstance(node_id, str):
                index.nodes.setdefault(node_id, node)
        for link in graph.get("links") or []:
            source, target = link.get("source"), link.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            index.out.setdefault(source, []).append(link)
            index.inc.setdefault(target, []).append(link)
            # A link endpoint with no node of its own is a dangling
            # target (``merge.dangling_targets``). It is still a real
            # answer — a config->design stitch into a design tier that
            # was never exported names a module that exists — so it is indexed as
            # an attribute-less node rather than dropped.
            for endpoint in (source, target):
                if endpoint not in index.nodes:
                    index.nodes[endpoint] = {"id": endpoint, "dangling": True}
        return index

    def node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def edges(self, node_id: str) -> list[tuple[str, dict]]:
        """``(direction, link)`` for every edge touching ``node_id``."""
        return [("out", link) for link in self.out.get(node_id, ())] + [
            ("in", link) for link in self.inc.get(node_id, ())
        ]

    def models_yaml_for(self, module_name: str) -> str | None:
        """The ``models.yaml`` declaring ``module_name``, via ``maps_to``.

        The config tier's ``maps_to`` edge is the only thing that knows
        which ``models.yaml`` a design-tier module came from, and
        ``rb hier-query`` needs it (``-c``) whenever it is not run from
        that file's directory. Without this the ``cite`` command a
        payload hands back would only work from one directory, which is
        not a citation an agent can use.

        This is the **model** stitch specifically, which is why the
        split of the config->design edge into three verbs matters here:
        a testbench (``elaborates_as``) or a non-simulation run
        (``targets``) also points at ``module:<name>``, and their
        ``file`` is a ``tests.yaml`` / ``synth.yaml`` that ``-c`` must
        never be filled from. The edge type rules them out on its own.
        """
        target = f"module:{module_name}"
        for link in self.inc.get(target, ()):
            if link.get("type") != "maps_to":
                continue
            model_node = self.nodes.get(str(link.get("source"))) or {}
            path = model_node.get("file")
            if path:
                return str(path)
            # Fall back to the id's own encoding, `model:<path>#<name>`.
            source = str(link.get("source", ""))
            if source.startswith("model:") and "#" in source:
                return source[len("model:") :].split("#", 1)[0]
        return None


@dataclass
class GraphContext:
    """A loaded graph plus the results overlay beside it."""

    project_root: Path
    graph_path: Path
    graph: dict
    index: GraphIndex
    overlay: dict | None = None
    overlay_path: Path | None = None

    def envelope(self) -> dict:
        """Header keys every payload from this context carries."""
        return {
            "schema_version": QUERY_SCHEMA_VERSION,
            "graph": rel_path(self.project_root, self.graph_path),
            "overlay": (
                rel_path(self.project_root, self.overlay_path)
                if self.overlay_path is not None and self.overlay is not None
                else None
            ),
            "counts": {
                "nodes": len(self.graph.get("nodes") or []),
                "links": len(self.graph.get("links") or []),
            },
        }

    def results_for(self, node_id: str) -> dict | None:
        return overlay_for_node(self.overlay, node_id)

    def coverage_for(self, node_id: str) -> dict | None:
        return coverage_for_node(self.overlay, node_id)

    def coverage(self) -> dict | None:
        """The overlay's coverage block, minus its per-node maps.

        The header a payload can afford to repeat: which manifest the
        numbers came from, the run totals, and the declared-vs-observed
        tally. The ``nodes`` map itself is joined per node instead — it
        is as long as the graph.
        """
        block = coverage_block(self.overlay)
        if block is None:
            return None
        return {k: v for k, v in block.items() if k not in ("nodes", "undeclared")}


def load_context(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
    with_results: bool = True,
) -> GraphContext:
    """Load ``graph.json`` and, unless told not to, the overlay next to it.

    A missing overlay is never an error: the graph is fully queryable
    without one, and "no results known" is a state every consumer has to
    handle anyway.
    """
    root = Path(os.path.realpath(str(project_root)))
    resolved = resolve_graph_path(root, graph_path)
    graph = load_graph(resolved)
    overlay = None
    overlay_file = None
    if with_results:
        overlay_file = (
            Path(overlay_path)
            if overlay_path is not None
            else resolved.parent / RESULTS_OVERLAY_NAME
        )
        overlay = load_overlay(overlay_file)
    return GraphContext(
        project_root=root,
        graph_path=resolved,
        graph=graph,
        index=GraphIndex.build(graph),
        overlay=overlay,
        overlay_path=overlay_file,
    )


# ---------------------------------------------------------------------------
# keyword matching
# ---------------------------------------------------------------------------


def tokenize(question: str) -> tuple[list[str], set[str]]:
    """``question`` -> (scoring terms, node-type preferences).

    Type words are pulled *out* of the scoring terms: "which tests cover
    A-COV-1" must score on ``a-cov-1``, not on every node whose id
    contains "test".
    """
    terms: list[str] = []
    hints: set[str] = set()
    for raw in _TOKEN_RE.findall(question or ""):
        token = raw.lower().strip(".:#-/")
        if not token or len(token) < 2 or token in _STOPWORDS:
            continue
        if token in _TYPE_WORDS:
            hints.add(_TYPE_WORDS[token])
            continue
        if token not in terms:
            terms.append(token)
    return terms, hints


def _tokens_of(text: str) -> set[str]:
    return {part for part in _SPLIT_RE.split(text) if part}


def score_node(node: dict, terms: list[str], type_hints: set[str]) -> int:
    """Fixed keyword rubric — no learning, no ranking model.

    The tiers are: an exact id beats an exact label beats a whole-word
    hit beats a substring, and a match in the id beats the same match in
    a file path. A node's *type* only adds to a score that is already
    non-zero, so a type word can promote but never conjure a match.
    """
    node_id = str(node.get("id", "")).lower()
    label = str(node.get("label", "")).lower()
    # An indexed collision label (`tb_top(3)`) carries the design's real
    # name in base_label; it scores at the same tiers as label, so the
    # rubric is unchanged for the node class a name query most likely
    # means.
    base_label = str(node.get("base_label", "")).lower()
    file_path = str(node.get("file", "")).lower()
    id_tokens = _tokens_of(node_id)
    label_tokens = _tokens_of(label) | _tokens_of(base_label)

    score = 0
    for term in terms:
        if term == node_id:
            score += 100
            continue
        if (label and term == label) or (base_label and term == base_label):
            score += 60
            continue
        if term in label_tokens:
            score += 40
        elif term in id_tokens:
            score += 30
        elif (label and term in label) or (base_label and term in base_label):
            score += 20
        elif term in node_id:
            score += 12
        elif file_path and term in file_path:
            score += 6
    if score and node.get("type") in type_hints:
        score += 15
    return score


def match_nodes(
    ctx: GraphContext,
    question: str,
    *,
    node_type: str | None = None,
    tier: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[tuple[int, dict]], list[str], set[str], int]:
    """Score every node, return the best ``limit`` of them.

    Returns ``(scored, terms, type_hints, total_matched)``. Ordering is
    ``(-score, id)`` — the id tie-break is what makes two runs over the
    same graph byte-identical.

    With no scoring terms left (``rb graph query "list all tests"``) the
    question degenerates to "everything of this type", which is a useful
    answer rather than an error.
    """
    terms, type_hints = tokenize(question)
    scored: list[tuple[int, dict]] = []
    for node in ctx.index.nodes.values():
        if node_type is not None and node.get("type") != node_type:
            continue
        if tier is not None and node.get("tier") != tier:
            continue
        if not terms:
            # No terms: an explicit --type/--tier filter (or a bare type
            # word) is the whole question.
            if node_type is None and tier is None and not type_hints:
                continue
            if type_hints and node.get("type") not in type_hints:
                continue
            scored.append((0, node))
            continue
        score = score_node(node, terms, type_hints)
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id", ""))))
    return scored[: max(limit, 0)], terms, type_hints, len(scored)


# ---------------------------------------------------------------------------
# payload building blocks
# ---------------------------------------------------------------------------

#: Node attributes lifted into the top level of every summary. The rest
#: of a node — its type-specific attributes (a port's ``dir``, a test's
#: ``reglvl``) — rides under ``attributes`` when the summary is a full
#: one, because those fields are tens of tokens and reading one of them
#: used to cost a whole ``explain`` call (#388).
_SUMMARY_KEYS = ("id", "type", "label", "base_label", "tier", "file", "line")

#: The keys a *lean* reference to a peer node carries: enough to know
#: what it is and whether to hop, nothing that repeats its tier's data.
_LEAN_KEYS = ("id", "type", "label", "base_label")


def _node_attributes(node: dict) -> dict:
    """The node's own type-specific attributes — never a peer's.

    ``None`` values are dropped: a tier that records ``"width": null``
    is saying it knows nothing, and repeating that on every port summary
    is exactly the payload padding #388 removes.
    """
    return {
        key: value
        for key, value in sorted(node.items())
        if key not in _SUMMARY_KEYS and key != "dangling" and value is not None
    }


def cite_hint(node: dict, models_yaml: str | None = None) -> dict | None:
    """How to quote this node's source, if it can be quoted.

    An instance node's id already contains the two arguments
    ``rb hier-query <top> source-snippet <dot.path>`` needs, so the
    payload hands over the command itself — with ``-c <models.yaml>``
    when the graph knows which one declares the model, because without
    it the command only runs from that file's own directory and an agent
    invoked from anywhere else gets a config error instead of a
    citation. Everything else that knows its file reports
    ``file``/``line`` and lets the agent open it. This is the "locate in
    the graph, cite from source" half of the agent contract made
    mechanical.
    """
    node_id = str(node.get("id", ""))
    hint: dict = {}
    if node.get("type") == "instance" and node_id.startswith("inst:"):
        rest = node_id[len("inst:") :]
        if "/" in rest:
            top, path = rest.split("/", 1)
            command = f"rb hier-query {top} source-snippet {path}"
            if models_yaml:
                command += f" -c {models_yaml}"
            hint["command"] = command
    if node.get("file"):
        hint["file"] = node["file"]
        if node.get("line") is not None:
            hint["line"] = node["line"]
    return hint or None


def node_summary(
    ctx: GraphContext,
    node: dict,
    *,
    results: bool = True,
    attributes: bool = False,
) -> dict:
    """The full form of a node: summary keys, cite, joins, own attributes.

    ``attributes=True`` adds the node's *own* type-specific attributes
    (#388) — they are small, and without them a single ``reglvl`` or
    port ``dir`` costs a whole ``explain`` round-trip. ``explain`` keeps
    reporting them at the payload's top level instead, so it passes
    ``False`` for its own node.
    """
    summary = {key: node[key] for key in _SUMMARY_KEYS if node.get(key) is not None}
    if node.get("dangling"):
        summary["dangling"] = True
    models_yaml = None
    node_id = str(node.get("id", ""))
    if node.get("type") == "instance" and node_id.startswith("inst:"):
        models_yaml = ctx.index.models_yaml_for(
            node_id[len("inst:") :].split("/", 1)[0]
        )
    cite = cite_hint(node, models_yaml)
    # ``file``/``line`` already sit on the summary; a cite block earns
    # its bytes only when it adds the runnable command (#388).
    if cite and cite.get("command"):
        summary["cite"] = cite
    if attributes:
        extra = _node_attributes(node)
        if extra:
            summary["attributes"] = extra
    _join_overlay(ctx, summary, node_id, results=results)
    return summary


def _lean_summary(ctx: GraphContext, node: dict, *, results: bool = True) -> dict:
    """A reference to a node: id, label, type — decide, then hop (#388).

    The results/coverage join stays even here: "which tests cover X and
    did they pass" must remain one round-trip, and a status is a few
    tokens where a repeated ``cite``/attribute block is a few hundred.
    """
    summary = {key: node[key] for key in _LEAN_KEYS if node.get(key) is not None}
    if node.get("dangling"):
        summary["dangling"] = True
    _join_overlay(ctx, summary, str(node.get("id", "")), results=results)
    return summary


def _join_overlay(
    ctx: GraphContext, summary: dict, node_id: str, *, results: bool
) -> None:
    if not results:
        return
    entry = ctx.results_for(node_id)
    if entry is not None:
        summary["results"] = entry
    coverage = ctx.coverage_for(node_id)
    if coverage is not None:
        summary["coverage"] = coverage


def _link_summary(direction: str, link: dict, peer_id: str) -> dict:
    summary = {
        "type": link.get("type"),
        "direction": direction,
        "confidence": link.get("confidence"),
        "peer": peer_id,
    }
    for key, value in sorted(link.items()):
        if key in ("source", "target", "type", "confidence", "key"):
            continue
        summary[key] = value
    return {k: v for k, v in summary.items() if v is not None}


def _edge_entry(
    ctx: GraphContext,
    direction: str,
    link: dict,
    peer_id: str,
    *,
    expand: bool,
    results: bool,
) -> dict:
    """One edge of an ``explain`` payload: lean triple, or expanded peer.

    Lean is the default (#388): the link's own facts plus the peer's
    id/label/type. ``expand`` adds the full peer summary — attributes,
    cite, joins — which is the pre-#388 payload for the caller who would
    otherwise ``explain`` every peer anyway.
    """
    peer = ctx.index.node(peer_id) or {"id": peer_id, "dangling": True}
    entry = _link_summary(direction, link, peer_id)
    # The ``outgoing`` / ``incoming`` bucket already says which way the
    # edge points; repeating it on every entry is padding (#388).
    entry.pop("direction", None)
    if peer.get("label") is not None:
        entry["peer_label"] = peer["label"]
    if peer.get("type") is not None:
        entry["peer_type"] = peer["type"]
    if expand:
        entry["node"] = node_summary(ctx, peer, results=results, attributes=True)
    return entry


def _truncation(**buckets: list[dict]) -> dict | None:
    """What a limit cut off — count and kinds, never a silent flag (#388).

    None when nothing was dropped, so the key is simply absent from the
    payload: ``neighbors_truncated`` and ``explain``'s ``truncated``
    spell "nothing was cut" the same way, rather than one omitting the
    key and the other saying ``false``.

    ``kinds`` counts by the entry's own ``type``, and **which vocabulary
    that is depends on the caller**: :func:`neighborhood` drops node
    summaries, so its kinds are *node* types (``test``, ``module``);
    :func:`explain` drops edge entries, so its kinds are *edge* types
    (``covers``, ``runs_on``). One key, two vocabularies — read it
    against the verb that produced it. ``peer_type`` is the fallback for
    an entry carrying no ``type`` of its own.

    Where a verb cuts more than one bucket, ``buckets`` names what each
    lost: an ``explain`` that hits the limit on ``outgoing`` alone is a
    different answer from one that lost both ways, and a single total
    cannot tell them apart.
    """
    total = sum(len(entries) for entries in buckets.values())
    if not total:
        return None
    kinds: dict[str, int] = {}
    for entries in buckets.values():
        for entry in entries:
            key = str(entry.get("type") or entry.get("peer_type") or "unknown")
            kinds[key] = kinds.get(key, 0) + 1
    block = {"dropped": total, "kinds": dict(sorted(kinds.items()))}
    if len(buckets) > 1:
        block["buckets"] = {
            name: len(entries) for name, entries in sorted(buckets.items()) if entries
        }
    return block


def neighborhood(
    ctx: GraphContext,
    node_id: str,
    *,
    depth: int = DEFAULT_DEPTH,
    limit: int = DEFAULT_MAX_NEIGHBORS,
    results: bool = True,
    expand: bool = False,
) -> tuple[list[dict], dict | None]:
    """Breadth-first expansion around one node, both directions.

    Both directions on purpose: "which tests cover this coverage item"
    reads ``covers`` backwards, and an expansion that only followed
    edges forwards would answer half the questions asked of it.

    Returns ``(neighbours, truncation)`` where ``truncation`` is
    ``None`` when nothing was cut, else ``{"dropped": N, "kinds": {...}}``
    describing what the ``limit`` threw away — a truncated answer that
    does not say *what* it lost invites a wrong conclusion cheaply.
    """
    depth = max(0, min(depth, MAX_DEPTH))
    if depth == 0:
        return [], None
    seen = {node_id}
    found: list[dict] = []
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])
    while queue:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for direction, link in sorted(
            ctx.index.edges(current),
            key=lambda pair: (
                str(pair[1].get("type", "")),
                str(pair[1].get("target" if pair[0] == "out" else "source", "")),
            ),
        ):
            peer = link["target"] if direction == "out" else link["source"]
            if peer in seen:
                continue
            seen.add(peer)
            node = ctx.index.node(peer) or {"id": peer, "dangling": True}
            if expand:
                entry = node_summary(ctx, node, results=results, attributes=True)
            else:
                entry = _lean_summary(ctx, node, results=results)
            entry["distance"] = distance + 1
            entry["via"] = _link_summary(direction, link, current)
            found.append(entry)
            queue.append((peer, distance + 1))
    found.sort(key=lambda entry: (entry["distance"], str(entry.get("id", ""))))
    return found[:limit], _truncation(neighbors=found[limit:])


# ---------------------------------------------------------------------------
# node reference resolution
# ---------------------------------------------------------------------------


def resolve_node(ctx: GraphContext, ref: str) -> dict:
    """A user's node reference -> the node, or a loud failure.

    Exact ids win outright; otherwise a bare name (``blk_a``,
    ``t_cocotb``) is matched against labels and against the trailing
    component of every id, because those are what a human types. An
    ambiguous name fails with the candidates rather than picking one —
    silently answering about the wrong ``blk_a`` is worse than asking
    again.
    """
    if ref in ctx.index.nodes:
        return ctx.index.nodes[ref]
    lowered = ref.lower()
    exact_ci = [n for nid, n in ctx.index.nodes.items() if nid.lower() == lowered]
    if len(exact_ci) == 1:
        return exact_ci[0]

    candidates: list[dict] = []
    for node_id, node in ctx.index.nodes.items():
        if (
            str(node.get("label", "")).lower() == lowered
            or str(node.get("base_label", "")).lower() == lowered
        ):
            candidates.append(node)
            continue
        tail = re.split(r"[#/]", node_id)[-1]
        if tail.lower() == lowered or node_id.lower().endswith(":" + lowered):
            candidates.append(node)
    unique = {str(n.get("id")): n for n in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        raise GraphQueryError(
            f"graph: {ref!r} matches {len(unique)} nodes; use a full node id",
            candidates=sorted(unique),
        )

    scored, _terms, _hints, _total = match_nodes(ctx, ref, limit=5)
    raise GraphQueryError(
        f"graph: no node matches {ref!r}",
        candidates=[str(node.get("id")) for _score, node in scored],
    )


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------


def query(
    ctx: GraphContext,
    question: str,
    *,
    node_type: str | None = None,
    tier: str | None = None,
    limit: int = DEFAULT_LIMIT,
    depth: int = DEFAULT_DEPTH,
    max_neighbors: int = DEFAULT_MAX_NEIGHBORS,
    results: bool = True,
    expand: bool = False,
) -> dict:
    """``rb graph query`` — keyword match plus neighbourhood expansion.

    Matches are full summaries (the node asked about earns its
    attributes and cite); neighbours are lean references unless
    ``expand`` says otherwise (#388). A truncated neighbourhood says
    what it dropped.
    """
    scored, terms, type_hints, total = match_nodes(
        ctx, question, node_type=node_type, tier=tier, limit=limit
    )
    matches = []
    for score, node in scored:
        entry = node_summary(ctx, node, results=results, attributes=True)
        entry["score"] = score
        neighbors, truncated = neighborhood(
            ctx,
            str(node.get("id", "")),
            depth=depth,
            limit=max_neighbors,
            results=results,
            expand=expand,
        )
        entry["neighbors"] = neighbors
        if truncated:
            entry["neighbors_truncated"] = truncated
        matches.append(entry)

    log_event(
        logger,
        logging.INFO,
        "graph_query.matched",
        question=question,
        terms=len(terms),
        matches=len(matches),
        total=total,
    )
    return {
        **ctx.envelope(),
        "question": question,
        "terms": terms,
        "type_hints": sorted(type_hints),
        "filters": {"type": node_type, "tier": tier},
        "depth": max(0, min(depth, MAX_DEPTH)),
        "matched": total,
        "truncated": total > len(matches),
        "matches": matches,
    }


def _shortest_paths(
    ctx: GraphContext,
    source: str,
    target: str,
    *,
    directed: bool,
    max_paths: int,
) -> list[list[str]]:
    """Every shortest path from ``source`` to ``target``, capped.

    Undirected by default: the graph's edge directions encode *roles*
    (a suite declares a test, a test runs on a testbench), not
    reachability, so "how is this test related to this module" must be
    allowed to walk an edge backwards. ``--directed`` is there for the
    times the direction is the question.
    """
    if source == target:
        return [[source]]

    def step(node_id: str) -> list[str]:
        peers = [link["target"] for link in ctx.index.out.get(node_id, ())]
        if not directed:
            peers += [link["source"] for link in ctx.index.inc.get(node_id, ())]
        return sorted(set(peers))

    dist = {source: 0}
    queue: deque[str] = deque([source])
    while queue:
        current = queue.popleft()
        if current == target:
            break
        for peer in step(current):
            if peer not in dist:
                dist[peer] = dist[current] + 1
                queue.append(peer)
    if target not in dist:
        return []

    # Walk back from the target along nodes exactly one hop closer,
    # depth-first over sorted ids so the enumeration is stable.
    paths: list[list[str]] = []

    def back(node_id: str, tail: list[str]) -> None:
        if len(paths) >= max_paths:
            return
        if node_id == source:
            paths.append([source] + tail)
            return
        for peer in step(node_id):
            if dist.get(peer) == dist[node_id] - 1:
                back(peer, [node_id] + tail)
                if len(paths) >= max_paths:
                    return

    back(target, [])
    return paths


def _edges_between(ctx: GraphContext, a: str, b: str) -> list[dict]:
    """Every link joining two adjacent path nodes, either way round."""
    edges = []
    for link in ctx.index.out.get(a, ()):
        if link.get("target") == b:
            edges.append(_link_summary("out", link, b))
    for link in ctx.index.inc.get(a, ()):
        if link.get("source") == b:
            edges.append(_link_summary("in", link, b))
    edges.sort(key=lambda e: (str(e.get("type")), str(e.get("direction"))))
    return edges


def path(
    ctx: GraphContext,
    source: str,
    target: str,
    *,
    directed: bool = False,
    max_paths: int = DEFAULT_MAX_PATHS,
    results: bool = True,
) -> dict:
    """``rb graph path`` — the shortest chain of edges between two nodes.

    ``path`` spends where ``explain`` saves: every node on every walk
    carries its full summary *including* ``attributes``. That is the one
    payload in #388 that grew, and deliberately — a path is already
    bounded by ``max_paths`` and by the hop count (the benchmark's
    longest is five), so the attribute blocks are tens of tokens against
    an answer the caller asked to be given whole, and they are precisely
    what the caller would spend an ``explain`` round-trip on next. The
    saving in ``explain`` comes from the opposite case: an unbounded
    fan-out (44 edges on ``module:ip_cdc_sync``) where the peer data is
    a menu to choose from, not the answer.

    Stated rather than measured: none of the benchmark's six tasks calls
    ``graph path``, so this is a reasoned trade and not a number.
    """
    src = resolve_node(ctx, source)
    dst = resolve_node(ctx, target)
    src_id, dst_id = str(src.get("id")), str(dst.get("id"))
    walks = _shortest_paths(
        ctx, src_id, dst_id, directed=directed, max_paths=max(1, max_paths)
    )

    payload_paths = []
    for walk in walks:
        payload_paths.append(
            {
                "length": len(walk) - 1,
                "nodes": [
                    node_summary(
                        ctx,
                        ctx.index.node(node_id) or {"id": node_id, "dangling": True},
                        results=results,
                        attributes=True,
                    )
                    for node_id in walk
                ],
                "edges": [
                    {
                        "source": walk[i],
                        "target": walk[i + 1],
                        "links": _edges_between(ctx, walk[i], walk[i + 1]),
                    }
                    for i in range(len(walk) - 1)
                ],
            }
        )

    log_event(
        logger,
        logging.INFO,
        "graph_query.path",
        source=src_id,
        target=dst_id,
        found=len(payload_paths),
    )
    return {
        **ctx.envelope(),
        "source": node_summary(ctx, src, results=results, attributes=True),
        "target": node_summary(ctx, dst, results=results, attributes=True),
        "directed": directed,
        "found": bool(payload_paths),
        "length": payload_paths[0]["length"] if payload_paths else None,
        "paths": payload_paths,
    }


def explain(
    ctx: GraphContext,
    node_ref: str,
    *,
    results: bool = True,
    limit: int = 200,
    expand: bool = False,
) -> dict:
    """``rb graph explain`` — one node, its edges, its result, its coverage.

    Edges are lean by default (#388): the link's own facts plus the
    peer's id/label/type, because a payload that embedded a full summary
    per endpoint cost ~1k tokens whatever it described — the single
    constant that lost the #381 benchmark. ``expand=True`` restores the
    full peer summaries for the caller who would otherwise ``explain``
    each peer next.

    ``coverage`` is what this node's row of the coverage join says: a
    ratio for a module or instance, an exercised / declared-only
    verdict with the observed cover points behind it for a
    ``covitem:`` node, and ``None`` for everything else. ``coverage_run``
    names the manifest those numbers came from, so an answer can never
    be mistaken for a fresher run than it is.
    """
    node = resolve_node(ctx, node_ref)
    node_id = str(node.get("id"))

    outgoing, incoming = [], []
    for direction, link in ctx.index.edges(node_id):
        peer_id = link["target"] if direction == "out" else link["source"]
        entry = _edge_entry(
            ctx, direction, link, peer_id, expand=expand, results=results
        )
        (outgoing if direction == "out" else incoming).append(entry)
    for bucket in (outgoing, incoming):
        bucket.sort(key=lambda e: (str(e.get("type")), str(e.get("peer"))))

    degree: dict[str, dict[str, int]] = {"out": {}, "in": {}}
    for direction, bucket in (("out", outgoing), ("in", incoming)):
        for entry in bucket:
            key = str(entry.get("type"))
            degree[direction][key] = degree[direction].get(key, 0) + 1

    attributes = _node_attributes(node)
    summary = node_summary(ctx, node, results=results)

    log_event(
        logger,
        logging.INFO,
        "graph_query.explain",
        node=node_id,
        out_edges=len(outgoing),
        in_edges=len(incoming),
    )
    truncated = _truncation(outgoing=outgoing[limit:], incoming=incoming[limit:])
    return {
        **ctx.envelope(),
        "node": summary,
        "attributes": attributes,
        "results": summary.get("results"),
        "coverage": summary.get("coverage"),
        "coverage_run": ctx.coverage(),
        "degree": degree,
        "outgoing": outgoing[:limit],
        "incoming": incoming[:limit],
        # Absent when nothing was cut — the spelling `neighbors_truncated`
        # already uses, rather than a second way to say the same thing.
        **({"truncated": truncated} if truncated else {}),
    }


def test_status(
    ctx: GraphContext,
    *,
    test: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> dict:
    """The results overlay on its own, optionally filtered.

    The one question that is pure overlay — "what passed last night" —
    without making the caller match a node first. ``test`` accepts a full
    ``test:<suite>#<name>`` id or a bare test name; ``status`` filters on
    the envelope verdict.

    Each entry carries its ``coverage`` scalars when the run wrote a
    coverage model (#402), and ``coverage_run`` names the manifest they
    came from — "did it pass" and "what did it cover" are the same
    question asked twice, and answering them in one payload is the
    difference between one round-trip and two.
    """
    entries = list(((ctx.overlay or {}).get("tests") or {}).values())
    if test:
        lowered = test.lower()
        entries = [
            entry
            for entry in entries
            if str(entry.get("id", "")).lower() == lowered
            or str(entry.get("test", "")).lower() == lowered
        ]
    if status:
        wanted = status.upper()
        entries = [
            entry for entry in entries if str(entry.get("status", "")).upper() == wanted
        ]
    entries.sort(key=lambda entry: str(entry.get("id", "")))
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("status", "UNKNOWN"))
        counts[key] = counts.get(key, 0) + 1
    return {
        **ctx.envelope(),
        "filters": {"test": test, "status": status},
        "available": ctx.overlay is not None,
        "matched": len(entries),
        "statuses": counts,
        "coverage_run": ctx.coverage(),
        "with_coverage": sum(1 for entry in entries if entry.get("coverage")),
        "truncated": len(entries) > limit,
        "tests": entries[:limit],
    }


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_NEIGHBORS",
    "DEFAULT_MAX_PATHS",
    "MAX_DEPTH",
    "QUERY_SCHEMA_VERSION",
    "GraphContext",
    "GraphIndex",
    "GraphQueryError",
    "cite_hint",
    "explain",
    "load_context",
    "load_graph",
    "match_nodes",
    "neighborhood",
    "node_summary",
    "path",
    "query",
    "resolve_graph_path",
    "resolve_node",
    "score_node",
    "test_status",
    "tokenize",
]
