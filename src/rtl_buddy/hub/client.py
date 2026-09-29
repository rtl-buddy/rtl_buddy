"""Synchronous TCP client for the hub protocol, used by ``rb hub send`` and other short-lived peers.

Discovery order: the ``$RTL_BUDDY_HUB`` override (``host:port``), then ``.rtl-buddy/hub.json`` found by walking up from the current directory or ``project_root``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..logging_utils import log_event
from . import discovery
from .protocol import (
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    make_hello,
    new_id,
)


logger = logging.getLogger(__name__)


HELLO_TIMEOUT_SECONDS = 2.0
"""Read deadline for the welcome reply."""

DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
"""Default seconds to wait for a response."""


class HubUnavailable(RuntimeError):
    """No live hub was found for the project."""


class HubClientError(RuntimeError):
    """Handshake or transport failure."""


@dataclass(frozen=True)
class Welcome:
    """Decoded welcome reply."""

    server_version: str
    registered_clients: tuple[str, ...]


class HubClient:
    """Sync TCP client for the v1 hub protocol.

    Create it with :meth:`connect` or the :func:`connect` context manager. Send events with :meth:`emit`, requests with :meth:`request`, and read inbound events with :meth:`drain_events` or :meth:`wait_event`.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._buf = b""
        self._lock = threading.Lock()
        self._events: list[Envelope] = []
        self._responses: dict[str, Envelope] = {}
        self._errors: dict[str, Envelope] = {}
        self._reply_event = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="hub-client-reader"
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        *,
        project_root: Path | None = None,
        origin: Origin = Origin.CLI,
        client_version: str = "0.1.0",
        capabilities: tuple[str, ...] = (),
    ) -> "HubClient":
        """Find the hub, connect and complete the hello/welcome handshake.

        Raises :class:`HubUnavailable` if no hub is found, or :class:`HubClientError` on a connect or handshake failure.
        """

        addr = _discover_hub_addr(project_root=project_root)
        if addr is None:
            raise HubUnavailable(
                "no live hub for project (no .rtl-buddy/hub.json and "
                "$RTL_BUDDY_HUB unset). Start one with `rb hub start`."
            )

        try:
            sock = socket.create_connection(addr, timeout=HELLO_TIMEOUT_SECONDS)
        except OSError as exc:
            raise HubClientError(
                f"hub at {addr[0]}:{addr[1]} refused connection: {exc}"
            ) from exc

        client = cls(sock)
        try:
            client._handshake(
                origin=origin,
                client_version=client_version,
                capabilities=capabilities,
            )
        except Exception:
            sock.close()
            raise

        sock.settimeout(None)
        client._reader.start()
        return client

    def close(self) -> None:
        """Send a best-effort ``bye``, close the socket and stop the reader."""

        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._send_envelope(
                Envelope(
                    origin=Origin.CLI,
                    kind=Kind.EVENT,
                    type="bye",
                    id=new_id(),
                    payload={},
                )
            )
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
        if self._reader.is_alive():
            self._reader.join(timeout=1.0)

    def __enter__(self) -> "HubClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def emit(
        self, type_: str, payload: dict[str, Any], *, origin: Origin = Origin.CLI
    ) -> str:
        """Send an event without waiting for a reply and return its envelope id."""

        env = Envelope(
            origin=origin,
            kind=Kind.EVENT,
            type=type_,
            id=new_id(),
            payload=payload,
        )
        self._send_envelope(env)
        return env.id

    def request(
        self,
        type_: str,
        payload: dict[str, Any],
        *,
        origin: Origin = Origin.CLI,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> Envelope:
        """Send a request and block for the reply.

        Returns the response envelope, or an envelope with ``kind=Kind.ERROR`` carrying the hub's error. Raises :class:`TimeoutError` if no reply arrives within ``timeout`` seconds.
        """

        env = Envelope(
            origin=origin,
            kind=Kind.REQUEST,
            type=type_,
            id=new_id(),
            payload=payload,
        )
        self._send_envelope(env)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                reply = self._responses.pop(env.id, None) or self._errors.pop(
                    env.id, None
                )
                if reply is not None:
                    return reply
            self._reply_event.wait(timeout=max(0.0, deadline - time.time()))
            self._reply_event.clear()
        raise TimeoutError(f"no reply to {type_!r} within {timeout}s")

    # ------------------------------------------------------------------
    # inbound observation
    # ------------------------------------------------------------------

    def drain_events(self) -> list[Envelope]:
        """Return and clear the events received since the last drain."""

        with self._lock:
            out = list(self._events)
            self._events.clear()
            return out

    def wait_event(self, type_: str, *, timeout: float = 2.0) -> Envelope:
        """Block until an event of ``type_`` arrives and return it. Raises :class:`TimeoutError`."""

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for i, e in enumerate(self._events):
                    if e.type == type_:
                        return self._events.pop(i)
            time.sleep(0.02)
        raise TimeoutError(f"no event {type_!r} within {timeout}s")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _handshake(
        self,
        *,
        origin: Origin,
        client_version: str,
        capabilities: tuple[str, ...],
    ) -> Welcome:
        hello = make_hello(
            client=origin,
            version=client_version,
            capabilities=list(capabilities),
        )
        self._send_envelope(hello)
        try:
            welcome = self._recv_one()
        except OSError as exc:
            raise HubClientError(f"hub did not reply to hello: {exc}") from exc

        if welcome.type == "error":
            payload = welcome.payload if isinstance(welcome.payload, dict) else {}
            raise HubClientError(
                f"hub rejected hello: code={payload.get('code')} "
                f"message={payload.get('message')}"
            )

        if welcome.type != "welcome" or welcome.kind is not Kind.RESPONSE:
            raise HubClientError(
                f"unexpected handshake reply: kind={welcome.kind.value} "
                f"type={welcome.type}"
            )
        if welcome.id != hello.id:
            raise HubClientError(
                f"handshake id mismatch: hello={hello.id} welcome={welcome.id}"
            )
        log_event(
            logger,
            logging.INFO,
            "hub.client.connected",
            origin=origin.value,
            server_version=welcome.payload.get("server_version", ""),
            registered=welcome.payload.get("registered_clients", []),
        )
        return Welcome(
            server_version=str(welcome.payload.get("server_version", "")),
            registered_clients=tuple(
                welcome.payload.get("registered_clients", []) or ()
            ),
        )

    def _send_envelope(self, env: Envelope) -> None:
        payload = encode(env).encode("utf-8") + b"\n"
        with self._send_lock:
            self._sock.sendall(payload)

    def _recv_one(self) -> Envelope:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("hub closed the connection mid-message")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return decode(line)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            self._buf += chunk
            while b"\n" in self._buf:
                line, _, self._buf = self._buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    env = decode(line)
                except HubProtocolError as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "hub.client.bad_envelope",
                        error=str(exc),
                    )
                    continue
                with self._lock:
                    if env.kind is Kind.EVENT:
                        self._events.append(env)
                    elif env.kind is Kind.RESPONSE:
                        self._responses[env.id] = env
                    elif env.kind is Kind.ERROR:
                        self._errors[env.id] = env
                self._reply_event.set()


def _discover_hub_addr(*, project_root: Path | None) -> tuple[str, int] | None:
    """Return the hub ``(host, port)`` from ``$RTL_BUDDY_HUB`` or the project's hub record, or ``None``."""

    env = discovery.env_override()
    if env:
        host, _, port_s = env.rpartition(":")
        if not host or not port_s:
            raise HubClientError(f"malformed $RTL_BUDDY_HUB={env!r}")
        try:
            return (host, int(port_s))
        except ValueError as exc:
            raise HubClientError(f"non-integer port in $RTL_BUDDY_HUB={env!r}") from exc

    start = project_root or Path.cwd()
    root = discovery.find_project_root_with_hub(start)
    if root is None:
        return None
    try:
        record = discovery.read_record(root)
    except discovery.HubDiscoveryError as exc:
        log_event(
            logger,
            logging.WARNING,
            "hub.client.discovery_unreadable",
            error=str(exc),
        )
        return None
    if record is None:
        return None
    if not discovery._pid_is_live(record.pid):  # noqa: SLF001
        log_event(
            logger,
            logging.INFO,
            "hub.client.stale_record",
            pid=record.pid,
        )
        return None
    host, _, port_s = record.tcp.rpartition(":")
    try:
        return (host, int(port_s))
    except ValueError:
        return None


@contextmanager
def connect(**kwargs: Any) -> Iterator[HubClient]:
    """Context manager around :meth:`HubClient.connect` that closes the client on exit."""

    client = HubClient.connect(**kwargs)
    try:
        yield client
    finally:
        client.close()


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "HELLO_TIMEOUT_SECONDS",
    "HubClient",
    "HubClientError",
    "HubUnavailable",
    "Welcome",
    "connect",
]
