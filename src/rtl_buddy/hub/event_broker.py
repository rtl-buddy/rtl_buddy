"""In-memory pub/sub broker for SPA↔notebook state sync.

Used by Phase 3 of the marimo umbrella (axi-profiler #16): the SPA's
``AxiPerfView`` and a spawned marimo notebook each open a WebSocket
to ``/api/events/sync``; clicking a bundle in the SPA reaches the
notebook and brushing a time window in the notebook reaches the SPA.

The broker treats every message as an opaque string. Topic routing
and echo suppression (via a ``source`` field) live in the clients —
the broker just relays each inbound message to every *other*
connected client. Keeps the schema versionable without re-deploying
the hub.

Slow clients get bounded outbound queues: when full, the oldest
queued message is dropped to make room. State-sync messages are
throwaway — a stale ``selection`` is worthless once a fresh one has
already arrived.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Per-client outbound queue depth. 64 is enough to absorb a burst
# from a chatty publisher without ballooning memory if a single slow
# subscriber stalls.
_CLIENT_QUEUE_MAX = 64


@dataclass
class BrokerClient:
    """Handle the broker hands back to a connected WS handler.

    The handler reads ``queue`` to forward messages downstream; the
    broker writes to ``queue`` for every inbound from any peer.
    """

    queue: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
    )
    name: str = ""


class EventBroker:
    """Process-wide pub/sub fanout. Not thread-safe; single asyncio loop."""

    def __init__(self) -> None:
        self._clients: dict[int, BrokerClient] = {}
        self._next_id: int = 0

    def add_client(self, name: str = "") -> tuple[int, BrokerClient]:
        client_id = self._next_id
        self._next_id += 1
        client = BrokerClient(name=name or f"c{client_id}")
        self._clients[client_id] = client
        log_event(
            logger,
            logging.DEBUG,
            "hub.event_broker.client_added",
            client_id=client_id,
            name=client.name,
            total=len(self._clients),
        )
        return client_id, client

    def remove_client(self, client_id: int) -> None:
        client = self._clients.pop(client_id, None)
        if client is None:
            return
        log_event(
            logger,
            logging.DEBUG,
            "hub.event_broker.client_removed",
            client_id=client_id,
            name=client.name,
            total=len(self._clients),
        )

    def broadcast(self, sender_id: int, message: str) -> None:
        """Push ``message`` to every connected client except ``sender_id``.

        Drops the oldest queued message for any client whose queue
        is full; logs a single warning per overflow event so a stuck
        consumer is visible without flooding the log.
        """
        for client_id, client in self._clients.items():
            if client_id == sender_id:
                continue
            self._enqueue(client, message)

    @staticmethod
    def _enqueue(client: BrokerClient, message: str) -> None:
        try:
            client.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Drop oldest, retry once. A second failure would require a
        # concurrent producer on the same queue, which we don't have
        # (single-loop), so the bare ``put_nowait`` below should always
        # succeed.
        try:
            client.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - racy
            pass
        log_event(
            logger,
            logging.WARNING,
            "hub.event_broker.queue_overflow",
            name=client.name,
        )
        client.queue.put_nowait(message)

    @property
    def client_count(self) -> int:
        return len(self._clients)
