# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Design knowledge graph extractors (#375).

Each tier of the graph is produced by a separate extractor and the tiers
are merged by node-id union. This package owns the **config tier**: the
tests / testbenches / models / specs / coverage relationships that the
YAML configs already encode explicitly. The design tier (modules,
instances, ports) is produced by ``rtl-buddy-view``; the binding tier
(Python-level call graph) by Graphify, topped up by
:mod:`rtl_buddy.graph.binding` — the post-merge stage that ties cocotb
tests to their Python modules, those modules to the DUT, and their
``dut.<name>`` accesses to design-tier ports.

:mod:`rtl_buddy.graph.build` is the orchestrator behind ``rb graph
build``: it runs each tier, unions them with
:func:`rtl_buddy.graph.merge.merge_graphs`, and writes
``artefacts/graph/graph.json`` plus its ``graph-meta.json`` sidecar.

:mod:`rtl_buddy.graph.results` owns the volatile half that deliberately
never enters ``graph.json``: ``rb graph results`` reads the per-run
result envelopes and the artefact layout into
``artefacts/graph/results-overlay.json``, keyed by the same test node
ids, and :func:`~rtl_buddy.graph.results.load_overlay` /
:func:`~rtl_buddy.graph.results.overlay_for_node` join the two back up.

:mod:`rtl_buddy.graph.query` is the read side: the ``query`` / ``path`` /
``explain`` verbs behind ``rb graph query`` and the ``rb mcp`` graph
tools, with the overlay joined onto every node they return. Matching is
deterministic keyword scoring — the graph exists to cost fewer tokens
than reading the tree, so searching it must not spend a model call.

The shared JSON envelope is documented in ``docs/concepts/graph.md``.
"""

from .binding import (
    BINDING_TIER,
    PY_NODE_PREFIX,
    PYTHON_MODULE_TYPE,
    BindingStage,
    bind_python,
    scan_python_source,
)
from .build import (
    DESIGN_TIER,
    VIEW_GRAPH_MIN_VERSION,
    GraphBuild,
    TestbenchTarget,
    TierReport,
    build_graph,
    models_from_design_tree,
    models_from_regression,
    testbenches_from_suites,
)
from .config_tier import (
    CONFIG_TIER,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    SCHEMA_VERSION,
    ConfigTier,
    build_config_tier,
    default_graph_dir,
    extract_config_tier,
    serialize_graph,
    write_graph_json,
    write_graph_meta,
)
from .query import (
    DEFAULT_DEPTH,
    DEFAULT_LIMIT,
    DEFAULT_MAX_PATHS,
    MAX_DEPTH,
    QUERY_SCHEMA_VERSION,
    GraphContext,
    GraphIndex,
    GraphQueryError,
    explain,
    load_context,
    load_graph,
    match_nodes,
)
from .merge import (
    MERGED_TIER,
    TIER_ORDER,
    dangling_targets,
    merge_graphs,
    stitch_points,
)
from .query import (
    neighborhood,
    node_summary,
)
from .query import path as query_path
from .query import query as query_graph
from .query import resolve_node, test_status
from .results import (
    OVERLAY_FILETYPE,
    OVERLAY_SCHEMA_VERSION,
    RESULTS_OVERLAY_NAME,
    ResultsOverlay,
    annotate_graph,
    collect_results,
    load_overlay,
    overlay_for_node,
    refresh_results_overlay,
    results_overlay_path,
    write_overlay,
)

__all__ = [
    "BINDING_TIER",
    "CONFIG_TIER",
    "DESIGN_TIER",
    "GRAPH_JSON_NAME",
    "GRAPH_META_NAME",
    "MERGED_TIER",
    "OVERLAY_FILETYPE",
    "OVERLAY_SCHEMA_VERSION",
    "PY_NODE_PREFIX",
    "QUERY_SCHEMA_VERSION",
    "PYTHON_MODULE_TYPE",
    "RESULTS_OVERLAY_NAME",
    "SCHEMA_VERSION",
    "TIER_ORDER",
    "VIEW_GRAPH_MIN_VERSION",
    "DEFAULT_DEPTH",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_PATHS",
    "MAX_DEPTH",
    "BindingStage",
    "ConfigTier",
    "GraphContext",
    "GraphIndex",
    "GraphQueryError",
    "GraphBuild",
    "ResultsOverlay",
    "TestbenchTarget",
    "TierReport",
    "annotate_graph",
    "bind_python",
    "build_graph",
    "build_config_tier",
    "collect_results",
    "dangling_targets",
    "default_graph_dir",
    "explain",
    "extract_config_tier",
    "load_context",
    "load_graph",
    "load_overlay",
    "match_nodes",
    "merge_graphs",
    "models_from_design_tree",
    "models_from_regression",
    "neighborhood",
    "node_summary",
    "overlay_for_node",
    "query_graph",
    "query_path",
    "refresh_results_overlay",
    "resolve_node",
    "results_overlay_path",
    "scan_python_source",
    "serialize_graph",
    "stitch_points",
    "testbenches_from_suites",
    "test_status",
    "write_graph_json",
    "write_graph_meta",
    "write_overlay",
]
