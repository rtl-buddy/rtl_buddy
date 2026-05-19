"""In-memory selection / cursor / scope cache.

The hub keeps a one-slot cache per coordinate type so a client that
reconnects mid-session can ask "what is the current selection?"
without forcing the other clients to re-broadcast. This is the
minimum amount of server-side state required to keep cross-view sync
useful across reconnects; everything else (the design tree, the
waveform, the source files) is owned by the producers.

This module ships the cache structures only — no observers, no
broadcast plumbing. PR 2 (the WS/TCP server) layers an asyncio pub/sub
on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .protocol import Origin


@dataclass(frozen=True, slots=True)
class Selection:
    """Last broadcast ``selection_changed`` payload + its origin."""

    instance_path: tuple[str, ...]
    """Always a tuple — a single-path selection is length 1 and the
    multi-driver collapse case (§7) is length > 1. Stored as a tuple
    so the dataclass stays hashable / frozen."""

    origin: Origin


@dataclass(frozen=True, slots=True)
class SignalSelection:
    """Last broadcast ``signal_selected`` payload + its origin."""

    signal: str
    wave_scope: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class CursorTime:
    """Last broadcast ``cursor_time_changed`` payload + its origin.

    Time is preserved as the on-wire decimal string to avoid JSON
    number precision loss; the only consumers of the numeric value are
    surfer (which deserialises it itself) and resolvers that don't
    care about the time at all.
    """

    t_fs: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class WaveScope:
    """Last broadcast ``scope_changed`` payload + its origin."""

    wave_scope: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class DiagnosticsBundle:
    """Last ``diagnostics_set`` payload for one producer ``source``.

    Stored as the raw on-wire items so the server can replay them to
    newly-connected clients verbatim. The empty-tuple case is a "this
    source has been cleared" record — replaying it tells late joiners
    to clear the source on their side too.
    """

    items: tuple[dict[str, Any], ...]
    origin: Origin


@dataclass
class HubState:
    """One-slot cache per coordinate type.

    Mutable on purpose; the server replaces fields wholesale when a new
    state event is observed. Lock-free in this module — the asyncio
    server in PR 2 holds the only writer task so no synchronisation is
    needed there either.
    """

    selection: Optional[Selection] = None
    signal_selection: Optional[SignalSelection] = None
    cursor_time: Optional[CursorTime] = None
    wave_scope: Optional[WaveScope] = None
    diagnostics: dict[str, DiagnosticsBundle] = field(default_factory=dict)

    registered_clients: set[Origin] = field(default_factory=set)

    def reset(self) -> None:
        """Clear all cached slots (used on ``waveforms_loaded`` and tests)."""

        self.selection = None
        self.signal_selection = None
        self.cursor_time = None
        self.wave_scope = None
        self.diagnostics = {}


__all__ = [
    "Selection",
    "SignalSelection",
    "CursorTime",
    "WaveScope",
    "DiagnosticsBundle",
    "HubState",
]
