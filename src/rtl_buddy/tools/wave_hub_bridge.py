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
    "wave_get_items",
    "wave_remove_items",
    "wave_move_items",
    "wave_add_comments",
    "signal_selected",
    "cursor_time_changed",
    "scope_changed",
    "wave_values_changed",
)

# Maximum wall-time the bridge will wait on a query_variable_values
# response before giving up on a cursor-driven sample. Short by design
# — surfer's WCP is in-process and a query against a handful of
# variables typically returns in <10 ms; anything longer than this and
# the user has scrubbed past the sample point anyway.
QUERY_TIMEOUT_SECONDS = 1.0


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
        # Hierarchy strings the user has asked surfer to track via
        # ``wave_add_variables``. ``query_variable_values`` only returns
        # data for variables whose signal payload has been loaded — which
        # the add_variables flow guarantees. The set is the
        # working-cache for cursor-driven re-sampling.
        self._tracked_variables: list[str] = []
        # Single-in-flight gate for cursor-driven queries. Cursor
        # scrubbing fires at ~60 Hz; one outstanding round-trip is enough
        # to keep the viewer painted, and dropping the rest avoids
        # piling up queries surfer can't process faster than they arrive.
        self._query_in_flight = threading.Lock()

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
        # Cursor-driven wave-values producer. Runs AFTER the
        # cursor_time_changed broadcast so peers always see the cursor
        # advance before the new values land — without this ordering,
        # a fast worker thread can race ahead and emit
        # wave_values_changed first, which looks wrong in event logs
        # even though the viewer's merge semantics are identical.
        if event_name == "cursor_moved":
            ts = msg.get("timestamp")
            if (
                isinstance(ts, int)
                and self._tracked_variables
                and self._query_in_flight.acquire(blocking=False)
            ):
                t = threading.Thread(
                    target=self._produce_wave_values,
                    args=(ts,),
                    name="wave-hub-bridge-values",
                    daemon=True,
                )
                t.start()

    def _wcp_to_hub_event(self, event_name: str, msg: dict) -> Envelope | None:
        if event_name == "cursor_moved":
            ts = msg.get("timestamp")
            if not isinstance(ts, int):
                return None
            # NB: the cursor-driven ``wave_values_changed`` producer is
            # kicked off in :meth:`on_wcp_event` AFTER the
            # ``cursor_time_changed`` envelope has been sent, so peers
            # see cursor-move-then-values rather than the other way
            # around. See the matching block there.
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
        elif env.type == "wave_get_items":
            self._handle_get_items(env)
        elif env.type == "wave_remove_items":
            self._handle_remove_items(env)
        elif env.type == "wave_move_items":
            self._handle_move_items(env)
        elif env.type == "wave_add_comments":
            self._handle_add_comments(env)
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
        # Update the tracked-variables cache so cursor-driven
        # ``wave_values_changed`` queries (below) target the set the
        # user actually cares about. Track only the variables surfer
        # resolved — querying for paths that came back in ``not_found``
        # would just waste a round-trip.
        not_found_set = set()
        if isinstance(resp, dict):
            nf = resp.get("not_found")
            if isinstance(nf, list):
                not_found_set = {s for s in nf if isinstance(s, str)}
        resolved = [v for v in variables if v not in not_found_set]
        if resolved:
            existing = set(self._tracked_variables)
            for v in resolved:
                if v not in existing:
                    self._tracked_variables.append(v)
                    existing.add(v)
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
        self._drive_ack(
            env,
            {
                "type": "command",
                "command": "set_cursor",
                "timestamp": ts,
                "time_unit": "fs",
            },
            strict=False,
        )

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
        self._drive_ack(
            env,
            {
                "type": "command",
                "command": "set_viewport_to",
                "timestamp": ts,
                "time_unit": "fs",
            },
            strict=False,
        )

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
        self._drive_ack(
            env,
            {
                "type": "command",
                "command": "set_viewport_range",
                "start": start_ts,
                "end": end_ts,
                "time_unit": "fs",
            },
            strict=False,
        )

    def _handle_zoom_to_fit(self, env: Envelope) -> None:
        """Zoom out surfer's viewport to fit the entire waveform."""
        # WCP's zoom_to_fit takes a viewport_idx; surfer only opens viewport 0
        # in the wcp-initiate flow, so hard-code that until we expose a second
        # viewport from the hub.
        self._drive_ack(
            env,
            {"type": "command", "command": "zoom_to_fit", "viewport_idx": 0},
            strict=False,
        )

    def _handle_set_scope(self, env: Envelope) -> None:
        payload = env.payload if isinstance(env.payload, dict) else {}
        scope = payload.get("wave_scope")
        if not isinstance(scope, str) or not scope:
            return self._reply_bad_request(env, "missing wave_scope")
        # surfer's `set_scope` (rtl-buddy fork PR #6) navigates the active
        # scope without mutating the displayed item list — the right
        # semantics for "follow source-focus" tinting. Best-effort ack: an
        # explicit surfer rejection (e.g. unknown scope) is now propagated
        # back to the requesting peer as a hub error; a missing reply still
        # resolves as ok so a slow/old surfer doesn't stall scope-follow.
        self._drive_ack(
            env,
            {"type": "command", "command": "set_scope", "scope": scope},
            strict=False,
        )

    # ------------------------------------------------------------------
    # inbound — wave-view item management (list / remove / move / comment)
    # ------------------------------------------------------------------

    WCP_REPLY_TIMEOUT = 2.0
    """Wall-time the bridge waits on a surfer WCP reply for a hub-driven
    command before treating it as no-reply. Surfer's WCP is in-process and
    acks in well under this; the budget is generous for a loaded design."""

    def _drive_ack(self, env: Envelope, frame: dict[str, Any], *, strict: bool) -> bool:
        """Send an ack-returning WCP command and report genuine status back.

        ``strict=True`` turns a no-reply into a hub error (the caller needs
        confirmation — destructive / structural commands). ``strict=False``
        is best-effort: an explicit surfer ``error`` is still surfaced as a
        hub error, but a missing reply resolves as ``{"ok": true}`` so the
        high-frequency navigation commands never stall on a dropped ack.

        Returns ``True`` when an ``{"ok": true}`` response was sent, ``False``
        when a hub error was sent instead.
        """
        self._send_to_surfer(frame)
        reply = self._listener.await_reply({"ack"}, timeout=self.WCP_REPLY_TIMEOUT)
        if reply is None:
            if strict:
                self._reply_error(
                    env,
                    "not_connected",
                    f"surfer did not acknowledge {frame.get('command')}",
                )
                return False
            self._reply_response(env, type_=env.type, payload={"ok": True})
            return True
        kind, msg = reply
        if kind == "error":
            self._reply_surfer_error(env, msg)
            return False
        self._reply_response(env, type_=env.type, payload={"ok": True})
        return True

    def _fetch_item_ids(self) -> list[int] | None:
        """Return surfer's current displayed-item ids, or ``None`` on
        no-reply / surfer error."""
        self._send_to_surfer({"type": "command", "command": "get_item_list"})
        reply = self._listener.await_reply(
            {"get_item_list"}, timeout=self.WCP_REPLY_TIMEOUT
        )
        if reply is None or reply[0] == "error":
            return None
        ids = reply[1].get("ids")
        return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []

    def _handle_get_items(self, env: Envelope) -> None:
        """List the items currently in surfer's view (get_item_list + info)."""
        self._send_to_surfer({"type": "command", "command": "get_item_list"})
        list_reply = self._listener.await_reply(
            {"get_item_list"}, timeout=self.WCP_REPLY_TIMEOUT
        )
        if list_reply is None:
            return self._reply_error(
                env, "not_connected", "surfer did not return the item list"
            )
        if list_reply[0] == "error":
            return self._reply_surfer_error(env, list_reply[1])
        ids = list_reply[1].get("ids")
        ids = [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []
        if not ids:
            return self._reply_response(env, type_=env.type, payload={"items": []})

        self._send_to_surfer(
            {"type": "command", "command": "get_item_info", "ids": ids}
        )
        info_reply = self._listener.await_reply(
            {"get_item_info"}, timeout=self.WCP_REPLY_TIMEOUT
        )
        if info_reply is None:
            return self._reply_error(
                env, "not_connected", "surfer did not return item info"
            )
        if info_reply[0] == "error":
            return self._reply_surfer_error(env, info_reply[1])
        items = self._build_items(info_reply[1].get("results"))
        self._reply_response(env, type_=env.type, payload={"items": items})

    @staticmethod
    def _build_items(results: Any) -> list[dict[str, Any]]:
        """Translate surfer ``get_item_info`` results into hub item dicts.

        surfer reports item kinds capitalised (``Variable``, ``Divider``,
        ``Marker``, ``Group``, …); they are normalised to lower-case here so
        the hub exposes a stable ``variable | divider | marker | group | …``
        vocabulary regardless of surfer's internal casing. For a variable
        whose name is a dotted hierarchy path, the leading scope is split
        out into the optional ``scope`` field so a consumer can address it
        without re-parsing.
        """
        items: list[dict[str, Any]] = []
        if not isinstance(results, list):
            return items
        for row in results:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            if not isinstance(rid, int):
                continue
            name = row.get("name")
            itype = row.get("type")
            item: dict[str, Any] = {
                "id": rid,
                "type": itype.lower()
                if isinstance(itype, str) and itype
                else "unknown",
                "name": name if isinstance(name, str) else "",
            }
            if item["type"] == "variable" and isinstance(name, str) and "." in name:
                scope, _, _signal = name.rpartition(".")
                if scope:
                    item["scope"] = scope
            items.append(item)
        return items

    def _handle_remove_items(self, env: Envelope) -> None:
        """Remove items by id, reporting which were actually removed.

        surfer's ``remove_items`` acks unconditionally and silently ignores
        unknown ids, so the bridge diffs the item list before/after to
        report genuine ``removed`` / ``not_found`` sets to the caller.
        """
        payload = env.payload if isinstance(env.payload, dict) else {}
        ids = payload.get("ids")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, int) for i in ids)
        ):
            return self._reply_bad_request(env, "missing/invalid ids")
        requested = list(dict.fromkeys(ids))

        before = self._fetch_item_ids()
        if before is None:
            return self._reply_error(
                env, "not_connected", "surfer did not return the item list"
            )

        self._send_to_surfer(
            {"type": "command", "command": "remove_items", "ids": requested}
        )
        rm_reply = self._listener.await_reply({"ack"}, timeout=self.WCP_REPLY_TIMEOUT)
        if rm_reply is not None and rm_reply[0] == "error":
            return self._reply_surfer_error(env, rm_reply[1])

        after = self._fetch_item_ids()
        after_set = set(after) if after is not None else set()
        before_set = set(before)
        removed = [i for i in requested if i in before_set and i not in after_set]
        not_found = [i for i in requested if i not in before_set]
        self._reply_response(
            env,
            type_=env.type,
            payload={"ok": True, "removed": removed, "not_found": not_found},
        )

    def _handle_move_items(self, env: Envelope) -> None:
        """Reorder items: move ids so the block starts at to_index."""
        payload = env.payload if isinstance(env.payload, dict) else {}
        ids = payload.get("ids")
        to_index = payload.get("to_index")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, int) for i in ids)
        ):
            return self._reply_bad_request(env, "missing/invalid ids")
        if not isinstance(to_index, int) or to_index < 0:
            return self._reply_bad_request(env, "missing/invalid to_index")
        self._drive_ack(
            env,
            {
                "type": "command",
                "command": "move_items",
                "ids": list(dict.fromkeys(ids)),
                "target_index": to_index,
            },
            strict=True,
        )

    def _handle_add_comments(self, env: Envelope) -> None:
        """Add comment rows (named dividers) to the view; return their ids."""
        payload = env.payload if isinstance(env.payload, dict) else {}
        texts = payload.get("texts")
        if (
            not isinstance(texts, list)
            or not texts
            or not all(isinstance(t, str) and t for t in texts)
        ):
            return self._reply_bad_request(env, "missing/invalid texts")
        frame: dict[str, Any] = {
            "type": "command",
            "command": "add_dividers",
            "names": list(texts),
        }
        after_id = payload.get("after_id")
        if isinstance(after_id, int):
            frame["after"] = after_id
        self._send_to_surfer(frame)
        reply = self._listener.await_reply(
            {"add_dividers"}, timeout=self.WCP_REPLY_TIMEOUT
        )
        if reply is None:
            return self._reply_error(
                env, "not_connected", "surfer did not acknowledge add_dividers"
            )
        if reply[0] == "error":
            return self._reply_surfer_error(env, reply[1])
        ids = reply[1].get("ids")
        out_ids = (
            [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []
        )
        self._reply_response(env, type_=env.type, payload={"ids": out_ids})

    def _produce_wave_values(self, native_timestamp: int) -> None:
        """Sample every tracked variable at the cursor and broadcast.

        Runs on a daemon thread; the in-flight gate is released before
        return so the next cursor_moved can spawn its own worker.
        """

        try:
            variables = list(self._tracked_variables)
            if not variables:
                return
            self._send_to_surfer(
                {
                    "type": "command",
                    "command": "query_variable_values",
                    "variables": variables,
                    # No timestamp → surfer samples at its current
                    # cursor. The cursor_moved event we're responding
                    # to means CursorSet has already landed, so this
                    # matches the event's timestamp by construction
                    # and avoids re-sending the value the bridge just
                    # received in the event payload.
                }
            )
            resp = self._listener.await_response(
                "query_variable_values", timeout=QUERY_TIMEOUT_SECONDS
            )
            if resp is None:
                # Either surfer didn't reply in time or the WCP socket
                # dropped. Either way we just skip this sample; the
                # next cursor_moved will retry.
                log_event(
                    logger,
                    logging.DEBUG,
                    "wave.hub.query_timeout",
                    variables=len(variables),
                )
                return
            envelope = self._wave_values_envelope(resp, native_timestamp)
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
        except Exception:
            logger.exception("wave.hub.produce_wave_values_failed")
        finally:
            self._query_in_flight.release()

    def _wave_values_envelope(
        self, resp: dict, fallback_native_ts: int
    ) -> Envelope | None:
        """Translate a surfer ``query_variable_values`` response into the
        hub's ``wave_values_changed`` event envelope.

        ``fallback_native_ts`` is the native-tick timestamp from the
        cursor_moved event that triggered this query — used when the
        response doesn't carry a parsable ``timestamp`` (defensive; the
        new surfer command always sets one).
        """

        # surfer serializes the response timestamp as a decimal string
        # (see surfer-wcp proto.rs). The cursor_moved event uses an
        # integer in native ticks. The hub-side ``t_fs`` envelope wants
        # a decimal string in *fs* — so we have to convert through
        # surfer's tick → fs mapping, which the bridge doesn't track
        # explicitly. Instead, reuse the cursor_moved event's timestamp
        # (already in surfer-native ticks) since the new surfer command
        # samples at-cursor and the two values agree by construction.
        # If a future caller passes an explicit ``timestamp`` to the
        # query, we re-derive from the response.
        ts_raw = resp.get("timestamp")
        native_ts: int
        if isinstance(ts_raw, str):
            try:
                native_ts = int(ts_raw)
            except ValueError:
                native_ts = fallback_native_ts
        elif isinstance(ts_raw, int):
            native_ts = ts_raw
        else:
            native_ts = fallback_native_ts

        rows = resp.get("values")
        if not isinstance(rows, list):
            return None
        values: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            variable = row.get("variable")
            value = row.get("value")
            if not isinstance(variable, str) or "." not in variable:
                continue
            if not isinstance(value, str):
                # value is null when the variable has no transition
                # before the sample point. Drop from the broadcast so
                # the viewer's "absent signals retain prior values"
                # semantics keep painting whatever was last known.
                continue
            scope, _, signal = variable.rpartition(".")
            if not scope or not signal:
                continue
            values.append({"wave_scope": scope, "signal": signal, "value": value})

        return Envelope(
            origin=Origin.WAVE,
            kind=Kind.EVENT,
            type="wave_values_changed",
            id=new_id(),
            payload={"t_fs": str(native_ts), "values": values},
        )

    @staticmethod
    def _build_add_reply(resp: dict | None) -> dict:
        """Translate surfer's WCP add_* response into the hub reply payload.

        ``not_found`` is surfaced only when present and non-empty so older
        surfer binaries (no not_found field) keep the legacy shape.
        """
        if resp is None:
            return {"ids": []}
        ids = resp.get("ids") if isinstance(resp.get("ids"), list) else []
        out: dict[str, Any] = {"ids": ids}
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

    def _reply_error(
        self,
        env: Envelope,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        from ..hub.protocol import make_error

        err = make_error(
            origin=Origin.WAVE,
            code=code,
            message=message,
            context=context,
            in_reply_to=env.id,
        )
        try:
            self._send_envelope(err)
        except (OSError, HubProtocolError):
            pass

    def _reply_bad_request(self, env: Envelope, message: str) -> None:
        self._reply_error(
            env,
            "bad_request",
            message,
            context=env.payload if isinstance(env.payload, dict) else None,
        )

    def _reply_surfer_error(self, env: Envelope, err_msg: dict) -> None:
        """Translate a surfer WCP ``error`` frame into a hub error reply.

        surfer's error carries ``error`` (a short tag, usually the command
        name), ``arguments``, and a human ``message``. We map it to the
        hub's ``bad_request`` code — a surfer rejection means the request
        couldn't be applied (unknown id, illegal move, unknown scope) — and
        preserve the surfer detail in ``context``.
        """
        message = (
            err_msg.get("message")
            or err_msg.get("error")
            or "surfer rejected the command"
        )
        context: dict[str, Any] = {}
        tag = err_msg.get("error")
        if isinstance(tag, str) and tag:
            context["surfer_error"] = tag
        args = err_msg.get("arguments")
        if isinstance(args, list) and args:
            context["arguments"] = [a for a in args if isinstance(a, str)]
        self._reply_error(env, "bad_request", str(message), context or None)


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
