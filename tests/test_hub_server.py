"""End-to-end tests for ``rtl_buddy.hub.server.HubServer``.

Spins the asyncio server on an ephemeral port, connects mock clients
over real TCP, and exercises:

* hello / welcome handshake (success + protocol-mismatch + duplicate
  origin paths),
* origin-suppressed broadcast (the loop-prevention guarantee),
* request routing to the correct origin (with the ``not_connected``
  fallback when no client is registered),
* hub-handled ``resolve_*`` requests returning the PR-2 stub error,
* response routing back to the original requester by ``id``,
* request-ID dedupe (duplicates dropped silently),
* clean disconnect (``bye`` broadcast on connection close).

Every test acquires the server, runs a quick exchange, and shuts down
in the same task so leaks are visible as hangs in the suite rather
than as ghost sockets.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub.protocol import Envelope, Kind, Origin, decode, encode, new_id
from rtl_buddy.hub.server import HubServer


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class MockClient:
    """Thin TCP client used by the tests.

    Owns the asyncio reader/writer pair, exposes ``send`` / ``recv``
    helpers, and tracks the last seen welcome so tests can assert on
    the registered-clients list. Does not implement the dispatch loop
    — each test reads explicitly to keep ordering obvious.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "MockClient":
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer)

    async def send(self, env: Envelope) -> None:
        self.writer.write(encode(env).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def send_raw(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def recv(self, *, timeout: float = 1.0) -> Envelope:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        if not line:
            raise EOFError("connection closed without a message")
        return decode(line)

    async def expect_no_message(self, *, within: float = 0.1) -> None:
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=within)
        except asyncio.TimeoutError:
            return
        if line:
            pytest.fail(f"expected no message but got: {line!r}")

    async def hello(
        self, origin: Origin, *, version: str = "0.1.0", caps: list[str] | None = None
    ) -> Envelope:
        hello = Envelope(
            origin=origin,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={
                "client": origin.value,
                "version": version,
                "capabilities": caps or [],
            },
        )
        await self.send(hello)
        return await self.recv()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


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


# ---------------------------------------------------------------------------
# handshake
# ---------------------------------------------------------------------------


async def test_hello_welcome_round_trip(server: HubServer):
    client = await MockClient.connect(server.host, server.port)
    try:
        welcome = await client.hello(Origin.VIEW)
        assert welcome.type == "welcome"
        assert welcome.kind is Kind.RESPONSE
        assert welcome.payload["registered_clients"] == ["view"]
        assert Origin.VIEW in server.registered_origins
    finally:
        await client.close()


async def test_first_message_must_be_hello(server: HubServer):
    client = await MockClient.connect(server.host, server.port)
    try:
        bogus = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top"},
        )
        await client.send(bogus)
        err = await client.recv()
        assert err.type == "error"
        assert err.payload["code"] == "protocol_mismatch"
    finally:
        await client.close()


async def test_duplicate_origin_refused(server: HubServer):
    a = await MockClient.connect(server.host, server.port)
    b = await MockClient.connect(server.host, server.port)
    try:
        await a.hello(Origin.VIEW)
        err = await b.hello(Origin.VIEW)
        assert err.type == "error"
        assert err.payload["code"] == "not_connected"
        assert "view" in err.payload["message"]
    finally:
        await a.close()
        await b.close()


async def test_bad_request_on_malformed_handshake(server: HubServer):
    client = await MockClient.connect(server.host, server.port)
    try:
        await client.send_raw(b"{not json\n")
        err = await client.recv()
        assert err.type == "error"
        assert err.payload["code"] == "bad_request"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# broadcast / origin suppression
# ---------------------------------------------------------------------------


async def test_state_event_broadcast_skips_origin(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    src = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)
        await src.hello(Origin.SRC)

        evt = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top.u_fifo"},
        )
        await view.send(evt)

        # wave + src must receive it; view must not echo back.
        got_wave = await wave.recv()
        got_src = await src.recv()
        assert got_wave.id == evt.id
        assert got_src.id == evt.id
        assert got_wave.payload == {"instance_path": "top.u_fifo"}

        await view.expect_no_message()
    finally:
        await view.close()
        await wave.close()
        await src.close()


async def test_state_is_recorded_after_event(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        evt = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top.u_fifo"},
        )
        await view.send(evt)
        await asyncio.sleep(0.05)
        assert server.state.selection is not None
        assert server.state.selection.instance_path == ("top.u_fifo",)
        assert server.state.selection.origin is Origin.VIEW
    finally:
        await view.close()


async def test_unknown_event_type_silently_dropped(server: HubServer):
    a = await MockClient.connect(server.host, server.port)
    b = await MockClient.connect(server.host, server.port)
    try:
        await a.hello(Origin.VIEW)
        await b.hello(Origin.WAVE)
        evt = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="future_v2_event",
            id=new_id(),
            payload={"x": 1},
        )
        await a.send(evt)
        await b.expect_no_message()
    finally:
        await a.close()
        await b.close()


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


async def test_request_routed_to_wave_origin(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_add_variables",
            id=new_id(),
            payload={"variables": ["tb.dut.x"]},
        )
        await view.send(req)

        forwarded = await wave.recv()
        assert forwarded.id == req.id
        assert forwarded.type == "wave_add_variables"
        assert forwarded.payload == {"variables": ["tb.dut.x"]}
    finally:
        await view.close()
        await wave.close()


async def test_request_to_missing_origin_returns_not_connected(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_set_cursor",
            id=new_id(),
            payload={"t_fs": "0"},
        )
        await view.send(req)
        err = await view.recv()
        assert err.type == "error"
        assert err.payload["code"] == "not_connected"
    finally:
        await view.close()


async def test_resolve_request_without_resolver_returns_unresolvable(server: HubServer):
    """Server with no resolver attached → resolve_* surfaces unresolvable."""

    view = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="resolve_view_to_wave",
            id=new_id(),
            payload={"instance_path": "top.u_fifo"},
        )
        await view.send(req)
        err = await view.recv()
        assert err.type == "error"
        assert err.payload["code"] == "unresolvable"
        assert "resolver not configured" in err.payload["message"]
    finally:
        await view.close()


async def test_response_routed_back_to_requester(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_add_variables",
            id=new_id(),
            payload={"variables": ["tb.dut.x"]},
        )
        await view.send(req)
        forwarded = await wave.recv()
        assert forwarded.id == req.id

        resp = Envelope(
            origin=Origin.WAVE,
            kind=Kind.RESPONSE,
            type="wave_add_variables",
            id=req.id,
            payload={"ids": [17]},
        )
        await wave.send(resp)

        got = await view.recv()
        assert got.id == req.id
        assert got.kind is Kind.RESPONSE
        assert got.payload == {"ids": [17]}
    finally:
        await view.close()
        await wave.close()


async def test_duplicate_request_dropped(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        rid = new_id()
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_add_variables",
            id=rid,
            payload={"variables": ["tb.dut.x"]},
        )
        await view.send(req)
        first = await wave.recv()
        assert first.id == rid

        # Same id again: should be dropped silently.
        await view.send(req)
        await wave.expect_no_message()
    finally:
        await view.close()
        await wave.close()


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


async def test_disconnect_broadcasts_bye(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        await view.close()

        bye = await wave.recv()
        assert bye.type == "bye"
        assert bye.origin is Origin.VIEW
    finally:
        await wave.close()


async def test_explicit_bye_unregisters(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        bye = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="bye",
            id=new_id(),
            payload={},
        )
        await view.send(bye)

        got = await wave.recv()
        assert got.type == "bye"
        assert got.origin is Origin.VIEW

        # And the registry should reflect the unregister.
        await asyncio.sleep(0.05)
        assert Origin.VIEW not in server.registered_origins
    finally:
        await view.close()
        await wave.close()


# ---------------------------------------------------------------------------
# misc smoke
# ---------------------------------------------------------------------------


async def test_unknown_request_type_returns_bad_request(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="future_v2_request",
            id=new_id(),
            payload={},
        )
        await view.send(req)
        err = await view.recv()
        assert err.type == "error"
        assert err.payload["code"] == "bad_request"
    finally:
        await view.close()


async def test_second_hello_on_same_connection_rejected(server: HubServer):
    view = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        # Sending another hello on the same socket is a misuse — should
        # come back as protocol_mismatch since hello may only be sent once.
        again = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "view", "version": "0.1.0", "capabilities": []},
        )
        await view.send(again)
        err = await view.recv()
        assert err.type == "error"
        assert err.payload["code"] == "protocol_mismatch"
    finally:
        await view.close()


# ---------------------------------------------------------------------------
# raw wire shape sanity
# ---------------------------------------------------------------------------


async def test_messages_are_line_delimited(server: HubServer):
    """Two envelopes back-to-back should still parse separately."""

    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)

        e1 = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top.a"},
        )
        e2 = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top.b"},
        )
        # Send both in one write — server must still demultiplex.
        await view.send_raw(
            encode(e1).encode("utf-8") + b"\n" + encode(e2).encode("utf-8") + b"\n"
        )

        got1 = await wave.recv()
        got2 = await wave.recv()
        assert {got1.payload["instance_path"], got2.payload["instance_path"]} == {
            "top.a",
            "top.b",
        }
    finally:
        await view.close()
        await wave.close()


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


async def test_welcome_id_matches_hello(server: HubServer):
    """welcome.id must echo hello.id so the client can correlate."""

    client = await MockClient.connect(server.host, server.port)
    try:
        hello = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "view", "version": "0.1.0", "capabilities": []},
        )
        await client.send(hello)
        welcome = await client.recv()
        assert welcome.id == hello.id
        assert _is_uuid(welcome.id)
        as_json = json.loads(encode(welcome))
        assert as_json["payload"]["server_version"] == "0.0.0+test"
    finally:
        await client.close()
