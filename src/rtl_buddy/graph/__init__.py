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

__all__ = [
    "BINDING_TIER",
    "CONFIG_TIER",
    "DESIGN_TIER",
    "GRAPH_JSON_NAME",
    "GRAPH_META_NAME",
    "MERGED_TIER",
    "PY_NODE_PREFIX",
    "PYTHON_MODULE_TYPE",
    "SCHEMA_VERSION",
    "TIER_ORDER",
    "VIEW_GRAPH_MIN_VERSION",
    "BindingStage",
    "ConfigTier",
    "GraphBuild",
    "TierReport",
    "bind_python",
    "build_graph",
    "build_config_tier",
    "dangling_targets",
    "default_graph_dir",
    "extract_config_tier",
    "merge_graphs",
    "models_from_design_tree",
    "models_from_regression",
    "scan_python_source",
    "serialize_graph",
    "stitch_points",
    "write_graph_json",
    "write_graph_meta",
]
