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
(Python-level call graph) by Graphify.

The shared JSON envelope is documented in ``docs/concepts/graph.md``.
"""

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

__all__ = [
    "CONFIG_TIER",
    "GRAPH_JSON_NAME",
    "GRAPH_META_NAME",
    "SCHEMA_VERSION",
    "ConfigTier",
    "build_config_tier",
    "default_graph_dir",
    "extract_config_tier",
    "serialize_graph",
    "write_graph_json",
    "write_graph_meta",
]
