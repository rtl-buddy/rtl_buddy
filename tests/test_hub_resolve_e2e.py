"""End-to-end resolve_* tests: real ``HubServer`` + real ``Resolver``.

These complement the unit-level ``test_hub_resolver.py`` (which pokes
the resolver directly) and the broad ``test_hub_server.py`` (which
runs the server without a resolver). Here we stand up both and drive
the protocol over the wire to confirm the server's resolve dispatch
matches the spec's request/response shapes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub.config import HubMappingConfig
from rtl_buddy.hub.protocol import Envelope, Kind, Origin, decode, encode, new_id
from rtl_buddy.hub.resolver import Resolver
from rtl_buddy.hub.server import HubServer


pytestmark = pytest.mark.asyncio


# Mirrors the rtl-buddy-view JSON contract today (counter fixture).
_VIEW_JSON = {
    "schema_version": "1.0",
    "tool": {"name": "rtl-buddy-view", "version": "0.1.0"},
    "design": {"top": "counter"},
    "nodes": [
        {
            "instance_path": "counter",
            "module_name": "counter",
            "port_connections": [],
            "location": {
                "file": "/abs/rtl/counter.sv",
                "start_line": 5,
                "start_column": 1,
                "end_line": 12,
                "end_column": 10,
            },
        },
        {
            "instance_path": "counter.u_ff",
            "module_name": "counter_ff",
            "port_connections": [
                {"port_name": "clk", "net_expr_text": "clk"},
                {"port_name": "q", "net_expr_text": "q"},
            ],
            "location": {
                "file": "/abs/rtl/counter.sv",
                "start_line": 10,
                "start_column": 16,
                "end_line": 10,
                "end_column": 39,
            },
        },
    ],
    "edges": [{"parent": "counter", "child": "counter.u_ff"}],
}


@pytest_asyncio.fixture
async def server_with_resolver(tmp_path: Path) -> AsyncIterator[HubServer]:
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(_VIEW_JSON), encoding="utf-8")

    resolver = Resolver(
        view_json_path=view_path,
        mapping=HubMappingConfig(tb_prefix="tb.dut."),
    )
    s = HubServer(
        host="127.0.0.1", port=0, server_version="0.0.0+test", resolver=resolver
    )
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


class _Client:
    """Local mock client. Mirrors tests/test_hub_server.py::MockClient."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "_Client":
        r, w = await asyncio.open_connection(host, port)
        return cls(r, w)

    async def send(self, env: Envelope) -> None:
        self.writer.write(encode(env).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def recv(self, *, timeout: float = 1.0) -> Envelope:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return decode(line)

    async def hello(self, origin: Origin) -> Envelope:
        h = Envelope(
            origin=origin,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": origin.value, "version": "0.1", "capabilities": []},
        )
        await self.send(h)
        return await self.recv()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# resolve_view_to_wave
# ---------------------------------------------------------------------------


async def test_view_to_wave_round_trip(server_with_resolver: HubServer):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.VIEW)
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="resolve_view_to_wave",
            id=new_id(),
            payload={"instance_path": "counter.u_ff"},
        )
        await c.send(req)
        resp = await c.recv()
        assert resp.kind is Kind.RESPONSE
        assert resp.id == req.id
        assert resp.payload == {"wave_scope": "tb.dut.u_ff"}
    finally:
        await c.close()


async def test_view_to_wave_unknown_path_returns_unresolvable(
    server_with_resolver: HubServer,
):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.VIEW)
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="resolve_view_to_wave",
            id=new_id(),
            payload={"instance_path": "counter.u_dbg"},
        )
        await c.send(req)
        err = await c.recv()
        assert err.type == "error"
        assert err.payload["code"] == "unresolvable"
        assert err.payload["context"]["tb_prefix"] == "tb.dut."
    finally:
        await c.close()


async def test_view_to_wave_missing_payload_returns_bad_request(
    server_with_resolver: HubServer,
):
    """Server validation path: schema-rejecting payloads → bad_request.

    The codec's encode would reject this on the client side, so we send
    the raw wire bytes — that's the exposure surface a buggy or
    non-Python client could realistically hit.
    """

    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.VIEW)
        req_id = new_id()
        raw = (
            json.dumps(
                {
                    "v": 1,
                    "id": req_id,
                    "origin": "view",
                    "kind": "request",
                    "type": "resolve_view_to_wave",
                    "payload": {"instance_path": ""},  # empty fails schema
                }
            )
            + "\n"
        ).encode("utf-8")
        c.writer.write(raw)
        await c.writer.drain()
        err = await c.recv()
        assert err.type == "error"
        assert err.payload["code"] == "bad_request"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# resolve_signal_to_view
# ---------------------------------------------------------------------------


async def test_signal_to_view_round_trip(server_with_resolver: HubServer):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.WAVE)
        req = Envelope(
            origin=Origin.WAVE,
            kind=Kind.REQUEST,
            type="resolve_signal_to_view",
            id=new_id(),
            payload={"signal": "q", "wave_scope": "tb.dut."},
        )
        await c.send(req)
        resp = await c.recv()
        assert resp.kind is Kind.RESPONSE
        assert resp.id == req.id
        assert resp.payload == {
            "instance_path": ["counter.u_ff"],
            "port": "q",
        }
    finally:
        await c.close()


async def test_signal_to_view_unknown_signal_unresolvable(
    server_with_resolver: HubServer,
):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.WAVE)
        req = Envelope(
            origin=Origin.WAVE,
            kind=Kind.REQUEST,
            type="resolve_signal_to_view",
            id=new_id(),
            payload={"signal": "ghost", "wave_scope": "tb.dut."},
        )
        await c.send(req)
        err = await c.recv()
        assert err.type == "error"
        assert err.payload["code"] == "unresolvable"
    finally:
        await c.close()


async def test_signal_to_view_missing_field_bad_request(
    server_with_resolver: HubServer,
):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.WAVE)
        req_id = new_id()
        raw = (
            json.dumps(
                {
                    "v": 1,
                    "id": req_id,
                    "origin": "wave",
                    "kind": "request",
                    "type": "resolve_signal_to_view",
                    "payload": {"signal": "q", "wave_scope": ""},
                }
            )
            + "\n"
        ).encode("utf-8")
        c.writer.write(raw)
        await c.writer.drain()
        err = await c.recv()
        assert err.type == "error"
        assert err.payload["code"] == "bad_request"
    finally:
        await c.close()
