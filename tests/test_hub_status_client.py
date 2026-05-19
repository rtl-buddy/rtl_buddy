"""Tests for ``rtl_buddy.hub.status_client`` (issue #124).

The status client opens a TCP socket against a running hub, runs the
hello/welcome handshake as ``Origin.CLI``, reads the registry from
the welcome envelope, and disconnects. These tests pin that round
trip against the real :class:`HubServer` fixture and the error path
when no hub is listening.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub.protocol import Envelope, Kind, Origin, encode, new_id
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.status_client import (
    DISPLAY_ORIGINS,
    HubStatusQueryError,
    query_registered_origins,
)


@pytest_asyncio.fixture
async def server() -> AsyncIterator[HubServer]:
    s = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    await s.start()
    serve_task = asyncio.create_task(s.serve_forever())
    try:
        yield s
    finally:
        await s.shutdown()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass


async def _register(server: HubServer, origin: Origin) -> asyncio.StreamWriter:
    """Background-register an origin so the next status query sees it."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    hello = Envelope(
        origin=origin,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={
            "client": origin.value,
            "version": "0.1.0",
            "capabilities": [],
        },
    )
    writer.write(encode(hello).encode("utf-8") + b"\n")
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=1.0)  # consume welcome
    return writer


@pytest.mark.asyncio
async def test_query_returns_registered_origins(server: HubServer):
    """A status query against a hub with two peers registered should
    report both alongside the calling ``cli`` origin."""
    wave_writer = await _register(server, Origin.WAVE)
    src_writer = await _register(server, Origin.SRC)
    try:
        registered = await query_registered_origins(server.host, server.port)
    finally:
        wave_writer.close()
        await wave_writer.wait_closed()
        src_writer.close()
        await src_writer.wait_closed()
    # The CLI origin appears because the query itself just registered.
    assert "cli" in registered
    assert "wave" in registered
    assert "src" in registered
    # The view peer is *not* connected in this fixture — confirm the
    # query doesn't fabricate it.
    assert "view" not in registered


@pytest.mark.asyncio
async def test_query_against_no_hub_raises():
    """A connect to a dead port surfaces :class:`HubStatusQueryError`
    rather than a bare ``OSError`` — the CLI renders the wrapped
    message as a peer-state warning."""
    with pytest.raises(HubStatusQueryError, match="connect to"):
        await query_registered_origins("127.0.0.1", 1, timeout=0.5)


@pytest.mark.asyncio
async def test_query_disconnect_clears_cli_slot(server: HubServer):
    """Status queries are short-lived; back-to-back queries should
    succeed (the previous CLI client has disconnected before the next
    hello runs). Pins the "no leaked CLI registration" guarantee."""
    first = await query_registered_origins(server.host, server.port)
    assert "cli" in first
    # The hub broadcasts ``bye`` on disconnect; let the registry settle
    # before the second hello.
    await asyncio.sleep(0.05)
    second = await query_registered_origins(server.host, server.port)
    assert "cli" in second


def test_display_origins_does_not_include_cli():
    """``cli`` represents the status query itself; rendering it as a
    peer would always be confusing. Pin the exclusion."""
    assert "cli" not in DISPLAY_ORIGINS
    # View / wave / editor (src) are the v1 production peers.
    assert "view" in DISPLAY_ORIGINS
    assert "wave" in DISPLAY_ORIGINS
    assert "src" in DISPLAY_ORIGINS
