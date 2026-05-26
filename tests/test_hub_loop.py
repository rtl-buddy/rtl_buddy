"""Integration test for ``rtl_buddy.hub.loop`` orchestration.

The real ``loop.serve`` installs SIGINT/SIGTERM handlers and calls
``asyncio.run``; the signal handlers require the asyncio loop to be on
the main thread, which makes it awkward to test the full thing through
``CliRunner``. This file exercises the same start → serve → shutdown
sequence directly with asyncio primitives, so the lifecycle (discovery
file writes + cleanup, listener teardown) is covered without forking
the test runner.
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
    """Mirrors what ``loop.serve`` does without the signal-handler half."""

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

    # Exercise one round trip.
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
    """Two servers on the same port → second .start() raises OSError.

    Confirms that the listener does what the discovery layer's
    "one hub per project" rule expects at the OS level.
    """

    a = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    host, port = await a.start()
    try:
        b = HubServer(host=host, port=port, server_version="0.0.0+test")
        with pytest.raises(OSError):
            await b.start()
    finally:
        await a.shutdown()
