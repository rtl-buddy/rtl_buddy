"""asyncio TCP server for rtl-buddy-hub.

Transport: line-delimited JSON over TCP, one envelope per line, UTF-8
(§2 of the protocol spec). The browser-facing WebSocket layer lives in
the viewer-integration PR and reuses this server's dispatch surface.

Responsibilities of this module:

* Accept client connections, run the ``hello``/``welcome`` handshake,
  and maintain a registry keyed by :class:`Origin`. The spec allows at
  most one client per origin in v1 — a second ``hello`` for an already
  registered origin is refused with ``not_connected``.
* Dispatch incoming envelopes:
    - **state events** (selection_changed, signal_selected, …) are
      broadcast to every connected client *except* the one whose
      origin matches the event's ``origin`` (§6 rule 1).
    - **requests** are routed to the client whose origin owns the
      target coordinate system; if no such client is registered, the
      hub replies with ``error{code: "not_connected"}``.
    - **responses / errors** are routed back to the original requester
      by ``id``.
* Maintain a bounded LRU of recently seen request IDs so duplicate
  requests (§6 rule 2) are silently dropped.

The resolver (view ↔ wave ↔ src) is a PR 3 follow-up; this module
returns ``error{code: "unresolvable"}`` for ``resolve_*`` requests so
the wire surface is real even before the resolver lands.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..logging_utils import log_event
from .protocol import (
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    make_diagnostics_set,
    make_error,
    make_welcome,
    new_id,
)
from .resolver import Resolver
from .state import (
    CursorTime,
    DiagnosticsBundle,
    HubState,
    Selection,
    SignalSelection,
    WaveScope,
)


logger = logging.getLogger(__name__)


STATE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "selection_changed",
        "signal_selected",
        "cursor_time_changed",
        "scope_changed",
        "source_focused",
        "diagnostics_set",
    }
)
"""Event ``type`` strings that broadcast to all clients except origin.

``bye`` is technically also broadcast, but the connection close handler
emits it explicitly; it's not a payload-bearing event the application
emits, so it's tracked separately."""


REQUEST_ROUTING: dict[str, Origin] = {
    "wave_add_variables": Origin.WAVE,
    "wave_set_scope": Origin.WAVE,
    "wave_set_cursor": Origin.WAVE,
    "open_source": Origin.SRC,
    "view_pan_to": Origin.VIEW,
}
"""Request ``type`` → ``origin`` of the client that handles it.

``resolve_*`` requests are hub-handled and not in this table; ``hello``
is intercepted upfront by the handshake stage."""


HUB_HANDLED_REQUESTS: frozenset[str] = frozenset(
    {
        "resolve_view_to_wave",
        "resolve_wave_to_view",
        "resolve_signal_to_view",
        "state_snapshot",
    }
)


DEFAULT_DEDUPE_CAPACITY = 1024
DEFAULT_DEDUPE_TTL_SECONDS = 60.0


class HubServerError(Exception):
    """Server-side error not routed back over the wire."""


@dataclass
class ClientConnection:
    """One TCP-connected client."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str
    origin: Origin | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    client_version: str = ""

    async def send(self, env: Envelope) -> None:
        """Serialise + write one envelope; flush via ``drain``."""

        line = encode(env).encode("utf-8") + b"\n"
        self.writer.write(line)
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


@dataclass
class _PendingRequest:
    """In-flight request awaiting a response or error."""

    requester_origin: Origin
    seen_at: float


class _LruIdSet:
    """Bounded LRU of request IDs with a TTL.

    Used both as a dedupe filter for incoming requests (§6 rule 2) and
    as the table of currently-pending request IDs for response routing.
    Two different concerns mapped onto the same data structure is
    intentional: dedupe and routing both want "have we recently seen
    this id, and what was it for?"
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_DEDUPE_CAPACITY,
        ttl_seconds: float = DEFAULT_DEDUPE_TTL_SECONDS,
    ) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, _PendingRequest] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        self._evict_expired()
        return key in self._store

    def get(self, key: str) -> _PendingRequest | None:
        self._evict_expired()
        return self._store.get(key)

    def put(self, key: str, requester_origin: Origin) -> None:
        self._store[key] = _PendingRequest(
            requester_origin=requester_origin, seen_at=time.monotonic()
        )
        self._store.move_to_end(key)
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    def pop(self, key: str) -> _PendingRequest | None:
        return self._store.pop(key, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        cutoff = now - self._ttl
        while self._store:
            _, value = next(iter(self._store.items()))
            if value.seen_at >= cutoff:
                return
            self._store.popitem(last=False)


class HubServer:
    """The hub's asyncio TCP server.

    Lifecycle:

    1. :meth:`start` binds the listening socket on the configured
       interface and returns the resolved ``(host, port)`` pair.
    2. The caller then writes the discovery record and arranges for
       :meth:`serve_forever` to be awaited.
    3. :meth:`shutdown` triggers a clean tear-down: broadcasts ``bye``,
       closes connections, stops accepting new ones.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        server_version: str = "0.0.0",
        dedupe_capacity: int = DEFAULT_DEDUPE_CAPACITY,
        dedupe_ttl_seconds: float = DEFAULT_DEDUPE_TTL_SECONDS,
        resolver: Resolver | None = None,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.server_version = server_version
        self.resolver = resolver
        self._registry: dict[Origin, ClientConnection] = {}
        self._pending = _LruIdSet(
            capacity=dedupe_capacity, ttl_seconds=dedupe_ttl_seconds
        )
        self._dedupe = _LruIdSet(
            capacity=dedupe_capacity, ttl_seconds=dedupe_ttl_seconds
        )
        self.state = HubState()
        self._asyncio_server: asyncio.base_events.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> tuple[str, int]:
        self._asyncio_server = await asyncio.start_server(
            self._handle_client, self.host, self.requested_port
        )
        sockets = self._asyncio_server.sockets or ()
        if not sockets:
            raise HubServerError("hub server bound 0 sockets")
        host, port = sockets[0].getsockname()[:2]
        self.host = host
        self.port = port
        log_event(
            logger,
            logging.INFO,
            "hub.server.listening",
            host=host,
            port=port,
            requested_port=self.requested_port,
        )
        return host, port

    async def serve_forever(self) -> None:
        if self._asyncio_server is None:
            raise HubServerError("call start() before serve_forever()")
        try:
            await self._asyncio_server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._stopped.set()

    async def shutdown(self) -> None:
        """Broadcast ``bye``, close connections, stop the listener.

        Idempotent; multiple calls (e.g. signal handler + finally
        block) are safe.
        """

        if self._asyncio_server is None or not self._asyncio_server.is_serving():
            self._asyncio_server = None
            return

        for conn in list(self._registry.values()):
            try:
                await conn.send(self._bye_envelope(origin=Origin.CLI))
            except Exception:
                pass
            conn.close()

        self._asyncio_server.close()
        try:
            await self._asyncio_server.wait_closed()
        except Exception:
            pass
        self._asyncio_server = None
        log_event(logger, logging.INFO, "hub.server.shutdown")

    @property
    def registered_origins(self) -> list[Origin]:
        return list(self._registry.keys())

    # ------------------------------------------------------------------
    # per-connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = _peer_repr(writer)
        conn = ClientConnection(reader=reader, writer=writer, peer=peer)
        log_event(logger, logging.DEBUG, "hub.client.accepted", peer=peer)
        try:
            if not await self._run_handshake(conn):
                return
            await self._dispatch_loop(conn)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("hub.client.unhandled_error peer=%s", peer)
        finally:
            await self._cleanup_connection(conn)

    async def _run_handshake(self, conn: ClientConnection) -> bool:
        try:
            line = await conn.reader.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return False
        if not line:
            return False

        try:
            env = decode(line)
        except HubProtocolError as exc:
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="bad_request",
                    message=str(exc),
                    context=({"path": exc.json_pointer} if exc.json_pointer else None),
                ),
            )
            return False

        if env.kind is not Kind.REQUEST or env.type != "hello":
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="protocol_mismatch",
                    message="first message must be a hello request",
                    in_reply_to=env.id,
                ),
            )
            return False

        client = Origin(env.payload["client"])
        takeover = bool(env.payload.get("takeover", False))
        if client in self._registry:
            if not takeover:
                await self._safe_send(
                    conn,
                    make_error(
                        origin=Origin.CLI,
                        code="not_connected",
                        message=f"{client.value} client already registered",
                        in_reply_to=env.id,
                    ),
                )
                return False
            # Takeover path: the incoming hello asked to replace any
            # existing registration for this client slot (e.g. a fresh
            # browser tab supersedes a stale one). Bye-broadcast the
            # outgoing peer to everyone else so listeners refresh
            # their peer indicators, close its socket, and drop it
            # from the registry before the new connection registers.
            existing = self._registry.pop(client)
            self.state.registered_clients = set(self._registry.keys())
            log_event(
                logger,
                logging.INFO,
                "hub.client.superseded",
                origin=client.value,
                old_peer=existing.peer,
                new_peer=conn.peer,
            )
            await self._broadcast(
                self._bye_envelope(origin=client), suppress_origin=None
            )
            try:
                await self._safe_send(
                    existing,
                    make_error(
                        origin=Origin.CLI,
                        code="superseded",
                        message=(
                            f"{client.value} client replaced by a newer registration"
                        ),
                    ),
                )
            except Exception:
                pass
            existing.close()

        conn.origin = client
        conn.capabilities = tuple(env.payload.get("capabilities", []))
        conn.client_version = str(env.payload.get("version", ""))
        self._registry[client] = conn
        self.state.registered_clients = set(self._registry.keys())

        await conn.send(
            make_welcome(
                in_reply_to=env.id,
                server_version=self.server_version,
                registered_clients=self.registered_origins,
            )
        )

        # Replay cached state to the just-welcomed peer so a fresh
        # client (new browser tab, restarted nvim, rb hub send drop-in)
        # immediately knows what the user is looking at, without having
        # to wait for the next user action. Each event is unicast to
        # this peer only — existing peers don't see duplicated state.
        # Each replayed envelope carries the original `origin` that
        # produced the cached event so loop-prevention semantics match
        # the live broadcast.
        await self._replay_cached_state(conn)

        log_event(
            logger,
            logging.INFO,
            "hub.client.registered",
            origin=client.value,
            peer=conn.peer,
            version=conn.client_version,
            capabilities=list(conn.capabilities),
        )

        # Tell already-registered peers that a new peer is online.
        # The new peer learnt the current snapshot from `welcome`;
        # this is the delta for the rest. ``suppress_origin=client``
        # keeps the joining peer from receiving a peer_joined event
        # about itself.
        await self._broadcast(
            self._peer_joined_envelope(origin=client),
            suppress_origin=client,
        )
        return True

    async def _dispatch_loop(self, conn: ClientConnection) -> None:
        while True:
            line = await conn.reader.readline()
            if not line:
                return
            try:
                env = decode(line)
            except HubProtocolError as exc:
                await self._safe_send(
                    conn,
                    make_error(
                        origin=Origin.CLI,
                        code="bad_request",
                        message=str(exc),
                        context=(
                            {"path": exc.json_pointer} if exc.json_pointer else None
                        ),
                    ),
                )
                continue

            await self._dispatch(env, conn)

    async def _dispatch(self, env: Envelope, conn: ClientConnection) -> None:
        if env.kind is Kind.EVENT:
            await self._handle_event(env, conn)
        elif env.kind is Kind.REQUEST:
            await self._handle_request(env, conn)
        elif env.kind in (Kind.RESPONSE, Kind.ERROR):
            await self._handle_response_or_error(env)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    async def _handle_event(self, env: Envelope, conn: ClientConnection) -> None:
        if env.type == "bye":
            await self._cleanup_connection(conn)
            return

        if env.type in STATE_EVENT_TYPES:
            self._update_state(env)
            await self._broadcast(env, suppress_origin=env.origin)
            if env.type == "source_focused":
                await self._augment_source_focused(env)
            return

        # Unknown event types are silently dropped per §11.
        log_event(logger, logging.DEBUG, "hub.event.dropped_unknown", type=env.type)

    async def _augment_source_focused(self, env: Envelope) -> None:
        """Derive a ``selection_changed`` from ``source_focused`` and
        broadcast it.

        Without this, the SPA receives ``source_focused {file, line, col}``
        and silently drops it (its switch statement has no case). The
        schematic SPA already handles ``selection_changed`` — pan/highlight
        the matching instance — so resolving file/line/col → instance_path
        on the hub side lets ``:RtlBuddyShow`` from nvim light up the
        schematic with no SPA-side protocol changes.

        Multiple matches can occur (nested instances); the resolver returns
        them smallest-range-first, and we forward the full list so the SPA
        picks the most-specific (per its existing ``Array.isArray ? [0] : ip``
        logic in ``useHub.applyEnvelope``).
        """
        if self.resolver is None:
            return
        payload = env.payload or {}
        file = payload.get("file")
        line = payload.get("line")
        col = payload.get("col")
        if not (isinstance(file, str) and isinstance(line, int)):
            return
        try:
            matches = self.resolver.src_to_view(
                file=file,
                line=line,
                col=col if isinstance(col, int) else None,
            )
        except Exception:  # noqa: BLE001 — resolver errors mustn't abort the loop
            log_event(
                logger,
                logging.WARNING,
                "hub.augment.src_to_view_failed",
                file=file,
                line=line,
            )
            return
        if not matches:
            log_event(
                logger,
                logging.DEBUG,
                "hub.augment.src_to_view_empty",
                file=file,
                line=line,
            )
            return

        instance_path: object = matches[0] if len(matches) == 1 else list(matches)
        derived = Envelope(
            origin=Origin.CLI,
            kind=Kind.EVENT,
            type="selection_changed",
            id=new_id(),
            payload={"instance_path": instance_path},
        )
        self._update_state(derived)
        await self._broadcast(derived, suppress_origin=None)
        log_event(
            logger,
            logging.INFO,
            "hub.augment.src_to_view_resolved",
            file=file,
            line=line,
            instance_path=instance_path,
        )

    def _update_state(self, env: Envelope) -> None:
        try:
            if env.type == "selection_changed":
                ip = env.payload["instance_path"]
                paths = (ip,) if isinstance(ip, str) else tuple(ip)
                self.state.selection = Selection(instance_path=paths, origin=env.origin)
            elif env.type == "signal_selected":
                self.state.signal_selection = SignalSelection(
                    signal=env.payload["signal"],
                    wave_scope=env.payload["wave_scope"],
                    origin=env.origin,
                )
            elif env.type == "cursor_time_changed":
                self.state.cursor_time = CursorTime(
                    t_fs=env.payload["t_fs"], origin=env.origin
                )
            elif env.type == "scope_changed":
                self.state.wave_scope = WaveScope(
                    wave_scope=env.payload["wave_scope"], origin=env.origin
                )
            elif env.type == "diagnostics_set":
                source = env.payload["source"]
                items = tuple(env.payload["items"])
                self.state.diagnostics[source] = DiagnosticsBundle(
                    items=items, origin=env.origin
                )
        except (KeyError, TypeError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "hub.state.update_failed",
                type=env.type,
                error=str(exc),
            )

    async def _broadcast(
        self, env: Envelope, *, suppress_origin: Origin | None
    ) -> None:
        for origin, conn in list(self._registry.items()):
            if origin == suppress_origin:
                continue
            await self._safe_send(conn, env)

    async def broadcast_event(
        self, env: Envelope, *, suppress_origin: Origin | None = None
    ) -> None:
        """Public broadcast hook for hub-internal events.

        Used by the viewer HTTP layer to push ``view_changed`` events
        to connected WS peers (and any TCP adapters) without having to
        reach into ``_broadcast``. ``suppress_origin=None`` means every
        connected client receives the envelope — the appropriate
        default for hub-originated events.
        """

        await self._broadcast(env, suppress_origin=suppress_origin)

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------

    async def _handle_request(self, env: Envelope, conn: ClientConnection) -> None:
        if env.type == "hello":
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="protocol_mismatch",
                    message="hello may only be sent once per connection",
                    in_reply_to=env.id,
                ),
            )
            return

        if env.id in self._dedupe:
            log_event(
                logger,
                logging.WARNING,
                "hub.request.duplicate_dropped",
                id=env.id,
                type=env.type,
            )
            return
        self._dedupe.put(env.id, requester_origin=env.origin)

        if env.type in HUB_HANDLED_REQUESTS:
            await self._handle_hub_request(env, conn)
            return

        target = REQUEST_ROUTING.get(env.type)
        if target is None:
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="bad_request",
                    message=f"unknown request type: {env.type}",
                    in_reply_to=env.id,
                ),
            )
            return

        target_conn = self._registry.get(target)
        if target_conn is None:
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="not_connected",
                    message=f"no {target.value} client registered for {env.type}",
                    in_reply_to=env.id,
                ),
            )
            return

        # Track the pending request so responses route back correctly.
        self._pending.put(env.id, requester_origin=env.origin)
        await self._safe_send(target_conn, env)

    async def _handle_hub_request(self, env: Envelope, conn: ClientConnection) -> None:
        """Run a hub-side request and reply on the same socket."""

        # state_snapshot is pure HubState read; no resolver needed.
        if env.type == "state_snapshot":
            await self._handle_state_snapshot(env, conn)
            return

        if self.resolver is None:
            await self._reply_unresolvable(
                env, conn, "resolver not configured (no view.json available)"
            )
            return

        if env.type == "resolve_view_to_wave":
            await self._handle_resolve_view_to_wave(env, conn)
        elif env.type == "resolve_wave_to_view":
            await self._handle_resolve_wave_to_view(env, conn)
        elif env.type == "resolve_signal_to_view":
            await self._handle_resolve_signal_to_view(env, conn)
        else:
            # Shouldn't happen — HUB_HANDLED_REQUESTS gates this dispatch.
            await self._reply_unresolvable(
                env, conn, f"unhandled hub request {env.type}"
            )

    async def _handle_resolve_view_to_wave(
        self, env: Envelope, conn: ClientConnection
    ) -> None:
        assert self.resolver is not None
        payload = env.payload if isinstance(env.payload, dict) else {}
        instance_path = payload.get("instance_path")
        if not isinstance(instance_path, str) or not instance_path:
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="bad_request",
                    message="resolve_view_to_wave payload missing instance_path",
                    context={"received": payload},
                    in_reply_to=env.id,
                ),
            )
            return

        wave_scope = self.resolver.view_to_wave(instance_path)
        if wave_scope is None:
            await self._reply_unresolvable(
                env,
                conn,
                f"no wave scope matches instance {instance_path!r}",
                context={
                    "instance_path": instance_path,
                    "tb_prefix": self.resolver.mapping.tb_prefix,
                },
            )
            return

        await self._safe_send(
            conn,
            Envelope(
                origin=Origin.CLI,
                kind=Kind.RESPONSE,
                type="resolve_view_to_wave",
                id=env.id,
                payload={"wave_scope": wave_scope},
            ),
        )

    async def _handle_resolve_signal_to_view(
        self, env: Envelope, conn: ClientConnection
    ) -> None:
        assert self.resolver is not None
        payload = env.payload if isinstance(env.payload, dict) else {}
        signal = payload.get("signal")
        wave_scope = payload.get("wave_scope")
        if not (
            isinstance(signal, str)
            and signal
            and isinstance(wave_scope, str)
            and wave_scope
        ):
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="bad_request",
                    message="resolve_signal_to_view payload requires signal+wave_scope",
                    context={"received": payload},
                    in_reply_to=env.id,
                ),
            )
            return

        drivers = self.resolver.signal_drivers(signal=signal, wave_scope=wave_scope)
        if not drivers:
            await self._reply_unresolvable(
                env,
                conn,
                f"no driver of {signal!r} found under {wave_scope!r}",
                context={"signal": signal, "wave_scope": wave_scope},
            )
            return

        # §7: payload.instance_path is the list of driver paths; "port"
        # is the driven port. When drivers disagree on port name (rare)
        # we surface the first one and log; downstream surfacing is the
        # client's call.
        ports = {d.port for d in drivers}
        if len(ports) > 1:
            log_event(
                logger,
                logging.INFO,
                "hub.resolver.signal_drivers_port_mismatch",
                signal=signal,
                wave_scope=wave_scope,
                ports=sorted(ports),
            )

        await self._safe_send(
            conn,
            Envelope(
                origin=Origin.CLI,
                kind=Kind.RESPONSE,
                type="resolve_signal_to_view",
                id=env.id,
                payload={
                    "instance_path": [d.instance_path for d in drivers],
                    "port": drivers[0].port,
                },
            ),
        )

    async def _handle_resolve_wave_to_view(
        self, env: Envelope, conn: ClientConnection
    ) -> None:
        """Reverse of ``resolve_view_to_wave`` — wave_scope → instance_path."""

        assert self.resolver is not None
        payload = env.payload if isinstance(env.payload, dict) else {}
        wave_scope = payload.get("wave_scope")
        if not isinstance(wave_scope, str) or not wave_scope:
            await self._safe_send(
                conn,
                make_error(
                    origin=Origin.CLI,
                    code="bad_request",
                    message="resolve_wave_to_view payload missing wave_scope",
                    context={"received": payload},
                    in_reply_to=env.id,
                ),
            )
            return

        instance_path = self.resolver.wave_to_view(wave_scope)
        if instance_path is None:
            await self._reply_unresolvable(
                env,
                conn,
                f"no instance matches wave scope {wave_scope!r}",
                context={
                    "wave_scope": wave_scope,
                    "tb_prefix": self.resolver.mapping.tb_prefix,
                },
            )
            return

        await self._safe_send(
            conn,
            Envelope(
                origin=Origin.CLI,
                kind=Kind.RESPONSE,
                type="resolve_wave_to_view",
                id=env.id,
                payload={"instance_path": instance_path},
            ),
        )

    async def _handle_state_snapshot(
        self, env: Envelope, conn: ClientConnection
    ) -> None:
        """Return a snapshot of every coordinate cached on ``HubState``.

        Pure HubState read — no resolver required, no events emitted.
        Lets a fresh client (a sidebar SPA, an agent dropping in
        mid-session, a CI guardrail) know "where is the user looking
        right now" without scraping the event stream.
        """

        s = self.state
        selection_payload: dict[str, Any] | None = None
        if s.selection is not None:
            # On-wire selection_changed payload uses a string for the
            # single-driver case and a list for the multi-driver collapse
            # (§7). state_snapshot always emits a string and picks the
            # first path when collapsed — multi-driver collapse is a
            # semantic the snapshot doesn't model.
            paths = s.selection.instance_path
            selection_payload = {
                "instance_path": paths[0] if paths else "",
                "origin": s.selection.origin.value,
            }

        cursor_payload: dict[str, Any] | None = None
        if s.cursor_time is not None:
            cursor_payload = {
                "t_fs": s.cursor_time.t_fs,
                "origin": s.cursor_time.origin.value,
            }

        scope_payload: dict[str, Any] | None = None
        if s.wave_scope is not None:
            scope_payload = {
                "wave_scope": s.wave_scope.wave_scope,
                "origin": s.wave_scope.origin.value,
            }

        await self._safe_send(
            conn,
            Envelope(
                origin=Origin.CLI,
                kind=Kind.RESPONSE,
                type="state_snapshot",
                id=env.id,
                payload={
                    "active_model": s.active_model,
                    "selection": selection_payload,
                    "cursor_time": cursor_payload,
                    "wave_scope": scope_payload,
                    "peers": sorted(o.value for o in s.registered_clients),
                    "diagnostics_sources": sorted(s.diagnostics.keys()),
                },
            ),
        )

    async def _replay_cached_state(self, conn: ClientConnection) -> None:
        """Unicast the hub's cached state events to one client.

        Called from :meth:`_register` after ``welcome`` has been sent.
        Skips any cache slot that's still empty (e.g. a freshly-started
        hub). The cached ``origin`` is preserved on each replayed
        envelope so the receiving client can apply the same
        loop-prevention rules it would for a live broadcast.

        diagnostics_set bundles ship even when ``items`` is empty so a
        producer that cleared its findings before this peer connected
        still reaches the peer as a "cleared" record (matches the
        behaviour added in #128).
        """
        s = self.state

        if s.selection is not None:
            paths = s.selection.instance_path
            payload: dict[str, Any] = {
                # On-wire selection_changed accepts string or list (§7
                # collapse case is list-valued). Replay matches whatever
                # we cached: tuple of length 1 → string, longer → list.
                "instance_path": paths[0] if len(paths) == 1 else list(paths),
            }
            await self._safe_send(
                conn,
                Envelope(
                    origin=s.selection.origin,
                    kind=Kind.EVENT,
                    type="selection_changed",
                    id=new_id(),
                    payload=payload,
                ),
            )

        if s.signal_selection is not None:
            await self._safe_send(
                conn,
                Envelope(
                    origin=s.signal_selection.origin,
                    kind=Kind.EVENT,
                    type="signal_selected",
                    id=new_id(),
                    payload={
                        "signal": s.signal_selection.signal,
                        "wave_scope": s.signal_selection.wave_scope,
                    },
                ),
            )

        if s.cursor_time is not None:
            await self._safe_send(
                conn,
                Envelope(
                    origin=s.cursor_time.origin,
                    kind=Kind.EVENT,
                    type="cursor_time_changed",
                    id=new_id(),
                    payload={"t_fs": s.cursor_time.t_fs},
                ),
            )

        if s.wave_scope is not None:
            await self._safe_send(
                conn,
                Envelope(
                    origin=s.wave_scope.origin,
                    kind=Kind.EVENT,
                    type="scope_changed",
                    id=new_id(),
                    payload={"wave_scope": s.wave_scope.wave_scope},
                ),
            )

        for source, bundle in self.state.diagnostics.items():
            await self._safe_send(
                conn,
                make_diagnostics_set(
                    origin=bundle.origin,
                    source=source,
                    items=list(bundle.items),
                ),
            )

    async def _reply_unresolvable(
        self,
        env: Envelope,
        conn: ClientConnection,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self._safe_send(
            conn,
            make_error(
                origin=Origin.CLI,
                code="unresolvable",
                message=message,
                context=context
                if context is not None
                else (env.payload if isinstance(env.payload, dict) else None),
                in_reply_to=env.id,
            ),
        )

    # ------------------------------------------------------------------
    # responses / errors
    # ------------------------------------------------------------------

    async def _handle_response_or_error(self, env: Envelope) -> None:
        pending = self._pending.pop(env.id)
        if pending is None:
            log_event(
                logger,
                logging.WARNING,
                "hub.response.no_matching_request",
                id=env.id,
                kind=env.kind.value,
                type=env.type,
            )
            return

        requester = self._registry.get(pending.requester_origin)
        if requester is None:
            log_event(
                logger,
                logging.INFO,
                "hub.response.requester_gone",
                id=env.id,
                origin=pending.requester_origin.value,
            )
            return

        await self._safe_send(requester, env)

    # ------------------------------------------------------------------
    # bookkeeping
    # ------------------------------------------------------------------

    async def _cleanup_connection(self, conn: ClientConnection) -> None:
        if conn.origin is None:
            conn.close()
            return

        registered = self._registry.pop(conn.origin, None)
        if registered is not conn:
            # Already cleaned up by a prior bye / disconnect.
            conn.close()
            return

        self.state.registered_clients = set(self._registry.keys())
        log_event(
            logger,
            logging.INFO,
            "hub.client.disconnected",
            origin=conn.origin.value,
            peer=conn.peer,
        )

        # Broadcast bye to remaining clients carrying the leaving origin.
        await self._broadcast(
            self._bye_envelope(origin=conn.origin), suppress_origin=None
        )
        conn.close()

    async def _safe_send(self, conn: ClientConnection, env: Envelope) -> None:
        try:
            await conn.send(env)
        except (ConnectionResetError, BrokenPipeError):
            await self._cleanup_connection(conn)
        except Exception:
            logger.exception("hub.send.unhandled_error peer=%s", conn.peer)
            await self._cleanup_connection(conn)

    def _bye_envelope(self, *, origin: Origin) -> Envelope:
        return Envelope(
            origin=origin,
            kind=Kind.EVENT,
            type="bye",
            id=new_id(),
            payload={},
        )

    def _peer_joined_envelope(self, *, origin: Origin) -> Envelope:
        """Build a ``peer_joined`` event for the named origin.

        Symmetric to :meth:`_bye_envelope` — the joining peer is in the
        envelope's ``origin`` field, payload is empty. Consumers (the
        SPA's useHub, the nvim plugin's hub.lua) treat this as the
        join half of the lifecycle pair: ``welcome`` carries the
        current snapshot, ``peer_joined`` is the delta for joiners
        that arrive after.
        """
        return Envelope(
            origin=origin,
            kind=Kind.EVENT,
            type="peer_joined",
            id=new_id(),
            payload={},
        )


def _peer_repr(writer: asyncio.StreamWriter) -> str:
    """Stable string for log lines: ``host:port`` or ``<unknown>``."""

    info: Any = writer.get_extra_info("peername")
    if info is None:
        return "<unknown>"
    if isinstance(info, tuple) and len(info) >= 2:
        return f"{info[0]}:{info[1]}"
    return str(info)


__all__ = [
    "STATE_EVENT_TYPES",
    "REQUEST_ROUTING",
    "HUB_HANDLED_REQUESTS",
    "DEFAULT_DEDUPE_CAPACITY",
    "DEFAULT_DEDUPE_TTL_SECONDS",
    "HubServer",
    "HubServerError",
    "ClientConnection",
]
