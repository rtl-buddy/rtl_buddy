"""Tests for ``rtl_buddy.hub.viewer_http`` — the HTTP + WS layer.

The ``ViewerServer`` is wired up against a real ``HubServer`` running
on a sibling port so the WS proxy exercises actual handshake +
broadcast through the live hub dispatch. HTTP tests cover the
placeholder body, hub-address injection, and the optional viewer
bundle path.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
import websockets

from rtl_buddy.hub.protocol import (
    Envelope,
    Kind,
    Origin,
    decode,
    encode,
    new_id,
)
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.viewer_http import (
    PLACEHOLDER_HTML,
    ViewerServer,
    render_index_html,
)


# ---------------------------------------------------------------------------
# render_index_html
# ---------------------------------------------------------------------------


def test_render_index_html_injects_hub_addr():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:54321")
    assert b"window.__RTL_BUDDY_HUB__" in body
    assert b"127.0.0.1:54321" in body
    # Placeholder marker should have been removed:
    assert b"%HUB_INJECTION%" not in body


def test_render_index_html_uses_bundle_when_present(tmp_path: Path):
    idx = tmp_path / "index.html"
    idx.write_text(
        "<!doctype html><html><head><title>real</title></head>"
        "<body>real viewer</body></html>",
        encoding="utf-8",
    )
    body = render_index_html(bundle_index=idx, hub_addr="127.0.0.1:1234")
    assert b"real viewer" in body
    assert b"window.__RTL_BUDDY_HUB__" in body
    assert b"127.0.0.1:1234" in body


def test_render_index_html_falls_back_to_placeholder(tmp_path: Path):
    body = render_index_html(
        bundle_index=tmp_path / "missing.html", hub_addr="127.0.0.1:1"
    )
    assert b"viewer placeholder" in body.lower() or b"placeholder" in body.lower()


def test_placeholder_html_contains_injection_marker():
    """The marker must exist so render_index_html can do a direct replace."""

    assert "%HUB_INJECTION%" in PLACEHOLDER_HTML


# ---------------------------------------------------------------------------
# combined HubServer + ViewerServer fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def hub_and_viewer() -> AsyncIterator[tuple[HubServer, ViewerServer]]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())

    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    viewer_task = asyncio.create_task(viewer.serve_forever())

    try:
        yield hub, viewer
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (viewer_task, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest.mark.asyncio
async def test_http_root_returns_placeholder(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"window.__RTL_BUDDY_HUB__" in body
    assert f"{viewer.hub_host}:{viewer.hub_port}".encode("utf-8") in body


@pytest.mark.asyncio
async def test_http_index_html_route(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/index.html"
    status, _, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert b"placeholder" in body.lower()


@pytest.mark.asyncio
async def test_http_healthz(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/healthz"
    status, _, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert body.strip() == b"ok"


@pytest.mark.asyncio
async def test_http_404_for_unknown_path(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/does/not/exist"
    try:
        await asyncio.to_thread(_http_get, url)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        pytest.fail("expected 404")


@pytest.mark.asyncio
async def test_http_serves_static_from_bundle(tmp_path: Path):
    """When --viewer-bundle is a directory, static files under it are served."""

    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html>bundle index</html>", encoding="utf-8")
    (bundle / "assets").mkdir()
    (bundle / "assets" / "app.css").write_text("body{}", encoding="utf-8")

    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host, hub_port=hub_port, http_port=0, viewer_bundle=bundle
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url_idx = f"http://127.0.0.1:{viewer.http_port}/"
        status, _, body = await asyncio.to_thread(_http_get, url_idx)
        assert status == 200
        assert b"bundle index" in body
        assert b"window.__RTL_BUDDY_HUB__" in body

        url_css = f"http://127.0.0.1:{viewer.http_port}/assets/app.css"
        status, headers, body = await asyncio.to_thread(_http_get, url_css)
        assert status == 200
        assert "text/css" in headers.get("Content-Type", "")
        assert body == b"body{}"
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_http_rejects_path_traversal_in_bundle(tmp_path: Path):
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("ok", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host, hub_port=hub_port, http_port=0, viewer_bundle=bundle
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        # urllib normalises ".." before sending, so we open a raw socket
        # and send "GET /../secret.txt" verbatim to exercise the
        # server-side traversal guard.
        reader, writer = await asyncio.open_connection("127.0.0.1", viewer.http_port)
        writer.write(b"GET /../secret.txt HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        data = b""
        for _ in range(40):
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                break
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        # Whatever the server returns, it MUST NOT be the secret body.
        assert b"nope" not in data
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# WebSocket proxying
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_hello_welcome_round_trip(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"ws://127.0.0.1:{viewer.http_port}/ws"
    async with websockets.connect(url) as ws:
        hello = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "view", "version": "0.1.0", "capabilities": []},
        )
        await ws.send(encode(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        welcome = decode(raw if isinstance(raw, str) else raw.decode("utf-8"))
        assert welcome.type == "welcome"
        assert welcome.id == hello.id
        assert "view" in welcome.payload["registered_clients"]


@pytest.mark.asyncio
async def test_ws_broadcast_reaches_ws_client(hub_and_viewer):
    """A TCP client's broadcast should arrive at the WS client via the proxy."""

    hub, viewer = hub_and_viewer
    ws_url = f"ws://127.0.0.1:{viewer.http_port}/ws"

    async with websockets.connect(ws_url) as ws:
        # WS client registers as view.
        hello = Envelope(
            origin=Origin.VIEW,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "view", "version": "0.1.0", "capabilities": []},
        )
        await ws.send(encode(hello))
        await ws.recv()  # welcome

        # TCP client registers as wave.
        reader, writer = await asyncio.open_connection(hub.host, hub.port)
        tcp_hello = Envelope(
            origin=Origin.WAVE,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "wave", "version": "0.1.0", "capabilities": []},
        )
        writer.write(encode(tcp_hello).encode("utf-8") + b"\n")
        await writer.drain()
        await reader.readline()  # welcome

        # WS view → broadcast a selection. Should reach the TCP wave client.
        evt = Envelope(
            origin=Origin.VIEW,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": "top.u_fifo"},
        )
        await ws.send(encode(evt))

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        received = decode(line)
        assert received.type == "selection_changed"
        assert received.payload == {"instance_path": "top.u_fifo"}

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_ws_close_unregisters(hub_and_viewer):
    hub, viewer = hub_and_viewer
    ws_url = f"ws://127.0.0.1:{viewer.http_port}/ws"
    async with websockets.connect(ws_url) as ws:
        hello = Envelope(
            origin=Origin.SRC,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={"client": "src", "version": "0.1.0", "capabilities": []},
        )
        await ws.send(encode(hello))
        await ws.recv()  # welcome
        assert Origin.SRC in hub.registered_origins

    # Allow the close handshake to propagate to the hub's TCP side.
    for _ in range(40):
        if Origin.SRC not in hub.registered_origins:
            break
        await asyncio.sleep(0.05)
    assert Origin.SRC not in hub.registered_origins


@pytest.mark.asyncio
async def test_ws_with_no_hub_upstream_closes_cleanly():
    """WS server should close the WS when the hub TCP upstream is unreachable."""

    # ViewerServer pointed at a TCP port nothing is listening on.
    viewer = ViewerServer(hub_host="127.0.0.1", hub_port=1, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"ws://127.0.0.1:{viewer.http_port}/ws"
        with pytest.raises(websockets.ConnectionClosed):
            async with websockets.connect(url) as ws:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        await viewer.shutdown()
        vtask.cancel()
        try:
            await vtask
        except (asyncio.CancelledError, Exception):
            pass
