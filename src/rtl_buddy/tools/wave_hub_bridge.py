"""Bridges the ``rb wave`` WCP listener to the rtl-buddy-hub.

When ``rb wave`` runs inside a project that has a live hub, this
adapter registers as the ``wave``-origin client and shuttles
information in both directions:

  surfer  ── WCP event ─►  bridge  ── hub event ─►  viewer + nvim
  viewer  ── hub req  ─►  bridge  ── WCP cmd  ─►  surfer

The bridge opens its own TCP connection to the hub (separate from the
WCP listener's connection to surfer) and runs in a dedicated thread.
The existing ``rb wave`` codebase is threaded, so a sync TCP + reader
thread matches the existing style; no asyncio runs inside the wave
adapter.

Discovery is per-project per §4.2 of the protocol spec:

* honour ``$RTL_BUDDY_HUB`` (``host:port``) when present,
* otherwise walk up from CWD looking for ``.rtl-buddy/hub.json``.

When no hub is reachable, :func:`maybe_connect_bridge` returns
``None`` and ``rb wave`` continues standalone — the spec's "graceful
degradation" requirement.
"""

from __future__ import annotations

import logging
import socket
import threading
from pathlib import Path
from typing import Any

from ..hub.discovery import (
    HubDiscoveryError,
    env_override,
    find_project_root_with_hub,
    read_record,
    _pid_is_live,
)
from ..hub.protocol import (
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    make_hello,
    new_id,
)
from ..logging_utils import log_event
from .surfer_wcp import SurferWcpListener


logger = logging.getLogger(__name__)


HELLO_TIMEOUT_SECONDS = 2.0
"""Read deadline for the welcome reply during connect."""

WAVE_CAPABILITIES: tuple[str, ...] = (
    "wave_add_variables",
    "wave_set_cursor",
    "wave_set_scope",
    "wave_set_viewport",
    "wave_zoom_to_range",
    "wave_zoom_to_fit",
    "signal_selected",
    "cursor_time_changed",
    "scope_changed",
)


class WaveHubBridgeError(Exception):
    """Raised on unrecoverable bridge setup errors (kept narrow on purpose)."""


def _parse_hub_addr(spec: str) -> tuple[str, int]:
    """Parse ``host:port`` (the ``$RTL_BUDDY_HUB`` form, also ``hub.json.tcp``)."""

    host, _, port_s = spec.rpartition(":")
    if not host or not port_s:
        raise WaveHubBridgeError(f"malformed hub address {spec!r}")
    try:
        return host, int(port_s)
    except ValueError as exc:
        raise WaveHubBridgeError(f"non-integer port in hub address {spec!r}") from exc


def _discover_hub_addr(*, project_root: Path | None) -> tuple[str, int] | None:
    """Resolve the hub address per §4.2 lookup order.

    Order:
    1. ``$RTL_BUDDY_HUB`` env var.
    2. Walk up from ``project_root`` (or CWD) for ``.rtl-buddy/hub.json``.
    """

    env = env_override()
    if env:
        return _parse_hub_addr(env)

    start = project_root or Path.cwd()
    root = find_project_root_with_hub(start)
    if root is None:
        return None
    try:
        record = read_record(root)
    except HubDiscoveryError as exc:
        log_event(
            logger,
            logging.WARNING,
            "wave.hub.discovery_unreadable",
            error=str(exc),
        )
        return None
    if record is None:
        return None
    if not _pid_is_live(record.pid):
        log_event(
            logger,
            logging.INFO,
            "wave.hub.stale_record",
            pid=record.pid,
        )
        return None
    return _parse_hub_addr(record.tcp)


class WaveHubBridge:
    """Owns the bridge connection between ``rb wave`` and the hub.

    Constructed by :func:`maybe_connect_bridge`; the constructor is for
    tests that want to inject a pre-connected socket.

    Thread model:
    * The constructor connects + runs the hello/welcome handshake on
      the calling thread.
    * :meth:`start` launches the bridge reader thread.
    * :meth:`on_wcp_event` is called from the WCP listener thread to
      translate outbound WCP events into hub events.
    * :meth:`stop` signals exit, attempts a polite ``bye``, closes the
      socket, and joins the reader thread.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        listener: SurferWcpListener,
        client_version: str = "0.1.0",
    ) -> None:
        self._sock = sock
        self._listener = listener
        self._client_version = client_version
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        addr: tuple[str, int],
        *,
        listener: SurferWcpListener,
        client_version: str = "0.1.0",
    ) -> "WaveHubBridge":
        """Open a TCP connection, run hello/welcome, return the bridge.

        Raises :class:`WaveHubBridgeError` on connect / handshake failure.
        """

        try:
            sock = socket.create_connection(addr, timeout=HELLO_TIMEOUT_SECONDS)
        except OSError as exc:
            raise WaveHubBridgeError(
                f"hub at {addr[0]}:{addr[1]} refused connection: {exc}"
            ) from exc

        bridge = cls(sock, listener=listener, client_version=client_version)
        try:
            bridge._do_handshake()
        except Exception:
            sock.close()
            raise

        sock.settimeout(None)
        return bridge

    def _do_handshake(self) -> None:
        hello = make_hello(
            client=Origin.WAVE,
            version=self._client_version,
            capabilities=list(WAVE_CAPABILITIES),
        )
        self._send_envelope(hello)
        try:
            welcome = self._recv_envelope()
        except OSError as exc:
            raise WaveHubBridgeError(f"hub did not reply to hello: {exc}") from exc

        if welcome.type != "welcome" or welcome.kind is not Kind.RESPONSE:
            raise WaveHubBridgeError(
                f"unexpected handshake reply: kind={welcome.kind.value} "
                f"type={welcome.type}"
            )
        if welcome.id != hello.id:
            raise WaveHubBridgeError(
                f"handshake id mismatch: hello={hello.id} welcome={welcome.id}"
            )
        log_event(
            logger,
            logging.INFO,
            "wave.hub.connected",
            server_version=welcome.payload.get("server_version", ""),
            registered=welcome.payload.get("registered_clients", []),
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._reader_thread is not None:
            return
        # Wire the listener observer first so we never miss an event
        # between thread start and the listener loop entering ``run()``.
        self._listener.event_observer = self.on_wcp_event
        thread = threading.Thread(
            target=self._read_loop, daemon=True, name="hub-bridge-reader"
        )
        thread.start()
        self._reader_thread = thread

    def stop(self) -> None:
        if self._listener.event_observer is self.on_wcp_event:
            self._listener.event_observer = None
        if self._stop.is_set():
            return
        self._stop.set()

        # Best-effort polite bye, then close.
        try:
            bye = Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="bye",
                id=new_id(),
                payload={},
            )
            self._send_envelope(bye)
        except (OSError, HubProtocolError):
            pass

        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    # ------------------------------------------------------------------
    # outbound — WCP → hub
    # ------------------------------------------------------------------

    def on_wcp_event(self, event_name: str, msg: dict) -> None:
        """Called from the WCP listener thread per relevant WCP event."""

        if self._stop.is_set():
            return
        try:
            envelope = self._wcp_to_hub_event(event_name, msg)
        except Exception:
            logger.exception("wave.hub.wcp_translate_failed event=%s", event_name)
            return
        if envelope is None:
            return
        try:
            self._send_envelope(envelope)
        except (OSError, HubProtocolError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "wave.hub.send_failed",
                type=envelope.type,
                error=str(exc),
            )

    def _wcp_to_hub_event(self, event_name: str, msg: dict) -> Envelope | None:
        if event_name == "cursor_moved":
            ts = msg.get("timestamp")
            if not isinstance(ts, int):
                return None
            return Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="cursor_time_changed",
                id=new_id(),
                payload={"t_fs": str(ts)},
            )
        if event_name == "scope_changed":
            scope = msg.get("scope")
            if not isinstance(scope, str) or not scope:
                return None
            return Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="scope_changed",
                id=new_id(),
                payload={"wave_scope": scope},
            )
        if event_name == "goto_declaration":
            variable = msg.get("variable")
            if not isinstance(variable, str) or "." not in variable:
                return None
            scope, _, signal = variable.rpartition(".")
            if not scope or not signal:
                return None
            return Envelope(
                origin=Origin.WAVE,
                kind=Kind.EVENT,
                type="signal_selected",
                id=new_id(),
                payload={"signal": signal, "wave_scope": scope},
            )
        return None

    # ------------------------------------------------------------------
    # inbound — hub → WCP
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        buf = b""
        sock = self._sock
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    env = decode(line)
                except HubProtocolError as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "wave.hub.bad_envelope",
                        error=str(exc),
                    )
                    continue
                try:
                    self._handle_inbound(env)
                except Exception:
                    logger.exception(
                        "wave.hub.inbound_unhandled type=%s kind=%s",
                        env.type,
                        env.kind.value,
                    )

    def _handle_inbound(self, env: Envelope) -> None:
        if env.kind is Kind.REQUEST:
            self._handle_request(env)
        # Hub-side events (selection_changed from view, source_focused from
        # src, etc.) are not surfer-actionable in v1; logged at DEBUG.
        elif env.kind is Kind.EVENT and env.type != "bye":
            log_event(
                logger,
                logging.DEBUG,
                "wave.hub.event_ignored",
                type=env.type,
                origin=env.origin.value,
            )
        # responses / errors → unused in v1, just log.
        elif env.kind in (Kind.RESPONSE, Kind.ERROR):
            log_event(
                logger,
                logging.DEBUG,
                "wave.hub.response_ignored",
                type=env.type,
                kind=env.kind.value,
            )

    def _handle_request(self, env: Envelope) -> None:
        if env.type == "wave_add_variables":
            self._handle_add_variables(env)
        elif env.type == "wave_set_cursor":
            self._handle_set_cursor(env)
        elif env.type == "wave_set_scope":
            self._handle_set_scope(env)
        elif env.type == "wave_set_viewport":
            self._handle_set_viewport(env)
        elif env.type == "wave_zoom_to_range":
            self._handle_zoom_to_range(env)
        elif env.type == "wave_zoom_to_fit":
            self._handle_zoom_to_fit(env)
        else:
            self._reply_bad_request(env, f"unhandled wave request {env.type}")

    def _handle_add_variables(self, env: Envelope) -> None:
        payload = env.payload if isinstance(env.payload, dict) else {}
        variables = payload.get("variables")
        if not isinstance(variables, list) or not all(
            isinstance(v, str) and v for v in variables
        ):
            return self._reply_bad_request(env, "missing/invalid variables")
        self._send_to_surfer(
            {
                "type": "command",
                "command": "add_variables",
                "variables": variables,
            }
        )
        # Wait for surfer's WCP response so we can pass ids + not_found back
        # to the hub caller (typically `rb hub send wave-add`). Surfer's WCP
        # has no request IDs but responses arrive in send order per-command,
        # so the listener uses a per-command FIFO of waiters. On timeout we
        # fall back to an optimistic empty reply rather than fail the call —
        # the bridge has run for years without this round-trip.
        resp = self._listener.await_response("add_variables", timeout=2.0)
        reply_payload = self._build_add_reply(resp)
        self._reply_response(env, type_=env.type, payload=reply_payload)

    def _handle_set_cursor(self, env: Envelope) -> None:
        payload = env.payload if isinstance(env.payload, dict) else {}
        t_fs = payload.get("t_fs")
        if not isinstance(t_fs, str) or not t_fs:
            return self._reply_bad_request(env, "missing t_fs")
        try:
            ts = int(t_fs)
        except ValueError:
            return self._reply_bad_request(env, f"non-numeric t_fs: {t_fs!r}")
        self._send_to_surfer(
            {
                "type": "command",
                "command": "set_cursor",
                "timestamp": ts,
                "time_unit": "fs",
            }
        )
        self._reply_response(env, type_=env.type, payload={"ok": True})

    def _handle_set_viewport(self, env: Envelope) -> None:
        """Pan the surfer viewport to center on a timestamp (zoom unchanged)."""
        payload = env.payload if isinstance(env.payload, dict) else {}
        t_fs = payload.get("t_fs")
        if not isinstance(t_fs, str) or not t_fs:
            return self._reply_bad_request(env, "missing t_fs")
        try:
            ts = int(t_fs)
        except ValueError:
            return self._reply_bad_request(env, f"non-numeric t_fs: {t_fs!r}")
        self._send_to_surfer(
            {
                "type": "command",
                "command": "set_viewport_to",
                "timestamp": ts,
                "time_unit": "fs",
            }
        )
        self._reply_response(env, type_=env.type, payload={"ok": True})

    def _handle_zoom_to_range(self, env: Envelope) -> None:
        """Zoom + pan to fit ``[start_fs, end_fs]`` in surfer's viewport."""
        payload = env.payload if isinstance(env.payload, dict) else {}
        start_fs = payload.get("start_fs")
        end_fs = payload.get("end_fs")
        if not isinstance(start_fs, str) or not start_fs:
            return self._reply_bad_request(env, "missing start_fs")
        if not isinstance(end_fs, str) or not end_fs:
            return self._reply_bad_request(env, "missing end_fs")
        try:
            start_ts = int(start_fs)
            end_ts = int(end_fs)
        except ValueError:
            return self._reply_bad_request(env, "non-numeric start_fs/end_fs")
        self._send_to_surfer(
            {
                "type": "command",
                "command": "set_viewport_range",
                "start": start_ts,
                "end": end_ts,
                "time_unit": "fs",
            }
        )
        self._reply_response(env, type_=env.type, payload={"ok": True})

    def _handle_zoom_to_fit(self, env: Envelope) -> None:
        """Zoom out surfer's viewport to fit the entire waveform."""
        # WCP's zoom_to_fit takes a viewport_idx; surfer only opens viewport 0
        # in the wcp-initiate flow, so hard-code that until we expose a second
        # viewport from the hub.
        self._send_to_surfer(
            {"type": "command", "command": "zoom_to_fit", "viewport_idx": 0}
        )
        self._reply_response(env, type_=env.type, payload={"ok": True})

    def _handle_set_scope(self, env: Envelope) -> None:
        payload = env.payload if isinstance(env.payload, dict) else {}
        scope = payload.get("wave_scope")
        if not isinstance(scope, str) or not scope:
            return self._reply_bad_request(env, "missing wave_scope")
        # §9.3 documents `set_scope` as a fork addition; surfer's current
        # close match is `add_scope` (which also adds variables). Until the
        # fork ships `set_scope`, use `add_scope` and document the gap.
        self._send_to_surfer(
            {"type": "command", "command": "add_scope", "scope": scope}
        )
        resp = self._listener.await_response("add_scope", timeout=2.0)
        reply_payload = self._build_add_reply(resp, ok_fallback=True)
        self._reply_response(env, type_=env.type, payload=reply_payload)

    @staticmethod
    def _build_add_reply(resp: dict | None, *, ok_fallback: bool = False) -> dict:
        """Translate surfer's WCP add_* response into the hub reply payload.

        ``not_found`` is surfaced only when present and non-empty so older
        surfer binaries (no not_found field) keep the legacy shape. The
        ``ok`` field is set when ``ok_fallback`` is True, matching the
        previous wave_set_scope reply contract for clients that ignore ids.
        """
        if resp is None:
            return {"ok": True, "ids": []} if ok_fallback else {"ids": []}
        ids = resp.get("ids") if isinstance(resp.get("ids"), list) else []
        out: dict[str, Any] = {"ids": ids}
        if ok_fallback:
            out["ok"] = True
        not_found = resp.get("not_found")
        if isinstance(not_found, list) and not_found:
            out["not_found"] = [s for s in not_found if isinstance(s, str)]
        return out

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _send_to_surfer(self, frame: dict[str, Any]) -> None:
        try:
            self._listener.send_to_surfer(frame)
        except Exception:
            logger.exception(
                "wave.hub.surfer_send_failed command=%s", frame.get("command")
            )

    def _send_envelope(self, env: Envelope) -> None:
        payload = encode(env).encode("utf-8") + b"\n"
        with self._send_lock:
            self._sock.sendall(payload)

    def _recv_envelope(self) -> Envelope:
        buf = b""
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("hub closed the connection mid-message")
            buf += chunk
            if b"\n" in buf:
                line, _, _rest = buf.partition(b"\n")
                return decode(line)

    def _reply_response(
        self, env: Envelope, *, type_: str, payload: dict[str, Any]
    ) -> None:
        reply = Envelope(
            origin=Origin.WAVE,
            kind=Kind.RESPONSE,
            type=type_,
            id=env.id,
            payload=payload,
        )
        try:
            self._send_envelope(reply)
        except (OSError, HubProtocolError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "wave.hub.reply_failed",
                id=env.id,
                error=str(exc),
            )

    def _reply_bad_request(self, env: Envelope, message: str) -> None:
        from ..hub.protocol import make_error

        err = make_error(
            origin=Origin.WAVE,
            code="bad_request",
            message=message,
            context=env.payload if isinstance(env.payload, dict) else None,
            in_reply_to=env.id,
        )
        try:
            self._send_envelope(err)
        except (OSError, HubProtocolError):
            pass


def maybe_connect_bridge(
    *,
    listener: SurferWcpListener,
    project_root: Path | None = None,
    client_version: str = "0.1.0",
) -> WaveHubBridge | None:
    """Discover the project hub and connect a bridge, or ``None``.

    The return value is the live bridge (with the reader thread
    running) when a hub was reachable. ``None`` means "no hub for this
    project / standalone mode" — the spec's graceful-degradation path.
    """

    addr = _discover_hub_addr(project_root=project_root)
    if addr is None:
        log_event(logger, logging.INFO, "wave.hub.absent")
        return None
    try:
        bridge = WaveHubBridge.connect(
            addr, listener=listener, client_version=client_version
        )
    except WaveHubBridgeError as exc:
        log_event(
            logger,
            logging.WARNING,
            "wave.hub.connect_failed",
            host=addr[0],
            port=addr[1],
            error=str(exc),
        )
        return None

    bridge.start()
    return bridge


__all__ = [
    "WaveHubBridge",
    "WaveHubBridgeError",
    "maybe_connect_bridge",
]
