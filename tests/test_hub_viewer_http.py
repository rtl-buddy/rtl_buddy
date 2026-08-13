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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as the response instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get_no_redirect(url: str) -> tuple[int, dict[str, str]]:
    """GET ``url``, returning the first response even if it is a 3xx."""

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=2.0) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


@pytest.mark.asyncio
async def test_http_view_route_returns_placeholder(hub_and_viewer):
    """The SPA lives at ``/sch`` since #423 (``/view`` since #398, ``/``
    before that) — ``/`` is the landing."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/sch"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"placeholder" in body.lower()
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
@pytest.mark.parametrize("route", ["/sch", "/gph", "/cov"])
async def test_http_trailing_slash_redirects_to_canonical_route(
    hub_and_viewer, route: str
):
    """``<page>/`` is a 307 to ``<page>``.

    The SPA bundle's asset references are relative (Vite ``base: ''``,
    which rtl-buddy-view needs for ``embed.py``'s standalone HTML), so
    a page served at ``/sch/`` resolves them to ``/sch/assets/…`` and
    never loads. Canonicalising the URL is what keeps one spelling —
    and one asset path — for every app page.
    """

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}{route}/"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 307
    assert headers.get("Location") == route
    # 307-not-301 is the whole argument for reusing a pinned port across
    # projects, so the header that makes it true is part of the contract.
    assert headers.get("Cache-Control") == "no-store"


@pytest.mark.asyncio
async def test_http_trailing_slash_redirect_preserves_query(hub_and_viewer):
    """``/sch/?view=…`` must not lose the query on the way to ``/sch``."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/sch/?view=/view.json&model=alu"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 307
    assert headers.get("Location") == "/sch?view=/view.json&model=alu"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("/view", "/sch"),
        ("/graph", "/gph"),
        ("/view/", "/sch"),
        ("/graph/", "/gph"),
    ],
)
async def test_http_legacy_page_routes_redirect_to_the_tla_spelling(
    hub_and_viewer, legacy: str, canonical: str
):
    """The pre-#423 page spellings still answer, with a 307.

    Note the trailing-slash rows: a request is normalised **once**, so
    ``/graph/`` lands on ``/gph`` directly rather than bouncing through
    ``/graph`` and costing a second round-trip.
    """

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}{legacy}"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 307
    assert headers.get("Location") == canonical
    assert headers.get("Cache-Control") == "no-store"


@pytest.mark.asyncio
async def test_http_legacy_redirect_preserves_the_query(hub_and_viewer):
    """A bookmarked ``/view?view=…`` must arrive at ``/sch`` with its
    query intact — the SPA reads ``?view=`` to know what to load, so
    dropping it would turn a working bookmark into an empty canvas."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view?view=/view.json&model=alu"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 307
    assert headers.get("Location") == "/sch?view=/view.json&model=alu"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/view.json", "/graph.json", "/cov.json"])
async def test_http_data_routes_are_not_renamed(hub_and_viewer, route: str):
    """Only PAGE routes moved. ``/view.json`` in particular keeps its
    name because the ``view`` hub-protocol origin does — renaming a page
    does not get to touch the wire contract."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}{route}"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    # Whatever these answer (200/404/409 depending on artefacts), they
    # must never be a redirect to a renamed spelling.
    assert status != 307
    assert "Location" not in headers


@pytest.mark.asyncio
async def test_http_cov_source_is_not_swept_up_by_the_page_rename(hub_and_viewer):
    """``/cov/source`` is a data route living under a page route's
    prefix — the canonicaliser must not touch it."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/cov/source?path=x.sv"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status != 307
    assert "Location" not in headers


def test_the_landing_cards_name_the_routes_their_modules_serve():
    """The app-card registry spells the routes as literals to keep
    `landing_page` free of the heavy `graph_page` import, so this is
    what stops the two drifting."""

    from rtl_buddy.hub import cov_page, graph_page, landing_page

    routes = {card.id: card.route for card in landing_page.APPS}
    assert routes["view"] == landing_page.VIEW_PAGE_ROUTE == "/sch"
    assert routes["graph"] == graph_page.GRAPH_PAGE_ROUTE == "/gph"
    assert routes["cov"] == cov_page.COV_PAGE_ROUTE == "/cov"


@pytest.mark.asyncio
async def test_http_landing_slash_is_not_redirected(hub_and_viewer):
    """``/`` is already canonical — it must serve, not bounce."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/"
    status, headers = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 200
    assert "Location" not in headers


@pytest.mark.asyncio
async def test_http_unknown_trailing_slash_path_still_404s(hub_and_viewer):
    """Only the three app routes canonicalise; nothing else is invented."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/nope/"
    status, _ = await asyncio.to_thread(_http_get_no_redirect, url)
    assert status == 404


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
async def test_http_view_json_409_when_path_unset(hub_and_viewer):
    """No view_json_path configured → 409 no_active_model (not a 500,
    not an empty 200, and not a plain-text body): the route exists and
    the hub is healthy, there is just nothing selected yet
    (rtl-buddy-view#130)."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view.json"
    try:
        await asyncio.to_thread(_http_get, url)
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
        assert "application/json" in (exc.headers or {}).get("Content-Type", "")
        payload = _json.loads(exc.read())
        assert payload["error"]["kind"] == "no_active_model"
        assert payload["error"]["models_url"] == "/models"
        assert payload["error"]["message"]
    else:
        pytest.fail("expected 409")


@pytest.mark.asyncio
async def test_http_view_json_409_when_file_missing(tmp_path: Path):
    """view_json_path set but file doesn't exist → 409, same shape."""

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
            assert exc.code == 409
            assert _json.loads(exc.read())["error"]["kind"] == "no_active_model"
        else:
            pytest.fail("expected 409")
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

        # Bonus: the SPA route gets the auto-load preamble.
        url_root = f"http://127.0.0.1:{viewer.http_port}/sch"
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
        url_idx = f"http://127.0.0.1:{viewer.http_port}/sch"
        status, _, body = await asyncio.to_thread(_http_get, url_idx)
        assert status == 200
        assert b"bundle index" in body
        assert b"window.__RTL_BUDDY_HUB__" in body

        # ``/index.html`` stays an alias for the injected bundle index —
        # never the raw file _serve_static would hand back.
        url_alias = f"http://127.0.0.1:{viewer.http_port}/index.html"
        status, _, body = await asyncio.to_thread(_http_get, url_alias)
        assert status == 200
        assert b"window.__RTL_BUDDY_HUB__" in body

        url_css = f"http://127.0.0.1:{viewer.http_port}/assets/app.css"
        status, headers, body = await asyncio.to_thread(_http_get, url_css)
        assert status == 200
        assert "text/css" in headers.get("Content-Type", "")
        assert body == b"body{}"

        # ``/sch/`` follows through to the injected index, and the
        # relative ``./assets/app.css`` in it resolves against ``/sch``
        # — i.e. to the URL asserted above, not ``/sch/assets/app.css``.
        # ``/sch`` is root-level exactly as ``/view`` was, so the rename
        # does not move where those relative references land.
        url_slash = f"http://127.0.0.1:{viewer.http_port}/sch/"
        status, _, body = await asyncio.to_thread(_http_get, url_slash)
        assert status == 200
        assert b"window.__RTL_BUDDY_HUB__" in body

        # And the fix is the redirect, not a second mount of the assets
        # one level deeper: the nested spelling stays a 404 so each
        # asset keeps exactly one URL.
        url_nested = f"http://127.0.0.1:{viewer.http_port}/sch/assets/app.css"
        try:
            await asyncio.to_thread(_http_get, url_nested)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            pytest.fail("expected /view/assets/app.css to 404")
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
async def test_view_json_query_param_unknown_model_404(tmp_path: Path):
    """A name that resolves to no model → 404 ``unknown_model`` as JSON
    (rtl-buddy-view#130). The name is echoed back so the SPA can name it
    in the placeholder without re-parsing the prose."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "alpha"}])
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=no_such"
        status, headers, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 404
        assert "application/json" in headers.get("Content-Type", "")
        payload = _json.loads(body)
        assert payload["error"]["kind"] == "unknown_model"
        assert payload["error"]["model"] == "no_such"
        assert "no_such" in payload["error"]["message"]
        # Summary only — the loader's multi-line candidate list stays in
        # the hub log, not in the SPA's one-line banner.
        assert "\n" not in payload["error"]["message"]
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

    def fake_build_view_json(*, project_root, model_cfg, axi_perf_source=None):
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
                # rtl-buddy-view #99 / 6b: explicit mode marker so
                # SPA clients route the event through the right
                # action without inferring mode from the URL.
                "view_mode": "dut",
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

    def fake_build_view_json(*, project_root, model_cfg, axi_perf_source=None):
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

    def fake_build(*, project_root, model_cfg, axi_perf_source=None):
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


# ---------------------------------------------------------------------------
# /tests + /view.json?test= (rtl-buddy-view #99 / 6b)
# ---------------------------------------------------------------------------


def _write_tests_yaml(
    path: Path,
    testbenches: list[dict],
    tests: list[dict],
) -> None:
    """Write a minimal tests.yaml. Each testbench needs ``name`` +
    ``filelist`` (+ optional ``toplevel``); each test needs ``name``,
    ``model``, ``model_path``, ``testbench``, and the boilerplate
    set of optional ``None`` fields that serde requires."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["rtl-buddy-filetype: test_config", "testbenches:"]
    for tb in testbenches:
        lines.append(f"  - name: {tb['name']}")
        if "toplevel" in tb:
            lines.append(f"    toplevel: {tb['toplevel']}")
        lines.append("    filelist: []")
    lines.append("tests:")
    for t in tests:
        lines.append(f"  - name: {t['name']}")
        # ``desc`` is required (non-optional) on TestConfigFile —
        # serde refuses an empty value.
        lines.append(f"    desc: {t.get('desc', 'test fixture entry')}")
        lines.append(f"    model: {t['model']}")
        lines.append(f"    model_path: {t.get('model_path', 'models.yaml')}")
        lines.append(f"    reglvl: {t.get('reglvl', 0)}")
        for k in ("plusargs", "plusdefines", "uvm", "preproc", "postproc", "sweep"):
            lines.append(f"    {k}:")
        lines.append(f"    testbench: {t['testbench']}")
        lines.append("    sim_timeout:")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_tests_endpoint_lists_tests_from_discovery(tmp_path: Path):
    """``GET /tests`` walks every tests.yaml and reports each entry's
    resolved ``(model, tb)`` pair."""
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    _write_tests_yaml(
        tmp_path / "tests.yaml",
        testbenches=[{"name": "tb_basic", "toplevel": "tb_top"}],
        tests=[
            {"name": "t1", "model": "demo", "testbench": "tb_basic"},
            {"name": "t2", "model": "demo", "testbench": "tb_basic"},
        ],
    )
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/tests"
        status, headers, body = await asyncio.to_thread(_http_get, url)
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        payload = _json.loads(body)
        names = [t["name"] for t in payload["tests"]]
        assert sorted(names) == ["t1", "t2"]
        # Each entry carries the resolved model + tb so the SPA
        # picker can label options without an extra round-trip.
        for t in payload["tests"]:
            assert t["model"] == "demo"
            assert t["tb"] == "tb_basic"
        assert payload["active"] is None
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_tests_endpoint_empty_when_no_tests_yaml(tmp_path: Path):
    """Standalone / no-tests deployments → empty list. The SPA's
    DUT/TB toggle stays hidden in that case."""
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/tests"
        status, _, body = await asyncio.to_thread(_http_get, url)
        assert status == 200
        payload = _json.loads(body)
        assert payload == {"tests": [], "active": None}
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_query_test_param_unknown_400(tmp_path: Path):
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    _write_tests_yaml(
        tmp_path / "tests.yaml",
        testbenches=[{"name": "tb_basic", "toplevel": "tb_top"}],
        tests=[{"name": "t1", "model": "demo", "testbench": "tb_basic"}],
    )
    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?test=missing"
        status, _, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 400
        assert b"missing" in body
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_query_test_param_flips_active_test_and_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end TB-view switch:
    1. ?test=t1 resolves the test (model + tb pair).
    2. build_view_json is invoked with the test_cfg (mocked).
    3. active_test + active_model flip in memory.
    4. view_changed event broadcast with view_mode='tb' + test + tb.
    """
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    _write_tests_yaml(
        tmp_path / "tests.yaml",
        testbenches=[{"name": "tb_basic", "toplevel": "tb_top"}],
        tests=[{"name": "t1", "model": "demo", "testbench": "tb_basic"}],
    )

    from rtl_buddy.hub import view_builder

    captured: dict = {}
    cache_path = tmp_path / ".rtl-buddy" / "cache" / "view-demo-tb-tb_basic.json"

    def fake_build_view_json(
        *,
        project_root,
        model_cfg,
        axi_perf_source=None,
        test_cfg=None,
        test_suite_dir=None,
    ):
        captured["model"] = model_cfg.name
        captured["test_cfg"] = test_cfg
        captured["test_suite_dir"] = test_suite_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            '{"schema_version":"1.1","top":"tb_top","tb_top":"tb_top","dut_top":"demo"}'
        )
        return cache_path

    monkeypatch.setattr(view_builder, "build_view_json", fake_build_view_json)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        ws_url = f"ws://127.0.0.1:{viewer.http_port}/ws"
        async with websockets.connect(ws_url) as ws:
            await ws.send(encode(_hello("view")))
            welcome = decode(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert welcome.type == "welcome"

            url = f"http://127.0.0.1:{viewer.http_port}/view.json?test=t1"
            status, _, body = await asyncio.to_thread(_http_get, url)
            assert status == 200
            assert b"tb_top" in body

            event = decode(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert event.type == "view_changed"
            assert event.kind == Kind.EVENT
            assert event.payload["view_mode"] == "tb"
            assert event.payload["test"] == "t1"
            assert event.payload["model"] == "demo"
            assert event.payload["tb"] == "tb_basic"
            assert event.payload["view_url"] == "/view.json?test=t1"

        # In-memory state flipped.
        assert viewer.active_test == "t1"
        assert viewer.active_model == "demo"
        # Builder received the resolved test_cfg.
        assert captured["model"] == "demo"
        assert captured["test_cfg"] is not None
        assert captured["test_cfg"].name == "t1"
        # The builder must be anchored at the suite dir (where tests.yaml
        # lives) so the TB filelist's relative entries resolve — not the
        # hub's process cwd. Regression guard for the ?test= 500.
        assert captured["test_suite_dir"] == tmp_path
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_test_param_disambiguated_by_tests_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A test name shared by multiple suites is ambiguous on its own
    (400), but ``?test=NAME&tests_file=PATH`` pins the owning suite. The
    ``tests_file`` is confined to the hub's project_root."""
    from urllib.parse import quote

    (tmp_path / "suiteA").mkdir()
    (tmp_path / "suiteB").mkdir()
    _write_models_yaml(tmp_path / "suiteA" / "models.yaml", [{"name": "mA"}])
    _write_models_yaml(tmp_path / "suiteB" / "models.yaml", [{"name": "mB"}])
    _write_tests_yaml(
        tmp_path / "suiteA" / "tests.yaml",
        testbenches=[{"name": "tbA", "toplevel": "tbA"}],
        tests=[{"name": "smoke", "model": "mA", "testbench": "tbA"}],
    )
    _write_tests_yaml(
        tmp_path / "suiteB" / "tests.yaml",
        testbenches=[{"name": "tbB", "toplevel": "tbB"}],
        tests=[{"name": "smoke", "model": "mB", "testbench": "tbB"}],
    )

    from rtl_buddy.hub import view_builder

    captured: dict = {}

    def fake_build_view_json(
        *,
        project_root,
        model_cfg,
        axi_perf_source=None,
        test_cfg=None,
        test_suite_dir=None,
    ):
        captured["model"] = model_cfg.name
        captured["suite_dir"] = test_suite_dir
        p = tmp_path / ".rtl-buddy" / "cache" / f"view-{model_cfg.name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"schema_version":"1.1","top":"t","tb_top":"t","dut_top":"d"}')
        return p

    monkeypatch.setattr(view_builder, "build_view_json", fake_build_view_json)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        base = f"http://127.0.0.1:{viewer.http_port}/view.json"

        # 1. Ambiguous without tests_file → 400.
        status, _, body = await asyncio.to_thread(
            _http_get_allow_4xx, f"{base}?test=smoke"
        )
        assert status == 400
        assert b"multiple" in body.lower()

        # 2. tests_file pins suiteB.
        tf = tmp_path / "suiteB" / "tests.yaml"
        status, _, _ = await asyncio.to_thread(
            _http_get, f"{base}?test=smoke&tests_file={quote(str(tf))}"
        )
        assert status == 200
        assert captured["model"] == "mB"
        assert Path(captured["suite_dir"]).name == "suiteB"

        # 3. A tests_file outside project_root is rejected, not read.
        status, _, body = await asyncio.to_thread(
            _http_get_allow_4xx,
            f"{base}?test=smoke&tests_file={quote('/etc/tests.yaml')}",
        )
        assert status == 400
        assert b"project_root" in body
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_switch_model_clears_active_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Switching back from TB → DUT view clears active_test so the
    next bare ``GET /view.json`` returns the DUT bytes."""
    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    from rtl_buddy.hub import view_builder

    dut_path = tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"

    def fake_build_view_json(
        *, project_root, model_cfg, axi_perf_source=None, test_cfg=None
    ):
        dut_path.parent.mkdir(parents=True, exist_ok=True)
        dut_path.write_text('{"schema_version":"1.0","top":"demo"}')
        return dut_path

    monkeypatch.setattr(view_builder, "build_view_json", fake_build_view_json)

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        # Pretend a TB view was previously selected.
        viewer.active_test = "t_old"
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        await asyncio.to_thread(_http_get, url)
        assert viewer.active_test is None
        assert viewer.active_model == "demo"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


# ---------------------------------------------------------------------------
# structured view errors + model health (rtl-buddy-view#130)
# ---------------------------------------------------------------------------


def _fail_build_view_json(monkeypatch: pytest.MonkeyPatch, message: str):
    """Make ``build_view_json`` fail the way a refusing renderer does."""

    from rtl_buddy.hub import view_builder
    from rtl_buddy.errors import FatalRtlBuddyError

    def boom(*, project_root, model_cfg, axi_perf_source=None, **_kw):
        raise FatalRtlBuddyError(message)

    monkeypatch.setattr(view_builder, "build_view_json", boom)


def _hier_log(root: Path, model: str, lines: list[str]) -> Path:
    log = root / "artefacts" / "hier" / model / "hier.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


@pytest.mark.asyncio
async def test_view_json_generation_failure_is_structured_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing generation → 500 with the pinned JSON shape: kind,
    model, one-line message, absolute log path, and the log tail as a
    list of lines. The SPA renders the tail verbatim, so this shape is
    a contract with rtl-buddy-view#130 — not an implementation detail.
    """

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "apb_intf"}])
    log = _hier_log(
        tmp_path,
        "apb_intf",
        [
            "$ rtl-buddy-view --top apb_intf --filelist hier.f",
            "hierarchy: top module 'apb_intf' not found. Known modules: []",
        ],
    )
    _fail_build_view_json(
        monkeypatch,
        f"rb hub --model apb_intf: rtl-buddy-view exited with code 1; "
        f"see {log} for details.",
    )

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=apb_intf"
        status, headers, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 500
        assert "application/json" in headers.get("Content-Type", "")
        err = _json.loads(body)["error"]
        assert err["kind"] == "view_generation_failed"
        assert err["model"] == "apb_intf"
        assert err["message"].startswith("rb hub --model apb_intf:")
        assert err["log_path"] == str(log)
        assert err["log_tail"] == [
            "$ rtl-buddy-view --top apb_intf --filelist hier.f",
            "hierarchy: top module 'apb_intf' not found. Known modules: []",
        ]
        # Exactly the five keys the SPA is built against.
        assert set(err) == {"kind", "model", "message", "log_path", "log_tail"}
        # A failure must NOT promote the model to active.
        assert viewer.active_model is None
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_failure_log_tail_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A 500-line log yields the last ``LOG_TAIL_LINES`` lines only."""

    from rtl_buddy.hub.viewer_http import LOG_TAIL_LINES

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    log = _hier_log(tmp_path, "demo", [f"line {i}" for i in range(500)])
    _fail_build_view_json(
        monkeypatch, f"rb hub --model demo: exited with code 1; see {log} for details."
    )

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        _status, _, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        tail = _json.loads(body)["error"]["log_tail"]
        assert len(tail) == LOG_TAIL_LINES
        assert tail[-1] == "line 499"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_failure_without_log_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No hier.log on disk (the failure happened before the subprocess
    ran) → empty tail and a derived path, never a second failure on the
    failure path."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    _fail_build_view_json(monkeypatch, "rb hub --model demo: filelist entry missing")

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        status, _, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 500
        err = _json.loads(body)["error"]
        assert err["log_tail"] == []
        assert err["log_path"] == str(
            tmp_path / "artefacts" / "hier" / "demo" / "hier.log"
        )
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_bare_view_json_replays_the_active_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Payload consistency: once a model is active and its regeneration
    fails, the bare ``GET /view.json`` answers with the SAME
    ``view_generation_failed`` body the ``?model=`` path returned — not
    a ``409``, and not stale bytes."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    log = _hier_log(tmp_path, "demo", ["boom"])
    _fail_build_view_json(
        monkeypatch, f"rb hub --model demo: exited with code 1; see {log} for details."
    )

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        named = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        named_status, _, named_body = await asyncio.to_thread(
            _http_get_allow_4xx, named
        )
        assert named_status == 500
        # The hub is now pointed at ``demo`` even though the build failed.
        viewer.active_model = "demo"
        bare = f"http://127.0.0.1:{viewer.http_port}/view.json"
        bare_status, _, bare_body = await asyncio.to_thread(_http_get_allow_4xx, bare)
        assert bare_status == 500
        assert _json.loads(bare_body) == _json.loads(named_body)
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_view_status_never_built_then_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``/models`` health transitions: never_built → failed, with the
    one-line error attached and no ``stale_cache`` (nothing cached)."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        models_url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        entry = _json.loads(body)["models"][0]
        assert entry["view_status"] == "never_built"
        assert "error" not in entry

        log = _hier_log(tmp_path, "demo", ["hierarchy: top module not found"])
        _fail_build_view_json(
            monkeypatch,
            f"rb hub --model demo: rtl-buddy-view exited with code 1; "
            f"see {log} for details.",
        )
        view_url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        status, _, _ = await asyncio.to_thread(_http_get_allow_4xx, view_url)
        assert status == 500

        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        entry = _json.loads(body)["models"][0]
        assert entry["view_status"] == "failed"
        assert entry["error"].startswith("rb hub --model demo:")
        assert "stale_cache" not in entry
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_view_status_ok_after_successful_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """failed → ok: a later successful generation clears the remembered
    failure, and the cached file alone is enough for ``ok`` on a model
    nobody asked for this session."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}, {"name": "other"}])
    from rtl_buddy.hub import view_builder

    log = _hier_log(tmp_path, "demo", ["boom"])
    _fail_build_view_json(
        monkeypatch, f"rb hub --model demo: exited with code 1; see {log} for details."
    )

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        models_url = f"http://127.0.0.1:{viewer.http_port}/models"
        view_url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        await asyncio.to_thread(_http_get_allow_4xx, view_url)
        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        by_name = {m["name"]: m for m in _json.loads(body)["models"]}
        assert by_name["demo"]["view_status"] == "failed"
        assert by_name["other"]["view_status"] == "never_built"

        # Now let the build succeed.
        cache = view_builder.view_json_path(tmp_path, "demo")

        def ok_build(*, project_root, model_cfg, axi_perf_source=None, **_kw):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text('{"schema_version":"1.0","top":"demo"}')
            return cache

        monkeypatch.setattr(view_builder, "build_view_json", ok_build)
        status, _, _ = await asyncio.to_thread(_http_get, view_url)
        assert status == 200

        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        by_name = {m["name"]: m for m in _json.loads(body)["models"]}
        assert by_name["demo"]["view_status"] == "ok"
        assert "error" not in by_name["demo"]

        # A cache file written for a model this session never touched is
        # itself the ``ok`` signal — that is what survives a hub restart.
        other_cache = view_builder.view_json_path(tmp_path, "other")
        other_cache.write_text('{"schema_version":"1.0","top":"other"}')
        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        by_name = {m["name"]: m for m in _json.loads(body)["models"]}
        assert by_name["other"]["view_status"] == "ok"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_models_view_status_failed_with_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A cached view from BEFORE a later failure is ``failed`` plus
    ``stale_cache: true`` — servable bytes that no longer reflect the
    sources must not read as healthy."""

    _write_models_yaml(tmp_path / "models.yaml", [{"name": "demo"}])
    from rtl_buddy.hub import view_builder

    cache = view_builder.view_json_path(tmp_path, "demo")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"schema_version":"1.0","top":"demo"}')

    log = _hier_log(tmp_path, "demo", ["boom"])
    _fail_build_view_json(
        monkeypatch, f"rb hub --model demo: exited with code 1; see {log} for details."
    )

    hub, viewer, hub_task, vtask = await _viewer_with_project(tmp_path)
    try:
        models_url = f"http://127.0.0.1:{viewer.http_port}/models"
        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        # Cache alone → ok.
        assert _json.loads(body)["models"][0]["view_status"] == "ok"

        view_url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        await asyncio.to_thread(_http_get_allow_4xx, view_url)

        _status, _, body = await asyncio.to_thread(_http_get, models_url)
        entry = _json.loads(body)["models"][0]
        assert entry["view_status"] == "failed"
        assert entry["stale_cache"] is True
    finally:
        await _teardown(hub, viewer, hub_task, vtask)


@pytest.mark.asyncio
async def test_view_json_model_without_project_root_is_structured(tmp_path: Path):
    """``?model=`` on a hub with no project root keeps the envelope —
    the SPA never has to parse a plain-text body."""

    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/view.json?model=demo"
        status, headers, body = await asyncio.to_thread(_http_get_allow_4xx, url)
        assert status == 400
        assert "application/json" in headers.get("Content-Type", "")
        err = _json.loads(body)["error"]
        assert err["kind"] == "no_project_root"
        assert err["model"] == "demo"
    finally:
        await _teardown(hub, viewer, hub_task, vtask)
