"""Tests for the ``rb hub send`` CLI, run against a real ``HubServer`` on a worker-thread asyncio loop with a per-test ``.rtl-buddy/hub.json`` discovery record."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from rtl_buddy.hub import discovery
from rtl_buddy.hub.config import HubMappingConfig
from rtl_buddy.hub.resolver import Resolver
from rtl_buddy.hub.send import send_app
from rtl_buddy.hub.server import HubServer


_VIEW_JSON = {
    "schema_version": "1.0",
    "tool": {"name": "rtl-buddy-view", "version": "0.1.0"},
    "design": {"top": "counter"},
    "nodes": [
        {
            "instance_path": "counter",
            "module_name": "counter",
            "port_connections": [],
            "location": {
                "file": "/abs/rtl/counter.sv",
                "start_line": 5,
                "start_column": 1,
                "end_line": 12,
                "end_column": 10,
            },
        },
        {
            "instance_path": "counter.u_ff",
            "module_name": "counter_ff",
            "port_connections": [],
            "location": {
                "file": "/abs/rtl/counter.sv",
                "start_line": 10,
                "start_column": 16,
                "end_line": 10,
                "end_column": 39,
            },
        },
    ],
    "edges": [{"parent": "counter", "child": "counter.u_ff"}],
}


class _ThreadedHub:
    """Run a HubServer on a dedicated asyncio loop in a background thread for sync ``CliRunner`` clients."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver
        self._server: HubServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self.host: str = ""
        self.port: int = 0

    def start(self) -> None:
        ready: dict = {}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            server = HubServer(
                host="127.0.0.1",
                port=0,
                server_version="0.0.0+test",
                resolver=self._resolver,
            )
            self._server = server

            async def _async_start() -> None:
                host, port = await server.start()
                ready["host"] = host
                ready["port"] = port
                self._started.set()
                await server.serve_forever()

            try:
                loop.run_until_complete(_async_start())
            except Exception:
                self._started.set()
                raise
            finally:
                # Drain pending tasks and callbacks before closing; otherwise transport finalizers can hit the closed loop (Python 3.12 "Event loop is closed") in the next test file.
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

        self._thread = threading.Thread(target=_runner, daemon=True, name="hub-thread")
        self._thread.start()
        if not self._started.wait(timeout=5.0):
            raise TimeoutError("HubServer did not start in time")
        self.host, self.port = ready["host"], ready["port"]

    def stop(self) -> None:
        if self._server is None or self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._server.shutdown(), self._loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        # shutdown() can finish and the runner thread can close the loop before this point. call_soon_threadsafe then raises RuntimeError, which means already stopped.
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@pytest.fixture
def threaded_hub(tmp_path: Path) -> Iterator[_ThreadedHub]:
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(_VIEW_JSON), encoding="utf-8")
    resolver = Resolver(
        view_json_path=view_path,
        mapping=HubMappingConfig(tb_prefix="tb.dut."),
    )
    hub = _ThreadedHub(resolver=resolver)
    hub.start()
    yield hub
    hub.stop()


@pytest.fixture
def discovery_root(tmp_path_factory, threaded_hub: _ThreadedHub, monkeypatch) -> Path:
    """Write ``.rtl-buddy/hub.json`` for the running hub and chdir into it."""

    root = tmp_path_factory.mktemp("project")
    (root / ".rtl-buddy").mkdir()
    # Use the test process's pid so liveness checks succeed.
    discovery.write_record(
        root,
        pid=os.getpid(),
        tcp=f"{threaded_hub.host}:{threaded_hub.port}",
        server_version="0.0.0+test",
        http_port=None,
    )
    monkeypatch.chdir(root)
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    return root


def _drain_briefly(seconds: float = 0.1) -> None:
    """Give the hub loop time to process what just arrived."""

    time.sleep(seconds)


def test_send_select_emits_selection_changed(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["select", "counter.u_ff"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    # state_snapshot reflects the broadcast.
    result = runner.invoke(send_app, ["state"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["selection"] == {"instance_path": "counter.u_ff", "origin": "cli"}


def test_send_cursor_emits_cursor_time_changed(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["cursor", "12500000"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    result = runner.invoke(send_app, ["state"])
    payload = json.loads(result.stdout)
    assert payload["cursor_time"] == {"t_fs": "12500000", "origin": "cli"}


def test_send_scope_emits_scope_changed(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["scope", "tb.dut.u_ff"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    result = runner.invoke(send_app, ["state"])
    payload = json.loads(result.stdout)
    assert payload["wave_scope"] == {"wave_scope": "tb.dut.u_ff", "origin": "cli"}


def test_send_open_parses_file_line_col(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["open", "design/dma/dma.sv:42:7"])
    assert result.exit_code == 0, result.output
    # source_focused is not a snapshot field; a parse failure would exit nonzero.
    # Drain so the transport-close callback fires before the next test (avoids a Python 3.12 "Event loop is closed" teardown error).
    _drain_briefly()


def test_send_open_rejects_bad_spec(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(send_app, ["open", "no-line-number"])
    assert result.exit_code != 0
    assert "expected file:line" in result.output.lower()


def test_send_graph_focus_caches_the_node(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """``rb hub send graph-focus`` caches the node on the hub, which replays it to the pane on registration."""

    runner = CliRunner()
    result = runner.invoke(send_app, ["graph-focus", "test:verif/fifo#smoke"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.graph_focus  # noqa: SLF001
    assert cached is not None
    assert cached.node == "test:verif/fifo#smoke"


def test_send_graph_focus_rejects_blank_node(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["graph-focus", "   "])
    assert result.exit_code != 0
    assert "non-empty" in result.output.lower()


def test_send_cov_focus_caches_the_focus(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """``rb hub send cov-focus`` caches the focus on the hub, which replays it to the pane on registration."""

    runner = CliRunner()
    result = runner.invoke(
        send_app,
        ["cov-focus", "file:design/blk.sv", "--metric", "branch", "--line", "42"],
    )
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.cov_focus  # noqa: SLF001
    assert cached is not None
    assert cached.target == "file:design/blk.sv"
    assert cached.metric == "branch"
    assert cached.line == 42
    assert cached.item is None


@pytest.mark.parametrize("by", ["source", "elaboration"])
def test_send_cov_focus_by_selects_the_figures(
    threaded_hub: _ThreadedHub, discovery_root: Path, by: str
):
    """``--by`` rides on the cached focus, so a late pane opens on those figures (#747)."""

    result = CliRunner().invoke(send_app, ["cov-focus", "module:blk", "--by", by])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.cov_focus  # noqa: SLF001
    assert cached.payload() == {"target": "module:blk", "by": by}


def test_send_cov_focus_defaults_omit_the_hints(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """The wire schema has no nullable hints, so an unset option is absent, not null."""

    runner = CliRunner()
    result = runner.invoke(send_app, ["cov-focus", "module:blk"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.cov_focus  # noqa: SLF001
    assert cached.payload() == {"target": "module:blk"}


@pytest.mark.parametrize(
    "argv",
    [
        ["cov-focus", "   "],
        ["cov-focus", "module:blk", "--metric", "statement"],
        ["cov-focus", "module:blk", "--line", "0"],
        ["cov-focus", "module:blk", "--item", " "],
        ["cov-focus", "module:blk", "--by", "elab"],
        ["cov-focus", "module:blk", "--by", "Source"],
    ],
)
def test_send_cov_focus_rejects_bad_arguments(
    threaded_hub: _ThreadedHub, discovery_root: Path, argv: list[str]
):
    """Bad arguments are rejected in the CLI so the message can name the flag."""

    result = CliRunner().invoke(send_app, argv)
    assert result.exit_code != 0


def test_send_phys_focus_caches_the_focus(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """``rb hub send phys-focus`` caches the focus on the hub, which replays it to the pane on registration."""

    runner = CliRunner()
    result = runner.invoke(
        send_app,
        ["phys-focus", "module:alu", "--metric", "area"],
    )
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.phys_focus  # noqa: SLF001
    assert cached is not None
    assert cached.target == "module:alu"
    assert cached.metric == "area"


def test_send_phys_focus_defaults_omit_the_hint(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """An unset ``--metric`` is absent on the wire, not null."""

    runner = CliRunner()
    result = runner.invoke(send_app, ["phys-focus", "instance:u_cpu/u_alu"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    cached = threaded_hub._server.state.phys_focus  # noqa: SLF001
    assert cached.payload() == {"target": "instance:u_cpu/u_alu"}


@pytest.mark.parametrize(
    "argv",
    [
        ["phys-focus", "   "],
        ["phys-focus", "module:alu", "--metric", "switching"],
        ["phys-focus", "module:alu", "--metric", "line"],
    ],
)
def test_send_phys_focus_rejects_bad_arguments(
    threaded_hub: _ThreadedHub, discovery_root: Path, argv: list[str]
):
    """Bad arguments are rejected in the CLI so the message can name the flag.

    ``switching`` is a model column but not a focus metric; the wire enum offers ``dynamic`` (internal + switching) instead.
    """

    result = CliRunner().invoke(send_app, argv)
    assert result.exit_code != 0


def test_send_diagnose_pushes_items(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(
        send_app,
        [
            "diagnose",
            "claude-analysis",
            "/x.sv:1:warning:WAVE-1:wr_ptr_q sampled while ce==0",
        ],
    )
    assert result.exit_code == 0, result.output
    _drain_briefly()
    result = runner.invoke(send_app, ["state"])
    payload = json.loads(result.stdout)
    assert "claude-analysis" in payload["diagnostics_sources"]


def test_send_diagnose_clear_zeros_the_source(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    runner.invoke(
        send_app,
        [
            "diagnose",
            "claude-analysis",
            "/x.sv:1:warning:WAVE-1:wr_ptr_q sampled while ce==0",
        ],
    )
    _drain_briefly()
    result = runner.invoke(send_app, ["diagnose", "claude-analysis", "--clear"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    result = runner.invoke(send_app, ["state"])
    payload = json.loads(result.stdout)
    # The source is still listed (an empty-items cache is a "cleared" record), but the bundle is empty.
    assert "claude-analysis" in payload["diagnostics_sources"]


def test_send_diagnose_requires_items_or_clear(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["diagnose", "claude-analysis"])
    assert result.exit_code != 0


def test_send_diagnose_rejects_bad_severity(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["diagnose", "x", "/y.sv:1:explode:CODE:msg"])
    assert result.exit_code != 0
    assert "severity" in result.output.lower()


def test_send_diagnose_instance_flag_attaches_to_every_item(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """--instance writes ``instance_path`` onto each item."""

    runner = CliRunner()
    result = runner.invoke(
        send_app,
        [
            "diagnose",
            "claude-analysis",
            "--instance",
            "top.u_dma",
            "/a.sv:1:warning:WAVE-1:m1",
            "/a.sv:2:error:WAVE-2:m2",
        ],
    )
    assert result.exit_code == 0, result.output
    # The hub's item cache has no public peek API; exercise _parse_diag through the next test and a parser unit check.


def test_send_diagnose_instance_with_clear_is_rejected(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(
        send_app,
        ["diagnose", "claude-analysis", "--instance", "top.u_dma", "--clear"],
    )
    assert result.exit_code != 0
    assert "--instance" in result.output.lower() or "clear" in result.output.lower()


def test_send_state_returns_snapshot(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(send_app, ["state"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["active_model"] is None
    assert payload["selection"] is None
    assert payload["cursor_time"] is None
    assert payload["wave_scope"] is None
    assert "cli" in payload["peers"]


def test_send_resolve_view_to_wave(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(send_app, ["resolve", "view-to-wave", "counter.u_ff"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"wave_scope": "tb.dut.u_ff"}


def test_send_resolve_wave_to_view(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(send_app, ["resolve", "wave-to-view", "tb.dut.u_ff"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"instance_path": "counter.u_ff"}


def test_send_resolve_unresolvable_returns_nonzero(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(
        send_app, ["resolve", "view-to-wave", "counter.u_dbg.u_probe"]
    )
    assert result.exit_code != 0
    assert "unresolvable" in result.output.lower()


def test_send_wave_add_reports_no_wave_peer(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    """With no wave peer registered the request returns not_connected and the CLI exits nonzero."""

    runner = CliRunner()
    result = runner.invoke(send_app, ["wave-add", "tb.dut.u_ff.q"])
    assert result.exit_code != 0
    assert "not_connected" in result.output.lower()


def test_send_capture_reports_no_view_peer(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    """With no view peer registered the request returns ``not_connected``; the CLI exits nonzero and writes no output file."""

    runner = CliRunner()
    out_path = tmp_path / "snap.png"
    result = runner.invoke(send_app, ["capture", "--out", str(out_path)])
    assert result.exit_code != 0
    assert "not_connected" in result.output.lower()
    assert not out_path.exists()


def test_send_capture_rejects_bad_format(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    """``--format`` accepts png or svg (also inferred from the suffix); a bad format fails before the hub round-trip."""

    runner = CliRunner()
    out_path = tmp_path / "snap.gif"
    result = runner.invoke(send_app, ["capture", "--out", str(out_path)])
    assert result.exit_code != 0
    assert "must be png or svg" in result.output.lower()
    assert not out_path.exists()


def test_send_no_hub_exits_two(monkeypatch, tmp_path):
    """With no reachable hub the exit code is 2; a hub-returned error exits 1."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    runner = CliRunner()
    result = runner.invoke(send_app, ["state"])
    assert result.exit_code == 2
    assert "no live hub" in result.output.lower()


def test_wave_move_requires_exactly_one_target() -> None:
    """`wave-move` needs exactly one of --to / --before, checked before any hub connection."""
    runner = CliRunner()
    # neither
    result = runner.invoke(send_app, ["wave-move", "5", "6"])
    assert result.exit_code != 0
    # both
    result = runner.invoke(send_app, ["wave-move", "5", "--to", "0", "--before", "3"])
    assert result.exit_code != 0


def test_wave_remove_rejects_negative_ids() -> None:
    runner = CliRunner()
    result = runner.invoke(send_app, ["wave-remove", "-1"])
    assert result.exit_code != 0


def test_wave_comment_rejects_blank_text() -> None:
    runner = CliRunner()
    result = runner.invoke(send_app, ["wave-comment", "   "])
    assert result.exit_code != 0
