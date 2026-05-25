"""Tests for the SPA↔notebook event broker (Phase 3 of axi-profiler #16).

Splits in two layers: the in-memory ``EventBroker`` (pure asyncio queue
fan-out, no IO) and the ``/api/events/sync`` WS endpoint on
``ViewerServer`` (round-trip through ``websockets.serve``).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from rtl_buddy.hub.event_broker import EventBroker, _CLIENT_QUEUE_MAX
from rtl_buddy.hub.viewer_http import ViewerServer


# ---------------------------------------------------------------------
# EventBroker (no IO)
# ---------------------------------------------------------------------


def test_broadcast_reaches_other_clients_not_sender() -> None:
    async def go() -> None:
        broker = EventBroker()
        a_id, a = broker.add_client(name="a")
        b_id, b = broker.add_client(name="b")
        c_id, c = broker.add_client(name="c")

        broker.broadcast(a_id, "hello")

        assert b.queue.get_nowait() == "hello"
        assert c.queue.get_nowait() == "hello"
        assert a.queue.empty()
        # _id values are returned so the handler can call remove_client.
        assert {a_id, b_id, c_id} == {0, 1, 2}

    asyncio.run(go())


def test_remove_client_unsubscribes_and_count_tracks() -> None:
    async def go() -> None:
        broker = EventBroker()
        a_id, _ = broker.add_client()
        b_id, b = broker.add_client()
        assert broker.client_count == 2

        broker.remove_client(a_id)
        assert broker.client_count == 1

        broker.broadcast(b_id, "x")  # only A would have received it
        assert b.queue.empty()

        # Removing an unknown id is a no-op.
        broker.remove_client(999)
        assert broker.client_count == 1

    asyncio.run(go())


def test_full_queue_drops_oldest_and_keeps_latest() -> None:
    async def go() -> None:
        broker = EventBroker()
        sender_id, _ = broker.add_client(name="sender")
        _, slow = broker.add_client(name="slow")

        for i in range(_CLIENT_QUEUE_MAX + 5):
            broker.broadcast(sender_id, f"m{i}")

        # Drained order: oldest 5 messages were evicted; queue still holds
        # exactly _CLIENT_QUEUE_MAX latest messages, ending with m{N-1}.
        drained = []
        while not slow.queue.empty():
            drained.append(slow.queue.get_nowait())
        assert len(drained) == _CLIENT_QUEUE_MAX
        assert drained[0] == "m5"
        assert drained[-1] == f"m{_CLIENT_QUEUE_MAX + 4}"

    asyncio.run(go())


# ---------------------------------------------------------------------
# /api/events/sync end-to-end (real websockets server)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_clients_receive_each_others_messages_not_their_own() -> None:
    server = ViewerServer(hub_host="127.0.0.1", hub_port=1, http_port=0)
    host, port = await server.start()
    try:
        url = f"ws://{host}:{port}/api/events/sync"
        async with (
            websockets.connect(url) as ws_a,
            websockets.connect(url) as ws_b,
        ):
            # Give the server a moment to register both clients before
            # publishing — otherwise B might miss A's first message.
            for _ in range(20):
                if server._event_broker.client_count == 2:
                    break
                await asyncio.sleep(0.01)
            assert server._event_broker.client_count == 2

            payload = json.dumps(
                {"topic": "selection", "data": {"bundle": "axi_xbar"}, "source": "spa"}
            )
            await ws_a.send(payload)
            got_b = await asyncio.wait_for(ws_b.recv(), timeout=2.0)
            assert got_b == payload

            # A should not get its own message back.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws_a.recv(), timeout=0.2)
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_disconnect_removes_client_from_broker() -> None:
    server = ViewerServer(hub_host="127.0.0.1", hub_port=1, http_port=0)
    host, port = await server.start()
    try:
        url = f"ws://{host}:{port}/api/events/sync"
        async with websockets.connect(url):
            for _ in range(20):
                if server._event_broker.client_count == 1:
                    break
                await asyncio.sleep(0.01)
            assert server._event_broker.client_count == 1
        # After ``async with`` exits, the close propagates; the server
        # cleanup runs in the handler's ``finally``.
        for _ in range(50):
            if server._event_broker.client_count == 0:
                break
            await asyncio.sleep(0.01)
        assert server._event_broker.client_count == 0
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_unknown_ws_path_is_404() -> None:
    server = ViewerServer(hub_host="127.0.0.1", hub_port=1, http_port=0)
    host, port = await server.start()
    try:
        url = f"ws://{host}:{port}/ws-does-not-exist"
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with websockets.connect(url):
                pass
        assert ei.value.response.status_code == 404
    finally:
        await server.shutdown()
