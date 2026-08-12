# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The tools ``rb mcp`` serves, and their handlers (#380).

Deliberately SDK-free. Everything here is plain Python — dataclasses,
dicts and JSON Schema literals — so the tool set can be built, listed and
called on a machine that has never installed the ``mcp`` package.
:mod:`rtl_buddy.mcp.server` is the only module that imports the SDK, and
all it does is transcribe these specs onto the wire.

Two groups of tools:

* **stateless** — always present. They read
  ``artefacts/graph/graph.json`` plus the results overlay, read
  ``cov_dir/manifest.json`` and its model for the coverage verbs, and
  shell out to ``rtl-buddy-view`` for the hierarchy verbs. No hub, no
  daemon, no session: identical behaviour in an IDE, on a CI runner, or
  on a dispatch node.
* **hub** — present only when a live hub is discovered. These drive the
  schematic/waveform session the user is looking at, which is the one
  thing no stateless server can offer. Discovery is
  ``.rtl-buddy/hub.json``, exactly what ``rb hub send`` reads, and the
  client is the same :class:`~rtl_buddy.hub.client.HubClient`, so there
  is no second protocol implementation to keep in step.

Every handler returns the payload its ``rb --machine`` counterpart
returns. That is not a convenience: two agent-facing surfaces that
describe the same graph in two shapes would drift within one release,
and the payload is the contract both are versioned against.
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
)

_SEVERITIES = ("error", "warning", "info", "hint")

#: ``cov_focus.metric`` enum, mirroring the wire schema — the same
#: literal ``rb hub send cov-focus`` spells, and for the same reason:
#: the contract is the hub's schema, not this process's coverage model.
_COV_METRICS = ("line", "branch", "toggle", "expression", "cover")


def _tool_version() -> str:
    try:
        return version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


class ToolError(Exception):
    """A tool call that could not be answered.

    Surfaces to the agent as ``ok: false`` plus a message, never as a
    transport error: an agent that asked about a module that does not
    exist has received an answer, and a protocol-level failure would
    teach it to stop asking.
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
    #: Human-mode command this tool mirrors, reported in every result so
    #: an agent can reproduce the answer in a terminal.
    command: str = ""

    def to_mcp_dict(self) -> dict:
        """The wire form, camelCase, as the MCP tool listing wants it."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class HubHandle:
    """What was discovered about a hub, and how to talk to it.

    Discovery happens once, at server start: an MCP client reads the tool
    list at session start and a tool set that changed mid-session would
    not be seen anyway. Each *call* opens and closes its own connection,
    the way ``rb hub send`` does — a long-lived socket would make the
    server a stateful peer, which is exactly what the stdio design is
    avoiding.
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
    """Decide whether the hub tools light up, exactly as ``rb hub send`` would.

    The decision is delegated to the client's own resolver — env
    override, then ``.rtl-buddy/hub.json`` walking up, then a PID
    liveness check — rather than re-deriving it here. A second opinion
    would eventually disagree, and the disagreement would show up as
    tools that are advertised and cannot connect. The record is read
    afterwards only for the metadata a caller likes to see (pid, hub
    version, active model).
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

    Stateless by construction: the graph and the overlay are re-read on
    every call rather than cached, so an agent that runs ``rb graph
    build`` in one tool-using turn sees the new graph in the next without
    restarting the server. The files are small and local; the alternative
    is an agent reasoning over a graph that no longer exists.
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

        Never raises for a bad question: an unknown module, a missing
        graph or a hub that went away all come back as ``ok: false`` with
        a message the agent can act on.
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
        except (graph_query.GraphQueryError, cov_query.CovQueryError) as exc:
            # Both carry near misses for a name that does not exist, and
            # both must be caught above FatalRtlBuddyError (CovQueryError
            # is one) or the candidates would be dropped.
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
        """The MCP result envelope — machine mode's, minus argv and git.

        ``rtl_buddy_version`` is here for the same reason it is in the
        ``--machine`` envelope: it is what both surfaces are versioned
        against, and an agent holding a payload it does not recognise
        needs to know which rtl_buddy produced it.
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
        """What this server can answer, before anything is asked of it."""
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
            results=bool(args.get("results", True)),
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
        return graph_query.explain(ctx, node, results=bool(args.get("results", True)))

    def _h_test_status(self, args: dict) -> dict:
        ctx = self._context({"results": True})
        return graph_query.test_status(
            ctx, test=args.get("test"), status=args.get("status")
        )

    # ------------------------------------------------------------------
    # coverage handlers
    # ------------------------------------------------------------------

    def _cov_context(self, args: dict) -> cov_query.CovContext:
        """Load the manifest and model a coverage tool answers from.

        Re-read per call, like the graph: an agent that runs a coverage
        regression in one turn asks about it in the next, and the
        alternative is answering from a run that no longer exists.

        A relative ``cov_dir``/``manifest`` is anchored on the project
        root, not on the process cwd. An MCP client has no invocation
        directory to speak from — the host spawns ``rb mcp`` wherever it
        happens to sit, and the agent never sees where that is — while
        the paths the payloads hand back are repo-relative
        (``artefacts.manifest`` is ``verif/blk_a/cov_dir/manifest.json``).
        Reading a relative argument against the server's cwd would answer
        a path the agent never named.
        """

        def rooted(value: str | None) -> str | None:
            if value is None:
                return None
            path = Path(value)
            return str(path if path.is_absolute() else self.project_root / path)

        return cov_query.load_context(
            self.project_root,
            cov_dir=rooted(args.get("cov_dir")),
            manifest=rooted(args.get("manifest")),
        )

    def _h_cov_summary(self, args: dict) -> dict:
        return cov_query.summary_payload(
            self._cov_context(args),
            limit=int(args.get("limit", cov_query.DEFAULT_FILE_LIMIT)),
        )

    def _h_cov_module(self, args: dict) -> dict:
        return cov_query.module_payload(
            self._cov_context(args), str(_req(args, "module"))
        )

    # ------------------------------------------------------------------
    # hierarchy handlers (rtl-buddy-view, subprocess)
    # ------------------------------------------------------------------

    def _model_cfg(self, name: str):
        """Resolve a model by name the way ``rb graph build`` does.

        The whole design tree is searched rather than one ``models.yaml``
        in a cwd, because an MCP server has no cwd worth speaking of —
        it is spawned by an agent host from wherever that host happens to
        sit.
        """
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
            # A lookup miss is the answer, not a crash — the viewer says
            # so on stderr and exits non-zero. Hand it back verbatim.
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
            # Origin stays ``cli``: it is the origin the hub's schema and
            # every existing peer already accept, and adding an ``mcp``
            # value would be a protocol change for no behavioural gain —
            # this server IS a one-shot CLI-shaped peer.
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
        # Optional keys are omitted rather than sent as null: the wire
        # schema is additionalProperties:false with no nullable hints,
        # so a null would be rejected by the hub, not ignored by it.
        payload: dict[str, Any] = {"target": str(_req(args, "target")).strip()}
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
            # Emit what was validated: the pane matches these strings, so
            # a trailing space is a miss, not a near miss. Mirrors
            # ``rb hub send cov-focus``.
            item = str(item).strip()
            if not item:
                raise ToolError("cov_focus: 'item' must be non-empty")
            payload["item"] = item
        return self._hub_emit("cov_focus", payload)


def _req(args: dict, key: str):
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolError(f"missing required argument {key!r}")
    return value


def _maybe_json(text: str):
    """Parse the viewer's stdout as JSON, or keep it as text.

    ``source-snippet`` answers with line-numbered source, every other
    verb with JSON. Sniffing beats hard-coding the verb list: the viewer
    owns that choice, and a new verb should not need a change here.
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

_COV_DIR_PROP = {
    "type": "string",
    "description": (
        "Coverage artefact directory to read; a relative path resolves "
        "against the project root, e.g. verif/blk_a/cov_dir. Default: the "
        "newest cov_dir/manifest.json under the project root, which is the "
        "run that finished last."
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

    ``hub`` is discovered here unless one is supplied (tests supply one).
    The hub tools are registered only when a hub is live, so an agent
    reading the listing on a CI node is never offered a tool that can
    only fail.
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
                "Keyword search over the design knowledge graph, with a "
                "neighbourhood expansion around every match. This is the "
                "cheapest way to locate anything in the project: modules, "
                "instances, ports, tests, testbenches, models, spec blocks and "
                "coverage items all live in one graph. Matching is "
                "deterministic keyword scoring, not a model, so phrase the "
                "question around the identifier you care about "
                "('which tests cover A-COV-1'). Each match arrives with its "
                "neighbours, their last regression status, and a 'cite' hint "
                "naming the file (and, for instances, the exact hier-query "
                "command) that quotes it."
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
                    "results": _RESULTS_PROP,
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
                "The shortest chain of edges connecting two nodes — how a test "
                "reaches a module, how a coverage item reaches its spec doc. "
                "Accepts full node ids ('test:verif/blk_a#t_cocotb') or bare "
                "names when they are unambiguous. Undirected by default, "
                "because edge direction encodes role rather than reachability."
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
                "Everything the graph knows about one node: its attributes, "
                "every incoming and outgoing edge with the far endpoint "
                "resolved, and — for a test node — its last regression status, "
                "seed and artefact paths from the results overlay. When the "
                "overlay carries a coverage join, a module or instance node "
                "also returns its coverage ratio and a coverage_item node "
                "returns whether the run exercised it, which cover points it "
                "correlated with, and how the tests declaring it fared."
            ),
            input_schema=_obj(
                {
                    "node": {
                        "type": "string",
                        "description": "Node id, or a bare name if unambiguous.",
                    },
                    "results": _RESULTS_PROP,
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
                "Last recorded status, seed, run token, artefact paths and — "
                "when the run wrote coverage — the per-test line/branch/"
                "toggle/expression scalars, straight from "
                "artefacts/graph/results-overlay.json. Use when the question "
                "is only about results; graph_query already joins the same "
                "data onto the nodes it returns."
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
                "How covered the last coverage run is, read from artefacts "
                "already on disk — no simulator runs. Returns run-level and "
                "per-test line/branch/toggle/expression scalars, the coldest "
                "files first, the module names the model knows, any SVA cover "
                "points, and where every coverage artefact landed. Stateless: "
                "a CI node answers this with no hub and no daemon. Start here "
                "when the question is 'what is under-covered', then call "
                "cov_module for the points."
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
                "Per-file, per-point coverage for one module's sources: every "
                "line, branch, toggle and expression point with its hit count "
                "and the tests behind it. A file included into several modules "
                "reports only the points belonging to the module asked for. An "
                "unknown name comes back as ok: false with 'candidates' — the "
                "module vocabulary is the coverage model's (Verilator's "
                "containing module), so check it against cov_summary.modules."
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
            name="find_module",
            title="Find a module in the hierarchy",
            command="rb hier-query <model> find-module",
            description=(
                "Locate a module in an elaborated hierarchy via rtl-buddy-view: "
                "its declaration file and line, and where it is instantiated. "
                "Needs rtl-buddy-view on PATH; unlike the graph tools it "
                "re-elaborates the sources, so prefer graph_query when the "
                "graph already holds the answer."
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
                    "Broadcast a coverage focus so the hub's /cov pane shows "
                    "what you are talking about. Target is prefixed: "
                    "'file:design/blk.sv', 'module:blk' or "
                    "'test:verif/blk#basic' (an unprefixed string is read as a "
                    "file path); metric foregrounds one coverage kind, line "
                    "scrolls a file target, item names a branch/toggle/"
                    "expression bin or an SVA cover point. Use the names "
                    "cov_summary and cov_module return. A target the pane's "
                    "model does not contain is a soft miss, and the hub "
                    "replays the latest focus to a pane that connects later, "
                    "so sending this before the tab is open works."
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
