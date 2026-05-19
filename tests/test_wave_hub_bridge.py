"""End-to-end tests for ``WaveHubBridge`` against a real HubServer.

The bridge runs in a worker thread, connects to a live asyncio hub
running in another thread, and we drive the WCP-side events through
its public observer hook. A fake ``SurferWcpListener`` stand-in
captures the WCP commands the bridge would send to surfer.
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
    """Captures `send_to_surfer` calls; mirrors the listener observer hook."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.event_observer = None

    def send_to_surfer(self, frame: dict) -> None:
        self.sent.append(frame)


# ---------------------------------------------------------------------------
# asyncio HubServer fixture in its own background thread
# ---------------------------------------------------------------------------


class _HubInThread:
    """Spin a HubServer on a dedicated asyncio loop in a thread.

    The bridge uses sync TCP; the hub is asyncio. Running the hub in a
    background thread is the cleanest way to exercise both from one
    test process.
    """

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
        self.loop.call_soon_threadsafe(self.loop.stop)
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
    """Bridge swallows malformed events rather than crashing the WCP thread."""

    host, port = hub_in_thread.server.host, hub_in_thread.server.port  # type: ignore[union-attr]
    listener = _FakeListener()
    bridge = WaveHubBridge.connect((host, port), listener=listener)
    try:
        # No timestamp field — bridge translates to None and drops.
        bridge.on_wcp_event("cursor_moved", {})
        # No exception, no message — success.
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
        assert resp.payload == {"ids": []}
        # And the bridge translated the request into a WCP command.
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
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


def test_wave_set_scope_translates_to_add_scope_until_fork(
    hub_in_thread: _HubInThread,
):
    """Per §9.3, surfer's fork will add `set_scope`; until then, `add_scope`."""

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
        assert resp.payload == {"ok": True}

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not listener.sent:
            time.sleep(0.02)
        assert listener.sent == [
            {
                "type": "command",
                "command": "add_scope",
                "scope": "tb.dut.u_fifo",
            }
        ]
    finally:
        view_sock.close()
        bridge.stop()


# ---------------------------------------------------------------------------
# bye / cleanup
# ---------------------------------------------------------------------------


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
