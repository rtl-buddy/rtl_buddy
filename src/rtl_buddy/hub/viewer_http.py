"""HTTP + WebSocket front-end for the rtl-buddy-hub viewer.

Browsers can't speak the hub's raw TCP transport, so this module
embeds an HTTP server alongside :mod:`rtl_buddy.hub.server` that:

* serves the hub landing page at ``/`` and the rtl-buddy-view SPA
  static bundle at ``/view`` (rtl-buddy/rtl_buddy#398 — ``/`` was the
  SPA until the hub grew a second app worth advertising),
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
from . import cov_page, graph_page, landing_page, theme
from .event_broker import EventBroker


logger = logging.getLogger(__name__)


# How many trailing lines of ``hier.log`` a ``view_generation_failed``
# body carries. The SPA renders the tail verbatim in its "no view
# available" placeholder (rtl-buddy-view#130), so this is a display
# budget, not a diagnostic one: the whole point is that the recurring
# causes (a top that never elaborated, an uninitialised vendor
# submodule, a ``-v`` library entry) name themselves in the last few
# renderer lines, and a longer tail scrolls the fix off the screen.
LOG_TAIL_LINES = 40

# ``build_view_json`` reports the log it wrote as "see <path> for
# details."  Reading the path back out of the message keeps one
# producer of it — the DUT and TB artefact dirs differ, and a second
# derivation here would be a second thing to keep in step.
_LOG_PATH_RE = re.compile(r"see (?P<path>\S.*?\.log) for details")


def _one_line(message: str) -> str:
    """First non-empty line of ``message``, whitespace-normalised.

    The structured error's ``message`` is a *summary* — the SPA puts it
    on one line above the log tail. Multi-line diagnostics (model
    discovery lists every candidate name) keep their detail in the
    log tail and in the hub log; only the headline travels here.
    """

    for line in str(message).splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


def _read_log_tail(log_path: Path, limit: int = LOG_TAIL_LINES) -> list[str]:
    """Last ``limit`` lines of ``log_path``; ``[]`` when unreadable.

    Never raises: a missing or unreadable log is a *thinner* error
    payload, not a second failure on the failure path.
    """

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
    The design knowledge graph pane at <a href="/graph"><code>/graph</code></a>
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
) -> bytes:
    """Return the HTML body served at ``/view`` with hub address injected.

    When ``bundle_index`` points at an existing file, its contents are
    served with the ``%HUB_INJECTION%`` placeholder (or a ``<head>``
    insertion when the placeholder is absent) replaced by the script
    preamble. Otherwise the built-in placeholder is served — same
    injection rules.

    When ``view_url`` is provided, ``window.__RTL_BUDDY_VIEW_URL__`` is
    set alongside ``__RTL_BUDDY_HUB__`` so the SPA bootstrap can fetch
    the view.json without the user passing ``?view=`` in the URL.

    ``graph_url`` does the same for the design knowledge graph (#382):
    it is set only when this hub has a built ``graph.json`` to serve, so
    an SPA overlay can advertise the graph pane on presence of the
    global instead of probing the endpoint and handling a 404.
    ``cov_url`` is the identical arrangement for the coverage pane
    (rtl-buddy/rtl_buddy#400), keyed on a discovered coverage manifest.
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
        axi_perf_source: Path | None = None,
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
        # Optional axi-perf.json the hub bakes into every model's
        # generated view.json (Phase 2.5 of the marimo umbrella).
        # ``rb hub start --axi-perf-from PATH`` populates this; the
        # path is forwarded to rtl-buddy-view via the
        # ``--overlay axi-perf=…`` form so the SPA's "Open in
        # marimo" button gets the test/suite_dir metadata for free.
        self.axi_perf_source = axi_perf_source
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
        # Per-model view-generation outcome for THIS hub session
        # (rtl-buddy-view#130). ``{model: {"ok": bool, "message": str,
        # "log_path": str, "log_tail": [str]}}``. Two readers: ``GET
        # /models`` turns it into ``view_status``, and the bare ``GET
        # /view.json`` replays a remembered failure so the named-model
        # and active-model request paths answer with the same body.
        # In memory only — a hub restart resets every model to what
        # the cache on disk says, which is the honest answer after a
        # restart anyway.
        self._model_view_outcomes: dict[str, dict[str, Any]] = {}
        # Per-test lock map for ``?test=NAME`` (TB view, #99 / 6b).
        # Same race-prevention as ``_model_locks`` — two SPA clicks on
        # the same test funnel through one build, two clicks on
        # different tests run in parallel.
        self._test_locks: dict[str, asyncio.Lock] = {}
        # Currently-active TB test (TB-view mode). None when the hub
        # is serving a DUT view (default) or hasn't built any view
        # yet. Flipped by ``?test=`` requests via
        # ``_set_active_test``.
        self.active_test: str | None = None
        # Marimo "Open in marimo" session cache (Phase 2.5).
        # ``(test, suite_dir) → LaunchResult``. Repeat clicks reuse
        # the cached entry when the spawned marimo is still alive
        # (``os.kill(pid, 0)`` succeeds). Per-key lock funnels
        # concurrent requests for the same notebook through one
        # spawn — analogous to ``_model_locks`` for /view.json?model=.
        self._axi_notebook_sessions: dict[tuple[str, str], Any] = {}
        self._axi_notebook_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Phase 3 SPA↔notebook sync. Opaque pub/sub — see
        # ``event_broker.py`` for the relay semantics.
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
        """Whether ``rb graph build`` has produced a graph for this root.

        Re-checked per request (one ``stat``) so a graph built while the
        hub is running is advertised without a restart — the same
        per-request-walk rule ``/models`` and ``/tests`` follow.
        """
        if self.project_root is None:
            return False
        return graph_page.graph_files_present(self.project_root)

    async def _has_cov_data(self) -> bool:
        """Whether any run under this root left a coverage manifest.

        Same advertise-on-data-presence rule as ``_has_graph_json``, but
        the lookup is a bounded walk rather than one ``stat`` (coverage
        artefacts land wherever the command ran), so ``cov_page`` caches
        the answer for a few seconds — see
        :data:`~rtl_buddy.hub.cov_page.PRESENCE_TTL_SECONDS`.

        Awaited in a thread, like every other ``/cov*`` handler here: the
        TTL bounds how *often* a miss happens, not what one costs, and a
        walk run on the event loop stalls the whole hub — including the
        ``/ws`` fan-out — for its duration. On a tree with an
        ``artefacts/`` directory from a finished regression that is not
        a bounded pause.
        """
        if self.project_root is None:
            return False
        return await asyncio.to_thread(cov_page.cov_data_present, self.project_root)

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
            if path in ("/ws", "/api/events/sync"):
                return None
            return _http_response(connection, 404, b"unknown ws path")

        # Plain HTTP.

        # ``/view/`` → ``/view`` and friends. The slashless spelling is
        # canonical: the SPA bundle is built with Vite ``base: ''`` so
        # every asset reference in its ``index.html`` is relative
        # (``./assets/index-*.js``), which the browser resolves against
        # the *directory* of the current URL. From ``/view`` that is
        # ``/assets/…`` and hits ``_serve_static``; from ``/view/`` it
        # becomes ``/view/assets/…`` and 404s, leaving a shell that
        # renders its chrome and then hangs on "Loading…". Redirecting
        # rather than also mounting the assets one level deeper keeps
        # one URL per asset, and rtl-buddy-view keeps the relative base
        # it needs for ``embed.py``'s standalone ``file://`` HTML.
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

        # ``/index.html`` stays an alias for the SPA: it is what the
        # bundle's own relative links resolve to, and letting it fall
        # through to ``_serve_static`` would serve the bundle's index
        # WITHOUT the hub injection — an SPA that cannot find its hub.
        if path in (landing_page.VIEW_PAGE_ROUTE, "/index.html"):
            cov_available = await self._has_cov_data()
            body = render_index_html(
                bundle_index=self._bundle_index,
                hub_addr=self.hub_address,
                view_url="/view.json" if self._has_view_json() else None,
                graph_url=(
                    graph_page.GRAPH_JSON_ROUTE if self._has_graph_json() else None
                ),
                cov_url=(cov_page.COV_JSON_ROUTE if cov_available else None),
            )
            return _http_response(
                connection, 200, body, content_type="text/html; charset=utf-8"
            )

        if path == "/healthz":
            return _http_response(connection, 200, b"ok\n", content_type="text/plain")

        if path == graph_page.GRAPH_PAGE_ROUTE:
            return self._handle_graph_page(connection)

        if path == graph_page.GRAPH_JSON_ROUTE:
            return await self._handle_graph_json(connection)

        if path == cov_page.COV_PAGE_ROUTE:
            return self._handle_cov_page(connection)

        if path == cov_page.COV_JSON_ROUTE:
            return await self._handle_cov_json(connection)

        if path == cov_page.COV_SOURCE_ROUTE:
            return await self._handle_cov_source(connection, query)

        if path == "/models":
            return await self._handle_models(connection)

        if path == "/tests":
            return await self._handle_tests(connection)

        if path == "/api/axi-profile/notebook":
            return await self._handle_axi_notebook(connection, query)

        if path == "/view.json":
            requested_test = query.get("test", [None])[0]
            if requested_test is not None:
                # ``tests_file`` disambiguates when a test name is shared
                # by multiple suites (e.g. ``smoke`` in several
                # tests.yaml). The SPA echoes back the ``tests_file`` it
                # got from ``GET /tests`` so the name resolves to exactly
                # one suite instead of erroring as ambiguous.
                requested_tests_file = query.get("tests_file", [None])[0]
                return await self._handle_view_json_for_test(
                    connection, requested_test, requested_tests_file
                )
            requested = query.get("model", [None])[0]
            if requested is not None:
                return await self._handle_view_json_for_model(connection, requested)
            # No ``?model=`` query → serve the active model.
            return self._serve_active_view_json(connection)

        # Bundle static assets: only served when the bundle is a directory.
        if self.viewer_bundle and self.viewer_bundle.is_dir():
            static = self._serve_static(connection, path)
            if static is not None:
                return static

        return _http_response(connection, 404, b"not found")

    def _canonical_route_redirect(
        self, connection: ServerConnection, path: str, query_string: str
    ) -> Response | None:
        """Redirect ``<page>/`` to ``<page>`` for the three app routes.

        Returns ``None`` for every other path, so this is a no-op for
        the landing page (``/`` *is* canonical), for the JSON and asset
        routes (nothing links to them with a trailing slash), and for
        bundle statics (a directory request there has always been a
        404).

        The redirect is **temporary** (307) rather than permanent on
        purpose: hub http ports are pinned and reused across projects,
        and a 301 cached against ``127.0.0.1:<port>`` would outlive the
        hub that issued it.
        """

        if len(path) < 2 or not path.endswith("/"):
            return None
        target = path.rstrip("/")
        if target not in _CANONICAL_PAGE_ROUTES:
            return None
        location = f"{target}?{query_string}" if query_string else target
        return _http_redirect(connection, location)

    # ------------------------------------------------------------------
    # / + /hub/* — landing, tokens, brand marks (issue #398)
    # ------------------------------------------------------------------

    async def _handle_hub_state(self, connection: ServerConnection) -> Response:
        """``GET /hub/state.json`` — what the landing page renders.

        Recomputed per request (two ``stat`` calls and a set read) for
        the same reason ``/models`` walks per request: a graph built, or
        a tab opened, while the landing is up must show up on its next
        poll rather than on a hub restart.

        Async because coverage presence is a tree walk on a cache miss
        (see :meth:`_has_cov_data`), and this is the route the landing
        page polls several times a minute — running that walk inline
        would stall every other connection with it.
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
            # The SPA route always answers — without a bundle it serves
            # the placeholder, which explains itself — so the card is
            # live either way and the note carries the caveat.
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
        )
        return _http_response(
            connection,
            200,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    def _handle_asset(self, connection: ServerConnection, name: str) -> Response:
        """``GET /hub/assets/<name>`` — the vendored brand marks.

        ``name`` is matched against the shipped listing rather than
        joined onto a path, so no traversal is possible here (unlike
        ``_serve_static``, which has to resolve arbitrary bundle paths
        and therefore carries its own containment check).
        """

        body = theme.asset_bytes(name)
        if body is None:
            return _http_response(connection, 404, b"not found")
        return _http_response(
            connection, 200, body, content_type=_guess_content_type(Path(name))
        )

    # ------------------------------------------------------------------
    # /graph + /graph.json (issue #382)
    # ------------------------------------------------------------------

    def _handle_graph_page(self, connection: ServerConnection) -> Response:
        """``GET /graph`` — the interactive design-knowledge-graph pane.

        Always 200, even with no graph built: the page's own empty state
        names ``rb graph build``, which is more useful than a 404 body
        the browser renders as a blank tab. The page is static; all the
        data arrives from ``GET /graph.json``.
        """

        return _http_response(
            connection,
            200,
            graph_page.render_graph_html(hub_addr=self.hub_address),
            content_type="text/html; charset=utf-8",
        )

    async def _handle_graph_json(self, connection: ServerConnection) -> Response:
        """``GET /graph.json`` — the merged graph joined with the overlay.

        Read off disk on every request rather than cached: the point of
        the ``reload`` button is that ``rb graph build`` / ``rb graph
        results`` in another terminal shows up here, and a cache keyed
        on anything less than the file's own bytes would have to be
        invalidated by exactly the events we cannot see.
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

    # ------------------------------------------------------------------
    # /cov + /cov.json + /cov/source (issue #400)
    # ------------------------------------------------------------------

    def _handle_cov_page(self, connection: ServerConnection) -> Response:
        """``GET /cov`` — the interactive coverage pane.

        Always 200, even with no coverage collected: the page's own
        empty state names the command that produces some, which is more
        useful than a 404 body the browser renders as a blank tab.
        """

        return _http_response(
            connection,
            200,
            cov_page.render_cov_html(hub_addr=self.hub_address),
            content_type="text/html; charset=utf-8",
        )

    async def _handle_cov_json(self, connection: ServerConnection) -> Response:
        """``GET /cov.json`` — the newest run's coverage model.

        Read off disk on every request, like ``/graph.json``: the point
        of the reload button is that a regression finishing in another
        terminal shows up here.
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
        """``GET /cov/source?path=…`` — one annotated file's text.

        Not folded into ``/cov.json``: a model on a real design names
        hundreds of files, and inlining every one of them would send tens
        of megabytes to render one.
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
                            # Model health (rtl-buddy-view#130) so the
                            # picker can badge a model that can never
                            # elaborate, instead of letting the user
                            # discover it via an empty canvas.
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
        """``GET /tests`` — list every test the hub can serve (#99 / 6b).

        Walks per-request so a freshly-edited ``tests.yaml`` shows up
        without restarting the hub. Each entry carries its resolved
        ``(model, tb)`` pair so the SPA's TB-mode picker can label
        options and skip an extra round-trip per click.

        Empty list is the standalone / no-tests signal — the SPA's
        DUT/TB toggle stays hidden in that case (matches the way
        ``GET /models`` returns ``[]`` for standalone deployments).
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

    # ------------------------------------------------------------------
    # Structured view errors + model health (rtl-buddy-view#130)
    # ------------------------------------------------------------------

    @staticmethod
    def _error_response(
        connection: ServerConnection,
        status: int,
        kind: str,
        message: str,
        **extra: Any,
    ) -> Response:
        """Every ``/view.json`` failure body, in one shape.

        ``{"error": {"kind": ..., "message": ..., <extra>}}`` with
        ``Content-Type: application/json``. The SPA branches on
        ``kind`` — never on the status code and never on the prose —
        so a new failure mode is a new ``kind`` and nothing else moves.
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

        The record is both the ``500`` body's ``error`` object (minus
        ``kind``) and what ``GET /models`` reads for ``view_status``,
        so the two endpoints cannot disagree about why a model is
        broken.
        """

        message = _one_line(str(exc))
        match = _LOG_PATH_RE.search(str(exc))
        if match:
            log_path = Path(match.group("path"))
        elif self.project_root is not None:
            # Mirrors ``RtlBuddyView``'s artefact root for the failure
            # modes that never reached the subprocess (a bad filelist,
            # an unresolvable cdc: back-pointer) and so never named a log.
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
        """``view_status`` (+ optional ``error``/``stale_cache``) for one
        ``GET /models`` entry.

        Three inputs, in precedence order: a remembered failure from
        this session wins over everything (a model that just failed is
        broken even though ``.rtl-buddy/cache/view-<m>.json`` may still
        hold last week's tree — that combination is exactly what
        ``stale_cache`` names); then a remembered success; then the
        cache file, which is what carries ``ok`` across a hub restart.
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
        """Bare ``GET /view.json`` with nothing to serve.

        ``409`` rather than ``404``: the route exists and the hub is
        healthy, it just has no model selected — which is a state the
        caller fixes (pick one from ``models_url``), not a wrong URL.
        """

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
        """``GET /view.json`` with no query — serve the active model.

        Same three answers the ``?model=`` path gives, for the same
        reasons: the bytes, the remembered failure for whatever model
        is active, or ``no_active_model``. Falls back to the start-time
        ``view.json`` (legacy path for pre-feature SPAs / embed.py
        users) when no model has been selected yet.
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
        """``GET /view.json?model=NAME`` — build (or reuse) the per-
        model view.json and serve it. Updates ``active_model`` on
        success and broadcasts ``view_changed``.

        Failures are structured JSON (rtl-buddy-view#130), never a
        plain-text body: an unresolvable name is ``404 unknown_model``,
        a renderer that refused to elaborate is ``500
        view_generation_failed`` carrying the ``hier.log`` path and its
        tail, because "which model, and what did the renderer say" is
        the whole content of the SPA's failure placeholder.
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

        # Resolve to ModelConfig — honours ``--models-file`` pin if
        # present so the start-time guard remains meaningful.
        try:
            models_yaml, loader = model_discovery.resolve_model(
                self.project_root,
                requested,
                models_file=self.models_file_pin,
            )
            model_cfg = loader.get_model(requested)
        except FatalRtlBuddyError as exc:
            # Every way a name fails to resolve to exactly one model
            # lands here — absent, ambiguous across models.yaml files,
            # or in a file that won't load. They are one state to the
            # SPA ("this name will not give you a view"); the loader's
            # own headline says which.
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

        # Per-model lock. Two concurrent ?model=requested requests
        # serialise; one runs build_view_json, the other waits.
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
                # ``RtlBuddyError`` rather than ``FatalRtlBuddyError``
                # so a ``FilelistError`` from the model filelist gets
                # the same structured answer instead of escaping into
                # the websockets layer's opaque fallback body (same
                # reason the ``?test=`` path widened its catch).
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
        """Promote ``model_name`` to the active model: flip in-memory
        state, update the discovery record, broadcast ``view_changed``.
        Idempotent — calling with the already-active model is a no-op
        beyond a redundant disk write.
        """
        from . import discovery
        from .protocol import Envelope, Kind, Origin, new_id

        self.active_model = model_name
        # Switching to a DUT view clears any TB-mode selection so the
        # next ``GET /view.json`` (no query) returns the DUT bytes and
        # the SPA's segmented control reflects the actual mode.
        self.active_test = None
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
                    # v1.1 protocol field (#99 / 6b): explicit
                    # ``view_mode`` so SPA clients route the event
                    # through the right action without inferring mode
                    # from the URL. Legacy SPAs ignore unknown fields.
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
        """``GET /view.json?test=NAME[&tests_file=PATH]`` — build (or
        reuse) the TB-rooted view for the named test (#99 / 6b) and serve
        it. Updates ``active_test`` + ``active_model`` on success and
        broadcasts ``view_changed`` with ``view_mode='tb'``.

        ``tests_file`` (optional) pins the owning ``tests.yaml`` so a test
        name shared across suites resolves unambiguously instead of
        erroring with "matches multiple tests.yaml files".
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
            # Confine to the hub's project_root — the param is
            # client-supplied, so never let it read a tests.yaml outside
            # the served tree.
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

        # Per-test lock funnels concurrent ?test=NAME requests through
        # one build_view_json call (same shape as ``_model_locks``).
        lock = self._test_locks.setdefault(requested, asyncio.Lock())
        async with lock:
            try:
                cache_path = await asyncio.to_thread(
                    view_builder.build_view_json,
                    project_root=self.project_root,
                    model_cfg=test_cfg.get_model(),
                    axi_perf_source=self.axi_perf_source,
                    test_cfg=test_cfg,
                    # The TB filelist entries are relative to the suite
                    # dir (where ``tests.yaml`` lives), not the hub's
                    # process cwd — anchor the merge there.
                    test_suite_dir=tests_yaml.parent,
                )
            except RtlBuddyError as exc:
                # ``RtlBuddyError`` (not just ``FatalRtlBuddyError``) so a
                # ``FilelistError`` from the TB filelist merge surfaces as
                # a clean 500 with the message rather than escaping to the
                # websockets layer's opaque "Failed to open a WebSocket
                # connection" fallback body.
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
        """Promote ``test_name`` to the active TB view: flip in-memory
        state and broadcast ``view_changed`` with ``view_mode='tb'``.

        The active model is also updated (the test pins both) so the
        DUT picker reflects what's resolved under the hood.
        Idempotent.
        """
        from .protocol import Envelope, Kind, Origin, new_id

        self.active_test = test_name
        self.active_model = model_name
        if self.hub_server is not None:
            self.hub_server.state.active_model = model_name
        # ``view_json_path`` now points at the per-(model, tb) cache.
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

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _handle_ws(self, ws: Any) -> None:
        """Dispatch the WS handler by path.

        ``/ws`` proxies hub envelopes (legacy). ``/api/events/sync``
        joins the in-memory pub/sub broker for SPA↔notebook state
        sync (Phase 3).
        """
        raw_path = getattr(getattr(ws, "request", None), "path", "/ws")
        path, _, _ = raw_path.partition("?")
        if path == "/api/events/sync":
            await self._handle_event_sync_ws(ws)
            return
        await self._handle_ws_envelope_proxy(ws)

    async def _handle_event_sync_ws(self, ws: Any) -> None:
        """Bridge a WS client to the in-memory ``EventBroker``.

        Every inbound message is broadcast to every other client.
        The client's own outbound queue is drained by the writer
        task. Disconnect cancels both tasks and removes the client
        from the broker.
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


# The app pages whose ``<page>/`` spelling redirects to ``<page>``. Only
# HTML routes belong here — the JSON and asset routes are fetched by
# code that spells them exactly.
_CANONICAL_PAGE_ROUTES = frozenset(
    {
        landing_page.VIEW_PAGE_ROUTE,
        graph_page.GRAPH_PAGE_ROUTE,
        cov_page.COV_PAGE_ROUTE,
    }
)


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
