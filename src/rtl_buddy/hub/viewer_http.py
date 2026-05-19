"""HTTP + WebSocket front-end for the rtl-buddy-hub viewer.

Browsers can't speak the hub's raw TCP transport, so this module
embeds an HTTP server alongside :mod:`rtl_buddy.hub.server` that:

* serves the rtl-buddy-view SPA static bundle at ``/``,
* injects the hub's host:port into the page via a
  ``window.__RTL_BUDDY_HUB__`` script preamble (§4.4),
* exposes the hub's JSON-message channel as a WebSocket at ``/ws``,
  framed one envelope per WebSocket message.

WebSocket connections proxy through to a fresh TCP connection on the
hub's main listener. This keeps the dispatch layer transport-agnostic
— a WS client looks just like any other TCP client to the core hub,
so we don't fork the handshake / routing code between transports.

The viewer SPA itself ships in rtl-buddy-view (Phase 5,
``rtl-buddy/rtl-buddy-view#18``). Until that lands, ``--viewer-bundle``
points at the build output's ``index.html``; without a bundle we
serve a small placeholder that proves the HTTP + WS layer works
end-to-end so client code can be wired against it today.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from ..logging_utils import log_event


logger = logging.getLogger(__name__)


PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>rtl-buddy-hub viewer placeholder</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 40rem;
           margin: 4rem auto; padding: 0 1rem; line-height: 1.5; }
    code { background: #f3f4f6; padding: 0 .25rem; border-radius: 3px; }
    h1 { font-size: 1.4rem; }
    .ok  { color: #16a34a; }
    .err { color: #dc2626; }
  </style>
  <script>
    %HUB_INJECTION%
  </script>
</head>
<body>
  <h1>rtl-buddy-hub <small>(viewer placeholder)</small></h1>
  <p>
    The HTTP + WebSocket layer is live, but no viewer bundle is configured
    for this hub. The real Vue/Vite SPA ships in <a
    href="https://github.com/rtl-buddy/rtl-buddy-view/issues/18">
    rtl-buddy-view#18</a>; until then, this page exists to confirm the
    transport works.
  </p>
  <p>
    Inspect <code>window.__RTL_BUDDY_HUB__</code> in DevTools, or watch
    the WebSocket round-trip below.
  </p>
  <p id="status">Connecting to <code>/ws</code>…</p>
  <script>
    (function () {
      const status = document.getElementById('status');
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(proto + '//' + location.host + '/ws');
      ws.addEventListener('open', () => {
        const hello = {
          v: 1,
          id: crypto.randomUUID(),
          origin: 'view',
          kind: 'request',
          type: 'hello',
          payload: { client: 'view', version: '0.0.0+placeholder', capabilities: [] },
        };
        ws.send(JSON.stringify(hello));
      });
      ws.addEventListener('message', (ev) => {
        try {
          const obj = JSON.parse(ev.data);
          if (obj.type === 'welcome') {
            status.innerHTML = '<span class="ok">connected.</span> server_version: <code>' +
              obj.payload.server_version + '</code>; registered: <code>' +
              JSON.stringify(obj.payload.registered_clients) + '</code>';
          }
        } catch (e) { /* ignore */ }
      });
      ws.addEventListener('close', () => {
        status.innerHTML = '<span class="err">disconnected from /ws.</span>';
      });
    })();
  </script>
</body>
</html>
"""


def render_index_html(*, bundle_index: Path | None, hub_addr: str) -> bytes:
    """Return the HTML body served at ``/`` with hub address injected.

    When ``bundle_index`` points at an existing file, its contents are
    served with the ``%HUB_INJECTION%`` placeholder (or a ``<head>``
    insertion when the placeholder is absent) replaced by the script
    preamble. Otherwise the built-in placeholder is served — same
    injection rules.
    """

    if bundle_index is not None and bundle_index.is_file():
        html = bundle_index.read_text(encoding="utf-8")
    else:
        html = PLACEHOLDER_HTML

    preamble = f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};"

    if "%HUB_INJECTION%" in html:
        html = html.replace("%HUB_INJECTION%", preamble)
    else:
        # Insert a <script> just after <head>; falls back to prefix if no <head>.
        injection = f"<script>{preamble}</script>"
        lowered = html.lower()
        head_idx = lowered.find("<head>")
        if head_idx >= 0:
            insert_at = head_idx + len("<head>")
            html = html[:insert_at] + injection + html[insert_at:]
        else:
            html = injection + html
    return html.encode("utf-8")


class ViewerServer:
    """Serves the HTTP + ``/ws`` surface for the viewer SPA.

    The HTTP request handler is wired into ``websockets.serve`` via the
    ``process_request`` hook: a non-upgrade HTTP request gets a normal
    HTTP response (the index page or a static asset); an upgrade
    request proceeds through to the WebSocket handler. This lets one
    asyncio port serve both transports.
    """

    def __init__(
        self,
        *,
        hub_host: str,
        hub_port: int,
        http_port: int = 0,
        viewer_bundle: Path | None = None,
    ) -> None:
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.requested_http_port = http_port
        self.http_port = http_port
        self.viewer_bundle = viewer_bundle
        self._server: Any | None = None
        self._bundle_index = self._resolve_bundle_index(viewer_bundle)

    @staticmethod
    def _resolve_bundle_index(bundle: Path | None) -> Path | None:
        if bundle is None:
            return None
        if bundle.is_file():
            return bundle
        candidate = bundle / "index.html"
        if candidate.is_file():
            return candidate
        return None

    @property
    def hub_address(self) -> str:
        return f"{self.hub_host}:{self.hub_port}"

    async def start(self) -> tuple[str, int]:
        """Bind the HTTP+WS listener; return ``(host, port)``."""

        self._server = await websockets.serve(
            self._handle_ws,
            host="127.0.0.1",
            port=self.requested_http_port,
            process_request=self._process_request,
        )
        sockets = self._server.sockets or ()
        if not sockets:
            raise RuntimeError("viewer http server bound 0 sockets")
        host, port = sockets[0].getsockname()[:2]
        self.http_port = port
        log_event(
            logger,
            logging.INFO,
            "hub.viewer_http.listening",
            host=host,
            port=port,
            bundle=str(self.viewer_bundle) if self.viewer_bundle else "",
        )
        return host, port

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("call start() before serve_forever()")
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass
        self._server = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        path = request.path.split("?", 1)[0]

        # WS upgrade?  Let websockets handle it.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if path == "/ws":
                return None
            return _http_response(connection, 404, b"unknown ws path")

        # Plain HTTP.
        if path in ("/", "/index.html"):
            body = render_index_html(
                bundle_index=self._bundle_index, hub_addr=self.hub_address
            )
            return _http_response(
                connection, 200, body, content_type="text/html; charset=utf-8"
            )

        if path == "/healthz":
            return _http_response(connection, 200, b"ok\n", content_type="text/plain")

        # Bundle static assets: only served when the bundle is a directory.
        if self.viewer_bundle and self.viewer_bundle.is_dir():
            static = self._serve_static(connection, path)
            if static is not None:
                return static

        return _http_response(connection, 404, b"not found")

    def _serve_static(self, connection: ServerConnection, path: str) -> Response | None:
        assert self.viewer_bundle is not None
        target = (self.viewer_bundle / path.lstrip("/")).resolve()
        try:
            target.relative_to(self.viewer_bundle.resolve())
        except ValueError:
            return _http_response(connection, 403, b"forbidden")
        if not target.is_file():
            return None
        return _http_response(
            connection,
            200,
            target.read_bytes(),
            content_type=_guess_content_type(target),
        )

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _handle_ws(self, ws: Any) -> None:
        """Proxy a WS connection to the hub's TCP port.

        Each WebSocket message is one hub envelope. Inbound (WS → hub)
        becomes a line-delimited write; outbound (hub → WS) splits on
        newlines so a hub broadcast turns into one WS message per
        envelope.
        """

        try:
            reader, writer = await asyncio.open_connection(self.hub_host, self.hub_port)
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "hub.viewer_http.upstream_refused",
                error=str(exc),
            )
            await ws.close(code=1011, reason="hub upstream refused")
            return

        async def ws_to_tcp() -> None:
            try:
                async for msg in ws:
                    if isinstance(msg, str):
                        data = msg.encode("utf-8")
                    else:
                        data = msg
                    writer.write(data + b"\n")
                    await writer.drain()
            except (OSError, ConnectionClosed):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

        async def tcp_to_ws() -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    payload = line.rstrip(b"\r\n").decode("utf-8", errors="replace")
                    if payload:
                        await ws.send(payload)
            except (OSError, ConnectionClosed):
                pass

        tasks = [
            asyncio.create_task(ws_to_tcp(), name="ws-bridge-up"),
            asyncio.create_task(tcp_to_ws(), name="ws-bridge-down"),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _http_response(
    connection: ServerConnection,
    status: int,
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> Response:
    """Build an HTTP response with arbitrary bytes (text or binary)."""

    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    return Response(
        status_code=status,
        reason_phrase=_REASON_PHRASES.get(status, ""),
        headers=headers,
        body=body,
    )


_REASON_PHRASES = {
    200: "OK",
    403: "Forbidden",
    404: "Not Found",
}


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".map": "application/json",
}


def _guess_content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


__all__ = [
    "PLACEHOLDER_HTML",
    "ViewerServer",
    "render_index_html",
]
