# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``rb mcp`` — rtl_buddy's Model Context Protocol server (#380).

The second LLM-facing surface, beside ``--machine``. Machine mode is the
one that works everywhere and is versioned by semver; MCP adds
discoverable tool schemas, removes the per-query shell-permission
prompt, and — the deciding reason — makes rtl_buddy reachable from agent
hosts that cannot invoke ``rb`` at all (IDE agents, web-hosted agents).

Three decisions shape this package.

**Stdio, not a daemon.** The server is spawned per agent session from a
static ``.mcp.json`` (``command: rb, args: [mcp]``). Answering "what
instantiates X?" must not require a running hub — that would be a
regression against ``rb hier-query`` on a CI or dispatch node — so the
stateless tools read ``artefacts/graph/graph.json`` plus the results
overlay and shell out to ``rtl-buddy-view`` exactly as the CLI does.

**Hub capabilities dial in.** When a live hub is discovered through
``.rtl-buddy/hub.json`` — the same record ``rb hub send`` uses — the
session tools (``hub_state``, ``hub_select``, ``hub_open_source``,
``hub_resolve``, ``hub_diagnose``) appear in the tool listing and drive
the schematic the user is looking at. Headless they are simply absent.
The hub's server-only invariant holds: this is one more peer dialling
in, never the hub dialling out.

**One payload vocabulary.** Every tool returns the payload shape its
``rb --machine`` counterpart returns, wrapped in a thin result envelope
carrying ``rtl_buddy_version``. The two surfaces cannot drift because
they call the same functions.

:mod:`rtl_buddy.mcp.toolset` holds the tools and is pure Python — it
imports no SDK, so the tool set is testable (and the schemas checkable)
on a machine that has never installed ``mcp``.
:mod:`rtl_buddy.mcp.server` is the only module that touches the SDK.
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
