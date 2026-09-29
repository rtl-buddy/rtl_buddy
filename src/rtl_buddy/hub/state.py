"""One-slot cache per coordinate type (selection, cursor, scope, focus, diagnostics).

A client that connects mid-session reads the current values from here instead of asking the other clients to re-broadcast. The cache holds data only; the server does the broadcasting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .protocol import Origin


@dataclass(frozen=True, slots=True)
class Selection:
    """Last broadcast ``selection_changed`` payload and its origin."""

    instance_path: tuple[str, ...]
    """One element for a single-path selection, several for a multi-driver collapse."""

    origin: Origin


@dataclass(frozen=True, slots=True)
class SignalSelection:
    """Last broadcast ``signal_selected`` payload and its origin."""

    signal: str
    wave_scope: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class CursorTime:
    """Last broadcast ``cursor_time_changed`` payload and its origin.

    ``t_fs`` stays the on-wire decimal string to avoid JSON number precision loss.
    """

    t_fs: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class WaveScope:
    """Last broadcast ``scope_changed`` payload and its origin."""

    wave_scope: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class GraphFocus:
    """Last broadcast ``graph_focus`` payload and its origin.

    Replayed on registration so a pane opened after ``rb hub send graph-focus`` starts focused.
    """

    node: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class CovFocus:
    """Last broadcast ``cov_focus`` payload and its origin.

    Replayed on registration like :class:`GraphFocus`. The optional ``metric``, ``line`` and ``item`` hints are kept so the replay lands on the same place as the original.
    """

    target: str
    origin: Origin
    metric: Optional[str] = None
    line: Optional[int] = None
    item: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        """The on-wire payload; unset hints are omitted, not null."""

        out: dict[str, Any] = {"target": self.target}
        if self.metric is not None:
            out["metric"] = self.metric
        if self.line is not None:
            out["line"] = self.line
        if self.item is not None:
            out["item"] = self.item
        return out


@dataclass(frozen=True, slots=True)
class PhysFocus:
    """Last broadcast ``phys_focus`` payload and its origin.

    Replayed on registration like :class:`GraphFocus`. The optional ``metric`` (cells, area, leakage, dynamic or total) is kept on replay.
    """

    target: str
    origin: Origin
    metric: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        """The on-wire payload; an unset metric is omitted, not null."""

        out: dict[str, Any] = {"target": self.target}
        if self.metric is not None:
            out["metric"] = self.metric
        return out


@dataclass(frozen=True, slots=True)
class DiagnosticsBundle:
    """Last ``diagnostics_set`` payload for one producer ``source``.

    ``items`` holds the raw on-wire items for verbatim replay. An empty tuple records that the source was cleared.
    """

    items: tuple[dict[str, Any], ...]
    origin: Origin


@dataclass
class HubState:
    """One-slot cache per coordinate type.

    Not thread-safe; the server's single writer task replaces fields wholesale.
    """

    selection: Optional[Selection] = None
    signal_selection: Optional[SignalSelection] = None
    cursor_time: Optional[CursorTime] = None
    wave_scope: Optional[WaveScope] = None
    graph_focus: Optional[GraphFocus] = None
    cov_focus: Optional[CovFocus] = None
    phys_focus: Optional[PhysFocus] = None
    diagnostics: dict[str, DiagnosticsBundle] = field(default_factory=dict)

    registered_clients: set[Origin] = field(default_factory=set)

    active_model: Optional[str] = None
    """Active model name, or ``None`` when no model has been selected.

    Owned by :class:`ViewerHTTP`; mirrored here for ``state_snapshot``."""

    def reset(self) -> None:
        """Clear all cached slots except ``registered_clients`` and ``active_model``."""

        self.selection = None
        self.signal_selection = None
        self.cursor_time = None
        self.wave_scope = None
        self.graph_focus = None
        self.cov_focus = None
        self.phys_focus = None
        self.diagnostics = {}


__all__ = [
    "Selection",
    "SignalSelection",
    "CursorTime",
    "WaveScope",
    "GraphFocus",
    "CovFocus",
    "PhysFocus",
    "DiagnosticsBundle",
    "HubState",
]
