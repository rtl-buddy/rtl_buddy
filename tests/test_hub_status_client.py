"""Tests for ``rtl_buddy.hub.status_client``.

The client does the hello/welcome handshake as ``Origin.CLI``, reads the registry from the welcome, and disconnects. The tests run it against a real :class:`HubServer` and against a dead port.
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
    """Register an origin in the background so the next status query sees it."""
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
    """A status query reports both registered peers and the calling ``cli`` origin."""
    wave_writer = await _register(server, Origin.WAVE)
    src_writer = await _register(server, Origin.SRC)
    try:
        registered = await query_registered_origins(server.host, server.port)
    finally:
        wave_writer.close()
        await wave_writer.wait_closed()
        src_writer.close()
        await src_writer.wait_closed()
    # The CLI origin appears because the query registered it.
    assert "cli" in registered
    assert "wave" in registered
    assert "src" in registered
    # The view peer is not connected in this fixture; the query must not invent it.
    assert "view" not in registered


@pytest.mark.asyncio
async def test_query_against_no_hub_raises():
    """A connect to a dead port raises :class:`HubStatusQueryError`, not a bare ``OSError``."""
    with pytest.raises(HubStatusQueryError, match="connect to"):
        await query_registered_origins("127.0.0.1", 1, timeout=0.5)


@pytest.mark.asyncio
async def test_query_disconnect_clears_cli_slot(server: HubServer):
    """Back-to-back queries succeed; no CLI registration leaks."""
    first = await query_registered_origins(server.host, server.port)
    assert "cli" in first
    # The hub broadcasts ``bye`` on disconnect; let the registry settle before the second hello.
    await asyncio.sleep(0.05)
    second = await query_registered_origins(server.host, server.port)
    assert "cli" in second


def test_display_origins_does_not_include_cli():
    """``cli`` is the status query itself and is excluded from the displayed peers."""
    assert "cli" not in DISPLAY_ORIGINS
    # View, wave and editor (src) are the v1 production peers.
    assert "view" in DISPLAY_ORIGINS
    assert "wave" in DISPLAY_ORIGINS
    assert "src" in DISPLAY_ORIGINS


def test_display_origins_includes_the_graph_pane():
    """``rb hub status`` lists the graph pane."""
    assert "graph" in DISPLAY_ORIGINS


def test_display_origins_includes_the_cov_pane():
    """``rb hub status`` lists the coverage pane."""

    assert "cov" in DISPLAY_ORIGINS


def test_display_origins_includes_the_phys_pane():
    """``rb hub status`` lists the `/phy` pane."""

    assert "phys" in DISPLAY_ORIGINS


def test_display_origins_are_real_protocol_origins():
    """Displayed origins are real protocol origins."""
    from rtl_buddy.hub.protocol import Origin

    values = {o.value for o in Origin}
    assert set(DISPLAY_ORIGINS) <= values
