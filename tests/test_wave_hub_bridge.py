"""End-to-end tests for `WaveHubBridge` against a real HubServer.

The bridge runs in a worker thread against a hub in another thread, WCP events are driven through the bridge's observer hook, and a fake `SurferWcpListener` records the commands sent to surfer.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from rtl_buddy.hub import discovery
from rtl_buddy.hub.protocol import Envelope, Kind, Origin, decode, encode, new_id
from rtl_buddy.hub.server import HubServer
from rtl_buddy.tools.wave_hub_bridge import (
    WaveHubBridge,
    WaveHubBridgeError,
    _discover_hub_addr,
    _parse_hub_addr,
    maybe_connect_bridge,
)


class _FakeListener:
    """Records `send_to_surfer` calls and exposes the listener observer hook."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.event_observer = None
        # Staged await_response results by command name; an empty queue returns None.
        self.next_responses: dict[str, list[dict | None]] = {}
        # Staged await_reply results by command name: ("response", msg), ("error", msg) or None for a timeout.
        # Ack-returning commands are keyed under "ack". An empty queue returns None.
        self.next_replies: dict[str, list[tuple[str, dict] | None]] = {}

    def send_to_surfer(self, frame: dict) -> None:
        self.sent.append(frame)

    def await_response(self, command: str, timeout: float = 2.0) -> dict | None:
        queue = self.next_responses.get(command, [])
        if not queue:
            return None
        return queue.pop(0)

    def await_reply(
        self, commands: set[str], timeout: float = 2.0
    ) -> tuple[str, dict] | None:
        for command in commands:
            queue = self.next_replies.get(command)
            if queue:
                return queue.pop(0)
        return None


# ---------------------------------------------------------------------------
# asyncio HubServer fixture in its own background thread
# ---------------------------------------------------------------------------


class _HubInThread:
    """Run a HubServer on its own asyncio loop in a background thread."""

    def __init__(self) -> None:
        self.server: HubServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.started = threading.Event()

    def start(self) -> tuple[str, int]:
        ready: dict = {}

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            server = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
            self.server = server

            async def _async_start():
                host, port = await server.start()
                ready["host"] = host
                ready["port"] = port
                self.started.set()
                await server.serve_forever()

            try:
                loop.run_until_complete(_async_start())
            except Exception:
                self.started.set()
                raise
            finally:
                # Drain pending tasks first; otherwise transport finalisers raise "Event loop is closed" in later fixtures.
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

        self.thread = threading.Thread(target=_runner, daemon=True, name="hub-thread")
        self.thread.start()
        if not self.started.wait(timeout=5.0):
            raise TimeoutError("HubServer did not start in time")
        return ready["host"], ready["port"]

    def stop(self) -> None:
        if self.server is None or self.loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop)
        try:
            future.result(timeout=5.0)
        except Exception:
            pass
        # The runner thread may already have closed the loop.
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass
        if self.thread is not None:
            self.thread.join(timeout=5.0)


@pytest.fixture
def hub_in_thread():
    h = _HubInThread()
    h.start()
    try:
        yield h
    finally:
        h.stop()


# ---------------------------------------------------------------------------
# parse / discovery
# ---------------------------------------------------------------------------


def test_parse_hub_addr_round_trip():
    assert _parse_hub_addr("127.0.0.1:54321") == ("127.0.0.1", 54321)


def test_parse_hub_addr_rejects_no_port():
    with pytest.raises(WaveHubBridgeError):
        _parse_hub_addr("no-port-here")


def test_parse_hub_addr_rejects_non_integer_port():
    with pytest.raises(WaveHubBridgeError):
        _parse_hub_addr("127.0.0.1:abc")


def test_discover_uses_env_override(monkeypatch):
    monkeypatch.setenv("RTL_BUDDY_HUB", "127.0.0.1:7777")
    assert _discover_hub_addr(project_root=Path("/nonexistent")) == (
        "127.0.0.1",
        7777,
    )


def test_discover_returns_none_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _discover_hub_addr(project_root=tmp_path) is None


def test_discover_uses_project_root_hub_json(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    discovery.write_record(
        tmp_path,
        pid=os.getpid(),
        tcp="127.0.0.1:65432",
        server_version="0.0.0+test",
    )
    monkeypatch.chdir(tmp_path)
    assert _discover_hub_addr(project_root=tmp_path) == ("127.0.0.1", 65432)


def test_discover_skips_stale_record(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    cfg_dir = tmp_path / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.json").write_text(
        '{"v": 1, "pid": 999999, "tcp": "127.0.0.1:1",'
        ' "server_version": "0.0",'
        f' "project_root": "{tmp_path}",'
        ' "started_at": "2026-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    assert _discover_hub_addr(project_root=tmp_path) is None


# ---------------------------------------------------------------------------
# connect / handshake
# ---------------------------------------------------------------------------


def test_connect_hello_welcome(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    try:
        # Hub should now have `wave` in its registry.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if Origin.WAVE in hub_in_thread.server.registered_origins:  # type: ignore[union-attr]
                break
            time.sleep(0.02)
        assert Origin.WAVE in hub_in_thread.server.registered_origins  # type: ignore[union-attr]
    finally:
        bridge.stop()


def test_connect_refused_when_hub_down():
    listener = _FakeListener()
    with pytest.raises(WaveHubBridgeError):
        WaveHubBridge.connect(("127.0.0.1", 1), listener=listener)


def test_maybe_connect_bridge_none_when_no_hub(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    monkeypatch.chdir(tmp_path)
    listener = _FakeListener()
    assert maybe_connect_bridge(listener=listener, project_root=tmp_path) is None


# ---------------------------------------------------------------------------
# WCP → hub event translation
# ---------------------------------------------------------------------------


def _connect_observer(hub_in_thread: _HubInThread, origin: Origin):
    """Helper: open a second client to observe broadcasts."""

    import socket

    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    sock = socket.create_connection((host, port), timeout=2.0)
    hello = Envelope(
        origin=origin,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={"client": origin.value, "version": "0.1", "capabilities": []},
    )
    sock.sendall(encode(hello).encode("utf-8") + b"\n")
    buf = b""
    while b"\n" not in buf:
        buf += sock.recv(4096)
    sock.settimeout(2.0)
    return sock


def _recv_line(sock) -> Envelope:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("closed")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return decode(line)


class _Recv:
    """Envelope receiver that keeps bytes read past the first newline.

    ``_recv_line`` drops them, which loses the second of two envelopes that TCP delivers in one recv.
    """

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""

    def next(self) -> Envelope:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("closed")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return decode(line)


def test_cursor_moved_becomes_cursor_time_changed(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        bridge.on_wcp_event(
            "cursor_moved",
            {"type": "event", "event": "cursor_moved", "timestamp": 12500000},
        )
        env = _recv_line(view_sock)
        assert env.type == "cursor_time_changed"
        assert env.origin is Origin.WAVE
        assert env.payload == {"t_fs": "12500000"}
    finally:
        view_sock.close()
        bridge.stop()


def test_scope_changed_event_broadcast(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        bridge.on_wcp_event(
            "scope_changed",
            {"type": "event", "event": "scope_changed", "scope": "tb.dut.u_fifo"},
        )
        env = _recv_line(view_sock)
        assert env.type == "scope_changed"
        assert env.payload == {"wave_scope": "tb.dut.u_fifo"}
    finally:
        view_sock.close()
        bridge.stop()


def test_goto_declaration_becomes_signal_selected(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        bridge.on_wcp_event(
            "goto_declaration",
            {
                "type": "event",
                "event": "goto_declaration",
                "variable": "tb.dut.u_fifo.wr_ptr_q",
            },
        )
        env = _recv_line(view_sock)
        assert env.type == "signal_selected"
        assert env.payload == {"signal": "wr_ptr_q", "wave_scope": "tb.dut.u_fifo"}
    finally:
        view_sock.close()
        bridge.stop()


def test_malformed_wcp_event_is_dropped(hub_in_thread: _HubInThread):
    """A malformed event is dropped without raising."""

    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    try:
        # No timestamp field.
        bridge.on_wcp_event("cursor_moved", {})
    finally:
        bridge.stop()


# ---------------------------------------------------------------------------
# hub → WCP request handling
# ---------------------------------------------------------------------------


def test_wave_add_variables_translates_to_wcp_command(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_add_variables",
            id=new_id(),
            payload={"variables": ["tb.dut.x", "tb.dut.y"]},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.id == req.id
        # No surfer response staged: the reply has empty ids.
        assert resp.payload == {"ids": []}
        # The request became a WCP command.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "add_variables",
                "variables": ["tb.dut.x", "tb.dut.y"],
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_add_variables_forwards_ids_and_not_found(hub_in_thread: _HubInThread):
    """The reply to add_variables carries surfer's ids and not_found."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_responses["add_variables"] = [
        {
            "type": "response",
            "command": "add_variables",
            "ids": [4, 5],
            "not_found": ["tb.dut.bogus"],
        }
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_add_variables",
            id=new_id(),
            payload={"variables": ["tb.dut.x", "tb.dut.bogus"]},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ids": [4, 5], "not_found": ["tb.dut.bogus"]}
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_scope_no_reply_acks_best_effort(
    hub_in_thread: _HubInThread,
):
    """With no surfer reply, set_scope still resolves {"ok": True}; an explicit surfer error is propagated instead."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_set_scope",
            id=new_id(),
            payload={"wave_scope": "tb.dut.does_not_exist"},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_cursor_translates_to_wcp_command(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_set_cursor",
            id=new_id(),
            payload={"t_fs": "12500000"},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "set_cursor",
                "timestamp": 12500000,
                "time_unit": "fs",
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_viewport_translates_to_wcp_command(hub_in_thread: _HubInThread):
    """`wave_set_viewport { t_fs }` becomes WCP `set_viewport_to { timestamp }`."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_set_viewport",
            id=new_id(),
            payload={"t_fs": "200000"},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "set_viewport_to",
                "timestamp": 200000,
                "time_unit": "fs",
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_zoom_to_range_translates_to_wcp_command(hub_in_thread: _HubInThread):
    """`wave_zoom_to_range { start_fs, end_fs }` becomes WCP `set_viewport_range { start, end }`."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_zoom_to_range",
            id=new_id(),
            payload={"start_fs": "50000", "end_fs": "100000"},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "set_viewport_range",
                "start": 50000,
                "end": 100000,
                "time_unit": "fs",
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_zoom_to_fit_translates_to_wcp_command(hub_in_thread: _HubInThread):
    """`wave_zoom_to_fit {}` becomes WCP `zoom_to_fit { viewport_idx: 0 }`."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_zoom_to_fit",
            id=new_id(),
            payload={},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "zoom_to_fit",
                "viewport_idx": 0,
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_scope_translates_to_wcp_set_scope(
    hub_in_thread: _HubInThread,
):
    """`wave_set_scope { wave_scope }` becomes WCP `set_scope { scope }` and leaves the variable panel alone."""

    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="wave_set_scope",
            id=new_id(),
            payload={"wave_scope": "tb.dut.u_fifo"},
        )
        view_sock.sendall(encode(req).encode("utf-8") + b"\n")
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        # No ids: set_scope adds no items.
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "set_scope",
                "scope": "tb.dut.u_fifo",
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


# ---------------------------------------------------------------------------
# wave-view item management
# ---------------------------------------------------------------------------


def _send_request(view_sock, type_: str, payload: dict) -> Envelope:
    req = Envelope(
        origin=Origin.VIEW,
        kind=Kind.REQUEST,
        type=type_,
        id=new_id(),
        payload=payload,
    )
    view_sock.sendall(encode(req).encode("utf-8") + b"\n")
    return req


def test_wave_get_items_lists_view(hub_in_thread: _HubInThread):
    """`wave_get_items` sends get_item_list then get_item_info and returns {id, type, name(, scope)} rows."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["get_item_list"] = [
        ("response", {"type": "response", "command": "get_item_list", "ids": [3, 5]})
    ]
    listener.next_replies["get_item_info"] = [
        (
            "response",
            {
                "type": "response",
                "command": "get_item_info",
                "results": [
                    {"id": 3, "type": "variable", "name": "tb.dut.u_fifo.wr_ptr_q"},
                    {"id": 5, "type": "divider", "name": "pointers"},
                ],
            },
        )
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        req = _send_request(view_sock, "wave_get_items", {})
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.id == req.id
        assert resp.payload == {
            "items": [
                {
                    "id": 3,
                    "type": "variable",
                    "name": "tb.dut.u_fifo.wr_ptr_q",
                    "scope": "tb.dut.u_fifo",
                },
                {"id": 5, "type": "divider", "name": "pointers"},
            ]
        }
        # Both WCP commands went out, in order.
        commands = [c.get("command") for c in listener.sent]
        assert commands == ["get_item_list", "get_item_info"]
        assert listener.sent[1]["ids"] == [3, 5]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_get_items_empty_view_skips_info(hub_in_thread: _HubInThread):
    """An empty item list skips get_item_info."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["get_item_list"] = [
        ("response", {"type": "response", "command": "get_item_list", "ids": []})
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(view_sock, "wave_get_items", {})
        resp = _recv_line(view_sock)
        assert resp.payload == {"items": []}
        assert [c.get("command") for c in listener.sent] == ["get_item_list"]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_get_items_no_surfer_reply_errors(hub_in_thread: _HubInThread):
    """No reply from surfer yields a hub error."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()  # nothing staged: await_reply returns None
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(view_sock, "wave_get_items", {})
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.ERROR
        assert resp.payload["code"] == "not_connected"
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_remove_items_reports_removed_and_not_found(hub_in_thread: _HubInThread):
    """removed and not_found come from diffing the item list before and after, because surfer acks unknown ids."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    # before: [3, 5, 9]; remove_items ack; after: [3, 9]
    listener.next_replies["get_item_list"] = [
        (
            "response",
            {"type": "response", "command": "get_item_list", "ids": [3, 5, 9]},
        ),
        ("response", {"type": "response", "command": "get_item_list", "ids": [3, 9]}),
    ]
    listener.next_replies["ack"] = [
        ("response", {"type": "response", "command": "ack"})
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(view_sock, "wave_remove_items", {"ids": [5, 99]})
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True, "removed": [5], "not_found": [99]}
        # remove_items was sent with the requested ids.
        remove = next(c for c in listener.sent if c.get("command") == "remove_items")
        assert remove["ids"] == [5, 99]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_move_items_translates_to_wcp_command(hub_in_thread: _HubInThread):
    """`wave_move_items { ids, to_index }` becomes WCP move_items { ids, target_index } and requires a surfer ack."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["ack"] = [
        ("response", {"type": "response", "command": "ack"})
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(view_sock, "wave_move_items", {"ids": [5, 6], "to_index": 0})
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ok": True}
        assert listener.sent == [
            {
                "type": "command",
                "command": "move_items",
                "ids": [5, 6],
                "target_index": 0,
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_move_items_surfer_error_propagates(hub_in_thread: _HubInThread):
    """A surfer error frame (unknown id, illegal move) becomes a hub error reply."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["ack"] = [
        (
            "error",
            {
                "type": "error",
                "error": "move_items",
                "arguments": ["99"],
                "message": "no item with id 99",
            },
        )
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(view_sock, "wave_move_items", {"ids": [99], "to_index": 0})
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.ERROR
        assert resp.payload["code"] == "bad_request"
        assert resp.payload["message"] == "no item with id 99"
        assert resp.payload["context"]["surfer_error"] == "move_items"
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_add_comments_returns_ids(hub_in_thread: _HubInThread):
    """`wave_add_comments { texts }` becomes WCP add_dividers { names } and returns the new item ids."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["add_dividers"] = [
        ("response", {"type": "response", "command": "add_dividers", "ids": [7, 8]})
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(
            view_sock,
            "wave_add_comments",
            {"texts": ["pointers", "flags"], "after_id": 3},
        )
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.RESPONSE
        assert resp.payload == {"ids": [7, 8]}
        assert listener.sent == [
            {
                "type": "command",
                "command": "add_dividers",
                "names": ["pointers", "flags"],
                "after": 3,
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_scope_surfer_error_propagates(hub_in_thread: _HubInThread):
    """A best-effort handler still returns an explicit surfer error (unknown scope) as a hub error, not {"ok": True}."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_replies["ack"] = [
        (
            "error",
            {
                "type": "error",
                "error": "set_scope",
                "arguments": [],
                "message": "no scope tb.dut.does_not_exist",
            },
        )
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        _send_request(
            view_sock, "wave_set_scope", {"wave_scope": "tb.dut.does_not_exist"}
        )
        resp = _recv_line(view_sock)
        assert resp.kind is Kind.ERROR
        assert resp.payload["code"] == "bad_request"
        assert "no scope" in resp.payload["message"]
    finally:
        view_sock.close()
        bridge.stop()


# ---------------------------------------------------------------------------
# bye / cleanup
# ---------------------------------------------------------------------------


def _send_wave_add(view_sock, variables: list[str]) -> Envelope:
    req = Envelope(
        origin=Origin.VIEW,
        kind=Kind.REQUEST,
        type="wave_add_variables",
        id=new_id(),
        payload={"variables": variables},
    )
    view_sock.sendall(encode(req).encode("utf-8") + b"\n")
    return req


def test_cursor_moved_broadcasts_wave_values_changed_for_tracked_vars(
    hub_in_thread: _HubInThread,
):
    """A viewer adds variables, the cursor moves, and ``wave_values_changed`` reaches the bus."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    # add_variables response: the variables become tracked.
    listener.next_responses["add_variables"] = [
        {
            "type": "response",
            "command": "add_variables",
            "ids": [10, 11],
            "not_found": [],
        }
    ]
    # query_variable_values response; surfer sends the timestamp as a decimal string.
    listener.next_responses["query_variable_values"] = [
        {
            "type": "response",
            "command": "query_variable_values",
            "timestamp": "12500000",
            "values": [
                {"variable": "tb.dut.q", "value": "1"},
                {"variable": "tb.dut.clk", "value": "0"},
            ],
            "not_found": [],
        }
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    rx = _Recv(view_sock)
    try:
        # Wait for the reply so the variables are tracked before the cursor moves.
        req = _send_wave_add(view_sock, ["tb.dut.q", "tb.dut.clk"])
        ack = rx.next()
        assert ack.id == req.id

        bridge.on_wcp_event(
            "cursor_moved",
            {"type": "event", "event": "cursor_moved", "timestamp": 12500000},
        )

        # cursor_time_changed arrives first.
        env_cursor = rx.next()
        assert env_cursor.type == "cursor_time_changed"
        assert env_cursor.payload == {"t_fs": "12500000"}

        # wave_values_changed follows, from the query thread.
        env_values = rx.next()
        assert env_values.type == "wave_values_changed"
        assert env_values.origin is Origin.WAVE
        assert env_values.payload["t_fs"] == "12500000"
        values = env_values.payload["values"]
        # Order follows the add_variables request.
        assert values == [
            {"wave_scope": "tb.dut", "signal": "q", "value": "1"},
            {"wave_scope": "tb.dut", "signal": "clk", "value": "0"},
        ]

        query_sent = next(
            (
                cmd
                for cmd in listener.sent
                if cmd.get("command") == "query_variable_values"
            ),
            None,
        )
        assert query_sent is not None
        assert query_sent == {
            "type": "command",
            "command": "query_variable_values",
            "variables": ["tb.dut.q", "tb.dut.clk"],
        }
    finally:
        view_sock.close()
        bridge.stop()


def test_cursor_moved_without_tracked_variables_skips_query(
    hub_in_thread: _HubInThread,
):
    """Without tracked variables, a cursor move sends no query to surfer."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    try:
        bridge.on_wcp_event(
            "cursor_moved",
            {"type": "event", "event": "cursor_moved", "timestamp": 9000},
        )
        # cursor_time_changed is still sent.
        env_cursor = _recv_line(view_sock)
        assert env_cursor.type == "cursor_time_changed"
        # Give a spurious worker thread time to act before checking.
        time.sleep(0.05)
        assert listener.sent == []
        # No wave_values_changed follows.
        view_sock.settimeout(0.2)
        with pytest.raises((TimeoutError, OSError, BlockingIOError)):
            _recv_line(view_sock)
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_values_changed_drops_null_values(hub_in_thread: _HubInThread):
    """A ``value: null`` (no transition before the sample point) is omitted from the broadcast."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_responses["add_variables"] = [
        {"type": "response", "command": "add_variables", "ids": [1, 2], "not_found": []}
    ]
    listener.next_responses["query_variable_values"] = [
        {
            "type": "response",
            "command": "query_variable_values",
            "timestamp": "100",
            "values": [
                {"variable": "tb.dut.q", "value": "1"},
                {"variable": "tb.dut.preset", "value": None},
            ],
            "not_found": [],
        }
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    rx = _Recv(view_sock)
    try:
        req = _send_wave_add(view_sock, ["tb.dut.q", "tb.dut.preset"])
        _ = rx.next()  # ack
        assert req.id

        bridge.on_wcp_event(
            "cursor_moved",
            {"type": "event", "event": "cursor_moved", "timestamp": 100},
        )
        _ = rx.next()  # cursor_time_changed
        env = rx.next()
        assert env.type == "wave_values_changed"
        # The null-valued preset is omitted.
        assert env.payload["values"] == [
            {"wave_scope": "tb.dut", "signal": "q", "value": "1"},
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_add_variables_not_found_paths_are_not_tracked(
    hub_in_thread: _HubInThread,
):
    """Variables in surfer's ``not_found`` are not tracked."""
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    listener.next_responses["add_variables"] = [
        {
            "type": "response",
            "command": "add_variables",
            "ids": [3],
            "not_found": ["tb.dut.bogus"],
        }
    ]
    listener.next_responses["query_variable_values"] = [
        {
            "type": "response",
            "command": "query_variable_values",
            "timestamp": "1",
            "values": [{"variable": "tb.dut.real_signal", "value": "0"}],
            "not_found": [],
        }
    ]
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    view_sock = _connect_observer(hub_in_thread, Origin.VIEW)
    rx = _Recv(view_sock)
    try:
        req = _send_wave_add(view_sock, ["tb.dut.real_signal", "tb.dut.bogus"])
        _ = rx.next()  # ack
        assert req.id

        bridge.on_wcp_event(
            "cursor_moved",
            {"type": "event", "event": "cursor_moved", "timestamp": 1},
        )
        _ = rx.next()  # cursor_time_changed
        _ = rx.next()  # wave_values_changed

        query_sent = next(
            (
                cmd
                for cmd in listener.sent
                if cmd.get("command") == "query_variable_values"
            ),
            None,
        )
        assert query_sent is not None
        # Only the resolved variable is queried.
        assert query_sent["variables"] == ["tb.dut.real_signal"]
    finally:
        view_sock.close()
        bridge.stop()


def test_stop_unregisters_from_hub(hub_in_thread: _HubInThread):
    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    bridge.start()
    deadline = time.monotonic() + 1.0
    while (
        time.monotonic() < deadline
        and Origin.WAVE not in hub_in_thread.server.registered_origins
    ):  # type: ignore[union-attr]
        time.sleep(0.02)
    assert Origin.WAVE in hub_in_thread.server.registered_origins  # type: ignore[union-attr]

    bridge.stop()

    deadline = time.monotonic() + 1.0
    while (
        time.monotonic() < deadline
        and Origin.WAVE in hub_in_thread.server.registered_origins
    ):  # type: ignore[union-attr]
        time.sleep(0.02)
    assert Origin.WAVE not in hub_in_thread.server.registered_origins  # type: ignore[union-attr]
