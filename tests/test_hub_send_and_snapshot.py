"""End-to-end tests for the four additions in #178:

1. ``resolve_wave_to_view`` request type.
2. ``state_snapshot`` request type.
3. Welcome-time replay of cached selection/cursor/scope.
4. ``rtl_buddy.hub.client.HubClient`` reusable client.

The CLI surface in ``rtl_buddy.hub.send`` is covered separately in
``test_hub_send_cli.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import discovery
from rtl_buddy.hub.client import HubClient
from rtl_buddy.hub.config import HubMappingConfig
from rtl_buddy.hub.protocol import Envelope, Kind, Origin, decode, encode, new_id
from rtl_buddy.hub.resolver import Resolver
from rtl_buddy.hub.server import HubServer


pytestmark = pytest.mark.asyncio


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


@pytest_asyncio.fixture
async def bare_server() -> AsyncIterator[HubServer]:
    """A server with no resolver — exercises the state_snapshot path
    that must not require resolver configuration."""

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


class _Client:
    """Async mock client. Same shape as test_hub_resolve_e2e."""

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
# (1) resolve_wave_to_view
# ---------------------------------------------------------------------------


async def test_resolve_wave_to_view_round_trip(server_with_resolver: HubServer):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.CLI)
        req = Envelope(
            origin=Origin.CLI,
            kind=Kind.REQUEST,
            type="resolve_wave_to_view",
            id=new_id(),
            payload={"wave_scope": "tb.dut.u_ff"},
        )
        await c.send(req)
        resp = await c.recv()
        assert resp.kind is Kind.RESPONSE
        assert resp.id == req.id
        assert resp.payload == {"instance_path": "counter.u_ff"}
    finally:
        await c.close()


async def test_resolve_wave_to_view_unknown_scope_unresolvable(
    server_with_resolver: HubServer,
):
    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.CLI)
        req = Envelope(
            origin=Origin.CLI,
            kind=Kind.REQUEST,
            type="resolve_wave_to_view",
            id=new_id(),
            payload={"wave_scope": "tb.dut.u_dbg.u_probe"},
        )
        await c.send(req)
        err = await c.recv()
        assert err.kind is Kind.ERROR
        assert err.payload["code"] == "unresolvable"
        assert err.payload["context"]["tb_prefix"] == "tb.dut."
    finally:
        await c.close()


async def test_resolve_wave_to_view_missing_scope_bad_request(
    server_with_resolver: HubServer,
):
    """Server-side defense in depth: a non-validating client (one that
    bypasses the schema and writes raw bytes) sending a payload missing
    ``wave_scope`` gets ``bad_request`` back instead of crashing the
    handler.

    The schema validator at the server's decode boundary catches the
    malformed envelope first, so the error is produced by the dispatch
    loop's catch-all rather than the handler's own guard — both paths
    return ``bad_request``, the handler guard is intentional defense in
    depth in case the schema is ever loosened."""

    import json as _json
    import uuid

    c = await _Client.connect(server_with_resolver.host, server_with_resolver.port)
    try:
        await c.hello(Origin.CLI)
        req_id = str(uuid.uuid4())
        raw = (
            _json.dumps(
                {
                    "v": 1,
                    "id": req_id,
                    "origin": "cli",
                    "kind": "request",
                    "type": "resolve_wave_to_view",
                    "payload": {},
                }
            ).encode("utf-8")
            + b"\n"
        )
        c.writer.write(raw)
        await c.writer.drain()
        err = await c.recv()
        assert err.kind is Kind.ERROR
        assert err.payload["code"] == "bad_request"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# (2) state_snapshot
# ---------------------------------------------------------------------------


async def test_state_snapshot_empty_on_fresh_server(bare_server: HubServer):
    """No events have fired; all event-derived fields must be null,
    peers reflects only the connected snapshot caller."""

    c = await _Client.connect(bare_server.host, bare_server.port)
    try:
        await c.hello(Origin.CLI)
        req = Envelope(
            origin=Origin.CLI,
            kind=Kind.REQUEST,
            type="state_snapshot",
            id=new_id(),
            payload={},
        )
        await c.send(req)
        resp = await c.recv()
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {
            "active_model": None,
            "selection": None,
            "cursor_time": None,
            "wave_scope": None,
            "peers": ["cli"],
            "diagnostics_sources": [],
        }
    finally:
        await c.close()


async def test_state_snapshot_after_events(bare_server: HubServer):
    """Drive a few state events, then snapshot and check each slot."""

    view = await _Client.connect(bare_server.host, bare_server.port)
    wave = await _Client.connect(bare_server.host, bare_server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)
        # Drain view's peer_joined notification from wave's handshake.
        await view.recv()

        await view.send(
            Envelope(
                origin=Origin.VIEW,
                kind=Kind.EVENT,
                type="selection_changed",
                id=new_id(),
                payload={"instance_path": "counter.u_ff"},
            )
        )
        await wave.send(
            Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="cursor_time_changed",
                id=new_id(),
                payload={"t_fs": "12500000"},
            )
        )
        await wave.send(
            Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="scope_changed",
                id=new_id(),
                payload={"wave_scope": "tb.dut.u_ff"},
            )
        )
        await wave.send(
            Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "rtl-buddy-cdc",
                    "items": [
                        {
                            "file": "/x.sv",
                            "line": 1,
                            "col": 1,
                            "severity": "warning",
                            "code": "CDC-1",
                            "message": "...",
                        }
                    ],
                },
            )
        )

        # Let the server process the broadcasts.
        await asyncio.sleep(0.05)

        # Use a third connection for the snapshot so we don't have to
        # demultiplex against broadcast traffic on view/wave.
        snap = await _Client.connect(bare_server.host, bare_server.port)
        try:
            await snap.hello(Origin.CLI)
            # `welcome` arrived; the cached state is replayed next as
            # selection_changed + cursor_time_changed + scope_changed +
            # diagnostics_set (no signal_selection in this test). Drain.
            for _ in range(4):
                await snap.recv()
            req = Envelope(
                origin=Origin.CLI,
                kind=Kind.REQUEST,
                type="state_snapshot",
                id=new_id(),
                payload={},
            )
            await snap.send(req)
            resp = await snap.recv()
            assert resp.kind is Kind.RESPONSE
            assert resp.payload["selection"] == {
                "instance_path": "counter.u_ff",
                "origin": "view",
            }
            assert resp.payload["cursor_time"] == {
                "t_fs": "12500000",
                "origin": "wave",
            }
            assert resp.payload["wave_scope"] == {
                "wave_scope": "tb.dut.u_ff",
                "origin": "wave",
            }
            assert sorted(resp.payload["peers"]) == ["cli", "view", "wave"]
            assert resp.payload["diagnostics_sources"] == ["rtl-buddy-cdc"]
        finally:
            await snap.close()
    finally:
        await view.close()
        await wave.close()


async def test_state_snapshot_does_not_require_resolver(bare_server: HubServer):
    """state_snapshot must succeed even when the hub has no resolver
    (the snapshot is pure HubState — no view.json involved)."""

    c = await _Client.connect(bare_server.host, bare_server.port)
    try:
        welcome = await c.hello(Origin.CLI)
        assert welcome.type == "welcome"
        req = Envelope(
            origin=Origin.CLI,
            kind=Kind.REQUEST,
            type="state_snapshot",
            id=new_id(),
            payload={},
        )
        await c.send(req)
        resp = await c.recv()
        assert resp.kind is Kind.RESPONSE
        # active_model is None — nothing set it on this bare server.
        assert resp.payload["active_model"] is None
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# (3) welcome-time replay
# ---------------------------------------------------------------------------


async def test_welcome_replays_cached_state_to_late_joiner(bare_server: HubServer):
    """A peer that arrives mid-session receives the cached events,
    not a broadcast — existing peers don't see duplicates."""

    view = await _Client.connect(bare_server.host, bare_server.port)
    wave = await _Client.connect(bare_server.host, bare_server.port)
    try:
        await view.hello(Origin.VIEW)
        await wave.hello(Origin.WAVE)
        await view.recv()  # peer_joined for wave

        await view.send(
            Envelope(
                origin=Origin.VIEW,
                kind=Kind.EVENT,
                type="selection_changed",
                id=new_id(),
                payload={"instance_path": "counter.u_ff"},
            )
        )
        await wave.send(
            Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="cursor_time_changed",
                id=new_id(),
                payload={"t_fs": "12500000"},
            )
        )
        # Drain the broadcasts so view/wave's recv queues are clean
        # before the late-joiner arrives.
        await wave.recv()  # selection_changed (origin: view; wave is not view)
        await view.recv()  # cursor_time_changed (origin: wave; view is not wave)

        # Late joiner — should receive cached state in unicast.
        src = await _Client.connect(bare_server.host, bare_server.port)
        try:
            welcome = await src.hello(Origin.SRC)
            assert welcome.type == "welcome"

            # Order: selection_changed, then cursor_time_changed (no signal_selection
            # or scope_changed cached). diagnostics are last but none were set.
            e1 = await src.recv()
            e2 = await src.recv()
            kinds = {e.type for e in (e1, e2)}
            assert kinds == {"selection_changed", "cursor_time_changed"}
            replayed_origins = {e.type: e.origin for e in (e1, e2)}
            assert replayed_origins["selection_changed"] is Origin.VIEW
            assert replayed_origins["cursor_time_changed"] is Origin.WAVE

            # Existing peers must NOT have received a duplicate broadcast
            # of the cached state (peer_joined for src is fine).
            joined = await wave.recv(timeout=0.5)
            assert joined.type == "peer_joined"
            joined = await view.recv(timeout=0.5)
            assert joined.type == "peer_joined"
            with pytest.raises(asyncio.TimeoutError):
                await wave.recv(timeout=0.2)
            with pytest.raises(asyncio.TimeoutError):
                await view.recv(timeout=0.2)
        finally:
            await src.close()
    finally:
        await view.close()
        await wave.close()


async def test_welcome_replays_cleared_diagnostics(bare_server: HubServer):
    """A diagnostics_set with empty items is a 'cleared source' record
    and must replay (otherwise late joiners would never learn that a
    previously-loud source went quiet)."""

    view = await _Client.connect(bare_server.host, bare_server.port)
    try:
        await view.hello(Origin.VIEW)
        await view.send(
            Envelope(
                origin=Origin.VIEW,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "rtl-buddy-cdc",
                    "items": [
                        {
                            "file": "/x.sv",
                            "line": 1,
                            "col": 1,
                            "severity": "error",
                            "code": "X",
                            "message": "y",
                        }
                    ],
                },
            )
        )
        # Then clear it.
        await view.send(
            Envelope(
                origin=Origin.VIEW,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={"source": "rtl-buddy-cdc", "items": []},
            )
        )
        await asyncio.sleep(0.05)

        late = await _Client.connect(bare_server.host, bare_server.port)
        try:
            await late.hello(Origin.CLI)
            replayed = await late.recv()
            assert replayed.type == "diagnostics_set"
            assert replayed.payload["source"] == "rtl-buddy-cdc"
            assert replayed.payload["items"] == []
        finally:
            await late.close()
    finally:
        await view.close()


# ---------------------------------------------------------------------------
# (4) HubClient against a real server, driven via discovery
# ---------------------------------------------------------------------------


def _write_discovery(project_root: Path, host: str, port: int, pid: int) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    rb_dir = project_root / ".rtl-buddy"
    rb_dir.mkdir(exist_ok=True)
    discovery.write_record(
        project_root,
        pid=pid,
        tcp=f"{host}:{port}",
        server_version="0.0.0+test",
        http_port=None,
    )


async def test_hub_client_round_trip(
    bare_server: HubServer, tmp_path: Path, monkeypatch
):
    """HubClient.connect uses discovery; once attached, request/emit
    behave like the WaveHubBridge unit tests."""

    import os

    _write_discovery(tmp_path, bare_server.host, bare_server.port, pid=os.getpid())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)

    # HubClient.connect is sync; run it off the event loop so the
    # asyncio server can answer.
    result: dict = {}

    def _drive() -> None:
        try:
            with HubClient.connect() as h:
                resp = h.request("state_snapshot", {})
                result["payload"] = resp.payload
                # Fire-and-forget event.
                h.emit("selection_changed", {"instance_path": "counter"})
        except Exception as exc:  # pragma: no cover
            result["error"] = exc

    t = threading.Thread(target=_drive)
    t.start()
    # Pump the loop until the worker thread finishes.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if not t.is_alive():
            break
    t.join(timeout=1.0)

    assert "error" not in result, result.get("error")
    assert result["payload"]["active_model"] is None
    assert "cli" in result["payload"]["peers"]
