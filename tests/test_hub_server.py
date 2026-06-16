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

    async def recv_until(self, type_: str, *, timeout: float = 1.0) -> Envelope:
        """Read envelopes until one with ``type == type_`` arrives.

        Used by multi-peer tests to skip past the ``peer_joined``
        broadcasts each later-arriving peer triggers — the hub fires
        one peer_joined per existing peer when a new peer hellos, so a
        test that connects three peers and then expects a single
        ``selection_changed`` has to filter past the peer_joined noise.
        """
        while True:
            env = await self.recv(timeout=timeout)
            if env.type == type_:
                return env

    async def expect_no_message(self, *, within: float = 0.1) -> None:
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=within)
        except asyncio.TimeoutError:
            return
        if line:
            pytest.fail(f"expected no message but got: {line!r}")

    async def hello(
        self,
        origin: Origin,
        *,
        version: str = "0.1.0",
        caps: list[str] | None = None,
        takeover: bool = False,
    ) -> Envelope:
        payload: dict = {
            "client": origin.value,
            "version": version,
            "capabilities": caps or [],
        }
        if takeover:
            payload["takeover"] = True
        hello = Envelope(
            origin=origin,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload=payload,
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


async def test_takeover_kicks_existing_registration(server: HubServer):
    """A second client may take over an in-use origin slot by
    setting ``payload.takeover=true`` on its hello. The old
    registration is replaced: its socket gets an ``error`` envelope
    with code ``superseded`` and is closed; remaining peers receive
    a ``bye`` for the displaced origin; the new client gets the
    usual welcome.
    """
    src = await MockClient.connect(server.host, server.port)
    old_view = await MockClient.connect(server.host, server.port)
    new_view = await MockClient.connect(server.host, server.port)
    try:
        # Register a sibling so we can observe the bye broadcast.
        await src.hello(Origin.SRC)
        await old_view.hello(Origin.VIEW)
        # Drain the peer_joined event src received when view joined.
        await src.recv_until("peer_joined")

        welcome = await new_view.hello(Origin.VIEW, takeover=True)
        assert welcome.type == "welcome", (
            f"takeover hello should be welcomed, got {welcome}"
        )

        # Old view receives the superseded error.
        kick = await old_view.recv()
        assert kick.type == "error"
        assert kick.payload["code"] == "superseded"
        # And its socket closes shortly after — readline returns
        # ``b""`` once the peer FIN'd.
        trailing = await asyncio.wait_for(old_view.reader.readline(), timeout=1.0)
        assert trailing == b"", f"expected EOF, got {trailing!r}"

        # Sibling sees bye(view) and then peer_joined(view) for the
        # new registration.
        bye = await src.recv_until("bye")
        assert bye.origin is Origin.VIEW
    finally:
        await src.close()
        await old_view.close()
        await new_view.close()


async def test_takeover_without_existing_registration_is_a_normal_hello(
    server: HubServer,
):
    """``takeover=true`` is harmless when the slot is empty —
    behaves like a regular hello, no spurious bye broadcast.
    """
    client = await MockClient.connect(server.host, server.port)
    try:
        welcome = await client.hello(Origin.VIEW, takeover=True)
        assert welcome.type == "welcome"
    finally:
        await client.close()


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

        # wave + src must receive it; view must not echo back. Use
        # recv_until to skip past the peer_joined broadcasts each later
        # peer triggered (view saw peer_joined(wave), peer_joined(src);
        # wave saw peer_joined(src)) and assert on selection_changed.
        got_wave = await wave.recv_until("selection_changed")
        got_src = await src.recv_until("selection_changed")
        assert got_wave.id == evt.id
        assert got_src.id == evt.id
        assert got_wave.payload == {"instance_path": "top.u_fifo"}

        # Drain the two peer_joined events view received during setup
        # before asserting it didn't get an echoed selection_changed.
        for _ in range(2):
            joined = await view.recv()
            assert joined.type == "peer_joined"
        await view.expect_no_message()
    finally:
        await view.close()
        await wave.close()
        await src.close()


async def test_source_focused_derives_selection_changed_via_resolver(
    tmp_path,
):
    """When a `src` peer sends source_focused and the resolver has a
    view.json that contains an instance whose source range covers the
    point, the hub augments by broadcasting a derived selection_changed
    with origin=cli. The SPA already handles selection_changed — this
    bridge is what makes `:RtlBuddyShow` light up the schematic without
    a SPA-side protocol change.
    """

    import json
    from rtl_buddy.hub.config import HubMappingConfig
    from rtl_buddy.hub.resolver import Resolver

    view_json = tmp_path / "view.json"
    view_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "top": "counter",
                "nodes": [
                    {
                        "id": "counter",
                        "source": {
                            "file": "/abs/rtl/counter.sv",
                            "start_line": 5,
                            "start_column": 1,
                            "end_line": 50,
                            "end_column": 10,
                        },
                    },
                    {
                        "id": "counter.u_ff",
                        "source": {
                            "file": "/abs/rtl/counter.sv",
                            "start_line": 20,
                            "start_column": 16,
                            "end_line": 25,
                            "end_column": 4,
                        },
                    },
                ],
                "edges": [{"from": "counter", "to": "counter.u_ff"}],
            }
        )
    )

    resolver = Resolver(view_json_path=view_json, mapping=HubMappingConfig())
    s = HubServer(
        host="127.0.0.1", port=0, server_version="0.0.0+test", resolver=resolver
    )
    await s.start()
    task = asyncio.create_task(s.serve_forever())
    try:
        view = await MockClient.connect(s.host, s.port)
        src = await MockClient.connect(s.host, s.port)
        try:
            await view.hello(Origin.VIEW)
            await src.hello(Origin.SRC)

            evt = Envelope(
                origin=Origin.SRC,
                kind=Kind.EVENT,
                type="source_focused",
                id=new_id(),
                payload={"file": "/abs/rtl/counter.sv", "line": 22, "col": 1},
            )
            await src.send(evt)

            # View receives both the raw source_focused and the
            # derived selection_changed. Order: source_focused first
            # (broadcast in the STATE_EVENT_TYPES path), then the
            # augmentation. Use recv_until to skip past peer_joined.
            raw = await view.recv_until("source_focused")
            assert raw.payload == {
                "file": "/abs/rtl/counter.sv",
                "line": 22,
                "col": 1,
            }

            derived = await view.recv_until("selection_changed")
            assert derived.origin is Origin.CLI
            # u_ff's [20, 25] range encloses line 22 and is smaller
            # than counter's [5, 50]; smallest-range-first ordering
            # means the resolver returns u_ff first.
            assert derived.payload == {"instance_path": ["counter.u_ff", "counter"]}
        finally:
            await view.close()
            await src.close()
    finally:
        await s.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_source_focused_with_no_match_does_not_broadcast_selection(
    tmp_path,
):
    """If the resolver returns no matches, the augmentation is silent —
    the raw source_focused is still broadcast (downstream may have its
    own use for it), but no spurious selection_changed is emitted.
    """

    import json
    from rtl_buddy.hub.config import HubMappingConfig
    from rtl_buddy.hub.resolver import Resolver

    view_json = tmp_path / "view.json"
    view_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "top": "counter",
                "nodes": [
                    {
                        "id": "counter",
                        "source": {
                            "file": "/abs/rtl/counter.sv",
                            "start_line": 5,
                            "start_column": 1,
                            "end_line": 10,
                            "end_column": 10,
                        },
                    }
                ],
                "edges": [],
            }
        )
    )

    resolver = Resolver(view_json_path=view_json, mapping=HubMappingConfig())
    s = HubServer(
        host="127.0.0.1", port=0, server_version="0.0.0+test", resolver=resolver
    )
    await s.start()
    task = asyncio.create_task(s.serve_forever())
    try:
        view = await MockClient.connect(s.host, s.port)
        src = await MockClient.connect(s.host, s.port)
        try:
            await view.hello(Origin.VIEW)
            await src.hello(Origin.SRC)

            evt = Envelope(
                origin=Origin.SRC,
                kind=Kind.EVENT,
                type="source_focused",
                id=new_id(),
                payload={"file": "/abs/rtl/other.sv", "line": 1, "col": 1},
            )
            await src.send(evt)

            raw = await view.recv_until("source_focused")
            assert raw.id == evt.id
            # No derived selection_changed should follow.
            await view.expect_no_message()
        finally:
            await view.close()
            await src.close()
    finally:
        await s.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_peer_joined_broadcast_to_existing_peers(server: HubServer):
    """When a new peer hellos, every already-registered peer receives a
    ``peer_joined`` event carrying the joining peer's origin. The
    joining peer itself does not (suppress_origin=client in the hub's
    hello handler).

    Symmetric to ``bye`` so consumers can maintain a live peer list
    without re-fetching ``registered_clients`` every time.
    """
    view = await MockClient.connect(server.host, server.port)
    wave = await MockClient.connect(server.host, server.port)
    try:
        # view hellos first: registry is empty, no one to notify.
        await view.hello(Origin.VIEW)

        # wave hellos second: view must get peer_joined(wave); wave
        # itself must not get a peer_joined about itself.
        await wave.hello(Origin.WAVE)

        joined = await view.recv()
        assert joined.type == "peer_joined"
        assert joined.kind is Kind.EVENT
        assert joined.origin is Origin.WAVE
        assert joined.payload == {}

        await wave.expect_no_message()
    finally:
        await view.close()
        await wave.close()


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
# diagnostics_set
# ---------------------------------------------------------------------------


def _diag_evt(origin: Origin, source: str, items: list[dict]) -> Envelope:
    return Envelope(
        origin=origin,
        kind=Kind.EVENT,
        type="diagnostics_set",
        id=new_id(),
        payload={"source": source, "items": items},
    )


async def test_diagnostics_set_broadcasts_and_caches(server: HubServer):
    publisher = await MockClient.connect(server.host, server.port)
    subscriber = await MockClient.connect(server.host, server.port)
    try:
        await publisher.hello(Origin.CLI)
        await subscriber.hello(Origin.SRC)

        items = [
            {"file": "/abs/a.sv", "line": 4, "severity": "error", "message": "x"},
            {
                "file": "/abs/b.sv",
                "line": 9,
                "col": 3,
                "severity": "warning",
                "code": "CDC-002",
                "message": "depth",
            },
        ]
        evt = _diag_evt(Origin.CLI, "rtl-buddy-cdc", items)
        await publisher.send(evt)

        # Skip the peer_joined(subscriber) the publisher already
        # received when subscriber connected; assert the actual
        # broadcast we care about lands at the subscriber.
        got = await subscriber.recv_until("diagnostics_set")
        assert got.payload["source"] == "rtl-buddy-cdc"
        assert got.payload["items"] == items
        # Publisher received peer_joined(subscriber) at setup time —
        # drain it before asserting it got no echoed diagnostics_set.
        joined = await publisher.recv()
        assert joined.type == "peer_joined"
        await publisher.expect_no_message()  # origin gets suppressed

        await asyncio.sleep(0.05)
        bundle = server.state.diagnostics["rtl-buddy-cdc"]
        assert bundle.origin is Origin.CLI
        assert len(bundle.items) == 2
    finally:
        await publisher.close()
        await subscriber.close()


async def test_diagnostics_set_replayed_to_late_joiner(server: HubServer):
    publisher = await MockClient.connect(server.host, server.port)
    try:
        await publisher.hello(Origin.CLI)
        await publisher.send(
            _diag_evt(
                Origin.CLI,
                "src-a",
                [{"file": "/x.sv", "line": 1, "severity": "info", "message": "m"}],
            )
        )
        await publisher.send(
            _diag_evt(
                Origin.CLI,
                "src-b",
                [{"file": "/y.sv", "line": 2, "severity": "warning", "message": "n"}],
            )
        )
        await asyncio.sleep(0.05)

        # Late joiner connects after the diagnostics were broadcast.
        late = await MockClient.connect(server.host, server.port)
        try:
            await late.hello(Origin.SRC)
            seen: dict[str, Envelope] = {}
            for _ in range(2):
                env = await late.recv()
                assert env.type == "diagnostics_set"
                seen[env.payload["source"]] = env
            assert set(seen.keys()) == {"src-a", "src-b"}
            assert seen["src-a"].payload["items"][0]["file"] == "/x.sv"
        finally:
            await late.close()
    finally:
        await publisher.close()


async def test_diagnostics_set_empty_items_is_cache_clear(server: HubServer):
    publisher = await MockClient.connect(server.host, server.port)
    subscriber = await MockClient.connect(server.host, server.port)
    try:
        await publisher.hello(Origin.CLI)
        await subscriber.hello(Origin.SRC)

        await publisher.send(
            _diag_evt(
                Origin.CLI,
                "rtl-buddy-cdc",
                [{"file": "/x.sv", "line": 1, "severity": "error", "message": "boom"}],
            )
        )
        first = await subscriber.recv()
        assert len(first.payload["items"]) == 1

        # Empty items is the legal "clear all" — must still broadcast.
        await publisher.send(_diag_evt(Origin.CLI, "rtl-buddy-cdc", []))
        second = await subscriber.recv()
        assert second.payload["items"] == []

        await asyncio.sleep(0.05)
        assert server.state.diagnostics["rtl-buddy-cdc"].items == ()
    finally:
        await publisher.close()
        await subscriber.close()


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
        # wave received peer_joined(wave) for itself? no — suppress_origin
        # skips that. But wave still has the peer_joined(view) it got at
        # its own welcome time? Actually view registered first so when
        # wave connected, view got peer_joined(wave) but wave got no
        # peer_joined (no earlier peers). So wave.recv() here would be
        # the forwarded request directly. Use recv_until anyway for
        # robustness against future broadcasts that might interleave.
        forwarded = await wave.recv_until("wave_add_variables")
        assert forwarded.id == req.id

        resp = Envelope(
            origin=Origin.WAVE,
            kind=Kind.RESPONSE,
            type="wave_add_variables",
            id=req.id,
            payload={"ids": [17]},
        )
        await wave.send(resp)

        # view's queue has peer_joined(wave) from when wave connected.
        # Skip it and assert on the response.
        got = await view.recv_until("wave_add_variables")
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
