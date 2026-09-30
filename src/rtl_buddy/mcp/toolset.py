# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The tools ``rb mcp`` serves, and their handlers.

SDK-free: dataclasses, dicts and JSON Schema literals, so the tool set can be built and called without the ``mcp`` package. Every handler returns the payload of its ``rb --machine`` counterpart.

- Stateless tools are always present. They read ``artefacts/graph/graph.json`` and the results overlay, the coverage and physical manifests and their models, and call ``rtl-buddy-view`` for hierarchy queries.
- Hub tools are present only when a live hub is discovered through ``.rtl-buddy/hub.json``, as ``rb hub send`` does. They drive the session the user is looking at.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field as dc_field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from ..cov import query as cov_query
from ..errors import FatalRtlBuddyError
from ..graph import query as graph_query
from ..logging_utils import log_event
from ..phys import query as phys_query

logger = logging.getLogger(__name__)

#: Tool names always served, hub or no hub.
STATELESS_TOOL_NAMES = (
    "graph_status",
    "graph_query",
    "graph_path",
    "graph_explain",
    "test_status",
    "cov_summary",
    "cov_module",
    "phys_runs",
    "phys_summary",
    "phys_module",
    "phys_instance",
    "find_module",
    "instances_of",
    "port_connections",
    "source_snippet",
)

#: Tool names served only when a live hub was discovered.
HUB_TOOL_NAMES = (
    "hub_state",
    "hub_select",
    "hub_open_source",
    "hub_resolve",
    "hub_diagnose",
    "cov_focus",
    "phys_focus",
)

_SEVERITIES = ("error", "warning", "info", "hint")

#: ``cov_focus.metric`` enum; mirrors the hub wire schema and ``rb hub send cov-focus``.
_COV_METRICS = ("line", "branch", "toggle", "expression", "cover")

#: ``phys_focus.metric`` enum; mirrors the hub wire schema and ``rb hub send phys-focus``.
#: ``dynamic`` is internal + switching, summed by the pane; no model column has it.
_PHYS_METRICS = ("cells", "area", "leakage", "dynamic", "total")


def _tool_version() -> str:
    try:
        return version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


class ToolError(Exception):
    """A tool call that could not be answered.

    Reported to the agent as ``ok: false`` plus a message, not as a transport error.
    """

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: its schema and the callable behind it."""

    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[[dict], dict]
    #: Human-mode command this tool mirrors, reported in every result.
    command: str = ""

    def to_mcp_dict(self) -> dict:
        """The wire form, with camelCase keys."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class HubHandle:
    """What was discovered about a hub.

    Discovery runs once at server start. Each call opens and closes its own connection, as ``rb hub send`` does.
    """

    present: bool
    tcp: str | None = None
    pid: int | None = None
    server_version: str | None = None
    active_model: str | None = None
    reason: str | None = None

    def payload(self) -> dict:
        return {
            "present": self.present,
            "tcp": self.tcp,
            "pid": self.pid,
            "server_version": self.server_version,
            "active_model": self.active_model,
            "reason": self.reason,
        }


def discover_hub(project_root: Path) -> HubHandle:
    """Decide whether the hub tools are offered, using the resolver ``rb hub send`` uses.

    The hub record is read afterwards only for pid, hub version and active model.
    """
    from ..hub import client as hub_client
    from ..hub import discovery as hub_discovery

    try:
        addr = hub_client._discover_hub_addr(project_root=project_root)  # noqa: SLF001
    except Exception as exc:
        return HubHandle(present=False, reason=f"hub discovery failed: {exc}")
    if addr is None:
        return HubHandle(
            present=False,
            reason=(
                "no live hub for this project (no .rtl-buddy/hub.json and "
                "$RTL_BUDDY_HUB unset)"
            ),
        )

    handle = HubHandle(present=True, tcp=f"{addr[0]}:{addr[1]}")
    try:
        root = hub_discovery.find_project_root_with_hub(project_root)
        record = hub_discovery.read_record(root) if root is not None else None
    except Exception:  # pragma: no cover - unreadable record, addr still good
        record = None
    if record is not None:
        handle.pid = record.pid
        handle.server_version = record.server_version
        handle.active_model = record.active_model
    return handle


@dataclass
class Toolset:
    """The tools one ``rb mcp`` process serves.

    The graph and overlay are re-read on every call, so a rebuilt graph is visible without restarting the server.
    """

    project_root: Path
    graph_path: Path | None = None
    overlay_path: Path | None = None
    view_executable: str = "rtl-buddy-view"
    design_dir: Path | None = None
    frontend: str | None = None
    hub: HubHandle = dc_field(default_factory=lambda: HubHandle(present=False))
    _specs: dict[str, ToolSpec] = dc_field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """Every tool this process serves, in listing order."""
        return [self._specs[name] for name in self.names()]

    def names(self) -> list[str]:
        return [name for name in self._specs]

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolError(
                f"unknown tool {name!r}; available: {', '.join(self._specs)}"
            )

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Invoke a tool and wrap its payload in the result envelope.

        Bad questions (unknown module, missing graph, vanished hub) return ``ok: false`` with a message, never raise.
        """
        args = dict(arguments or {})
        try:
            spec = self.spec(name)
        except ToolError as exc:
            return self._envelope(name, "", ok=False, error=str(exc))
        log_event(logger, logging.INFO, "mcp.tool_call", tool=name)
        try:
            payload = spec.handler(args)
        except ToolError as exc:
            return self._envelope(
                name, spec.command, ok=False, error=str(exc), **exc.details
            )
        except (
            graph_query.GraphQueryError,
            cov_query.CovQueryError,
            phys_query.PhysQueryError,
        ) as exc:
            # Must precede FatalRtlBuddyError (the cov and phys errors subclass it) or 'candidates' is lost.
            details = {"candidates": exc.candidates} if exc.candidates else {}
            return self._envelope(
                name, spec.command, ok=False, error=str(exc), **details
            )
        except FatalRtlBuddyError as exc:
            return self._envelope(name, spec.command, ok=False, error=str(exc))
        return self._envelope(name, spec.command, ok=True, payload=payload)

    def _envelope(
        self,
        tool: str,
        command: str,
        *,
        ok: bool,
        payload: dict | None = None,
        error: str | None = None,
        **extra,
    ) -> dict:
        """The MCP result envelope: machine mode's, without argv and git.

        ``meta.rtl_buddy_version`` identifies which rtl_buddy produced the payload.
        """
        envelope: dict[str, Any] = {
            "tool": tool,
            "ok": ok,
            "meta": {
                "rtl_buddy_version": _tool_version(),
                "project_root": str(self.project_root),
                "command": command or None,
            },
        }
        if payload is not None:
            envelope["payload"] = payload
        if error is not None:
            envelope["error"] = error
        envelope.update(extra)
        return envelope

    # ------------------------------------------------------------------
    # graph handlers
    # ------------------------------------------------------------------

    def _context(self, args: dict) -> graph_query.GraphContext:
        return graph_query.load_context(
            self.project_root,
            graph_path=self.graph_path,
            overlay_path=self.overlay_path,
            with_results=bool(args.get("results", True)),
        )

    def _h_graph_status(self, args: dict) -> dict:
        """What this server can answer before anything is asked of it."""
        graph_file = graph_query.resolve_graph_path(self.project_root, self.graph_path)
        status: dict[str, Any] = {
            "project_root": str(self.project_root),
            "graph": str(graph_file),
            "graph_present": graph_file.is_file(),
            "hub": self.hub.payload(),
            "tools": self.names(),
            "rtl_buddy_version": _tool_version(),
            "view_executable": self.view_executable,
        }
        if graph_file.is_file():
            ctx = self._context({"results": True})
            status.update(ctx.envelope())
            status["overlay_present"] = ctx.overlay is not None
            types: dict[str, int] = {}
            for node in ctx.graph.get("nodes") or []:
                key = str(node.get("type", "unknown"))
                types[key] = types.get(key, 0) + 1
            status["node_types"] = dict(sorted(types.items()))
        else:
            status["hint"] = "run `rb graph build` to create the graph"
        return status

    def _h_graph_query(self, args: dict) -> dict:
        question = str(args.get("question") or "").strip()
        if not question:
            raise ToolError("graph_query: 'question' is required")
        ctx = self._context(args)
        return graph_query.query(
            ctx,
            question,
            node_type=args.get("type"),
            tier=args.get("tier"),
            limit=int(args.get("limit", graph_query.DEFAULT_LIMIT)),
            depth=int(args.get("depth", graph_query.DEFAULT_DEPTH)),
            max_neighbors=int(
                args.get("max_neighbors", graph_query.DEFAULT_MAX_NEIGHBORS)
            ),
            results=bool(args.get("results", True)),
            expand=bool(args.get("expand", False)),
        )

    def _h_graph_path(self, args: dict) -> dict:
        source = str(args.get("source") or "").strip()
        target = str(args.get("target") or "").strip()
        if not source or not target:
            raise ToolError("graph_path: 'source' and 'target' are both required")
        ctx = self._context(args)
        return graph_query.path(
            ctx,
            source,
            target,
            directed=bool(args.get("directed", False)),
            max_paths=int(args.get("max_paths", graph_query.DEFAULT_MAX_PATHS)),
            results=bool(args.get("results", True)),
        )

    def _h_graph_explain(self, args: dict) -> dict:
        node = str(args.get("node") or "").strip()
        if not node:
            raise ToolError("graph_explain: 'node' is required")
        ctx = self._context(args)
        return graph_query.explain(
            ctx,
            node,
            results=bool(args.get("results", True)),
            expand=bool(args.get("expand", False)),
        )

    def _h_test_status(self, args: dict) -> dict:
        ctx = self._context({"results": True})
        return graph_query.test_status(
            ctx, test=args.get("test"), status=args.get("status")
        )

    # ------------------------------------------------------------------
    # coverage handlers
    # ------------------------------------------------------------------

    def _rooted(self, value: str | None) -> str | None:
        """Anchor a relative path override on the project root, not the server's cwd."""
        if value is None:
            return None
        path = Path(value)
        return str(path if path.is_absolute() else self.project_root / path)

    def _cov_context(self, args: dict) -> cov_query.CovContext:
        """Load the manifest and model a coverage tool answers from, re-read per call."""
        return cov_query.load_context(
            self.project_root,
            cov_dir=self._rooted(self._path_arg(args, "cov_dir")),
            manifest=self._rooted(self._path_arg(args, "manifest")),
        )

    def _h_cov_summary(self, args: dict) -> dict:
        return cov_query.summary_payload(
            self._cov_context(args),
            limit=self._limit_arg(args, cov_query.DEFAULT_FILE_LIMIT),
        )

    def _h_cov_module(self, args: dict) -> dict:
        return cov_query.module_payload(
            self._cov_context(args), str(_req(args, "module"))
        )

    # ------------------------------------------------------------------
    # physical-metrics handlers
    # ------------------------------------------------------------------

    def _phys_context(self, args: dict) -> phys_query.PhysContext:
        """Load the manifest and model a physical tool answers from, re-read per call.

        Takes no artefact lock, so a question can be answered while a flow is running.
        """
        return phys_query.load_context(
            self.project_root,
            phys_dir=self._rooted(self._path_arg(args, "phys_dir")),
            manifest=self._rooted(self._path_arg(args, "manifest")),
        )

    def _h_phys_runs(self, args: dict) -> dict:
        """Every run with physical artefacts under the project.

        Takes no ``phys_dir``: it lists the runs the other tools take one from.
        """
        return phys_query.runs_payload(
            self.project_root,
            limit=self._limit_arg(args, phys_query.DEFAULT_RUNS_LIMIT),
        )

    def _h_phys_summary(self, args: dict) -> dict:
        return phys_query.summary_payload(
            self._phys_context(args),
            limit=self._limit_arg(args, phys_query.DEFAULT_RANK_LIMIT),
            modules_limit=self._rank_limit_arg(args, "modules_limit"),
            instances_limit=self._rank_limit_arg(args, "instances_limit"),
        )

    def _h_phys_module(self, args: dict) -> dict:
        return phys_query.module_payload(
            self._phys_context(args),
            str(_req(args, "module")),
            limit=self._limit_arg(args, phys_query.DEFAULT_RANK_LIMIT),
        )

    def _h_phys_instance(self, args: dict) -> dict:
        return phys_query.instance_payload(
            self._phys_context(args),
            str(_req(args, "path")),
            limit=self._limit_arg(args, phys_query.DEFAULT_RANK_LIMIT),
        )

    @staticmethod
    def _path_arg(args: dict, key: str) -> str | None:
        """A discovery override as a path string, ``None`` if absent, else a ``ToolError``.

        The schema's ``"type": "string"`` is not enforced on forwarded arguments, and a non-string would otherwise raise ``TypeError`` past the envelope.
        """
        value = args.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ToolError(
                f"{key} must be a path string, not {value!r}; "
                "omit it to read the newest run under the project root"
            )
        return value

    @staticmethod
    def _limit_arg(args: dict, default: int, key: str = "limit") -> int:
        """The row cap a read tool applies, or a ``ToolError``.

        ``default`` is the calling tool's; by default a list is headed, and ``0`` means every row. Non-integers, booleans, non-integral floats and negative values are refused, because ``int()`` would coerce them to a number the caller did not ask for (``false`` to 0, ``-0.5`` to 0, and negatives are read as "no cap" downstream). Integral floats such as ``2.0`` and decimal strings are accepted.
        """
        raw = args.get(key, default)

        def not_an_integer() -> ToolError:
            return ToolError(
                f"{key} must be an integer, not {raw!r}; "
                "0 lists every row and a positive number heads the list"
            )

        # `int(False)` is 0, which means every row.
        if isinstance(raw, bool):
            raise not_an_integer()
        # `int()` truncates 2.7 to 2 and -0.5 to 0; `is_integer()` also rejects nan and inf.
        if isinstance(raw, float):
            if not raw.is_integer():
                raise not_an_integer()
            limit = int(raw)
        else:
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                raise not_an_integer() from None
        if limit < 0:
            raise ToolError(
                f"{key} must be 0 or greater, not {limit}; "
                "0 lists every row and a positive number heads the list"
            )
        return limit

    def _rank_limit_arg(self, args: dict, key: str) -> phys_query.RankLimit:
        """A ``phys_summary`` per-ranking override, or ``None`` when not given.

        ``RANK_NONE`` means no rows; other values are validated by :meth:`_limit_arg` under this key's name.
        """
        if key not in args:
            return None
        raw = args[key]
        if isinstance(raw, str) and raw.strip().lower() == phys_query.RANK_NONE:
            return phys_query.RANK_NONE
        return self._limit_arg(args, phys_query.DEFAULT_RANK_LIMIT, key)

    # ------------------------------------------------------------------
    # hierarchy handlers (rtl-buddy-view, subprocess)
    # ------------------------------------------------------------------

    def _model_cfg(self, name: str):
        """Resolve a model by name across the design tree, as ``rb graph build`` does."""
        from ..graph.build import models_from_design_tree

        design_dir = self.design_dir or (self.project_root / "design")
        by_name = {}
        for cfg in models_from_design_tree(design_dir):
            by_name.setdefault(cfg.name, cfg)
        if name not in by_name:
            raise ToolError(
                f"unknown model {name!r}; models declared under {design_dir}: "
                f"{', '.join(sorted(by_name)) or '(none)'}",
                details={"models": sorted(by_name)},
            )
        return by_name[name]

    def _view_query(self, verb: str, model: str, arg: str, **kwargs) -> dict:
        from ..tools.hier_rtl_buddy_view import RtlBuddyViewQuery

        runner = RtlBuddyViewQuery(
            name="rb mcp/hier-query",
            model_cfg=self._model_cfg(model),
            suite_dir=str(self.project_root),
            verb=verb,
            arg=arg,
            frontend=self.frontend,
            executable=self.view_executable,
            capture=True,
            **kwargs,
        )
        returncode = runner.run()
        stdout = (runner.stdout or "").strip()
        stderr = (runner.stderr or "").strip()
        payload: dict[str, Any] = {
            "command": f"rb hier-query {model} {verb} {arg}",
            "model": model,
            "verb": verb,
            "arg": arg,
            "exit_code": returncode,
        }
        if returncode != 0:
            # A lookup miss exits non-zero with the answer on stderr.
            raise ToolError(
                stderr or f"rtl-buddy-view query {verb} failed with {returncode}",
                details={"payload": payload},
            )
        parsed = _maybe_json(stdout)
        if parsed is None:
            payload["text"] = stdout
        else:
            payload["result"] = parsed
        if stderr:
            payload["stderr"] = stderr
        return payload

    def _h_find_module(self, args: dict) -> dict:
        return self._view_query(
            "find-module", _req(args, "model"), _req(args, "module")
        )

    def _h_instances_of(self, args: dict) -> dict:
        return self._view_query(
            "instances-of", _req(args, "model"), _req(args, "module")
        )

    def _h_port_connections(self, args: dict) -> dict:
        return self._view_query(
            "port-connections", _req(args, "model"), _req(args, "instance_path")
        )

    def _h_source_snippet(self, args: dict) -> dict:
        return self._view_query(
            "source-snippet",
            _req(args, "model"),
            _req(args, "instance_path"),
            context=args.get("context"),
            line_numbers=bool(args.get("line_numbers", True)),
        )

    # ------------------------------------------------------------------
    # hub handlers
    # ------------------------------------------------------------------

    def _hub_client(self):
        from ..hub.client import HubClient, HubClientError, HubUnavailable
        from ..hub.protocol import Origin

        try:
            # ``cli`` is the only origin the hub schema accepts for this peer.
            return HubClient.connect(project_root=self.project_root, origin=Origin.CLI)
        except HubUnavailable as exc:
            raise ToolError(f"no hub: {exc}")
        except HubClientError as exc:
            raise ToolError(f"hub connection failed: {exc}")

    def _hub_request(self, type_: str, payload: dict) -> dict:
        from ..hub.protocol import Kind

        with self._hub_client() as client:
            env = client.request(type_, payload)
        body = env.payload if isinstance(env.payload, dict) else {}
        if env.kind is Kind.ERROR:
            raise ToolError(
                f"hub error: {body.get('code')}: {body.get('message')}",
                details={"payload": {"request": type_, "hub_error": body}},
            )
        return {"request": type_, "result": body}

    def _hub_emit(self, type_: str, payload: dict) -> dict:
        with self._hub_client() as client:
            client.emit(type_, payload)
        return {"event": type_, "payload": payload, "delivered": True}

    def _h_hub_state(self, args: dict) -> dict:
        return self._hub_request("state_snapshot", {})

    def _h_hub_select(self, args: dict) -> dict:
        return self._hub_emit(
            "selection_changed", {"instance_path": _req(args, "instance_path")}
        )

    def _h_hub_open_source(self, args: dict) -> dict:
        return self._hub_request(
            "open_source",
            {
                "file": _req(args, "file"),
                "line": int(args.get("line", 1)),
                "col": int(args.get("col", 1)),
            },
        )

    def _h_hub_resolve(self, args: dict) -> dict:
        kind = _req(args, "kind")
        if kind == "view-to-wave":
            return self._hub_request(
                "resolve_view_to_wave", {"instance_path": _req(args, "instance_path")}
            )
        if kind == "wave-to-view":
            return self._hub_request(
                "resolve_wave_to_view", {"wave_scope": _req(args, "wave_scope")}
            )
        if kind == "signal-to-view":
            return self._hub_request(
                "resolve_signal_to_view",
                {
                    "signal": _req(args, "signal"),
                    "wave_scope": _req(args, "wave_scope"),
                },
            )
        raise ToolError(
            f"hub_resolve: unknown kind {kind!r}; expected view-to-wave, "
            f"wave-to-view or signal-to-view"
        )

    def _h_hub_diagnose(self, args: dict) -> dict:
        source = _req(args, "source")
        raw_items = args.get("items") or []
        clear = bool(args.get("clear", False))
        if clear and raw_items:
            raise ToolError("hub_diagnose: 'clear' is incompatible with 'items'")
        if not clear and not raw_items:
            raise ToolError("hub_diagnose: provide 'items', or set 'clear': true")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ToolError("hub_diagnose: each item must be an object")
            severity = str(raw.get("severity", "warning"))
            if severity not in _SEVERITIES:
                raise ToolError(
                    f"hub_diagnose: severity must be one of "
                    f"{'/'.join(_SEVERITIES)}, got {severity!r}"
                )
            item: dict[str, Any] = {
                "file": str(_req(raw, "file")),
                "line": int(_req(raw, "line")),
                "col": int(raw.get("col", 1)),
                "severity": severity,
                "code": str(raw.get("code", "rtl-buddy-mcp")),
                "message": str(_req(raw, "message")),
            }
            if raw.get("instance_path"):
                item["instance_path"] = str(raw["instance_path"])
            elif args.get("instance_path"):
                item["instance_path"] = str(args["instance_path"])
            items.append(item)
        return self._hub_emit("diagnostics_set", {"source": source, "items": items})

    def _h_cov_focus(self, args: dict) -> dict:
        # Omit optional keys: the wire schema rejects null.
        payload: dict[str, Any] = {"target": _focus_target("cov_focus", args)}
        metric = args.get("metric")
        if metric is not None:
            if metric not in _COV_METRICS:
                raise ToolError(
                    f"cov_focus: metric must be one of "
                    f"{'/'.join(_COV_METRICS)}, got {metric!r}"
                )
            payload["metric"] = metric
        line = args.get("line")
        if line is not None:
            line = int(line)
            if line < 1:
                raise ToolError(f"cov_focus: 'line' is 1-based, got {line}")
            payload["line"] = line
        item = args.get("item")
        if item is not None:
            # Send the stripped value; the pane matches exactly.
            item = str(item).strip()
            if not item:
                raise ToolError("cov_focus: 'item' must be non-empty")
            payload["item"] = item
        return self._hub_emit("cov_focus", payload)

    def _h_phys_focus(self, args: dict) -> dict:
        # Stripped target; optional key omitted, not null (as ``rb hub send phys-focus``).
        payload: dict[str, Any] = {"target": _focus_target("phys_focus", args)}
        metric = args.get("metric")
        if metric is not None:
            if metric not in _PHYS_METRICS:
                raise ToolError(
                    f"phys_focus: metric must be one of "
                    f"{'/'.join(_PHYS_METRICS)}, got {metric!r}"
                )
            payload["metric"] = metric
        return self._hub_emit("phys_focus", payload)


def _focus_target(tool: str, args: dict) -> str:
    """The ``target`` of a focus tool: a non-blank string, stripped.

    Non-strings are refused, because ``str()`` would turn ``false`` or ``[]`` into a target the hub caches and replays to every later pane.
    """
    value = _req(args, "target")
    if not isinstance(value, str):
        raise ToolError(
            f"{tool}: 'target' must be a string naming a module or an "
            f"instance path, not {value!r}"
        )
    return value.strip()


def _req(args: dict, key: str):
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolError(f"missing required argument {key!r}")
    return value


def _maybe_json(text: str):
    """Parse the viewer's stdout as JSON, or return ``None`` so it is kept as text.

    ``source-snippet`` prints numbered source; other verbs print JSON.
    """
    import json

    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_RESULTS_PROP = {
    "type": "boolean",
    "description": (
        "Join the regression-results overlay onto every node in the answer "
        "(default true). Set false for a pure structural answer."
    ),
}

_EXPAND_PROP = {
    "type": "boolean",
    "description": (
        "Embed a full node summary (attributes, cite, joins) for every "
        "peer instead of the lean id/label/type reference (default false). "
        "Large; prefer a second lean call on the one peer you need."
    ),
}

_COV_DIR_PROP = {
    "type": "string",
    "description": (
        "Coverage artefact directory to read; a relative path resolves "
        "against the project root, e.g. verif/blk_a/cov_dir. Default: the "
        "newest cov_dir/manifest.json under the project root."
    ),
}

_COV_MANIFEST_PROP = {
    "type": "string",
    "description": (
        "A manifest.json to read directly, bypassing discovery; a relative "
        "path resolves against the project root, e.g. "
        "verif/blk_a/cov_dir/manifest.json."
    ),
}

_PHYS_DIR_PROP = {
    "type": "string",
    "description": (
        "Artefact directory holding phys-manifest.json; a relative path "
        "resolves against the project root, e.g. "
        "verif/blk/artefacts/nightly. Default: the newest "
        "phys-manifest.json under the project root."
    ),
}

_PHYS_MANIFEST_PROP = {
    "type": "string",
    "description": (
        "A phys-manifest.json to read directly, bypassing discovery; a "
        "relative path resolves against the project root, e.g. "
        "verif/blk/artefacts/nightly/phys-manifest.json."
    ),
}


def _phys_limit_prop(rows: str) -> dict:
    """The ``limit`` input of a physical tool, worded for its own list; same semantics as the CLI ``--limit``."""
    return {
        "type": "integer",
        "description": (
            f"{rows}, heaviest/hottest first (default "
            f"{phys_query.DEFAULT_RANK_LIMIT}; 0 for all). The payload "
            "reports the applied limit and the untruncated count."
        ),
        "minimum": 0,
    }


def _phys_rank_limit_prop(rows: str) -> dict:
    """A ``phys_summary`` per-ranking limit override; ``"none"`` asks for an empty list, which ``0`` (all) cannot say."""
    return {
        "anyOf": [
            {"type": "integer", "minimum": 0},
            {"type": "string", "enum": [phys_query.RANK_NONE]},
        ],
        "description": (
            f"{rows}, overriding 'limit' for this ranking only (0 for all, "
            f"'{phys_query.RANK_NONE}' for no rows at all; omit to follow "
            "'limit'). 'none' skips a mapped design's six-figure instance "
            "list; the payload's 'counts' still reports its size and "
            "'limits' the applied values."
        ),
    }


def build_toolset(
    project_root: str | os.PathLike,
    *,
    graph_path: str | os.PathLike | None = None,
    overlay_path: str | os.PathLike | None = None,
    view_executable: str = "rtl-buddy-view",
    design_dir: str | os.PathLike | None = None,
    frontend: str | None = None,
    hub: HubHandle | None = None,
) -> Toolset:
    """Assemble the tool set for one project root.

    ``hub`` is discovered unless supplied. Hub tools are registered only when a hub is live.
    """
    root = Path(os.path.realpath(str(project_root)))
    handle = hub if hub is not None else discover_hub(root)
    ts = Toolset(
        project_root=root,
        graph_path=Path(graph_path) if graph_path is not None else None,
        overlay_path=Path(overlay_path) if overlay_path is not None else None,
        view_executable=view_executable,
        design_dir=Path(design_dir) if design_dir is not None else None,
        frontend=frontend,
        hub=handle,
    )

    def register(spec: ToolSpec) -> None:
        ts._specs[spec.name] = spec

    register(
        ToolSpec(
            name="graph_status",
            title="Graph status",
            command="rb graph build",
            description=(
                "What this server can answer: whether artefacts/graph/graph.json "
                "exists, its node and link counts by type, whether a results "
                "overlay is present, and whether a live rtl-buddy hub was "
                "discovered. Call this first when a graph tool returns an error."
            ),
            input_schema=_obj({}),
            handler=ts._h_graph_status,
        )
    )
    register(
        ToolSpec(
            name="graph_query",
            title="Query the design knowledge graph",
            command="rb graph query",
            description=(
                "Keyword search over the design knowledge graph, expanding the "
                "neighbourhood of each match. Use it first to locate anything: modules, "
                "instances, ports, tests, testbenches, models, spec blocks and coverage "
                "items share one graph. Scoring is deterministic keyword matching, so "
                "phrase the question around an identifier ('which tests cover A-COV-1'). "
                "Each match carries its attributes, a 'cite' hint naming the file (for "
                "instances, the hier-query command) that quotes it, and its neighbours as "
                "lean id/label/type references with last regression status; set "
                "'expand' for full neighbour summaries."
            ),
            input_schema=_obj(
                {
                    "question": {
                        "type": "string",
                        "description": (
                            "The question or identifier to search for, e.g. "
                            "'A-COV-1', 'which tests exercise blk_a', 'fifo'."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "Restrict to one node type (module, instance, port, "
                            "test, testbench, model, spec_block, coverage_item, "
                            "suite, spec_doc, golden_model, python_module)."
                        ),
                    },
                    "tier": {
                        "type": "string",
                        "description": "Restrict to one tier: design, config or binding.",
                        "enum": ["design", "config", "binding"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum matches to return (default 10).",
                        "minimum": 1,
                    },
                    "depth": {
                        "type": "integer",
                        "description": (
                            "Hops of neighbourhood expansion per match "
                            f"(default 1, maximum {graph_query.MAX_DEPTH})."
                        ),
                        "minimum": 0,
                        "maximum": graph_query.MAX_DEPTH,
                    },
                    "max_neighbors": {
                        "type": "integer",
                        "description": (
                            "Neighbours reported per match (default "
                            f"{graph_query.DEFAULT_MAX_NEIGHBORS}); anything "
                            "beyond is counted in neighbors_truncated "
                            "(dropped count and kinds), never dropped silently."
                        ),
                        "minimum": 1,
                    },
                    "results": _RESULTS_PROP,
                    "expand": _EXPAND_PROP,
                },
                ["question"],
            ),
            handler=ts._h_graph_query,
        )
    )
    register(
        ToolSpec(
            name="graph_path",
            title="Path between two graph nodes",
            command="rb graph path",
            description=(
                "The shortest chain of edges between two nodes, e.g. how a test reaches "
                "a module or a coverage item reaches its spec doc. Accepts full node ids "
                "('test:verif/blk_a#t_cocotb') or unambiguous bare names. Undirected by "
                "default, since edge direction encodes role, not reachability."
            ),
            input_schema=_obj(
                {
                    "source": {
                        "type": "string",
                        "description": "Start node id or name.",
                    },
                    "target": {"type": "string", "description": "End node id or name."},
                    "directed": {
                        "type": "boolean",
                        "description": "Follow edge direction (default false).",
                    },
                    "max_paths": {
                        "type": "integer",
                        "description": "Shortest paths to return (default 3).",
                        "minimum": 1,
                    },
                    "results": _RESULTS_PROP,
                },
                ["source", "target"],
            ),
            handler=ts._h_graph_path,
        )
    )
    register(
        ToolSpec(
            name="graph_explain",
            title="Explain one graph node",
            command="rb graph explain",
            description=(
                "Everything the graph knows about one node: its attributes and every "
                "incoming and outgoing edge with the far endpoint named (peer "
                "id/label/type; set 'expand' for full peer summaries). A test node adds "
                "its last regression status, seed and artefact paths from the results "
                "overlay. When the overlay has a coverage join, a module or instance "
                "node also returns its coverage ratio, and a coverage_item node returns "
                "whether the run exercised it, the cover points it correlated with, and "
                "how the tests declaring it fared."
            ),
            input_schema=_obj(
                {
                    "node": {
                        "type": "string",
                        "description": "Node id, or a bare name if unambiguous.",
                    },
                    "results": _RESULTS_PROP,
                    "expand": _EXPAND_PROP,
                },
                ["node"],
            ),
            handler=ts._h_graph_explain,
        )
    )
    register(
        ToolSpec(
            name="test_status",
            title="Regression results overlay",
            command="rb graph results",
            description=(
                "Last recorded status, seed, run token and artefact paths per test, plus "
                "per-test line/branch/toggle/expression scalars when the run wrote "
                "coverage, from artefacts/graph/results-overlay.json. Use when the "
                "question is only about results; graph_query already joins this data "
                "onto its nodes."
            ),
            input_schema=_obj(
                {
                    "test": {
                        "type": "string",
                        "description": (
                            "Full test node id or a bare test name; omit for all."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by verdict, e.g. PASS, FAIL, UNKNOWN.",
                    },
                }
            ),
            handler=ts._h_test_status,
        )
    )
    register(
        ToolSpec(
            name="cov_summary",
            title="Coverage of the last run",
            command="rb cov summary",
            description=(
                "How covered the last coverage run is, read from artefacts on disk; no "
                "simulator runs. Returns run-level and per-test line/branch/toggle/"
                "expression scalars, the coldest files first, the module names the model "
                "knows, any SVA cover points, and where each coverage artefact landed. "
                "'totals' scores a point per elaborated module, so a source point "
                "parameterised twice counts twice; 'source_totals' scores it once, "
                "covered when any elaboration hit it, and is what closure targets are "
                "normally stated against. Needs no hub. Start here for 'what is "
                "under-covered', then call cov_module."
            ),
            input_schema=_obj(
                {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Files to report, coldest first (default "
                            f"{cov_query.DEFAULT_FILE_LIMIT}; 0 for all)."
                        ),
                        "minimum": 0,
                    },
                    "cov_dir": _COV_DIR_PROP,
                    "manifest": _COV_MANIFEST_PROP,
                }
            ),
            handler=ts._h_cov_summary,
        )
    )
    register(
        ToolSpec(
            name="cov_module",
            title="Coverage of one module",
            command="rb cov module",
            description=(
                "Per-file, per-point coverage for one module's sources: every line, "
                "branch, toggle and expression point with its hit count and the tests "
                "behind it. A file included into several modules reports only the "
                "points of the module asked for. An unknown name returns ok: false with "
                "'candidates'; module names are the coverage model's (Verilator's "
                "containing module), so check them against cov_summary.modules."
            ),
            input_schema=_obj(
                {
                    "module": {
                        "type": "string",
                        "description": (
                            "Module name as the coverage model records it, "
                            "e.g. one of cov_summary's 'modules'."
                        ),
                    },
                    "cov_dir": _COV_DIR_PROP,
                    "manifest": _COV_MANIFEST_PROP,
                },
                ["module"],
            ),
            handler=ts._h_cov_module,
        )
    )
    register(
        ToolSpec(
            name="phys_runs",
            title="Physical runs in this project",
            command="rb phys runs",
            description=(
                "Every run that left physical artefacts under this project, newest "
                "first; the source of the 'phys_dir' the other physical tools take. Each "
                "entry has the artefact directory to pass back, run name and top, "
                "producing backends, power mode and switching drive (defaults, a "
                "synthetic toggle/duty pair, or a SAIF/VCD trace and its test), a "
                "fingerprint of the netlist-shaping configuration (platform, effort, "
                "constraints, digest of the effective tool options), the rb xplr "
                "experiment id if any, and the generation time. Reads manifests only; no "
                "EDA tool, no hub. Call it when a project has more than one synthesis or "
                "power run: runs of one design differ by configuration and power mode, "
                "not by top."
            ),
            input_schema=_obj(
                {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Runs to list, newest first (default "
                            f"{phys_query.DEFAULT_RUNS_LIMIT}; 0 for all). The "
                            "payload reports the applied limit and the "
                            "untruncated count."
                        ),
                        "minimum": 0,
                    },
                }
            ),
            handler=ts._h_phys_runs,
        )
    )
    register(
        ToolSpec(
            name="phys_summary",
            title="Physical metrics of the last run",
            command="rb phys summary",
            description=(
                "What the last synthesis or power run measured, read from artefacts on "
                "disk; no EDA tool runs. Returns the run header and backends, design "
                "totals (cells, area, the four power columns), the heaviest modules by "
                "cell count, the hottest instances by total power, which halves of the "
                "model are filled and the command that fills a missing one, and where "
                "each physical artefact landed. Needs no hub. Start here for 'what is "
                "big or hot', then call phys_module or phys_instance. 'limit' heads both "
                "rankings; 'modules_limit' and 'instances_limit' override it per "
                "ranking, and take 'none' for a ranking you do not want at all, e.g. the "
                "whole module table without six figures of leaf instance rows."
            ),
            input_schema=_obj(
                {
                    "limit": _phys_limit_prop("Rows per ranking"),
                    "modules_limit": _phys_rank_limit_prop("Module rows"),
                    "instances_limit": _phys_rank_limit_prop("Instance rows"),
                    "phys_dir": _PHYS_DIR_PROP,
                    "manifest": _PHYS_MANIFEST_PROP,
                }
            ),
            handler=ts._h_phys_summary,
        )
    )
    register(
        ToolSpec(
            name="phys_module",
            title="Physical metrics of one module",
            command="rb phys module",
            description=(
                "One module's cost: its synthesis row (cell count and area) and the "
                "instance rows whose module column matches, with the power they sum to. "
                "Reads artefacts on disk; no EDA tool runs. 'module' has two "
                "namespaces: RTL module names on the synthesis half, Liberty cell names "
                "on the power half's leaves. An RTL module name still gets its synthesis "
                "row, but the POWER is attributed by that join, so power and instances "
                "answer Liberty-cell questions ('how much do the DFFs burn') and only "
                "those, not 'how much power does u_cpu burn'. That holds for a flat "
                "netlist too: flattening the design changes the hierarchy rather than the "
                "namespace, so no leaf row carries an RTL module name, the top's "
                "included. 'instance_join' says when this happened; do not report the "
                "empty instance list as 'this block burns no power', and do not read it "
                "back onto the cells and area, which stand. 'namespaces' says where the "
                "name was found. When it is found in both, the row and the instances "
                "measure different things and 'instance_join' says so; report them "
                "separately, never as one module's totals. Either half may be absent; the "
                "payload names the command that supplies the rest. 'instances' is headed "
                "at 'limit' (default "
                f"{phys_query.DEFAULT_RANK_LIMIT}, 0 for all) while 'instance_count' and "
                "the 'power' sum cover every matching row. An unknown name returns "
                "ok: false with 'candidates'."
            ),
            input_schema=_obj(
                {
                    "module": {
                        "type": "string",
                        "description": (
                            "Module or Liberty cell name as the physical model "
                            "records it, e.g. one named in phys_summary's "
                            "'modules' ranking."
                        ),
                    },
                    "limit": _phys_limit_prop("Instance rows to list"),
                    "phys_dir": _PHYS_DIR_PROP,
                    "manifest": _PHYS_MANIFEST_PROP,
                },
                ["module"],
            ),
            handler=ts._h_phys_module,
        )
    )
    register(
        ToolSpec(
            name="phys_instance",
            title="Physical metrics of one instance or subtree",
            command="rb phys instance",
            description=(
                "Power for one instance path or, for a subtree, the leaves under it and "
                "their rolled-up total. Reads artefacts on disk; no EDA tool runs. The "
                "model stores leaf values only, so hierarchy questions are answered "
                "here. POWER ONLY: the rollup carries the four power columns and a leaf "
                "count; the model has no per-cell area, so area per instance or subtree "
                "cannot be answered. 'children' is ranked by total power, hottest "
                "first, and headed at 'limit' (default "
                f"{phys_query.DEFAULT_RANK_LIMIT}, 0 for all) while 'child_count' covers "
                "every leaf under the path. 'match' says how the path landed, and "
                "'rollup' follows it: 'prefix' is a subtree with no row of its own and "
                "rolls up every leaf under it; 'exact' is a leaf row and rolls up that "
                "row alone. Where a path is both, the rows below it are listed for "
                "navigation and are not in the total; only an exact row is a row"
                ", so pass phys_focus an 'exact' path or one of the 'children', never "
                "a subtree prefix. Instance rows come from the power half alone: a "
                "synthesis-only model returns ok: false naming `rb power`, and an "
                "unknown path returns 'candidates'."
            ),
            input_schema=_obj(
                {
                    "path": {
                        "type": "string",
                        "description": (
                            "Instance path, exact or the root of a subtree, as "
                            "the model records it (either '/' or '.' "
                            "separated — both are levelled before matching), "
                            "e.g. one from phys_summary's "
                            "'instances' ranking."
                        ),
                    },
                    "limit": _phys_limit_prop("Child rows to list"),
                    "phys_dir": _PHYS_DIR_PROP,
                    "manifest": _PHYS_MANIFEST_PROP,
                },
                ["path"],
            ),
            handler=ts._h_phys_instance,
        )
    )
    register(
        ToolSpec(
            name="find_module",
            title="Find a module in the hierarchy",
            command="rb hier-query <model> find-module",
            description=(
                "Locate a module in an elaborated hierarchy via rtl-buddy-view: its "
                "declaration file and line, and where it is instantiated. Needs "
                "rtl-buddy-view on PATH. Unlike the graph tools it re-elaborates the "
                "sources, so prefer graph_query when the graph already has the answer."
            ),
            input_schema=_obj(
                {
                    "model": {
                        "type": "string",
                        "description": "Model name from a models.yaml under design/.",
                    },
                    "module": {"type": "string", "description": "Module name to find."},
                },
                ["model", "module"],
            ),
            handler=ts._h_find_module,
        )
    )
    register(
        ToolSpec(
            name="instances_of",
            title="Instances of a module",
            command="rb hier-query <model> instances-of",
            description=(
                "Every instance path of one module inside a model's elaborated "
                "hierarchy — the answer to 'where is this instantiated?'."
            ),
            input_schema=_obj(
                {
                    "model": {"type": "string", "description": "Model name."},
                    "module": {"type": "string", "description": "Module name."},
                },
                ["model", "module"],
            ),
            handler=ts._h_instances_of,
        )
    )
    register(
        ToolSpec(
            name="port_connections",
            title="Port connections of an instance",
            command="rb hier-query <model> port-connections",
            description=(
                "Formal-to-actual port bindings for one instance, given its "
                "dot-separated path rooted at the model."
            ),
            input_schema=_obj(
                {
                    "model": {"type": "string", "description": "Model name."},
                    "instance_path": {
                        "type": "string",
                        "description": "Dot path rooted at the model, e.g. u_fifo.u_wr.",
                    },
                },
                ["model", "instance_path"],
            ),
            handler=ts._h_port_connections,
        )
    )
    register(
        ToolSpec(
            name="source_snippet",
            title="Cite an instance's source",
            command="rb hier-query <model> source-snippet",
            description=(
                "Line-numbered source text for one instance — the citation "
                "half of the workflow. Locate with graph_query, quote with "
                "this, instead of reading whole RTL files."
            ),
            input_schema=_obj(
                {
                    "model": {"type": "string", "description": "Model name."},
                    "instance_path": {
                        "type": "string",
                        "description": "Dot path rooted at the model.",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Context lines on each side.",
                        "minimum": 0,
                    },
                    "line_numbers": {
                        "type": "boolean",
                        "description": "Prefix each line with its number (default true).",
                    },
                },
                ["model", "instance_path"],
            ),
            handler=ts._h_source_snippet,
        )
    )

    if handle.present:
        register(
            ToolSpec(
                name="hub_state",
                title="Hub session state",
                command="rb hub send state",
                description=(
                    "Snapshot the live rtl-buddy hub: active model, current "
                    "selection, wave cursor and scope, and the connected peers "
                    "(schematic SPA, surfer, editor). This is the session the "
                    "user is looking at right now."
                ),
                input_schema=_obj({}),
                handler=ts._h_hub_state,
            )
        )
        register(
            ToolSpec(
                name="hub_select",
                title="Select an instance in the live view",
                command="rb hub send select",
                description=(
                    "Broadcast a selection to the hub so the schematic view "
                    "highlights and scrolls to one instance. Use the "
                    "instance_path from a graph instance node "
                    "('inst:<top>/<dot.path>' — pass the part after the slash)."
                ),
                input_schema=_obj(
                    {
                        "instance_path": {
                            "type": "string",
                            "description": "view.json instance path, e.g. top.u_fifo.",
                        }
                    },
                    ["instance_path"],
                ),
                handler=ts._h_hub_select,
            )
        )
        register(
            ToolSpec(
                name="hub_open_source",
                title="Open a file in the user's editor",
                command="rb hub send open-source",
                description=(
                    "Ask the hub's source peer (nvim) to open a file at a line "
                    "and column — for putting the user's cursor on the thing "
                    "you just explained."
                ),
                input_schema=_obj(
                    {
                        "file": {
                            "type": "string",
                            "description": "Repo-relative path, e.g. design/dma/dma.sv.",
                        },
                        "line": {"type": "integer", "minimum": 1},
                        "col": {"type": "integer", "minimum": 1},
                    },
                    ["file", "line"],
                ),
                handler=ts._h_hub_open_source,
            )
        )
        register(
            ToolSpec(
                name="hub_resolve",
                title="Resolve between schematic and waveform coordinates",
                command="rb hub send resolve",
                description=(
                    "Translate coordinates across the hub's view.json + "
                    "tb_prefix mapping: an instance path to a wave scope, a "
                    "wave scope back to an instance path, or a signal in a wave "
                    "scope to the instance(s) that drive it."
                ),
                input_schema=_obj(
                    {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "view-to-wave",
                                "wave-to-view",
                                "signal-to-view",
                            ],
                        },
                        "instance_path": {
                            "type": "string",
                            "description": "Required for view-to-wave.",
                        },
                        "wave_scope": {
                            "type": "string",
                            "description": (
                                "Required for wave-to-view and signal-to-view."
                            ),
                        },
                        "signal": {
                            "type": "string",
                            "description": "Required for signal-to-view.",
                        },
                    },
                    ["kind"],
                ),
                handler=ts._h_hub_resolve,
            )
        )
        register(
            ToolSpec(
                name="hub_diagnose",
                title="Push diagnostics to the live view",
                command="rb hub send diagnose",
                description=(
                    "Publish findings as diagnostics on the live session: they "
                    "appear as badges on the schematic and markers in the "
                    "editor. Latest write per 'source' wins, so re-pushing "
                    "replaces your previous set; pass clear: true to withdraw."
                ),
                input_schema=_obj(
                    {
                        "source": {
                            "type": "string",
                            "description": (
                                "Producer key, e.g. 'claude-analysis'. "
                                "Latest-writer-wins per source."
                            ),
                        },
                        "items": {
                            "type": "array",
                            "items": _obj(
                                {
                                    "file": {"type": "string"},
                                    "line": {"type": "integer", "minimum": 1},
                                    "col": {"type": "integer", "minimum": 1},
                                    "severity": {
                                        "type": "string",
                                        "enum": list(_SEVERITIES),
                                    },
                                    "code": {"type": "string"},
                                    "message": {"type": "string"},
                                    "instance_path": {"type": "string"},
                                },
                                ["file", "line", "message"],
                            ),
                        },
                        "instance_path": {
                            "type": "string",
                            "description": (
                                "Applied to every item that does not set its own."
                            ),
                        },
                        "clear": {
                            "type": "boolean",
                            "description": "Send an empty set, clearing this source.",
                        },
                    },
                    ["source"],
                ),
                handler=ts._h_hub_diagnose,
            )
        )
        register(
            ToolSpec(
                name="cov_focus",
                title="Point the live coverage pane at a target",
                command="rb hub send cov-focus",
                description=(
                    "Broadcast a coverage focus so the hub's /cov pane shows what you are "
                    "talking about. Target is prefixed: 'file:design/blk.sv', 'module:blk' "
                    "or 'test:verif/blk#basic' (an unprefixed string is read as a file "
                    "path). 'metric' foregrounds one coverage kind, 'line' scrolls a file "
                    "target, 'item' names a branch/toggle/expression bin or an SVA cover "
                    "point. Use the names cov_summary and cov_module return. A target missing "
                    "from the pane's model is a soft miss, and the hub replays the latest "
                    "focus to a pane that connects later, so sending before the tab opens "
                    "works."
                ),
                input_schema=_obj(
                    {
                        "target": {
                            "type": "string",
                            "description": (
                                "file:<path>, module:<name>, test:<suite>#<name>, "
                                "or a bare path."
                            ),
                        },
                        "metric": {
                            "type": "string",
                            "enum": list(_COV_METRICS),
                            "description": "Coverage kind to foreground.",
                        },
                        "line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "1-based source line, for a file target.",
                        },
                        "item": {
                            "type": "string",
                            "description": (
                                "Point within the target: a bin id as /cov.json "
                                "spells it, or an SVA cover point name."
                            ),
                        },
                    },
                    ["target"],
                ),
                handler=ts._h_cov_focus,
            )
        )
        register(
            ToolSpec(
                name="phys_focus",
                title="Point the live synth+power pane at a target",
                command="rb hub send phys-focus",
                description=(
                    "Broadcast a physical focus so the hub's /phy pane shows what you are "
                    "talking about. Target is prefixed: 'module:alu' or "
                    "'instance:u_cpu/u_alu' (an unprefixed string is read as an instance "
                    "path); 'metric' foregrounds one physical metric. Use the names "
                    "phys_summary, phys_module and phys_instance return, and for an instance "
                    "use one that names a ROW: the pane resolves exact leaf rows only. "
                    "phys_instance's echoed 'instance_path' is focusable when its 'match' "
                    "is 'exact'; when it is 'prefix' the path is a subtree with no row of "
                    "its own, so focus one of the 'children' instead. A target missing from "
                    "the pane's model is a soft miss, and the hub replays the latest focus "
                    "to a pane that connects later, so sending before the tab opens works."
                ),
                input_schema=_obj(
                    {
                        "target": {
                            "type": "string",
                            "description": (
                                "module:<name>, instance:<hierarchical path>, "
                                "or a bare instance path."
                            ),
                        },
                        "metric": {
                            "type": "string",
                            "enum": list(_PHYS_METRICS),
                            "description": (
                                "Physical metric to foreground. 'dynamic' is "
                                "internal + switching, summed by the pane."
                            ),
                        },
                    },
                    ["target"],
                ),
                handler=ts._h_phys_focus,
            )
        )

    return ts


__all__ = [
    "HUB_TOOL_NAMES",
    "STATELESS_TOOL_NAMES",
    "HubHandle",
    "ToolError",
    "ToolSpec",
    "Toolset",
    "build_toolset",
    "discover_hub",
]
