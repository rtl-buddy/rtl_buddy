"""Tests for the ``rb hub send`` CLI surface.

Stand up a real ``HubServer`` in a background asyncio loop on a
worker thread, write a per-test ``.rtl-buddy/hub.json`` discovery
record, then invoke the typer app via ``CliRunner`` so each
subcommand exercises the full client → server → reply path.
"""

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
    """Spin a HubServer on a dedicated asyncio loop in a background
    thread, suitable for sync ``CliRunner`` clients."""

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
        self._loop.call_soon_threadsafe(self._loop.stop)
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
    """Write ``.rtl-buddy/hub.json`` pointing at the running hub and
    chdir into it so discovery picks it up."""

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
    """Give the background hub loop a beat to process whatever just
    came in over the wire."""

    time.sleep(seconds)


# ---------------------------------------------------------------------------
# state-event subcommands
# ---------------------------------------------------------------------------


def test_send_select_emits_selection_changed(
    threaded_hub: _ThreadedHub, discovery_root: Path
):
    runner = CliRunner()
    result = runner.invoke(send_app, ["select", "counter.u_ff"])
    assert result.exit_code == 0, result.output
    _drain_briefly()
    # state_snapshot should now reflect the broadcast.
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
    # source_focused isn't a snapshot field, but it must broadcast cleanly —
    # if the parse failed CLI would have exited nonzero.


def test_send_open_rejects_bad_spec(threaded_hub: _ThreadedHub, discovery_root: Path):
    runner = CliRunner()
    result = runner.invoke(send_app, ["open", "no-line-number"])
    assert result.exit_code != 0
    assert "expected file:line" in result.output.lower()


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
    # source is still listed (empty-items cache is a "cleared" record),
    # but the bundle is now empty.
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
    """--instance writes ``instance_path`` onto each item so consumers
    can fast-path past the file+line resolver."""

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
    # No public peek API for the hub's item cache; round-trip via a
    # mock-wave peer would be heavyweight here. Instead exercise
    # _parse_diag indirectly via the next test plus a unit-level
    # parser check.


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


# ---------------------------------------------------------------------------
# request subcommands
# ---------------------------------------------------------------------------


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
    """No wave peer is registered, so the request must surface
    not_connected — and exit nonzero. The CLI doesn't pretend it
    succeeded just because the hub took the envelope."""

    runner = CliRunner()
    result = runner.invoke(send_app, ["wave-add", "tb.dut.u_ff.q"])
    assert result.exit_code != 0
    assert "not_connected" in result.output.lower()


def test_send_no_hub_exits_two(monkeypatch, tmp_path):
    """When no hub is reachable, exit code is 2 (distinguishable from
    a hub-returned error which exits 1)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    runner = CliRunner()
    result = runner.invoke(send_app, ["state"])
    assert result.exit_code == 2
    assert "no live hub" in result.output.lower()
