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
    TierReport,
    build_graph,
    models_from_design_tree,
    models_from_regression,
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
from .merge import (
    MERGED_TIER,
    TIER_ORDER,
    dangling_targets,
    merge_graphs,
    stitch_points,
)
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
    "PYTHON_MODULE_TYPE",
    "RESULTS_OVERLAY_NAME",
    "SCHEMA_VERSION",
    "TIER_ORDER",
    "VIEW_GRAPH_MIN_VERSION",
    "BindingStage",
    "ConfigTier",
    "GraphBuild",
    "ResultsOverlay",
    "TierReport",
    "annotate_graph",
    "bind_python",
    "build_graph",
    "build_config_tier",
    "collect_results",
    "dangling_targets",
    "default_graph_dir",
    "extract_config_tier",
    "load_overlay",
    "merge_graphs",
    "models_from_design_tree",
    "models_from_regression",
    "overlay_for_node",
    "refresh_results_overlay",
    "results_overlay_path",
    "scan_python_source",
    "serialize_graph",
    "stitch_points",
    "write_graph_json",
    "write_graph_meta",
    "write_overlay",
]
