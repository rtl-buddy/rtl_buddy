"""Coordinate translator between ``view.json`` instance paths, wave paths, source anchors and signals.

Implements §1 of the protocol spec: ``view`` (``top.u_fifo.u_wr_ptr``), ``wave`` (``tb.dut.u_fifo.u_wr_ptr``), ``src`` (``file, line, col``) and ``signal`` (``wr_ptr_q``). The view side comes from ``view.json`` as emitted by ``rtl-buddy-view --format json``. Nothing here touches the network.
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
    """Raised for unrecoverable ``view.json`` load errors.

    Failed lookups return ``None`` or an empty tuple instead.
    """


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """A ``view.json`` source range.

    :meth:`as_payload` gives the start point in wire shape; :meth:`contains` tests a point against the range.
    """

    file: str
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None

    def as_payload(self) -> dict[str, object]:
        """Return the ``{file, line, col}`` wire payload for the start point."""
        return {"file": self.file, "line": self.line, "col": self.col}

    def contains(self, *, line: int, col: int | None = None) -> bool:
        """Return whether ``(line, col)`` is inside the range.

        Multi-line ranges match on line alone, so a cursor at the indentation of an instantiation still matches. Single-line ranges also compare the column when both ``col`` and ``end_col`` are known.
        """
        if self.end_line is None:
            return line == self.line
        if not (self.line <= line <= self.end_line):
            return False
        if self.end_line != self.line:
            return True
        if col is None or self.end_col is None:
            return True
        return self.col <= col <= self.end_col

    def range_size(self) -> int:
        """Return the number of lines covered; smaller ranges are more specific."""
        if self.end_line is None:
            return 1
        return max(1, self.end_line - self.line + 1)


@dataclass(frozen=True, slots=True)
class SignalDriver:
    """One driver of a signal as found by :meth:`Resolver.signal_drivers`."""

    instance_path: str
    port: str


@dataclass
class Node:
    """A ``nodes[]`` entry, reduced to the fields the resolver uses."""

    instance_path: str
    location: SourceAnchor | None = None
    port_connections: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """``(port_name, net_expr_text)`` pairs in ``view.json`` order."""


@dataclass
class ViewModel:
    """In-memory image of ``view.json``."""

    top: str
    nodes_by_path: dict[str, Node]
    edges_parent_to_children: dict[str, tuple[str, ...]]
    source_path: Path | None = None
    source_mtime_ns: int | None = None
    # tb_top is set (view.json v1.1) when the view was rendered from the testbench top.
    tb_top: str | None = None
    dut_top: str | None = None

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: Path | None = None) -> "ViewModel":
        # A non-object top level must raise ResolverError, not AttributeError on .get.
        if not isinstance(raw, dict):
            raise ResolverError(
                f"view.json top level is {type(raw).__name__}, not an object"
            )
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

        # Accepts `top` at the root or the older `design.top`.
        top = raw.get("top")
        if not (isinstance(top, str) and top):
            top = (
                raw.get("design", {}).get("top")
                if isinstance(raw.get("design"), dict)
                else None
            )
        if not isinstance(top, str) or not top:
            raise ResolverError("view.json missing top (or design.top)")

        # Optional v1.1 fields; a wrong type is treated as absent.
        tb_top_raw = raw.get("tb_top")
        tb_top = tb_top_raw if isinstance(tb_top_raw, str) and tb_top_raw else None
        dut_top_raw = raw.get("dut_top")
        dut_top = dut_top_raw if isinstance(dut_top_raw, str) and dut_top_raw else None

        # Accepts `id` + `source` or the older `instance_path` + `location`.
        nodes_by_path: dict[str, Node] = {}
        for entry in raw.get("nodes", []):
            ip = entry.get("id")
            if not (isinstance(ip, str) and ip):
                ip = entry.get("instance_path")
            if not isinstance(ip, str) or not ip:
                continue
            loc = _location_to_anchor(entry.get("source") or entry.get("location"))
            ports: list[tuple[str, str]] = []
            # `ports` is {name, expr}; the older `port_connections` is {port_name, net_expr_text}.
            ports_raw = entry.get("ports")
            if isinstance(ports_raw, list):
                for p in ports_raw:
                    if not isinstance(p, dict):
                        continue
                    pn = p.get("name")
                    ne = p.get("expr")
                    if isinstance(pn, str) and isinstance(ne, str):
                        ports.append((pn, ne))
            else:
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

        # Accepts {from, to} or the older {parent, child}.
        edges: dict[str, list[str]] = {}
        for e in raw.get("edges", []):
            if not isinstance(e, dict):
                continue
            parent = e.get("from")
            child = e.get("to")
            if not (isinstance(parent, str) and isinstance(child, str)):
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
            tb_top=tb_top,
            dut_top=dut_top,
        )

    @property
    def tb_rooted(self) -> bool:
        """Return whether the view is rooted at the testbench top, so view paths equal wave paths."""
        return self.tb_top is not None and self.top == self.tb_top


def _location_to_anchor(raw: object) -> SourceAnchor | None:
    """Convert a ``view.json`` source block to a :class:`SourceAnchor`, or ``None`` if malformed.

    Accepts ``start_line``/``start_column`` or the older ``line``/``col``.
    """
    if not isinstance(raw, dict):
        return None
    file = raw.get("file")
    line = raw.get("start_line", raw.get("line"))
    col = raw.get("start_column", raw.get("col"))
    end_line = raw.get("end_line")
    end_col = raw.get("end_column")
    if (
        not isinstance(file, str)
        or not isinstance(line, int)
        or not isinstance(col, int)
    ):
        return None
    return SourceAnchor(
        file=file,
        line=line,
        col=col,
        end_line=end_line if isinstance(end_line, int) else None,
        end_col=end_col if isinstance(end_col, int) else None,
    )


class Resolver:
    """Coordinate translator backed by ``view.json``.

    The file is reloaded when its mtime changes. A lock makes lookups thread-safe. Use :meth:`update_mapping` to change ``tb_prefix`` or ``signal_aliases``.
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

    def view_to_wave(self, instance_path: str) -> str | None:
        """Return the wave path for a view instance path, or ``None``.

        Signal aliases are checked first. Then, if ``view.json`` is loaded, ``None`` is returned for an unknown path. A TB-rooted view maps a path to itself; otherwise the design top is stripped and ``tb_prefix`` prepended. With no ``view.json``, the prefix mapping is applied without checking the path.
        """

        if instance_path in self._view_alias_to_wave:
            return self._view_alias_to_wave[instance_path]

        model = self._load_if_possible()
        if model is not None and model.tb_rooted:
            return instance_path if instance_path in model.nodes_by_path else None

        if model is not None:
            if instance_path not in model.nodes_by_path:
                return None
            stripped = _strip_top(instance_path, model.top)
        else:
            stripped = instance_path

        prefix = self._mapping.tb_prefix
        if not prefix:
            return stripped
        return prefix + stripped

    def wave_to_view(self, wave_scope: str) -> str | None:
        """Return the view instance path for a wave path, or ``None``.

        Signal aliases are checked first. A TB-rooted view maps a path to itself if the node exists; otherwise ``tb_prefix`` is stripped and the design top prepended. With no ``view.json``, the prefix is stripped without checking the result.
        """

        if wave_scope in self._wave_alias_to_view:
            return self._wave_alias_to_view[wave_scope]

        model = self._load_if_possible()
        if model is not None and model.tb_rooted:
            return wave_scope if wave_scope in model.nodes_by_path else None

        prefix = self._mapping.tb_prefix
        if prefix and wave_scope.startswith(prefix):
            tail = wave_scope[len(prefix) :]
        elif not prefix:
            tail = wave_scope
        else:
            return None

        if model is None:
            return tail

        candidate = tail
        if not candidate.startswith(model.top + "."):
            candidate = f"{model.top}.{candidate}" if candidate else model.top

        if candidate in model.nodes_by_path:
            return candidate
        return None

    def view_to_src(self, instance_path: str) -> SourceAnchor | None:
        """Return the source anchor of an instance path, or ``None`` if unknown."""
        model = self._load_if_possible()
        if model is None:
            return None
        node = model.nodes_by_path.get(instance_path)
        if node is None:
            return None
        return node.location

    def src_to_view(
        self, *, file: str, line: int, col: int | None = None
    ) -> tuple[str, ...]:
        """Return the instance paths whose source range contains the point, smallest range first.

        File paths are compared after ``Path.resolve``, so relative and absolute spellings match. Nested instances all match; the first result is the most specific.
        """
        model = self._load_if_possible()
        if model is None:
            return ()

        target = self._normalise_path(file)
        if target is None:
            return ()

        matches: list[tuple[int, str]] = []
        for ip, node in model.nodes_by_path.items():
            anchor = node.location
            if anchor is None:
                continue
            if self._normalise_path(anchor.file) != target:
                continue
            if not anchor.contains(line=line, col=col):
                continue
            matches.append((anchor.range_size(), ip))

        matches.sort()
        return tuple(ip for _size, ip in matches)

    @staticmethod
    def _normalise_path(p: str) -> str | None:
        try:
            return str(Path(p).resolve())
        except (OSError, RuntimeError):
            return None

    def signal_drivers(
        self, *, signal: str, wave_scope: str
    ) -> tuple[SignalDriver, ...]:
        """Return the child instances of the node at ``wave_scope`` that have a port connected to ``signal``.

        The match is an exact string comparison against the port's net expression. An empty tuple means unresolvable (§7).
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
    """Remove a leading ``top.`` (or a bare ``top``) from a view path."""

    if instance_path == top:
        return ""
    prefix = top + "."
    if instance_path.startswith(prefix):
        return instance_path[len(prefix) :]
    return instance_path


def default_view_json_path(project_root: Path) -> Path:
    """Return the default ``view.json`` path, ``<project_root>/.rtl-buddy/view.json``."""

    return project_root / ".rtl-buddy" / "view.json"


def resolver_from_paths(
    *,
    view_json_path: Path | None,
    tb_prefix: str = "tb.dut.",
    signal_aliases: Iterable[SignalAlias] = (),
) -> Resolver:
    """Build a :class:`Resolver` from a path, ``tb_prefix`` and aliases."""
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
