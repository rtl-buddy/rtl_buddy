# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The MCP SDK boundary — the only module here that imports ``mcp``.

Everything the server *answers* lives in :mod:`rtl_buddy.mcp.toolset` as
plain Python. This module does two things: transcribe those specs onto
the wire, and run the stdio transport.

The SDK is an **optional dependency** (``pip install rtl_buddy[mcp]``).
It is imported lazily and behind :func:`require_sdk`, so a project that
never serves MCP does not pay for it and — more to the point — an
``import rtl_buddy`` on a machine without it does not explode.

The SDK's own API moved between its 1.x and 2.x lines: 1.x registered
handlers with ``@server.list_tools()`` decorators, 2.x passes
``on_list_tools`` / ``on_call_tool`` to the constructor. Both are
supported by :func:`build_server`, which is why the tool *definitions*
are kept out of this file: a third API shape should cost one adapter,
not a rewrite of the tools.
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
    """Fail with an actionable message when the SDK is absent.

    A missing optional dependency is a configuration error, not a crash:
    :class:`FatalRtlBuddyError` exits 2 with the install hint and the
    reminder that ``--machine`` answers the same questions.
    """
    if not sdk_available():
        raise FatalRtlBuddyError(_MISSING_SDK_HINT)


def _text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_payload(spec: ToolSpec) -> dict:
    """One tool's wire form (camelCase, as MCP spells it)."""
    return spec.to_mcp_dict()


def result_payload(envelope: dict) -> dict:
    """A tool result on the wire.

    The envelope goes out as one pretty-printed JSON text block *and* as
    ``structuredContent``, because MCP hosts are split on which they
    read. ``isError`` mirrors the envelope's ``ok`` so a host that only
    looks at the flag still sees the failure.
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
    """Construct an SDK ``Server`` wired to ``toolset``.

    Returns the SDK object. Raises :class:`FatalRtlBuddyError` when the
    SDK is not installed.
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

    # --- SDK 1.x: handlers are registered with decorators --------------
    # ``Server.list_tools`` is the 1.x decorator factory; 2.x dropped it
    # in favour of constructor callbacks. Its presence is the version
    # test — cheaper and more honest than parsing ``mcp.__version__``.
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

    Blocking. Returns 0 on a clean shutdown, including the ``Ctrl-C`` /
    host-hangup case, which is a normal end of session rather than a
    failure.
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
