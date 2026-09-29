"""In-memory pub/sub broker relaying state-sync messages between the SPA and spawned marimo notebooks.

Messages are opaque strings; each is relayed to every connected client except the sender. Topic routing and echo suppression are the clients' job.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Per-client outbound queue depth.
_CLIENT_QUEUE_MAX = 64


@dataclass
class BrokerClient:
    """A connected client: the broker writes relayed messages to ``queue`` for the WebSocket handler to forward."""

    queue: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
    )
    name: str = ""


class EventBroker:
    """Pub/sub fan-out for one asyncio loop; not thread-safe."""

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
        """Queue ``message`` for every client except ``sender_id``.

        When a client's queue is full, its oldest message is dropped, since a stale sync message is worthless.
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
        # Single loop: nothing else can refill the queue, so the final put_nowait succeeds.
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
