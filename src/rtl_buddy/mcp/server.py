# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The MCP SDK boundary: the only module here that imports ``mcp``.

It maps the specs from :mod:`rtl_buddy.mcp.toolset` onto the wire and runs the
stdio transport. The SDK is optional (``pip install rtl_buddy[mcp]``) and is
imported lazily behind :func:`require_sdk`. :func:`build_server` supports SDK
1.x (decorators) and 2.x (constructor callbacks).
"""

from __future__ import annotations

import json
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolset import ToolSpec, Toolset

logger = logging.getLogger(__name__)

#: Server name advertised in the MCP handshake.
SERVER_NAME = "rtl-buddy"

_MISSING_SDK_HINT = (
    "mcp: the Model Context Protocol SDK is not installed. Install it with "
    "`pip install 'rtl_buddy[mcp]'` (or `uv pip install mcp`). Every tool "
    "`rb mcp` serves is also reachable from the CLI with `--machine`, which "
    "needs no extra dependency."
)


def sdk_available() -> bool:
    """Whether the ``mcp`` SDK can be imported."""
    try:
        import mcp.types  # noqa: F401
    except ImportError:
        return False
    return True


def sdk_version() -> str | None:
    try:
        return version("mcp")
    except PackageNotFoundError:
        return None


def require_sdk() -> None:
    """Raise :class:`FatalRtlBuddyError` (exit 2) with an install hint when the SDK is absent."""
    if not sdk_available():
        raise FatalRtlBuddyError(_MISSING_SDK_HINT)


def _text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_payload(spec: ToolSpec) -> dict:
    """One tool's wire form, with camelCase keys."""
    return spec.to_mcp_dict()


def result_payload(envelope: dict) -> dict:
    """A tool result on the wire.

    The envelope is sent both as a JSON text block and as ``structuredContent``.
    ``isError`` is the negation of the envelope's ``ok``.
    """
    return {
        "content": [_text_content(json.dumps(envelope, indent=2, ensure_ascii=True))],
        "structuredContent": envelope,
        "isError": not envelope.get("ok", False),
    }


INSTRUCTIONS = (
    "rtl_buddy serves a project's design knowledge graph: modules, instances "
    "and ports from the elaborated RTL, plus tests, testbenches, models, spec "
    "blocks and coverage items from the project's YAML, in one graph with one "
    "id namespace. Start with graph_status, then graph_query to locate "
    "anything, graph_explain for one node's full context, and graph_path to "
    "see how two things are related. Prefer these over reading RTL or YAML "
    "files: every answer carries the file and line to cite, and test nodes "
    "carry their last regression status. Use source_snippet to quote source "
    "once the graph has told you where to look."
)


def _rtl_buddy_version() -> str:
    try:
        return version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


def build_server(toolset: Toolset):
    """Return an SDK ``Server`` wired to ``toolset``.

    Raises :class:`FatalRtlBuddyError` when the SDK is not installed.
    """
    require_sdk()
    import mcp.types as types
    from mcp.server import Server

    def _list() -> Any:
        return types.ListToolsResult.model_validate(
            {"tools": [tool_payload(spec) for spec in toolset.specs()]}
        )

    def _call(name: str, arguments: dict | None) -> Any:
        envelope = toolset.call(name, arguments or {})
        return types.CallToolResult.model_validate(result_payload(envelope))

    server_version = _rtl_buddy_version()

    # ``Server.list_tools`` exists only in SDK 1.x; its presence is the version test.
    if not hasattr(Server, "list_tools"):

        async def on_list_tools(_ctx, _params=None):
            return _list()

        async def on_call_tool(_ctx, params):
            return _call(params.name, getattr(params, "arguments", None))

        return Server(
            SERVER_NAME,
            version=server_version,
            instructions=INSTRUCTIONS,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )

    server = Server(SERVER_NAME, version=server_version, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools():  # pragma: no cover - exercised only on SDK 1.x
        return [
            types.Tool.model_validate(tool_payload(spec)) for spec in toolset.specs()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None):  # pragma: no cover
        envelope = toolset.call(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(envelope, indent=2))]

    return server


def serve_stdio(toolset: Toolset) -> int:
    """Run the stdio MCP server until the client disconnects.

    Blocks. Returns 0 on clean shutdown, including ``Ctrl-C`` and host hangup.
    """
    require_sdk()
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(toolset)
    log_event(
        logger,
        logging.INFO,
        "mcp.serve_start",
        transport="stdio",
        tools=len(toolset.specs()),
        hub=toolset.hub.present,
        sdk=sdk_version(),
    )

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    try:
        anyio.run(_run)
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        pass
    log_event(logger, logging.INFO, "mcp.serve_stop", transport="stdio")
    return 0


__all__ = [
    "INSTRUCTIONS",
    "SERVER_NAME",
    "build_server",
    "require_sdk",
    "result_payload",
    "sdk_available",
    "sdk_version",
    "serve_stdio",
    "tool_payload",
]
