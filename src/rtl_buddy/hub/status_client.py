"""TCP client used by ``rb hub status`` to list the origins registered with a running hub.

It connects, runs the ``hello`` handshake as :attr:`Origin.CLI`, reads the registry from the ``welcome``, and disconnects. Only one ``cli`` connection can be registered at a time, so a second concurrent ``rb hub status`` is refused.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from .protocol import Envelope, Kind, Origin, decode, encode, new_id

DEFAULT_TIMEOUT = 3.0


class HubStatusQueryError(Exception):
    """Raised when the hub does not answer a status query; the message names the cause."""


async def query_registered_origins(
    host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """Return the ``registered_clients`` list from the hub's ``welcome``.

    The list includes ``"cli"``, the caller itself.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HubStatusQueryError(f"connect to {host}:{port}: {exc}") from exc

    try:
        hello = Envelope(
            origin=Origin.CLI,
            kind=Kind.REQUEST,
            type="hello",
            id=new_id(),
            payload={
                "client": Origin.CLI.value,
                "version": "0.1.0",
                "capabilities": [],
            },
        )
        writer.write(encode(hello).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise HubStatusQueryError("hub closed connection before welcome")
        env = decode(line)
        if env.type != "welcome":
            raise HubStatusQueryError(f"expected welcome, got {env.type!r}")
        clients = env.payload.get("registered_clients", [])
        if not isinstance(clients, list):
            raise HubStatusQueryError("welcome.registered_clients is not a list")
        return [str(c) for c in clients]
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def query_registered_origins_sync(
    host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """Synchronous wrapper around :func:`query_registered_origins`."""
    return asyncio.run(query_registered_origins(host, port, timeout=timeout))


# ``cli`` (the query itself) and ``notebook`` (a per-session marimo peer) are left out.
DISPLAY_ORIGINS: Sequence[str] = ("view", "wave", "src", "graph", "cov", "phys")
