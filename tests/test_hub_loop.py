"""Tests for the ``rtl_buddy.hub.loop`` start, serve and shutdown sequence, driven with
asyncio primitives instead of ``loop.serve``'s signal handlers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from rtl_buddy.hub import discovery
from rtl_buddy.hub.protocol import Envelope, Kind, Origin, decode, encode, new_id
from rtl_buddy.hub.server import HubServer


pytestmark = pytest.mark.asyncio


async def test_serve_lifecycle_writes_and_clears_discovery(tmp_path: Path):
    """Run ``loop.serve``'s lifecycle without its signal handlers."""

    server = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    host, port = await server.start()

    discovery.write_record(
        tmp_path,
        pid=os.getpid(),
        tcp=f"{host}:{port}",
        server_version=server.server_version,
    )
    assert discovery.read_record(tmp_path) is not None

    serve_task = asyncio.create_task(server.serve_forever())

    reader, writer = await asyncio.open_connection(host, port)
    try:
        hello = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "view", "version": "0.1.0", "capabilities": []},
        )
        writer.write(encode(hello).encode("utf-8") + b"\n")
        await writer.drain()
        welcome = decode(await asyncio.wait_for(reader.readline(), timeout=1.0))
        assert welcome.type == "welcome"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    await server.shutdown()
    serve_task.cancel()
    try:
        await serve_task
    except (asyncio.CancelledError, Exception):
        pass

    discovery.delete_record_if_owner(tmp_path, expected_pid=os.getpid())
    assert discovery.read_record(tmp_path) is None


async def test_server_refuses_to_double_bind(tmp_path: Path):
    """A second server on the same port raises OSError from ``start()``."""

    a = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    host, port = await a.start()
    try:
        b = HubServer(host=host, port=port, server_version="0.0.0+test")
        with pytest.raises(OSError):
            await b.start()
    finally:
        await a.shutdown()
