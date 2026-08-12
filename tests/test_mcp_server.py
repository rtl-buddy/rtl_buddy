"""Tests for #380 — ``rb mcp``, the stdio Model Context Protocol server.

MCP is a *second* LLM-facing surface next to ``--machine``, and the way
two such surfaces fail is by drifting: one grows a field, the other
doesn't, and an agent's answer depends on which door it came through.
The design that prevents it is what these tests pin.

What these tests pin:

* the tool set is SDK-free — it builds, lists and answers on a machine
  that has never installed ``mcp``, which is also what makes the schemas
  checkable here;
* the stateless tools are always served — including the coverage reads,
  whose artefacts are on disk — and the hub tools appear only when a live
  hub was discovered, so an agent on a CI node is never offered a tool
  that can only fail;
* every result is the ``rb --machine`` payload verbatim, wrapped in an
  envelope reporting ``rtl_buddy_version``;
* a bad question (unknown tool, missing graph, unknown model) comes back
  as ``ok: false`` with a message, never as an exception — an agent that
  gets a transport error learns to stop asking;
* the SDK boundary itself: schemas validate against ``mcp.types.Tool``,
  and a real client can list and call tools over stdio.

The SDK-dependent tests skip when ``mcp`` is not installed. The extra is
in the ``test`` dependency group so CI runs them for real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.mcp import HUB_TOOL_NAMES, STATELESS_TOOL_NAMES, HubHandle, build_toolset
from rtl_buddy.mcp import server as mcp_server
from rtl_buddy.mcp import toolset as mcp_toolset
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"

_HAS_SDK = mcp_server.sdk_available()
requires_sdk = pytest.mark.skipif(
    not _HAS_SDK, reason="the `mcp` SDK is not installed (optional extra)"
)


@pytest.fixture
def mcp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a built (config-tier) graph and one result."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", target)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", target)
    for name in ("blk_a", "blk_b"):
        (target / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    monkeypatch.chdir(target)

    rb = RtlBuddy(name="test_mcp_server")
    runner = CliRunner()
    built = runner.invoke(rb.app, ["graph", "build", "--no-design", "--no-extract"])
    assert built.exit_code == 0, built.output
    rb._artifact_locks.release_all()
    return target


@pytest.fixture
def empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with no graph built yet."""
    target = tmp_path / "bare"
    target.mkdir()
    monkeypatch.chdir(target)
    return target


def _totals(found: int, hit: int) -> dict:
    return {"found": found, "hit": hit, "ratio": None if not found else hit / found}


def _cov_totals(**metrics: tuple[int, int]) -> dict:
    return {
        metric: _totals(*metrics.get(metric, (0, 0)))
        for metric in ("line", "branch", "toggle", "expression", "cover")
    }


@pytest.fixture
def cov_project(mcp_project: Path) -> Path:
    """The graph project, plus one run's coverage artefacts on disk.

    Hand-authored rather than simulated: what these tests pin is that
    the MCP tools hand back what the ``rb cov`` builders produce, and a
    real ``coverage.dat`` would only add a Verilator parse to the path.
    """
    from rtl_buddy.cov import manifest as manifest_mod
    from rtl_buddy.cov import model as model_mod

    cold_totals = _cov_totals(line=(3, 2), toggle=(1, 0))
    warm_totals = _cov_totals(line=(2, 2))
    model = {
        "schema_version": model_mod.MODEL_SCHEMA_VERSION,
        "generator": "rtl-buddy 0.0.0+test",
        "simulator": "verilator",
        "totals": _cov_totals(line=(5, 4), toggle=(1, 0)),
        "counts": {"files": 2, "tests": 1, "modules": 2},
        "modules": {
            "blk_a": ["design/blk_a/blk_a.sv"],
            "blk_b": ["design/blk_b/blk_b.sv"],
        },
        "tests": [
            {
                "name": "t_basic",
                "suite": "verif/blk_a/tests.yaml",
                "totals": _cov_totals(line=(5, 4), toggle=(1, 0)),
            }
        ],
        "files": [
            # Warm first, so a coldest-first answer had to reorder.
            {
                "path": "design/blk_b/blk_b.sv",
                "modules": ["blk_b"],
                "totals": dict(warm_totals),
                "line": [
                    {"line": 1, "hits": 2, "tests": {"t_basic": 2}},
                    {"line": 2, "hits": 2, "tests": {"t_basic": 2}},
                ],
                "branch": [],
                "toggle": [],
                "expression": [],
                "cover": [],
            },
            {
                "path": "design/blk_a/blk_a.sv",
                "modules": ["blk_a"],
                "totals": dict(cold_totals),
                "line": [
                    {"line": 1, "hits": 4, "tests": {"t_basic": 4}},
                    {"line": 2, "hits": 1, "tests": {"t_basic": 1}},
                    {"line": 3, "hits": 0, "tests": {"t_basic": 0}},
                ],
                "branch": [],
                "toggle": [
                    {
                        "line": 1,
                        "column": 9,
                        "name": "q[0]",
                        "module": "blk_a",
                        "hits": 0,
                        "tests": {"t_basic": 0},
                    }
                ],
                "expression": [],
                "cover": [],
            },
        ],
    }
    cov_dir = mcp_project / "verif" / "blk_a" / "cov_dir"
    cov_dir.mkdir(parents=True)
    model_path = model_mod.write_model(model, cov_dir)
    manifest_mod.write_manifest(
        manifest_mod.build_manifest(
            project_root=mcp_project,
            cov_dir=cov_dir,
            command="regression",
            suite=mcp_project / "verif" / "blk_a" / "regression.yaml",
            builder="verilator",
            simulator_family="verilator",
            merge_mode="raw",
            model_path=model_path,
            totals=model["totals"],
            merged={"info": cov_dir / "coverage_merged.info"},
            tests=[{"name": "t_basic", "raw": cov_dir / "t_basic.dat"}],
        ),
        cov_dir,
    )
    return mcp_project


def _toolset(project: Path, **kwargs):
    kwargs.setdefault("hub", HubHandle(present=False, reason="test: no hub"))
    return build_toolset(project, **kwargs)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_stateless_tools_are_served_without_a_hub(mcp_project: Path):
    """Headless is the primary case: no daemon, full stateless answers."""
    ts = _toolset(mcp_project)

    assert ts.names() == list(STATELESS_TOOL_NAMES)
    assert not any(name in ts.names() for name in HUB_TOOL_NAMES)


def test_hub_tools_dial_in_when_a_hub_is_live(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    assert ts.names() == list(STATELESS_TOOL_NAMES) + list(HUB_TOOL_NAMES)


def test_a_stale_hub_record_does_not_advertise_hub_tools(mcp_project: Path):
    """A dead PID must not light up tools that can only fail to connect."""
    hub_dir = mcp_project / ".rtl-buddy"
    hub_dir.mkdir(exist_ok=True)
    (hub_dir / "hub.json").write_text(
        json.dumps(
            {
                "v": 1,
                # A PID that cannot be live: the kernel never allocates 0.
                "pid": 0,
                "tcp": "127.0.0.1:9999",
                "server_version": "0.0.0",
                "project_root": str(mcp_project),
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    handle = mcp_toolset.discover_hub(mcp_project)

    assert handle.present is False
    assert "no live hub" in (handle.reason or "")


def test_coverage_reads_are_stateless_and_only_the_focus_needs_a_hub(
    mcp_project: Path,
):
    """Artefacts are on disk: a CI node answers coverage with no hub.

    Only ``cov_focus`` needs one, because pointing a pane at something
    is the one coverage question a headless process cannot answer.
    """
    headless = _toolset(mcp_project)
    live = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    assert {"cov_summary", "cov_module"} <= set(headless.names())
    assert "cov_focus" not in headless.names()
    assert "cov_focus" in live.names()
    assert live.spec("cov_focus").command == "rb hub send cov-focus"
    assert headless.spec("cov_summary").command == "rb cov summary"
    assert headless.spec("cov_module").command == "rb cov module"


def test_every_schema_is_a_closed_object_with_resolvable_requireds(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    for spec in ts.specs():
        schema = spec.input_schema
        assert schema["type"] == "object", spec.name
        assert schema["additionalProperties"] is False, spec.name
        assert set(schema["required"]) <= set(schema["properties"]), spec.name
        assert spec.description and spec.title, spec.name


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


def test_a_result_is_the_machine_payload_plus_the_version(mcp_project: Path):
    """Neither surface may grow a shape the other does not have."""
    from rtl_buddy.graph.query import load_context, query as run_query

    ts = _toolset(mcp_project)
    envelope = ts.call("graph_query", {"question": "which tests cover A-COV-1"})

    assert envelope["ok"] is True
    assert envelope["meta"]["rtl_buddy_version"]
    assert envelope["meta"]["command"] == "rb graph query"

    direct = run_query(load_context(mcp_project), "which tests cover A-COV-1")
    assert envelope["payload"] == direct


def test_graph_status_reports_what_can_be_answered(mcp_project: Path):
    ts = _toolset(mcp_project)

    payload = ts.call("graph_status", {})["payload"]

    assert payload["graph_present"] is True
    assert payload["overlay_present"] is False
    assert payload["node_types"]["coverage_item"] == 4
    assert payload["hub"]["present"] is False
    assert payload["tools"] == list(STATELESS_TOOL_NAMES)


def test_graph_status_on_a_bare_checkout_points_at_graph_build(empty_project: Path):
    ts = _toolset(empty_project)

    payload = ts.call("graph_status", {})["payload"]

    assert payload["graph_present"] is False
    assert "rb graph build" in payload["hint"]


def test_path_and_explain_mirror_their_cli_verbs(mcp_project: Path):
    ts = _toolset(mcp_project)

    found = ts.call(
        "graph_path",
        {"source": "test:verif/blk_a#t_basic", "target": "covitem:blk_a#A-COV-1"},
    )
    explained = ts.call("graph_explain", {"node": "test:verif/blk_a#t_basic"})

    assert found["payload"]["length"] == 1
    assert explained["payload"]["degree"]["out"]["runs_on"] == 1


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_cov_summary_is_the_rb_cov_payload_verbatim(cov_project: Path):
    """Same builder as ``rb --machine cov summary``, not a second shape."""
    from rtl_buddy.cov.query import load_context, summary_payload

    ts = _toolset(cov_project)
    envelope = ts.call("cov_summary", {})

    assert envelope["ok"] is True
    assert envelope["meta"]["command"] == "rb cov summary"
    assert envelope["payload"] == summary_payload(load_context(ts.project_root))
    assert envelope["payload"]["totals"]["line"]["hit"] == 4
    assert envelope["payload"]["modules"] == ["blk_a", "blk_b"]
    assert envelope["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/cov_dir/manifest.json"
    )


def test_cov_summary_truncates_coldest_first(cov_project: Path):
    """The one file a limited summary keeps is the one to go look at."""
    ts = _toolset(cov_project)

    everything = ts.call("cov_summary", {"limit": 0})["payload"]["files"]
    coldest = ts.call("cov_summary", {"limit": 1})["payload"]["files"]

    assert [row["path"] for row in everything] == [
        "design/blk_a/blk_a.sv",
        "design/blk_b/blk_b.sv",
    ]
    assert [row["path"] for row in coldest] == ["design/blk_a/blk_a.sv"]
    # A summary file row carries totals only; points are cov_module's job.
    assert "line" not in coldest[0]


def test_cov_module_returns_the_points_and_the_tests_behind_them(cov_project: Path):
    from rtl_buddy.cov.query import load_context, module_payload

    ts = _toolset(cov_project)
    envelope = ts.call("cov_module", {"module": "blk_a"})

    assert envelope["ok"] is True
    assert envelope["payload"] == module_payload(load_context(ts.project_root), "blk_a")
    assert envelope["payload"]["tests"] == ["t_basic"]
    cold = [p for p in envelope["payload"]["files"][0]["toggle"] if not p["hits"]]
    assert [point["name"] for point in cold] == ["q[0]"]


def test_an_unknown_cov_module_returns_its_candidates(cov_project: Path):
    """A typo is likelier than a coverage hole; hand back the near miss."""
    ts = _toolset(cov_project)

    envelope = ts.call("cov_module", {"module": "blk_z"})

    assert envelope["ok"] is False
    assert sorted(envelope["candidates"]) == ["blk_a", "blk_b"]


def test_cov_reads_a_named_cov_dir_instead_of_the_newest(cov_project: Path):
    ts = _toolset(cov_project)

    named = ts.call(
        "cov_summary", {"cov_dir": str(cov_project / "verif" / "blk_a" / "cov_dir")}
    )
    missing = ts.call("cov_summary", {"cov_dir": str(cov_project / "verif")})

    assert named["ok"] is True
    assert missing["ok"] is False
    assert "manifest.json" in missing["error"]


def test_cov_reads_a_relative_cov_dir_against_the_project_root(
    cov_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An MCP client has no invocation cwd; the payload speaks repo paths.

    The host spawns ``rb mcp`` in a directory the agent never sees, so the
    natural argument is the repo-relative one the payload itself hands
    back (``artefacts.manifest``). Resolving it against the server's cwd
    would answer a path nobody named — hence the chdir here.
    """
    ts = _toolset(cov_project)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relative = ts.call("cov_summary", {"cov_dir": "verif/blk_a/cov_dir"})
    absolute = ts.call(
        "cov_summary", {"cov_dir": str(cov_project / "verif" / "blk_a" / "cov_dir")}
    )

    assert relative["ok"] is True, relative.get("error")
    assert relative["payload"] == absolute["payload"]
    assert relative["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/cov_dir/manifest.json"
    )


def test_cov_reads_a_relative_manifest_against_the_project_root(
    cov_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``manifest`` is the path the summary reports back, verbatim."""
    ts = _toolset(cov_project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    envelope = ts.call(
        "cov_module",
        {"module": "blk_a", "manifest": "verif/blk_a/cov_dir/manifest.json"},
    )

    assert envelope["ok"] is True, envelope.get("error")
    assert envelope["payload"]["tests"] == ["t_basic"]


def test_a_project_with_no_coverage_run_names_the_command_that_makes_one(
    empty_project: Path,
):
    ts = _toolset(empty_project)

    envelope = ts.call("cov_summary", {})

    assert envelope["ok"] is False
    assert "--coverage-merge" in envelope["error"]


def test_cov_focus_omits_the_hints_it_was_not_given(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """``additionalProperties: false`` and no nullable hints on the wire."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    sent: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: sent.update({"type": type_, "payload": payload}) or {},
    )

    ts.call("cov_focus", {"target": "module:blk_a", "metric": "toggle"})

    assert sent["type"] == "cov_focus"
    assert sent["payload"] == {"target": "module:blk_a", "metric": "toggle"}


def test_cov_focus_puts_the_same_bytes_on_the_wire_as_its_cli_verb(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """Padded input, one payload: the MCP tool and ``rb hub send``.

    The pane matches ``target``/``item`` as strings, so a trailing space
    is a miss rather than a near miss, and a rule spelled one way on one
    surface and another way on the other is observable on the wire.
    Both validate *and* emit the stripped value.
    """
    from rtl_buddy.hub import send as hub_send

    padded = {"target": "  module:blk_a  ", "metric": "toggle", "item": "  q[0] \t"}

    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    from_mcp: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: (
            from_mcp.update({"type": type_, "payload": payload}) or {}
        ),
    )
    ts.call("cov_focus", dict(padded))

    from_cli: dict = {}

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def emit(self, type_, payload):
            from_cli.update({"type": type_, "payload": payload})

    monkeypatch.setattr(hub_send, "_open_or_exit", _Recorder)
    hub_send.cmd_cov_focus(
        padded["target"], metric=padded["metric"], item=padded["item"]
    )

    assert from_mcp == from_cli
    assert from_cli == {
        "type": "cov_focus",
        "payload": {"target": "module:blk_a", "metric": "toggle", "item": "q[0]"},
    }


def test_cov_focus_validates_before_dialling(mcp_project: Path):
    """Port 1 refuses connections: reaching it would mean no validation."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("cov_focus", {"target": "design/blk_a.sv", "metric": "lines"})

    assert envelope["ok"] is False
    assert "metric" in envelope["error"]


# ---------------------------------------------------------------------------
# Failure is an answer, not an exception
# ---------------------------------------------------------------------------


def test_an_unknown_tool_is_an_answer(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("no_such_tool", {})

    assert envelope["ok"] is False
    assert "unknown tool" in envelope["error"]
    assert "graph_query" in envelope["error"]


def test_a_missing_argument_is_an_answer(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("graph_query", {})

    assert envelope["ok"] is False
    assert "question" in envelope["error"]


def test_a_missing_graph_is_an_answer_naming_the_build_command(empty_project: Path):
    ts = _toolset(empty_project)

    envelope = ts.call("graph_query", {"question": "anything"})

    assert envelope["ok"] is False
    assert "rb graph build" in envelope["error"]


def test_an_ambiguous_node_returns_its_candidates(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("graph_explain", {"node": "blk_a"})

    assert envelope["ok"] is False
    assert "spec:blk_a" in envelope["candidates"]


def test_an_unknown_model_lists_the_models_that_exist(mcp_project: Path):
    """No viewer needed: the model name is checked before spawning it."""
    ts = _toolset(mcp_project)

    envelope = ts.call("find_module", {"model": "nope", "module": "fifo"})

    assert envelope["ok"] is False
    assert "unknown model" in envelope["error"]
    assert envelope["models"] == ["blk_a", "blk_b"]


def test_hub_tools_report_a_dead_hub_rather_than_crashing(mcp_project: Path):
    """The handle said yes at start; the socket may still say no."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("hub_state", {})

    assert envelope["ok"] is False
    assert "hub" in envelope["error"].lower()


def test_hub_diagnose_validates_before_dialling(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("hub_diagnose", {"source": "agent"})

    assert envelope["ok"] is False
    assert "clear" in envelope["error"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_list_tools_machine_envelope(mcp_project: Path):
    rb = RtlBuddy(name="test_mcp_server")
    result = CliRunner().invoke(rb.app, ["--machine", "mcp", "--list-tools"])
    rb._artifact_locks.release_all()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert [tool["name"] for tool in payload["tools"]] == list(STATELESS_TOOL_NAMES)
    assert payload["hub"]["present"] is False
    assert payload["sdk"]["available"] is mcp_server.sdk_available()
    # camelCase on the wire, as MCP spells it.
    assert "inputSchema" in payload["tools"][0]


def test_a_missing_sdk_is_a_configuration_error_with_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
):
    from rtl_buddy.errors import FatalRtlBuddyError

    monkeypatch.setattr(mcp_server, "sdk_available", lambda: False)

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        mcp_server.require_sdk()

    assert "rtl_buddy[mcp]" in str(excinfo.value)
    assert "--machine" in str(excinfo.value)


# ---------------------------------------------------------------------------
# SDK boundary
# ---------------------------------------------------------------------------


@requires_sdk
def test_schemas_validate_against_the_sdk_tool_model(mcp_project: Path):
    import mcp.types as types

    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    tools = [types.Tool.model_validate(mcp_server.tool_payload(s)) for s in ts.specs()]

    assert [tool.name for tool in tools] == ts.names()


@requires_sdk
def test_a_failed_call_is_flagged_is_error_for_hosts_that_only_read_the_flag(
    mcp_project: Path,
):
    import mcp.types as types

    ts = _toolset(mcp_project)
    payload = mcp_server.result_payload(ts.call("graph_query", {}))
    result = types.CallToolResult.model_validate(payload)

    assert result.is_error is True
    assert json.loads(result.content[0].text)["ok"] is False


@requires_sdk
def test_build_server_wires_the_toolset_to_an_sdk_server(mcp_project: Path):
    ts = _toolset(mcp_project)

    server = mcp_server.build_server(ts)

    assert server.server_info.name == mcp_server.SERVER_NAME


@pytest.mark.skipif(
    not _HAS_SDK or shutil.which("rb") is None,
    reason="needs the `mcp` SDK and an installed `rb` entry point",
)
def test_stdio_server_lists_and_calls_tools_over_the_wire(mcp_project: Path):
    """The acceptance criterion: a fresh checkout, no hub, tools answer."""
    proc = subprocess.Popen(
        [shutil.which("rb"), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(mcp_project),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    def send(message: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, "server closed the stream"
        return json.loads(line)

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        assert "result" in recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = recv()
        assert [t["name"] for t in listed["result"]["tools"]] == list(
            STATELESS_TOOL_NAMES
        )

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "graph_query",
                    "arguments": {"question": "which tests cover A-COV-1"},
                },
            }
        )
        called = recv()
        envelope = json.loads(called["result"]["content"][0]["text"])
        assert envelope["ok"] is True
        assert envelope["payload"]["matches"][0]["id"] == "covitem:blk_a#A-COV-1"
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung server
            proc.kill()
            proc.wait(timeout=10)
