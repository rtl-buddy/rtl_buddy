# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``rb mcp``: rtl_buddy's Model Context Protocol server, a second LLM-facing surface beside ``--machine``.

The server runs over stdio, spawned per agent session from ``.mcp.json``. The stateless tools read ``artefacts/graph/graph.json`` and the results overlay. The ``hub_*`` session tools are listed only when ``.rtl-buddy/hub.json`` names a live hub. Every tool returns the payload of its ``rb --machine`` counterpart inside a result envelope carrying ``rtl_buddy_version``.

:mod:`rtl_buddy.mcp.toolset` holds the tools and imports no SDK. :mod:`rtl_buddy.mcp.server` is the only module that does.
"""

from .toolset import (
    HUB_TOOL_NAMES,
    STATELESS_TOOL_NAMES,
    HubHandle,
    ToolError,
    ToolSpec,
    Toolset,
    build_toolset,
)

__all__ = [
    "HUB_TOOL_NAMES",
    "STATELESS_TOOL_NAMES",
    "HubHandle",
    "ToolError",
    "ToolSpec",
    "Toolset",
    "build_toolset",
]
