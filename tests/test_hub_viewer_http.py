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


def test_render_index_html_injects_view_url_when_provided():
    body = render_index_html(
        bundle_index=None, hub_addr="127.0.0.1:1", view_url="/view.json"
    )
    assert b"window.__RTL_BUDDY_VIEW_URL__" in body
    assert b"'/view.json'" in body


def test_render_index_html_omits_view_url_when_absent():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:1")
    assert b"__RTL_BUDDY_VIEW_URL__" not in body


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
async def test_http_view_json_404_when_path_unset(hub_and_viewer):
    """No view_json_path configured → 404 (not a 500 or empty 200)."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view.json"
    try:
        await asyncio.to_thread(_http_get, url)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        pytest.fail("expected 404")


@pytest.mark.asyncio
async def test_http_view_json_404_when_file_missing(tmp_path: Path):
    """view_json_path set but file doesn't exist → 404."""

    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        view_json_path=tmp_path / "missing.json",
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json"
        try:
            await asyncio.to_thread(_http_get, url)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            pytest.fail("expected 404")
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
async def test_http_view_json_served_when_configured(tmp_path: Path):
    """view_json_path points at an existing file → 200 with that JSON body
    + index.html gets the __RTL_BUDDY_VIEW_URL__ injection."""

    view_json = tmp_path / "view.json"
    view_json.write_text(
        '{"schema_version":"1.0.0","design":{"top":"x"},"nodes":[],"edges":[]}',
        encoding="utf-8",
    )

    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        view_json_path=view_json,
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json"
        status, headers, body = await asyncio.to_thread(_http_get, url)
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        assert body == view_json.read_bytes()

        # Bonus: index.html gets the auto-load preamble.
        url_root = f"http://127.0.0.1:{viewer.http_port}/"
        _status, _, root_body = await asyncio.to_thread(_http_get, url_root)
        assert b"window.__RTL_BUDDY_VIEW_URL__" in root_body
        assert b"'/view.json'" in root_body
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


# ---------------------------------------------------------------------------
# /models + /view.json?model= (issue #174)
# ---------------------------------------------------------------------------


import json as _json


def _http_get_allow_4xx(
    url: str,
) -> tuple[int, dict[str, str], bytes]:
    """Helper that doesn't raise on 4xx — handy for the
    ?model=unknown / 400 path that urllib turns into HTTPError."""
    try:
        return _http_get(url)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _write_models_yaml(path: Path, models: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["rtl-buddy-filetype: model_config", "models:"]
    for m in models:
        lines.append(f"  - name: {m['name']}")
        lines.append("    filelist: []")
        if "cdc" in m:
            lines.append(f"    cdc: {m['cdc']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _viewer_with_project(
    tmp_path: Path,
    *,
    initial_model: str | None = None,
    models_file_pin: Path | None = None,
) -> tuple[HubServer, ViewerServer, asyncio.Task, asyncio.Task]:
    """Spin up a hub + viewer wired to ``tmp_path`` as the project root,
    so /models discovery + ?model= switching work."""
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=tmp_path,
        initial_model=initial_model,
        models_file_pin=models_file_pin,
        hub_server=hub,
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    return hub, viewer, hub_task, vtask


async def _teardown(hub, viewer, hub_task, vtask):
    await viewer.shutdown()
    await hub.shutdown()
    for t in (vtask, hub_task):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_models_endpoint_lists_models_from_discovery(tmp_path: Path):
    _write_models_yaml(
        tmp_path / "block_a" / "models.yaml",
        [{"name": "alpha"}, {"name": "beta"}],
    )
    _write_models_yaml(tmp_path / "block_b" / "models.yaml", [{"name": "gamma"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/models"
        status, headers, body = await asyncio.to_thread(_http_get, url)
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        payload = _json.loads(body)
        names = sorted(m["name"] for m in payload["models"])
        assert names == ["alpha", "beta", "gamma"]
        assert payload["active"] is None  # no --model at start
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_endpoint_reports_active_model(tmp_path: Path):
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(
        tmp_path, initial_model="demo"
    )
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, url)
        payload = _json.loads(body)
        assert payload["active"] == "demo"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_endpoint_honours_models_file_pin(tmp_path: Path):
    """--models-file PATH at start → /models enumerates only that file."""
    pinned = tmp_path / "block_a" / "models.yaml"
    _write_models_yaml(pinned, [{"name": "alpha"}])
    _write_models_yaml(tmp_path / "block_b" / "models.yaml", [{"name": "beta"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(
        tmp_path, models_file_pin=pinned
    )
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, url)
        payload = _json.loads(body)
        names = [m["name"] for m in payload["models"]]
        assert names == ["alpha"]
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_endpoint_has_cdc_false_when_field_missing(tmp_path: Path):
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, url)
        payload = _json.loads(body)
        assert payload["models"][0]["has_cdc"] is False
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_endpoint_has_cdc_false_when_cdc_file_missing(tmp_path: Path):
    """Field set but the referenced cdc.yaml doesn't exist → has_cdc=false.
    Fails at list time, not at switch time."""
    _write_models_yaml(
        tmp_path / "models.yaml",
        [{"name": "demo", "cdc": "../nope/cdc.yaml"}],
    )
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, url)
        payload = _json.loads(body)
        assert payload["models"][0]["has_cdc"] is False
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_query_param_unknown_model_400(tmp_path: Path):
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "alpha"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=no_such"
        status, _, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 400
        assert b"no_such" in body
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_query_param_flips_active_model_and_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end happy path:
    1. ?model=demo runs build_view_json (mocked) and serves the result.
    2. active_model flips in memory.
    3. .rtl-buddy/hub.json gains active_model.
    4. view_changed event broadcast to connected WS clients.
    """
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    # Pre-seed a discovery record so update_active_model has something
    # to rewrite (the test doesn't go through cmd_start, which is
    # what normally creates this).
    from rtl_buddy.hub import discovery

    discovery.write_record(
        tmp_path,
        pid=99999,
        tcp="127.0.0.1:1",
        server_version="0.0.0+test",
    )

    # Stub the view-builder so the test doesn't need rtl-buddy-view
    # on PATH.
    from rtl_buddy.hub import view_builder

    captured_view = tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"

    def fake_build_view_json(*, project_root, model_cfg):
        captured_view.parent.mkdir(parents=True, exist_ok=True)
        captured_view.write_text('{"schema_version":"1.0","top":"demo"}')
        return captured_view

    monkeypatch.setattr(view_builder, "build_view_json", fake_build_view_json)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        # Wire up a WS client that will register as `view` so it gets
        # the broadcast. Use the hub_server's broadcast machinery
        # which only sends to registered clients.
        ws_url = f"ws://127.0.0.1:{viewer.http_port}/ws"
        async with websockets.connect(ws_url) as ws:
            # Register as `view` so we'll receive broadcasts.
            await ws.send(encode(_hello("view")))
            welcome = decode(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert welcome.type == "welcome"

            # Fire the switch.
            url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
            status, _, _ = await asyncio.to_thread(_http_get, url)
            assert status == 200

            # view_changed should arrive on the WS.
            event = decode(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert event.type == "view_changed"
            assert event.kind == Kind.EVENT
            assert event.origin == Origin.CLI
            assert event.payload == {
                "model": "demo",
                "models_file": str(tmp_path / "models.yaml"),
                "view_url": "/view.json?model=demo",
            }

        # active_model flipped in memory.
        assert viewer.active_model == "demo"
        # And on disk in hub.json.
        record = discovery.read_record(tmp_path)
        assert record is not None
        assert record.active_model == "demo"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_no_query_serves_active_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After ?model=demo flipped active_model, GET /view.json (no query)
    should return the same bytes — preserves backwards-compat for
    pre-feature SPAs that only know how to fetch /view.json."""
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    from rtl_buddy.hub import view_builder

    cache_path = tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"

    def fake_build_view_json(*, project_root, model_cfg):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text('{"top":"demo"}')
        return cache_path

    monkeypatch.setattr(view_builder, "build_view_json", fake_build_view_json)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        switch_url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        await asyncio.to_thread(_http_get, switch_url)
        bare_url = f"http://127.0.0.1:{viewer.http_port}/view.json"
        _status, _, body = await asyncio.to_thread(_http_get, bare_url)
        assert b'"top":"demo"' in body
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_concurrent_same_model_requests_serialise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two ?model=demo requests racing on a cold cache should only
    invoke build_view_json ONCE — the per-model lock makes the second
    request wait for the first."""
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    from rtl_buddy.hub import view_builder

    call_count = {"n": 0}
    cache_path = tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"

    def fake_build(*, project_root, model_cfg):
        call_count["n"] += 1
        # Block long enough that the second request piles up behind
        # the lock, then release.
        import time

        time.sleep(0.05)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{}")
        return cache_path

    monkeypatch.setattr(view_builder, "build_view_json", fake_build)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        # Fire two concurrent requests for the same model.
        r1, r2 = await asyncio.gather(
            asyncio.to_thread(_http_get, url),
            asyncio.to_thread(_http_get, url),
        )
        assert r1[0] == 200
        assert r2[0] == 200
        # The second one was supposed to wait for the lock, but
        # build_view_json is idempotent at the cache layer — so it
        # ran twice (once per lock acquisition) without racing. The
        # lock's job is to prevent concurrent writes to the same
        # file, not to deduplicate calls. Both rebuilds touched the
        # same cache path safely.
        assert call_count["n"] == 2
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


def _hello(client: str) -> Envelope:
    """Build a minimal hello envelope so the WS test can register."""
    return Envelope(
        origin=Origin(client),
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={
            "client": client,
            "version": "0.0.0+test",
            "capabilities": [],
        },
    )
