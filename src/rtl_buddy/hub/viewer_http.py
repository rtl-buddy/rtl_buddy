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
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

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


def render_index_html(
    *,
    bundle_index: Path | None,
    hub_addr: str,
    view_url: str | None = None,
) -> bytes:
    """Return the HTML body served at ``/`` with hub address injected.

    When ``bundle_index`` points at an existing file, its contents are
    served with the ``%HUB_INJECTION%`` placeholder (or a ``<head>``
    insertion when the placeholder is absent) replaced by the script
    preamble. Otherwise the built-in placeholder is served — same
    injection rules.

    When ``view_url`` is provided, ``window.__RTL_BUDDY_VIEW_URL__`` is
    set alongside ``__RTL_BUDDY_HUB__`` so the SPA bootstrap can fetch
    the view.json without the user passing ``?view=`` in the URL.
    """

    if bundle_index is not None and bundle_index.is_file():
        html = bundle_index.read_text(encoding="utf-8")
    else:
        html = PLACEHOLDER_HTML

    parts = [f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};"]
    if view_url is not None:
        parts.append(f"window.__RTL_BUDDY_VIEW_URL__ = {view_url!r};")
    preamble = "\n".join(parts)

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
        view_json_path: Path | None = None,
        project_root: Path | None = None,
        initial_model: str | None = None,
        models_file_pin: Path | None = None,
        hub_server: Any | None = None,
    ) -> None:
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.requested_http_port = http_port
        self.http_port = http_port
        self.viewer_bundle = viewer_bundle
        self.view_json_path = view_json_path
        # Runtime-switchable model state. ``active_model`` is the model
        # currently served by ``GET /view.json`` with no query; flipped
        # by SPA ``?model=`` requests via ``_set_active_model``.
        self.project_root = project_root
        self.active_model = initial_model
        self.models_file_pin = models_file_pin
        self.hub_server = hub_server
        # Mirror the active model onto HubState so the ``state_snapshot``
        # request type can return it without reaching back into the HTTP
        # layer. Safe when hub_server is None (tests).
        if hub_server is not None:
            hub_server.state.active_model = initial_model
        # Per-model lock map. Two ``?model=X`` requests racing on a
        # cold cache funnel through one ``build_view_json`` call; two
        # ``?model=X`` / ``?model=Y`` requests run in parallel. Locks
        # are allocated lazily and never garbage-collected per session.
        self._model_locks: dict[str, asyncio.Lock] = {}
        # Marimo "Open in marimo" session cache (Phase 2.5).
        # ``(test, suite_dir) → LaunchResult``. Repeat clicks reuse
        # the cached entry when the spawned marimo is still alive
        # (``os.kill(pid, 0)`` succeeds). Per-key lock funnels
        # concurrent requests for the same notebook through one
        # spawn — analogous to ``_model_locks`` for /view.json?model=.
        self._axi_notebook_sessions: dict[tuple[str, str], Any] = {}
        self._axi_notebook_locks: dict[tuple[str, str], asyncio.Lock] = {}
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

    def _has_view_json(self) -> bool:
        return self.view_json_path is not None and self.view_json_path.is_file()

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
        # Reap the marimo subprocesses we spawned for /api/axi-profile/
        # notebook before tearing down the HTTP server. Without this
        # they survive hub restarts as orphans — each one holds an
        # OS port and a marimo session that nobody can reach (the SPA
        # only knows the URL via the now-dead hub).
        for key, session in list(self._axi_notebook_sessions.items()):
            _terminate_pid(session.pid)
            self._axi_notebook_sessions.pop(key, None)
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

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        # Async because ``/view.json?model=`` runs ``build_view_json``
        # in a thread (rtl-buddy-view is a blocking subprocess) under
        # a per-model ``asyncio.Lock``. ``websockets.serve`` accepts
        # both sync and async ``process_request`` callbacks.
        raw_path, _, query_string = request.path.partition("?")
        path = raw_path
        query = parse_qs(query_string)

        # WS upgrade?  Let websockets handle it.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if path == "/ws":
                return None
            return _http_response(connection, 404, b"unknown ws path")

        # Plain HTTP.
        if path in ("/", "/index.html"):
            body = render_index_html(
                bundle_index=self._bundle_index,
                hub_addr=self.hub_address,
                view_url="/view.json" if self._has_view_json() else None,
            )
            return _http_response(
                connection, 200, body, content_type="text/html; charset=utf-8"
            )

        if path == "/healthz":
            return _http_response(connection, 200, b"ok\n", content_type="text/plain")

        if path == "/models":
            return await self._handle_models(connection)

        if path == "/api/axi-profile/notebook":
            return await self._handle_axi_notebook(connection, query)

        if path == "/view.json":
            requested = query.get("model", [None])[0]
            if requested is not None:
                return await self._handle_view_json_for_model(connection, requested)
            # No ``?model=`` query → serve the active model. Falls back
            # to the start-time view.json (legacy path for pre-feature
            # SPAs / embed.py users) when no model is active yet.
            if not self._has_view_json():
                return _http_response(
                    connection, 404, b"no view.json configured for this hub"
                )
            assert self.view_json_path is not None
            return _http_response(
                connection,
                200,
                self.view_json_path.read_bytes(),
                content_type="application/json",
            )

        # Bundle static assets: only served when the bundle is a directory.
        if self.viewer_bundle and self.viewer_bundle.is_dir():
            static = self._serve_static(connection, path)
            if static is not None:
                return static

        return _http_response(connection, 404, b"not found")

    # ------------------------------------------------------------------
    # /models + /view.json?model= (issue #174)
    # ------------------------------------------------------------------

    async def _handle_axi_notebook(
        self, connection: ServerConnection, query: dict[str, list[str]]
    ) -> Response:
        """``GET /api/axi-profile/notebook?test=NAME&suite_dir=PATH``.

        Spawns ``rb axi-profile notebook --headless`` for the given
        ``test`` (which must exist in ``<suite_dir>/tests.yaml``),
        waits up to 30 s for marimo to print its URL, returns JSON.
        The spawned marimo persists after this request completes —
        it's the user's notebook session, intended to outlive the
        single HTTP round-trip.

        Repeat clicks for the same ``(test, suite_dir)`` reuse the
        cached marimo when its pid is still alive (single-instance
        per notebook, Phase 2.5). When the cached marimo has died
        the entry is dropped and a fresh one spawns.

        Response::

          {
            "url":       "http://localhost:NNNN",
            "pid":       12345,
            "port":      NNNN,
            "test":      "basic_traffic",
            "suite_dir": "/abs/path/to/verif/demo_axi_2x2",
            "reused":    false                            ← true when cache hit
          }

        Errors surface as JSON-bodied 4xx/5xx with a single ``error``
        key. ``project_root`` must be set on the hub (always true when
        started via ``rb hub start``).
        """
        import json as _json

        from . import axi_notebook_launcher

        if self.project_root is None:
            return _http_response(
                connection,
                500,
                _json.dumps({"error": "hub has no project_root configured"}).encode(),
                content_type="application/json",
            )
        test = (query.get("test") or [""])[0]
        suite_dir = (query.get("suite_dir") or [""])[0]

        # Per-(test, suite_dir) lock funnels concurrent requests for
        # the same notebook through one spawn. Without this, two SPA
        # clicks within marimo's ~3 s startup window would both miss
        # the cache and spawn duplicate processes on different ports.
        key = (test, suite_dir)
        lock = self._axi_notebook_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._axi_notebook_sessions.get(key)
            if cached is not None and _is_pid_alive(cached.pid):
                # Cache hit — return the same URL the user got last time.
                body = _json.dumps(
                    {
                        "url": cached.url,
                        "pid": cached.pid,
                        "port": cached.port,
                        "test": cached.test,
                        "suite_dir": cached.suite_dir,
                        "reused": True,
                    }
                ).encode()
                return _http_response(
                    connection, 200, body, content_type="application/json"
                )
            # Cache miss or stale → drop the dead entry, spawn fresh.
            if cached is not None:
                self._axi_notebook_sessions.pop(key, None)
            try:
                result = await axi_notebook_launcher.launch(
                    test=test,
                    suite_dir=suite_dir,
                    project_root=self.project_root,
                )
            except axi_notebook_launcher.AxiNotebookLaunchError as e:
                return _http_response(
                    connection,
                    e.status,
                    _json.dumps({"error": str(e)}).encode(),
                    content_type="application/json",
                )
            # Cache under the resolved key (suite_dir may have been
            # normalised to an absolute path by the launcher's
            # validator; use the request key so the next request with
            # the same input hits the cache).
            self._axi_notebook_sessions[key] = result
            body = _json.dumps(
                {
                    "url": result.url,
                    "pid": result.pid,
                    "port": result.port,
                    "test": result.test,
                    "suite_dir": result.suite_dir,
                    "reused": False,
                }
            ).encode()
            return _http_response(
                connection, 200, body, content_type="application/json"
            )

    async def _handle_models(self, connection: ServerConnection) -> Response:
        """``GET /models`` — list every model the hub can serve.

        Walks per-request so a freshly-edited ``models.yaml`` shows
        up without restarting the hub. When ``--models-file`` was
        pinned at start time, enumerates only that file.
        """

        from . import model_discovery
        from ..config.model import ModelConfigLoader

        if self.project_root is None:
            # ViewerServer started without project_root (e.g.
            # standalone test) → only have the legacy single
            # active model to report on.
            payload: dict[str, Any] = {"models": [], "active": self.active_model}
            return _http_response(
                connection,
                200,
                json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )

        try:
            if self.models_file_pin is not None:
                files = [self.models_file_pin]
            else:
                files = model_discovery.discover_models_files(self.project_root)

            entries: list[dict[str, Any]] = []
            for mf in files:
                # Robust against malformed files: skip silently here
                # (the user's primary models.yaml is presumably valid,
                # discovery shouldn't 500 on a sibling project).
                try:
                    loader = ModelConfigLoader(str(mf))
                except Exception:
                    continue
                for m in loader.models:
                    m.path = str(mf)
                    entries.append(
                        {
                            "name": m.name,
                            "models_file": str(mf),
                            "has_cdc": self._model_has_resolvable_cdc(m),
                        }
                    )

            payload = {"models": entries, "active": self.active_model}
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.ERROR,
                "hub.viewer_http.models_failed",
                error=str(exc),
            )
            return _http_response(
                connection,
                500,
                f"failed to enumerate models: {exc}".encode("utf-8"),
            )

        return _http_response(
            connection,
            200,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    @staticmethod
    def _model_has_resolvable_cdc(model_cfg: Any) -> bool:
        """``has_cdc`` reflects end-to-end resolvability: the model
        has a ``cdc:`` field AND the referenced file exists AND at
        least one analysis resolves cleanly. Errors get swallowed so
        the listing endpoint doesn't 500 on one broken pointer."""
        if not getattr(model_cfg, "cdc", None):
            return False
        from .cdc_builder import _resolve_cdc_analysis

        try:
            return _resolve_cdc_analysis(model_cfg) is not None
        except Exception:
            return False

    async def _handle_view_json_for_model(
        self, connection: ServerConnection, requested: str
    ) -> Response:
        """``GET /view.json?model=NAME`` — build (or reuse) the per-
        model view.json and serve it. Updates ``active_model`` on
        success and broadcasts ``view_changed``.
        """

        from . import model_discovery, view_builder
        from ..errors import FatalRtlBuddyError

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                b"hub started without project_root; ?model= requires it",
            )

        # Resolve to ModelConfig — honours ``--models-file`` pin if
        # present so the start-time guard remains meaningful.
        try:
            models_yaml, loader = model_discovery.resolve_model(
                self.project_root,
                requested,
                models_file=self.models_file_pin,
            )
        except FatalRtlBuddyError as exc:
            return _http_response(connection, 400, str(exc).encode("utf-8"))
        model_cfg = loader.get_model(requested)

        # Per-model lock. Two concurrent ?model=requested requests
        # serialise; one runs build_view_json, the other waits.
        lock = self._model_locks.setdefault(requested, asyncio.Lock())
        async with lock:
            try:
                cache_path = await asyncio.to_thread(
                    view_builder.build_view_json,
                    project_root=self.project_root,
                    model_cfg=model_cfg,
                )
            except FatalRtlBuddyError as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "hub.viewer_http.view_json_build_failed",
                    model=requested,
                    error=str(exc),
                )
                return _http_response(connection, 500, str(exc).encode("utf-8"))

        await self._set_active_model(
            model_name=requested, models_file=models_yaml, view_path=cache_path
        )

        return _http_response(
            connection,
            200,
            cache_path.read_bytes(),
            content_type="application/json",
        )

    async def _set_active_model(
        self, *, model_name: str, models_file: Path, view_path: Path
    ) -> None:
        """Promote ``model_name`` to the active model: flip in-memory
        state, update the discovery record, broadcast ``view_changed``.
        Idempotent — calling with the already-active model is a no-op
        beyond a redundant disk write.
        """
        from . import discovery
        from .protocol import Envelope, Kind, Origin, new_id

        self.active_model = model_name
        if self.hub_server is not None:
            self.hub_server.state.active_model = model_name
        # ``view_json_path`` now points at the per-model cache so
        # ``GET /view.json`` (no query) returns the same bytes a
        # ``?model=NAME`` request just received.
        self.view_json_path = view_path

        if self.project_root is not None:
            try:
                discovery.update_active_model(self.project_root, model_name)
            except Exception as exc:  # pragma: no cover - defensive
                log_event(
                    logger,
                    logging.WARNING,
                    "hub.viewer_http.discovery_update_failed",
                    error=str(exc),
                )

        if self.hub_server is not None:
            env = Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="view_changed",
                id=new_id(),
                payload={
                    "model": model_name,
                    "models_file": str(models_file),
                    "view_url": f"/view.json?model={model_name}",
                },
            )
            try:
                await self.hub_server.broadcast_event(env, suppress_origin=None)
            except Exception as exc:  # pragma: no cover - defensive
                log_event(
                    logger,
                    logging.WARNING,
                    "hub.viewer_http.broadcast_failed",
                    error=str(exc),
                )

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


def _is_pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` raises ProcessLookupError when the pid no
    longer exists and PermissionError when it exists but belongs to
    a different user. We only spawn marimo as the hub's own uid, so
    PermissionError shouldn't fire in practice; treat any signal
    failure as "dead" to avoid sticky stale entries.
    """
    import os
    import signal

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # signal.SIG_DFL is just here to keep linters happy about the
    # import being intentional even when only os.kill is used.
    del signal
    return True


def _terminate_pid(pid: int) -> None:
    """Best-effort SIGTERM. Used during hub shutdown to clean up the
    marimos we spawned for the SPA's "Open in marimo" flow.

    No SIGKILL escalation, no wait — the hub is shutting down and
    we don't want to block on a marimo process that's hung. The OS
    will reap the orphan if SIGTERM fails to land within the kernel
    grace period.
    """
    import os
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
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
