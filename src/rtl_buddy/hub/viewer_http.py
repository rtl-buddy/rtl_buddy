"""HTTP and WebSocket front end for the rtl-buddy-hub viewer.

Serves the hub landing page at ``/``, the rtl-buddy-view SPA bundle at ``/sch``, and the
hub's JSON-envelope channel as a WebSocket at ``/ws``. Each WebSocket connection is
proxied to a fresh TCP connection on the hub's main listener.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from ..logging_utils import log_event
from . import cov_page, graph_page, landing_page, phys_page, theme
from .event_broker import EventBroker


logger = logging.getLogger(__name__)


# Trailing ``hier.log`` lines carried in a ``view_generation_failed`` body. The SPA
# shows them verbatim, so this is a display budget: a longer tail scrolls the cause off
# the screen.
LOG_TAIL_LINES = 40

# Recovers the log path from the message ``build_view_json`` writes, so the path has one
# producer.
_LOG_PATH_RE = re.compile(r"see (?P<path>\S.*?\.log) for details")


def _one_line(message: str) -> str:
    """First non-empty line of ``message``, whitespace-normalised.

    Only the headline travels in the error body; multi-line detail stays in the log tail
    and the hub log.
    """

    for line in str(message).splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


def _read_log_tail(log_path: Path, limit: int = LOG_TAIL_LINES) -> list[str]:
    """Last ``limit`` lines of ``log_path``; ``[]`` when unreadable. Never raises."""

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-limit:] if limit > 0 else lines


PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>rtl-buddy-sch placeholder</title>
  <link rel="icon" type="image/png" sizes="32x32" href="/hub/assets/rtl-buddy-favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/hub/assets/rtl-buddy-favicon-16.png">
  <link rel="stylesheet" href="/hub/theme.css">
  <style>
    body { font-family: var(--font-sans, system-ui), sans-serif; max-width: 40rem;
           margin: 4rem auto; padding: 0 1rem; line-height: 1.5;
           background: var(--bg, #f8fafc); color: var(--fg, #1e293b); }
    code { font-family: var(--font-mono, monospace);
           background: var(--panel-2, #f1f5f9); padding: 0 .25rem;
           border-radius: var(--radius-1, 3px); }
    a { color: var(--accent, #2563eb); }
    h1 { font-size: 1.4rem; }
    .ok  { color: var(--ok, #16a34a); }
    .err { color: var(--err, #dc2626); }
  </style>
  <script>
    %HUB_INJECTION%
  </script>
</head>
<body>
  <h1>rtl-buddy-sch <small>(schematic placeholder)</small></h1>
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
  <p>
    The design knowledge graph pane at <a href="/gph"><code>/gph</code></a>
    and the coverage pane at <a href="/cov"><code>/cov</code></a> are
    served independently of the SPA — they need built artefacts, not a
    viewer bundle. Every app this hub serves is listed on the landing
    page at <a href="/"><code>/</code></a>.
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
    graph_url: str | None = None,
    cov_url: str | None = None,
    phys_url: str | None = None,
) -> bytes:
    """Return the SPA HTML with the hub address injected.

    Serves ``bundle_index`` when it is an existing file, otherwise the built-in
    placeholder. The script preamble replaces ``%HUB_INJECTION%``, or is inserted after
    ``<head>`` when the placeholder is absent.

    ``view_url``, ``graph_url``, ``cov_url`` and ``phys_url`` set
    ``window.__RTL_BUDDY_VIEW_URL__``, ``__RTL_BUDDY_GRAPH_URL__``,
    ``__RTL_BUDDY_COV_URL__`` and ``__RTL_BUDDY_PHY_URL__`` when given. The hub passes
    the graph, coverage and physical URLs only when the matching data exists, so the SPA
    shows a pane's entry on the presence of the global.
    """

    if bundle_index is not None and bundle_index.is_file():
        html = bundle_index.read_text(encoding="utf-8")
    else:
        html = PLACEHOLDER_HTML

    parts = [f"window.__RTL_BUDDY_HUB__ = {hub_addr!r};"]
    if view_url is not None:
        parts.append(f"window.__RTL_BUDDY_VIEW_URL__ = {view_url!r};")
    if graph_url is not None:
        parts.append(f"window.__RTL_BUDDY_GRAPH_URL__ = {graph_url!r};")
    if cov_url is not None:
        parts.append(f"window.__RTL_BUDDY_COV_URL__ = {cov_url!r};")
    if phys_url is not None:
        parts.append(f"window.__RTL_BUDDY_PHY_URL__ = {phys_url!r};")
    preamble = "\n".join(parts)

    if "%HUB_INJECTION%" in html:
        html = html.replace("%HUB_INJECTION%", preamble)
    else:
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
    """Serves the HTTP and ``/ws`` surface for the viewer SPA on one asyncio port.

    Plain HTTP requests are answered from the ``process_request`` hook; upgrade requests
    proceed to the WebSocket handlers.
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
        axi_perf_source: Path | None = None,
        hub_server: Any | None = None,
    ) -> None:
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.requested_http_port = http_port
        self.http_port = http_port
        self.viewer_bundle = viewer_bundle
        self.view_json_path = view_json_path
        # Model served by a bare ``GET /view.json``; changed by ``?model=`` requests.
        self.project_root = project_root
        self.active_model = initial_model
        self.models_file_pin = models_file_pin
        # Optional axi-perf.json baked into every generated view.json (``rb hub start
        # --axi-perf-from``).
        self.axi_perf_source = axi_perf_source
        self.hub_server = hub_server
        # Mirrored onto HubState for ``state_snapshot``; hub_server is None in tests.
        if hub_server is not None:
            hub_server.state.active_model = initial_model
        # Per-model locks: concurrent requests for one model share one
        # ``build_view_json`` call, different models run in parallel.
        self._model_locks: dict[str, asyncio.Lock] = {}
        # Per-model view-generation outcome for this session, ``{model: {"ok",
        # "message", "log_path", "log_tail"}}``. ``GET /models`` derives ``view_status``
        # from it and a bare ``GET /view.json`` replays a remembered failure. In memory
        # only.
        self._model_view_outcomes: dict[str, dict[str, Any]] = {}
        # Per-test locks for ``?test=``, same purpose as ``_model_locks``.
        self._test_locks: dict[str, asyncio.Lock] = {}
        # Active TB test; None while serving a DUT view.
        self.active_test: str | None = None
        # "Open in marimo" sessions, ``(test, suite_dir) -> LaunchResult``. A cached
        # entry is reused while its pid is alive; a per-key lock stops concurrent
        # requests from spawning duplicates.
        self._axi_notebook_sessions: dict[tuple[str, str], Any] = {}
        self._axi_notebook_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # SPA and notebook state sync; see ``event_broker.py``.
        self._event_broker = EventBroker()
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

    def _has_graph_json(self) -> bool:
        """Whether ``rb graph build`` has produced a graph for this root."""
        if self.project_root is None:
            return False
        return graph_page.graph_files_present(self.project_root)

    async def _has_cov_data(self) -> bool:
        """Whether any run under this root left a coverage manifest.

        The lookup is a tree walk, cached briefly by ``cov_page``
        (:data:`~rtl_buddy.hub.cov_page.PRESENCE_TTL_SECONDS`). Await it in a thread: a
        walk on the event loop stalls the whole hub, including the ``/ws`` fan-out.
        """
        if self.project_root is None:
            return False
        return await asyncio.to_thread(cov_page.cov_data_present, self.project_root)

    async def _has_phys_data(self) -> bool:
        """Whether any run under this root left a physical manifest.

        Same rules as :meth:`_has_cov_data`: the manifest can be in any run directory,
        so the walk is cached by ``phys_page`` and awaited in a thread.
        """
        if self.project_root is None:
            return False
        return await asyncio.to_thread(phys_page.phys_data_present, self.project_root)

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
        # Reap the marimo processes spawned for the notebook endpoint; otherwise they
        # outlive the hub as orphans.
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

    # HTTP

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        # Async because ``/view.json?model=`` builds in a thread under a per-model
        # ``asyncio.Lock``.
        raw_path, _, query_string = request.path.partition("?")
        path = raw_path
        query = parse_qs(query_string)

        if request.headers.get("Upgrade", "").lower() == "websocket":
            if path in ("/ws", "/api/events/sync"):
                return None
            return _http_response(connection, 404, b"unknown ws path")

        # Redirects to the slashless spelling: the SPA bundle uses relative asset URLs,
        # which resolve against the directory of the current URL and 404 from ``/sch/``.
        if redirect := self._canonical_route_redirect(connection, path, query_string):
            return redirect

        if path == landing_page.LANDING_PAGE_ROUTE:
            return _http_response(
                connection,
                200,
                landing_page.render_landing_html(hub_addr=self.hub_address),
                content_type="text/html; charset=utf-8",
            )

        if path == landing_page.STATE_JSON_ROUTE:
            return await self._handle_hub_state(connection)

        if path == theme.THEME_CSS_ROUTE:
            return _http_response(
                connection,
                200,
                theme.theme_css_bytes(),
                content_type="text/css; charset=utf-8",
            )

        if path.startswith(theme.ASSETS_ROUTE_PREFIX):
            return self._handle_asset(
                connection, path[len(theme.ASSETS_ROUTE_PREFIX) :]
            )

        # ``/index.html`` must be served with hub injection; the bundle's relative links
        # resolve to it, and ``_serve_static`` would omit the injection.
        if path in (landing_page.VIEW_PAGE_ROUTE, "/index.html"):
            cov_available = await self._has_cov_data()
            phys_available = await self._has_phys_data()
            body = render_index_html(
                bundle_index=self._bundle_index,
                hub_addr=self.hub_address,
                view_url="/view.json" if self._has_view_json() else None,
                graph_url=(
                    graph_page.GRAPH_JSON_ROUTE if self._has_graph_json() else None
                ),
                cov_url=(cov_page.COV_JSON_ROUTE if cov_available else None),
                phys_url=(phys_page.PHYS_JSON_ROUTE if phys_available else None),
            )
            return _http_response(
                connection, 200, body, content_type="text/html; charset=utf-8"
            )

        if path == "/healthz":
            return _http_response(connection, 200, b"ok\n", content_type="text/plain")

        if path == graph_page.GRAPH_PAGE_ROUTE:
            return await self._handle_graph_page(connection)

        if path == graph_page.GRAPH_JSON_ROUTE:
            return await self._handle_graph_json(connection)

        if path == cov_page.COV_PAGE_ROUTE:
            return self._handle_cov_page(connection)

        if path == cov_page.COV_JSON_ROUTE:
            return await self._handle_cov_json(connection)

        if path == cov_page.COV_SOURCE_ROUTE:
            return await self._handle_cov_source(connection, query)

        if path == phys_page.PHYS_PAGE_ROUTE:
            return self._handle_phys_page(connection)

        if path == phys_page.PHYS_JSON_ROUTE:
            return await self._handle_phys_json(connection, query)

        if path == "/models":
            return await self._handle_models(connection)

        if path == "/tests":
            return await self._handle_tests(connection)

        if path == "/api/axi-profile/notebook":
            return await self._handle_axi_notebook(connection, query)

        if path == "/view.json":
            requested_test = query.get("test", [None])[0]
            if requested_test is not None:
                # ``tests_file`` disambiguates a test name shared by several suites.
                requested_tests_file = query.get("tests_file", [None])[0]
                return await self._handle_view_json_for_test(
                    connection, requested_test, requested_tests_file
                )
            requested = query.get("model", [None])[0]
            if requested is not None:
                return await self._handle_view_json_for_model(connection, requested)
            return self._serve_active_view_json(connection)

        if self.viewer_bundle and self.viewer_bundle.is_dir():
            static = self._serve_static(connection, path)
            if static is not None:
                return static

        return _http_response(connection, 404, b"not found")

    def _canonical_route_redirect(
        self, connection: ServerConnection, path: str, query_string: str
    ) -> Response | None:
        """Redirect a non-canonical page path to its canonical spelling in one hop.

        Drops a trailing slash (``/gph/`` to ``/gph``) and renames legacy page paths
        (``/graph`` to ``/gph``, ``/view`` to ``/sch``). Returns None for everything
        else, including the landing page and all JSON, asset and static routes.

        The redirect is 307, not 301: hub ports are reused across projects, and a cached
        301 would outlive the hub that issued it.
        """

        target = path.rstrip("/")
        target = _LEGACY_PAGE_ROUTES.get(target, target)
        if target == path:
            return None
        if target not in _CANONICAL_PAGE_ROUTES:
            return None
        location = f"{target}?{query_string}" if query_string else target
        return _http_redirect(connection, location)

    # Landing page, tokens and brand marks

    async def _handle_hub_state(self, connection: ServerConnection) -> Response:
        """``GET /hub/state.json``: the landing page's state, recomputed per request.

        Async because coverage and physical presence are tree walks on a cache miss (see
        :meth:`_has_cov_data`).
        """

        peers = (
            [o.value for o in self.hub_server.registered_origins]
            if self.hub_server is not None
            else []
        )
        graph_present, graph_path, graph_mtime = landing_page.graph_state(
            self.project_root
        )
        payload = landing_page.build_state_payload(
            hub_addr=self.hub_address,
            server_version=(
                getattr(self.hub_server, "server_version", None)
                if self.hub_server is not None
                else None
            ),
            project_root=self.project_root,
            active_model=self.active_model,
            active_test=self.active_test,
            peers=peers,
            # Always live: without a bundle the route serves the self-explaining
            # placeholder.
            view_available=True,
            view_note=(
                None
                if self._bundle_index is not None
                else "no viewer bundle installed — serving the placeholder page"
            ),
            graph_present=graph_present,
            graph_path=graph_path,
            graph_mtime=graph_mtime,
            cov_available=await self._has_cov_data(),
            phys_available=await self._has_phys_data(),
        )
        return _http_response(
            connection,
            200,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    def _handle_asset(self, connection: ServerConnection, name: str) -> Response:
        """``GET /hub/assets/<name>``: a vendored brand mark.

        ``name`` is matched against the shipped listing, never joined onto a path.
        """

        body = theme.asset_bytes(name)
        if body is None:
            return _http_response(connection, 404, b"not found")
        return _http_response(
            connection, 200, body, content_type=_guess_content_type(Path(name))
        )

    # /graph and /graph.json

    async def _handle_graph_page(self, connection: ServerConnection) -> Response:
        """``GET /graph``: the design-knowledge-graph pane.

        Always 200; the page's empty state names ``rb graph build``. Data comes from
        ``/graph.json`` and, for the heat overlay, ``/phy.json``. The physical URL is
        injected under the same presence probe as the landing page, so the two cannot
        disagree.
        """

        return _http_response(
            connection,
            200,
            graph_page.render_graph_html(
                hub_addr=self.hub_address,
                phys_url=(
                    phys_page.PHYS_JSON_ROUTE if await self._has_phys_data() else None
                ),
            ),
            content_type="text/html; charset=utf-8",
        )

    async def _handle_graph_json(self, connection: ServerConnection) -> Response:
        """``GET /graph.json``: the merged graph plus overlay, read from disk per
        request.
        """

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                json.dumps(
                    {
                        "error": "hub started without project_root; /graph.json requires it"
                    }
                ).encode("utf-8"),
                content_type="application/json",
            )
        status, body = await asyncio.to_thread(
            graph_page.graph_payload_bytes, self.project_root
        )
        return _http_response(connection, status, body, content_type="application/json")

    # /cov, /cov.json and /cov/source

    def _handle_cov_page(self, connection: ServerConnection) -> Response:
        """``GET /cov``: the coverage pane. Always 200; the empty state names the
        command.
        """

        return _http_response(
            connection,
            200,
            cov_page.render_cov_html(hub_addr=self.hub_address),
            content_type="text/html; charset=utf-8",
        )

    async def _handle_cov_json(self, connection: ServerConnection) -> Response:
        """``GET /cov.json``: the newest run's coverage model, read from disk per
        request.
        """

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                json.dumps(
                    {"error": "hub started without project_root; /cov.json requires it"}
                ).encode("utf-8"),
                content_type="application/json",
            )
        status, body = await asyncio.to_thread(
            cov_page.cov_payload_bytes, self.project_root
        )
        return _http_response(connection, status, body, content_type="application/json")

    async def _handle_cov_source(
        self, connection: ServerConnection, query: dict[str, list[str]]
    ) -> Response:
        """``GET /cov/source?path=...``: one annotated file's text.

        Served separately from ``/cov.json`` because a model names hundreds of files.
        """

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                json.dumps(
                    {
                        "error": "hub started without project_root; "
                        "/cov/source requires it"
                    }
                ).encode("utf-8"),
                content_type="application/json",
            )
        requested = query.get("path", [""])[0]
        status, body = await asyncio.to_thread(
            cov_page.read_source_lines, self.project_root, requested
        )
        return _http_response(connection, status, body, content_type="application/json")

    # /phy and /phy.json

    def _handle_phys_page(self, connection: ServerConnection) -> Response:
        """``GET /phy``: the synth+power pane. Always 200; the empty state names the
        commands.
        """

        return _http_response(
            connection,
            200,
            phys_page.render_phys_html(hub_addr=self.hub_address),
            content_type="text/html; charset=utf-8",
        )

    async def _handle_phys_json(
        self, connection: ServerConnection, query: dict[str, list[str]]
    ) -> Response:
        """``GET /phy.json``: one run's physical model, read from disk on every request.

        ``?dir=<project-relative phys_dir>`` selects a run; bare ``/phy.json`` serves
        the newest manifest. The directory is validated by
        :func:`rtl_buddy.hub.phys_page.contained_phys_dir`: 403 outside the project
        root, 404 without a manifest.
        """

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                json.dumps(
                    {"error": "hub started without project_root; /phy.json requires it"}
                ).encode("utf-8"),
                content_type="application/json",
            )
        status, body = await asyncio.to_thread(
            functools.partial(
                phys_page.phys_payload_bytes,
                self.project_root,
                requested_dir=query.get(phys_page.PHYS_DIR_PARAM, [None])[0],
            )
        )
        return _http_response(connection, status, body, content_type="application/json")

    # Models and /view.json

    async def _handle_axi_notebook(
        self, connection: ServerConnection, query: dict[str, list[str]]
    ) -> Response:
        """``GET /api/axi-profile/notebook?test=NAME&suite_dir=PATH``: launch a marimo
        notebook.

        Spawns ``rb axi-profile notebook --headless`` for ``test`` (which must exist in
        ``<suite_dir>/tests.yaml``) and waits up to 30 s for marimo to print its URL.
        The marimo process outlives the request. A repeat request for the same ``(test,
        suite_dir)`` reuses the cached process while its pid is alive.

        Returns JSON ``{"url", "pid", "port", "test", "suite_dir", "reused"}``, where
        ``reused`` is true on a cache hit. Errors are 4xx/5xx JSON bodies with a single
        ``error`` key. Requires ``project_root`` on the hub.
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

        # Serialises requests for one notebook; two clicks inside marimo's startup
        # window would otherwise spawn duplicates.
        key = (test, suite_dir)
        lock = self._axi_notebook_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._axi_notebook_sessions.get(key)
            if cached is not None and _is_pid_alive(cached.pid):
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
            if cached is not None:
                self._axi_notebook_sessions.pop(key, None)
            try:
                result = await axi_notebook_launcher.launch(
                    test=test,
                    suite_dir=suite_dir,
                    project_root=self.project_root,
                    events_url=(
                        f"ws://127.0.0.1:{self.http_port}/api/events/sync"
                        if self.http_port
                        else None
                    ),
                )
            except axi_notebook_launcher.AxiNotebookLaunchError as e:
                return _http_response(
                    connection,
                    e.status,
                    _json.dumps({"error": str(e)}).encode(),
                    content_type="application/json",
                )
            # Cache under the request key, not the launcher's normalised suite_dir, so a
            # repeat request hits.
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
        """``GET /models``: every model the hub can serve, discovered per request.

        Enumerates only the ``--models-file`` when one was pinned.
        """

        from . import model_discovery
        from ..config.model import ModelConfigLoader

        if self.project_root is None:
            # No project_root (standalone test): report only the active model.
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
                # Skip malformed sibling files rather than failing the listing.
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
                            # Model health, so the picker can badge a model that cannot
                            # elaborate.
                            **self._view_status_fields(m.name),
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

    async def _handle_tests(self, connection: ServerConnection) -> Response:
        """``GET /tests``: every test the hub can serve, discovered per request.

        Each entry carries its resolved ``(model, tb)`` pair. An empty list means
        standalone, and the SPA hides its DUT/TB toggle.
        """

        from . import test_discovery

        if self.project_root is None:
            payload: dict[str, Any] = {"tests": [], "active": self.active_test}
            return _http_response(
                connection,
                200,
                json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )

        try:
            entries = test_discovery.list_tests(self.project_root)
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.ERROR,
                "hub.viewer_http.tests_failed",
                error=str(exc),
            )
            return _http_response(
                connection,
                500,
                f"failed to enumerate tests: {exc}".encode("utf-8"),
            )

        payload = {
            "tests": [
                {
                    "name": e.name,
                    "model": e.model,
                    "tb": e.tb,
                    "tests_file": str(e.tests_file),
                }
                for e in entries
            ],
            "active": self.active_test,
        }
        return _http_response(
            connection,
            200,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    @staticmethod
    def _model_has_resolvable_cdc(model_cfg: Any) -> bool:
        """Whether the model has a ``cdc:`` field that resolves to at least one
        analysis.

        Errors count as False.
        """
        if not getattr(model_cfg, "cdc", None):
            return False
        from .cdc_builder import _resolve_cdc_analysis

        try:
            return _resolve_cdc_analysis(model_cfg) is not None
        except Exception:
            return False

    # Structured view errors and model health

    @staticmethod
    def _error_response(
        connection: ServerConnection,
        status: int,
        kind: str,
        message: str,
        **extra: Any,
    ) -> Response:
        """Build a ``/view.json`` failure body, ``{"error": {"kind", "message", ...}}``,
        as JSON.

        The SPA branches on ``kind``, not on the status code or message.
        """

        payload = {"error": {"kind": kind, "message": message, **extra}}
        return _http_response(
            connection,
            status,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    def _view_generation_error(self, model: str, exc: Exception) -> dict[str, Any]:
        """Turn a failed generation into the remembered outcome record.

        The record is the ``error`` object of the 500 body (minus ``kind``) and the
        source of ``view_status`` in ``GET /models``.
        """

        message = _one_line(str(exc))
        match = _LOG_PATH_RE.search(str(exc))
        if match:
            log_path = Path(match.group("path"))
        elif self.project_root is not None:
            # Mirrors RtlBuddyView's artefact root for failures that never reached the
            # subprocess.
            log_path = self.project_root / "artefacts" / "hier" / model / "hier.log"
        else:
            log_path = Path("hier.log")
        return {
            "ok": False,
            "model": model,
            "message": message,
            "log_path": str(log_path),
            "log_tail": _read_log_tail(log_path),
        }

    def _record_view_failure(
        self, model: str, outcome: dict[str, Any]
    ) -> dict[str, Any]:
        self._model_view_outcomes[model] = outcome
        return outcome

    def _record_view_success(self, model: str) -> None:
        self._model_view_outcomes[model] = {"ok": True, "model": model}

    def _failed_view_response(
        self, connection: ServerConnection, outcome: dict[str, Any]
    ) -> Response:
        return self._error_response(
            connection,
            500,
            "view_generation_failed",
            outcome["message"],
            model=outcome["model"],
            log_path=outcome["log_path"],
            log_tail=outcome["log_tail"],
        )

    def _view_status_fields(self, model_name: str) -> dict[str, Any]:
        """``view_status`` (plus optional ``error`` and ``stale_cache``) for one ``GET
        /models`` entry.

        Precedence: a remembered failure from this session, then a remembered success,
        then the cache file. A failure with an existing cache file is reported as
        ``stale_cache``.
        """

        from . import view_builder

        outcome = self._model_view_outcomes.get(model_name)
        cached = (
            self.project_root is not None
            and view_builder.view_json_path(self.project_root, model_name).is_file()
        )
        if outcome is not None and not outcome["ok"]:
            fields: dict[str, Any] = {
                "view_status": "failed",
                "error": outcome["message"],
            }
            if cached:
                fields["stale_cache"] = True
            return fields
        if outcome is not None or cached:
            return {"view_status": "ok"}
        return {"view_status": "never_built"}

    def _no_active_model_response(self, connection: ServerConnection) -> Response:
        """Bare ``GET /view.json`` with no model selected: 409 ``no_active_model``."""

        log_event(
            logger,
            logging.INFO,
            "hub.viewer_http.view_json_no_active_model",
            active_model=self.active_model or "",
        )
        return self._error_response(
            connection,
            409,
            "no_active_model",
            "no model is active on this hub; select one from /models "
            "or start the hub with `rb hub start --model NAME`",
            models_url="/models",
        )

    def _serve_active_view_json(self, connection: ServerConnection) -> Response:
        """``GET /view.json`` with no query: serve the active model.

        Answers with the bytes, the remembered failure for the active model, or
        ``no_active_model``. Falls back to the start-time ``view.json`` when no model
        has been selected.
        """

        if self._has_view_json():
            assert self.view_json_path is not None
            return _http_response(
                connection,
                200,
                self.view_json_path.read_bytes(),
                content_type="application/json",
            )
        outcome = (
            self._model_view_outcomes.get(self.active_model)
            if self.active_model is not None
            else None
        )
        if outcome is not None and not outcome["ok"]:
            return self._failed_view_response(connection, outcome)
        return self._no_active_model_response(connection)

    async def _handle_view_json_for_model(
        self, connection: ServerConnection, requested: str
    ) -> Response:
        """``GET /view.json?model=NAME``: build or reuse the model's view.json and serve
        it.

        On success sets the active model and broadcasts ``view_changed``. Failures are
        structured JSON: 404 ``unknown_model`` for an unresolvable name, 500
        ``view_generation_failed`` with the ``hier.log`` path and tail.
        """

        from . import model_discovery, view_builder
        from ..errors import FatalRtlBuddyError, RtlBuddyError

        if self.project_root is None:
            return self._error_response(
                connection,
                400,
                "no_project_root",
                "hub started without project_root; ?model= requires it",
                model=requested,
            )

        # Honours the ``--models-file`` pin.
        try:
            models_yaml, loader = model_discovery.resolve_model(
                self.project_root,
                requested,
                models_file=self.models_file_pin,
            )
            model_cfg = loader.get_model(requested)
        except FatalRtlBuddyError as exc:
            # Absent, ambiguous and unloadable names are one state to the SPA; the
            # loader's headline says which.
            log_event(
                logger,
                logging.INFO,
                "hub.viewer_http.view_json_unknown_model",
                model=requested,
                error=str(exc),
            )
            return self._error_response(
                connection,
                404,
                "unknown_model",
                _one_line(str(exc)),
                model=requested,
            )

        lock = self._model_locks.setdefault(requested, asyncio.Lock())
        async with lock:
            try:
                cache_path = await asyncio.to_thread(
                    view_builder.build_view_json,
                    project_root=self.project_root,
                    model_cfg=model_cfg,
                    axi_perf_source=self.axi_perf_source,
                )
            except RtlBuddyError as exc:
                # Catches ``RtlBuddyError`` so a ``FilelistError`` gets the same
                # structured answer.
                outcome = self._record_view_failure(
                    requested, self._view_generation_error(requested, exc)
                )
                log_event(
                    logger,
                    logging.ERROR,
                    "hub.viewer_http.view_json_build_failed",
                    model=requested,
                    error=str(exc),
                    log_path=outcome["log_path"],
                )
                return self._failed_view_response(connection, outcome)

        self._record_view_success(requested)
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
        """Make ``model_name`` the active model and broadcast ``view_changed``.
        Idempotent.
        """
        from . import discovery
        from .protocol import Envelope, Kind, Origin, new_id

        self.active_model = model_name
        # A DUT view clears any TB-mode selection.
        self.active_test = None
        if self.hub_server is not None:
            self.hub_server.state.active_model = model_name
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
                    # Explicit ``view_mode``; legacy SPAs ignore unknown fields.
                    "view_mode": "dut",
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

    async def _handle_view_json_for_test(
        self,
        connection: ServerConnection,
        requested: str,
        requested_tests_file: str | None = None,
    ) -> Response:
        """``GET /view.json?test=NAME[&tests_file=PATH]``: build or reuse the TB-rooted
        view and serve it.

        On success sets the active test and model and broadcasts ``view_changed`` with
        ``view_mode='tb'``. ``tests_file`` pins the owning ``tests.yaml`` when several
        suites share the test name.
        """

        from . import test_discovery, view_builder
        from ..errors import FatalRtlBuddyError, RtlBuddyError

        if self.project_root is None:
            return _http_response(
                connection,
                400,
                b"hub started without project_root; ?test= requires it",
            )

        tests_file: Path | None = None
        if requested_tests_file:
            candidate = Path(requested_tests_file).resolve()
            root = self.project_root.resolve()
            # Client-supplied: confine to the hub's project_root.
            if not candidate.is_relative_to(root):
                return _http_response(
                    connection,
                    400,
                    b"tests_file must be inside the hub's project_root",
                )
            tests_file = candidate

        try:
            tests_yaml, test_cfg = test_discovery.resolve_test(
                self.project_root, requested, tests_file=tests_file
            )
        except FatalRtlBuddyError as exc:
            return _http_response(connection, 400, str(exc).encode("utf-8"))

        lock = self._test_locks.setdefault(requested, asyncio.Lock())
        async with lock:
            try:
                cache_path = await asyncio.to_thread(
                    view_builder.build_view_json,
                    project_root=self.project_root,
                    model_cfg=test_cfg.get_model(),
                    axi_perf_source=self.axi_perf_source,
                    test_cfg=test_cfg,
                    # TB filelist entries are relative to the suite dir, not the hub's
                    # cwd.
                    test_suite_dir=tests_yaml.parent,
                )
            except RtlBuddyError as exc:
                # Catches ``RtlBuddyError`` so a ``FilelistError`` becomes a clean 500
                # with its message.
                log_event(
                    logger,
                    logging.ERROR,
                    "hub.viewer_http.view_json_build_failed",
                    test=requested,
                    error=str(exc),
                )
                return _http_response(connection, 500, str(exc).encode("utf-8"))

        await self._set_active_test(
            test_name=requested,
            tests_file=tests_yaml,
            model_name=test_cfg.get_model().name,
            tb_name=test_cfg.tb.name,
            view_path=cache_path,
        )

        return _http_response(
            connection,
            200,
            cache_path.read_bytes(),
            content_type="application/json",
        )

    async def _set_active_test(
        self,
        *,
        test_name: str,
        tests_file: Path,
        model_name: str,
        tb_name: str,
        view_path: Path,
    ) -> None:
        """Make ``test_name`` the active TB view and broadcast ``view_changed`` with
        ``view_mode='tb'``.

        Also sets the active model, since the test pins both. Idempotent.
        """
        from .protocol import Envelope, Kind, Origin, new_id

        self.active_test = test_name
        self.active_model = model_name
        if self.hub_server is not None:
            self.hub_server.state.active_model = model_name
        self.view_json_path = view_path

        if self.hub_server is not None:
            env = Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="view_changed",
                id=new_id(),
                payload={
                    "model": model_name,
                    "test": test_name,
                    "tb": tb_name,
                    "tests_file": str(tests_file),
                    "view_url": f"/view.json?test={test_name}",
                    "view_mode": "tb",
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

    # WebSocket

    async def _handle_ws(self, ws: Any) -> None:
        """Dispatch by path: ``/ws`` proxies hub envelopes, ``/api/events/sync`` joins
        the broker.
        """
        raw_path = getattr(getattr(ws, "request", None), "path", "/ws")
        path, _, _ = raw_path.partition("?")
        if path == "/api/events/sync":
            await self._handle_event_sync_ws(ws)
            return
        await self._handle_ws_envelope_proxy(ws)

    async def _handle_event_sync_ws(self, ws: Any) -> None:
        """Bridge a WebSocket client to the in-memory ``EventBroker``.

        Every inbound message is broadcast to the other clients. Disconnect cancels the
        reader and writer tasks and removes the client.
        """
        client_id, client = self._event_broker.add_client(name="ws")

        async def reader() -> None:
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        try:
                            text = msg.decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                    else:
                        text = msg
                    self._event_broker.broadcast(client_id, text)
            except ConnectionClosed:
                pass

        async def writer() -> None:
            try:
                while True:
                    msg = await client.queue.get()
                    await ws.send(msg)
            except ConnectionClosed:
                pass

        tasks = [
            asyncio.create_task(reader(), name="event-sync-reader"),
            asyncio.create_task(writer(), name="event-sync-writer"),
        ]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            self._event_broker.remove_client(client_id)

    async def _handle_ws_envelope_proxy(self, ws: Any) -> None:
        """Proxy a WebSocket connection to the hub's TCP port.

        One WebSocket message is one hub envelope: inbound messages become
        line-delimited writes, and outbound output is split on newlines into one message
        per envelope.
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
    """Whether ``pid`` exists. Any signal failure counts as dead."""
    import os
    import signal

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # Keeps the signal import in use.
    del signal
    return True


def _terminate_pid(pid: int) -> None:
    """Best-effort SIGTERM at hub shutdown, with no escalation or wait."""
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
    """Build an HTTP response from text or binary bytes."""

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


def _http_redirect(
    connection: ServerConnection, location: str, *, status: int = 307
) -> Response:
    """Build a redirect to ``location`` with an empty body."""

    headers = Headers()
    headers["Location"] = location
    headers["Content-Length"] = "0"
    headers["Cache-Control"] = "no-store"
    return Response(
        status_code=status,
        reason_phrase=_REASON_PHRASES.get(status, ""),
        headers=headers,
        body=b"",
    )


_REASON_PHRASES = {
    200: "OK",
    307: "Temporary Redirect",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
}


# App page routes in canonical spelling. HTML routes only; JSON and asset routes are
# fetched by exact path.
_CANONICAL_PAGE_ROUTES = frozenset(
    {
        landing_page.VIEW_PAGE_ROUTE,
        graph_page.GRAPH_PAGE_ROUTE,
        cov_page.COV_PAGE_ROUTE,
        phys_page.PHYS_PAGE_ROUTE,
    }
)

# Legacy page paths and their canonical replacements. Page routes only; ``/view.json``,
# ``/graph.json``, ``/cov.json``, ``/phy.json`` and the ``view`` protocol origin are
# unchanged.
_LEGACY_PAGE_ROUTES = {
    landing_page.LEGACY_VIEW_PAGE_ROUTE: landing_page.VIEW_PAGE_ROUTE,
    graph_page.LEGACY_GRAPH_PAGE_ROUTE: graph_page.GRAPH_PAGE_ROUTE,
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
    "LOG_TAIL_LINES",
    "PLACEHOLDER_HTML",
    "ViewerServer",
    "render_index_html",
]
