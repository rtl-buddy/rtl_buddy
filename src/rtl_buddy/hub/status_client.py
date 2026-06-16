"""Tiny TCP client used by ``rb hub status`` to query a running hub.

The hub's ``hello`` / ``welcome`` handshake (§3 of the protocol spec)
already carries the registry — every welcome envelope lists every
origin currently registered with the server. This module opens a
fresh connection, runs the handshake as :attr:`Origin.CLI`, reads the
welcome, and disconnects. Cheap, single round trip, no dependency on
the optional HTTP layer.

The hub's per-origin dedup means at most one ``cli`` query is in
flight at a time; a second concurrent ``rb hub status`` would be
refused with ``not_connected``. That is acceptable for v1 — status
queries are interactive and short-lived.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from .protocol import Envelope, Kind, Origin, decode, encode, new_id

DEFAULT_TIMEOUT = 3.0


class HubStatusQueryError(Exception):
    """Raised when the hub doesn't respond to a status query.

    The message carries the underlying cause (connect failed, no
    welcome, malformed welcome). Callers render it as a peer-state
    line in :func:`rb hub status` rather than re-raising.
    """


async def query_registered_origins(
    host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """Run ``hello`` against the hub and return the welcome's registry.

    Returns the ``registered_clients`` field from the welcome envelope
    as a list of origin strings. The hub adds the calling CLI to the
    registry *before* sending the welcome (§3.2 of the spec), so the
    returned list includes ``"cli"``; callers filter that out when
    rendering peer state.
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


# Origins users of ``rb hub status`` care about. ``cli`` is excluded
# because it represents the status query itself; the remaining three
# are the v1 production peers (viewer SPA, ``rb wave`` bridge, editor
# adapter — nvim today, more later — registers as ``src``).
DISPLAY_ORIGINS: Sequence[str] = ("view", "wave", "src")
