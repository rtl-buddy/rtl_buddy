"""Coordinate translator for the rtl-buddy-hub.

Implements §1 of the protocol spec: the hub's only real intelligence is
mapping between three coordinate systems plus a derived fourth.

* **view**   ``top.u_fifo.u_wr_ptr``      — node in ``view.json``
* **wave**   ``tb.dut.u_fifo.u_wr_ptr``  — surfer / WCP path
* **src**    ``(file, line, col)``       — source anchor
* **signal** ``wr_ptr_q``                — flat signal name in surfer

The four mappings used at the wire layer:

* ``view ↔ wave``   — testbench-prefix strip + per-instance aliases.
* ``view → src``    — `nodes[].location` lookup.
* ``signal → view`` — walk the ``port_connections`` of the node at the
  given ``wave_scope`` and return every child whose port net matches
  the signal name.

Source of view-side truth is ``view.json`` as emitted by
``rtl-buddy-view --format json`` (rtl-buddy/rtl-buddy-view). The
contract is pinned by ``schema_version`` + ``JSON_CONTRACT`` upstream;
this loader is strict about the fields it consumes and tolerant about
the rest.

Lookup pieces in this module are pure functions / dataclasses; the
hub's server layer wraps them into request handlers so the resolver
itself never touches the network.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..logging_utils import log_event
from .config import HubMappingConfig, SignalAlias


logger = logging.getLogger(__name__)


SUPPORTED_VIEW_SCHEMA_MAJOR = 1


class ResolverError(Exception):
    """Raised for unrecoverable load-time errors.

    Per-request resolution failures use sentinel return values (``None``
    / empty lists) — the server layer translates those into
    ``error{code: "unresolvable"}`` envelopes.
    """


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """A ``view.json`` ``location`` value — translated to wire shape.

    The wire protocol's ``open_source`` / ``source_focused`` payloads
    are ``{file, line, col}``; ``view.json`` uses ``start_line`` /
    ``start_column`` (with end positions too). This dataclass holds
    the canonical wire shape so the server doesn't have to translate
    on every request.
    """

    file: str
    line: int
    col: int

    def as_payload(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "col": self.col}


@dataclass(frozen=True, slots=True)
class SignalDriver:
    """One driver of a signal as found by :meth:`Resolver.signal_drivers`."""

    instance_path: str
    port: str


@dataclass
class Node:
    """A single ``nodes[]`` entry — only the fields the resolver uses."""

    instance_path: str
    location: SourceAnchor | None = None
    port_connections: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """``(port_name, net_expr_text)`` pairs in the order view.json emits them."""


@dataclass
class ViewModel:
    """In-memory image of ``view.json`` — keyed for O(1) lookups."""

    top: str
    nodes_by_path: dict[str, Node]
    edges_parent_to_children: dict[str, tuple[str, ...]]
    source_path: Path | None = None
    source_mtime_ns: int | None = None

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: Path | None = None) -> "ViewModel":
        schema = raw.get("schema_version", "")
        try:
            major = int(str(schema).split(".", 1)[0])
        except ValueError as exc:
            raise ResolverError(
                f"view.json schema_version unparseable: {schema!r}"
            ) from exc
        if major != SUPPORTED_VIEW_SCHEMA_MAJOR:
            raise ResolverError(
                f"view.json schema major {major} not supported "
                f"(expected {SUPPORTED_VIEW_SCHEMA_MAJOR})"
            )

        top = raw.get("design", {}).get("top")
        if not isinstance(top, str) or not top:
            raise ResolverError("view.json missing design.top")

        nodes_by_path: dict[str, Node] = {}
        for entry in raw.get("nodes", []):
            ip = entry.get("instance_path")
            if not isinstance(ip, str) or not ip:
                continue
            loc = _location_to_anchor(entry.get("location"))
            ports: list[tuple[str, str]] = []
            for pc in entry.get("port_connections", []):
                if not isinstance(pc, dict):
                    continue
                pn = pc.get("port_name")
                ne = pc.get("net_expr_text")
                if isinstance(pn, str) and isinstance(ne, str):
                    ports.append((pn, ne))
            nodes_by_path[ip] = Node(
                instance_path=ip, location=loc, port_connections=tuple(ports)
            )

        edges: dict[str, list[str]] = {}
        for e in raw.get("edges", []):
            parent = e.get("parent")
            child = e.get("child")
            if not (isinstance(parent, str) and isinstance(child, str)):
                continue
            edges.setdefault(parent, []).append(child)
        edges_frozen = {k: tuple(v) for k, v in edges.items()}

        return cls(
            top=top,
            nodes_by_path=nodes_by_path,
            edges_parent_to_children=edges_frozen,
            source_path=source_path,
        )


def _location_to_anchor(raw: object) -> SourceAnchor | None:
    if not isinstance(raw, dict):
        return None
    file = raw.get("file")
    line = raw.get("start_line", raw.get("line"))
    col = raw.get("start_column", raw.get("col"))
    if (
        not isinstance(file, str)
        or not isinstance(line, int)
        or not isinstance(col, int)
    ):
        return None
    return SourceAnchor(file=file, line=line, col=col)


class Resolver:
    """Coordinate translator backed by a ``view.json`` snapshot.

    Thread-safe for read paths via a lock around lazy reloads. The
    resolver lazily reloads view.json when the file's ``mtime`` changes
    so a re-run of ``rb hier --format json`` mid-session is picked up
    without restarting the hub.

    Mapping config — ``tb_prefix``, ``signal_aliases`` — is held by
    reference; rotate it in place from the server layer when config is
    reloaded.
    """

    def __init__(
        self,
        *,
        view_json_path: Path | None,
        mapping: HubMappingConfig,
    ) -> None:
        self._view_json_path = view_json_path
        self._mapping = mapping
        self._model: ViewModel | None = None
        self._lock = threading.Lock()
        self._wave_alias_to_view: dict[str, str] = {}
        self._view_alias_to_wave: dict[str, str] = {}
        self._rebuild_alias_index()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @property
    def view_json_path(self) -> Path | None:
        return self._view_json_path

    @property
    def mapping(self) -> HubMappingConfig:
        return self._mapping

    def update_mapping(self, mapping: HubMappingConfig) -> None:
        with self._lock:
            self._mapping = mapping
            self._rebuild_alias_index()

    def update_view_json_path(self, path: Path | None) -> None:
        with self._lock:
            self._view_json_path = path
            self._model = None

    def _rebuild_alias_index(self) -> None:
        self._wave_alias_to_view = {
            a.wave: a.view for a in self._mapping.signal_aliases
        }
        self._view_alias_to_wave = {
            a.view: a.wave for a in self._mapping.signal_aliases
        }

    # ------------------------------------------------------------------
    # view ↔ wave (pure transforms; no view.json required)
    # ------------------------------------------------------------------

    def view_to_wave(self, instance_path: str) -> str | None:
        """Return the wave path for an instance path, or ``None``.

        Returns ``None`` when ``instance_path`` does not exist in the
        loaded ``view.json`` — the spec requires that the hub does not
        guess. If view.json is absent, returns the prefix-transformed
        path optimistically; this is the "no resolver loaded" fallback
        the server's error handler exposes as unresolvable when the
        guess matters.
        """

        if instance_path in self._view_alias_to_wave:
            return self._view_alias_to_wave[instance_path]

        # Drop the design.top root if present, then prepend tb_prefix.
        model = self._load_if_possible()
        if model is not None:
            if instance_path not in model.nodes_by_path:
                return None
            stripped = _strip_top(instance_path, model.top)
        else:
            # Best-effort: no top to anchor against.
            stripped = instance_path

        prefix = self._mapping.tb_prefix
        if not prefix:
            return stripped
        return prefix + stripped

    def wave_to_view(self, wave_scope: str) -> str | None:
        """Return the view instance path for a wave path, or ``None``."""

        if wave_scope in self._wave_alias_to_view:
            return self._wave_alias_to_view[wave_scope]

        prefix = self._mapping.tb_prefix
        if prefix and wave_scope.startswith(prefix):
            tail = wave_scope[len(prefix) :]
        elif not prefix:
            tail = wave_scope
        else:
            return None

        model = self._load_if_possible()
        if model is None:
            return tail  # Best-effort.

        candidate = tail
        if not candidate.startswith(model.top + "."):
            candidate = f"{model.top}.{candidate}" if candidate else model.top

        if candidate in model.nodes_by_path:
            return candidate
        return None

    # ------------------------------------------------------------------
    # view → src
    # ------------------------------------------------------------------

    def view_to_src(self, instance_path: str) -> SourceAnchor | None:
        model = self._load_if_possible()
        if model is None:
            return None
        node = model.nodes_by_path.get(instance_path)
        if node is None:
            return None
        return node.location

    # ------------------------------------------------------------------
    # signal → drivers (the spec's resolve_signal_to_view)
    # ------------------------------------------------------------------

    def signal_drivers(
        self, *, signal: str, wave_scope: str
    ) -> tuple[SignalDriver, ...]:
        """Return the list of view instances that drive ``signal`` at ``wave_scope``.

        The spec (§7) requires this be a list — a bus driven by N flops
        in a generate, or a packed-array assignment, collapses to N
        instance paths. Empty tuple → "unresolvable".

        Implementation: walk the children of the node at ``wave_scope``
        and return every child whose ``port_connections`` carry a
        ``net_expr_text`` equal to ``signal``. Today's view.json is
        textual (``net_expr_text`` is a raw AST snippet), so this is an
        exact string match; richer port_pair data lands with Phase 4
        view.json v1.
        """

        model = self._load_if_possible()
        if model is None:
            return ()
        parent_view = self.wave_to_view(wave_scope)
        if parent_view is None:
            return ()
        parent = model.nodes_by_path.get(parent_view)
        if parent is None:
            return ()

        children = model.edges_parent_to_children.get(parent_view, ())
        drivers: list[SignalDriver] = []
        for child_path in children:
            child = model.nodes_by_path.get(child_path)
            if child is None:
                continue
            for port_name, net_expr in child.port_connections:
                if net_expr == signal:
                    drivers.append(
                        SignalDriver(instance_path=child_path, port=port_name)
                    )
                    break
        return tuple(drivers)

    # ------------------------------------------------------------------
    # internal: lazy view.json load
    # ------------------------------------------------------------------

    def _load_if_possible(self) -> ViewModel | None:
        with self._lock:
            path = self._view_json_path
            if path is None or not path.is_file():
                self._model = None
                return None
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                self._model = None
                return None

            cached = self._model
            if cached is not None and cached.source_mtime_ns == mtime_ns:
                return cached

            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "hub.resolver.view_json_unreadable",
                    path=str(path),
                    error=str(exc),
                )
                self._model = None
                return None

            try:
                model = ViewModel.from_dict(raw, source_path=path)
            except ResolverError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "hub.resolver.view_json_invalid",
                    path=str(path),
                    error=str(exc),
                )
                self._model = None
                return None

            model.source_mtime_ns = mtime_ns
            self._model = model
            log_event(
                logger,
                logging.INFO,
                "hub.resolver.view_json_loaded",
                path=str(path),
                top=model.top,
                nodes=len(model.nodes_by_path),
            )
            return model


def _strip_top(instance_path: str, top: str) -> str:
    """Drop the leading ``top.`` (or bare ``top``) anchor from a view path."""

    if instance_path == top:
        return ""
    prefix = top + "."
    if instance_path.startswith(prefix):
        return instance_path[len(prefix) :]
    return instance_path


def default_view_json_path(project_root: Path) -> Path:
    """Where the resolver looks for view.json inside ``project_root``."""

    return project_root / ".rtl-buddy" / "view.json"


# Convenience used by tests that want to skip the lazy-load dance.
def resolver_from_paths(
    *,
    view_json_path: Path | None,
    tb_prefix: str = "tb.dut.",
    signal_aliases: Iterable[SignalAlias] = (),
) -> Resolver:
    mapping = HubMappingConfig(
        tb_prefix=tb_prefix, signal_aliases=tuple(signal_aliases)
    )
    return Resolver(view_json_path=view_json_path, mapping=mapping)


__all__ = [
    "SUPPORTED_VIEW_SCHEMA_MAJOR",
    "Resolver",
    "ResolverError",
    "Node",
    "SignalDriver",
    "SourceAnchor",
    "ViewModel",
    "default_view_json_path",
    "resolver_from_paths",
]
