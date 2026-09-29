"""Tests for the rtl-buddy-cdc to hub diagnostics publisher.

Covers ``build_items_from_cdc_report`` (pure translation to wire-shaped items) and ``publish_cdc_report`` (end to end against a real HubServer on a background thread, including the no-op when no hub is reachable).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Iterator

import pytest

from rtl_buddy.hub import discovery
from rtl_buddy.hub.protocol import Origin, decode
from rtl_buddy.hub.server import HubServer
from rtl_buddy.tools.cdc_publisher import (
    build_items_from_cdc_report,
    publish_cdc_report,
)


def _minimal_report(violations: list[dict]) -> dict:
    return {
        "tool": {"name": "rtl-buddy-cdc", "version": "0.1.0"},
        "module": "ip_dut",
        "summary": {
            "violations": len(violations),
            "suppressed": 0,
            "crossings": 0,
        },
        "violations": violations,
    }


def test_build_items_from_minimal_violation():
    report = _minimal_report(
        [
            {
                "rule_id": "CDC-001",
                "severity": "error",
                "message": "missing 2FF synchronizer",
                "cell_name": "u_sync",
                "instance_path": ["top", "u_dma", "u_rb"],
                "location": {
                    "file": "/abs/dma.sv",
                    "start_line": 142,
                    "start_column": 5,
                    "end_line": 144,
                    "end_column": 10,
                },
            }
        ]
    )
    items = build_items_from_cdc_report(report)
    assert items == [
        {
            "file": "/abs/dma.sv",
            "line": 142,
            "severity": "error",
            "message": "missing 2FF synchronizer",
            "col": 5,
            "end_line": 144,
            "end_col": 10,
            "code": "CDC-001",
            "instance_path": "top.u_dma.u_rb",
        }
    ]


def test_build_items_drops_violations_without_location():
    """A violation without a file fails schema validation and cannot be anchored, so it is dropped."""

    report = _minimal_report(
        [
            {
                "rule_id": "CDC-002",
                "severity": "warning",
                "message": "sync depth too shallow",
                "instance_path": ["top"],
            }
        ]
    )
    assert build_items_from_cdc_report(report) == []


def test_build_items_drops_violations_with_empty_file():
    report = _minimal_report(
        [
            {
                "rule_id": "CDC-003",
                "severity": "info",
                "message": "m",
                "location": {"file": "", "start_line": 1},
            }
        ]
    )
    assert build_items_from_cdc_report(report) == []


def test_build_items_drops_violations_with_bad_severity():
    report = _minimal_report(
        [
            {
                "rule_id": "CDC-004",
                "severity": "FATAL",
                "message": "m",
                "location": {"file": "/a.sv", "start_line": 1},
            }
        ]
    )
    assert build_items_from_cdc_report(report) == []


def test_build_items_skips_instance_path_when_empty():
    report = _minimal_report(
        [
            {
                "rule_id": "CDC-005",
                "severity": "warning",
                "message": "m",
                "instance_path": [],
                "location": {"file": "/a.sv", "start_line": 1},
            }
        ]
    )
    items = build_items_from_cdc_report(report)
    assert items == [
        {
            "file": "/a.sv",
            "line": 1,
            "severity": "warning",
            "message": "m",
            "code": "CDC-005",
        }
    ]


def test_build_items_handles_missing_violations_key():
    """A report without a violations[] field returns []."""

    assert build_items_from_cdc_report({"summary": {"violations": 0}}) == []
    assert build_items_from_cdc_report({"violations": None}) == []


def test_build_items_handles_multiple_violations():
    report = _minimal_report(
        [
            {
                "rule_id": "CDC-001",
                "severity": "error",
                "message": "a",
                "instance_path": ["top", "u_dma"],
                "location": {"file": "/a.sv", "start_line": 10},
            },
            {
                "rule_id": "CDC-002",
                "severity": "warning",
                "message": "b",
                "instance_path": ["top", "u_cred"],
                "location": {"file": "/b.sv", "start_line": 20, "start_column": 3},
            },
        ]
    )
    items = build_items_from_cdc_report(report)
    assert len(items) == 2
    assert items[0]["instance_path"] == "top.u_dma"
    assert items[1]["instance_path"] == "top.u_cred"
    assert items[1]["col"] == 3


class _ThreadedHub:
    """Run a HubServer on a dedicated asyncio loop in a thread, without the resolver."""

    def __init__(self) -> None:
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
            server = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
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
                # Drain pending transport callbacks (Python 3.12), or they fire against a closed loop in sibling files' teardowns.
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

        self._thread = threading.Thread(target=_runner, daemon=True, name="hub")
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
        # shutdown() can let the runner thread close the loop first; treat RuntimeError as already stopped.
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@pytest.fixture(scope="module")
def threaded_hub() -> Iterator[_ThreadedHub]:
    """Module-scoped: Python 3.12 asyncio errors on transport callbacks after loop close, so one hub serves the whole file."""

    h = _ThreadedHub()
    h.start()
    yield h
    h.stop()


@pytest.fixture
def discovery_root(tmp_path_factory, threaded_hub: _ThreadedHub, monkeypatch) -> Path:
    root = tmp_path_factory.mktemp("project")
    (root / ".rtl-buddy").mkdir()
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


def test_publish_cdc_report_pushes_violations_to_running_hub(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    """The publisher pushes a diagnostics_set that the hub broadcasts to a peer that said hello first."""

    report = _minimal_report(
        [
            {
                "rule_id": "CDC-001",
                "severity": "error",
                "message": "async crossing",
                "instance_path": ["top", "u_dma"],
                "location": {"file": "/abs/x.sv", "start_line": 7},
            }
        ]
    )
    json_path = tmp_path / "cdc.json"
    json_path.write_text(json.dumps(report))

    # A raw socket subscribes the view peer, since the blocking HubClient.connect() cannot share the loop thread with publish.
    import socket as _socket
    from rtl_buddy.hub.protocol import encode, make_hello

    sock = _socket.create_connection((threaded_hub.host, threaded_hub.port))
    sock.sendall(
        encode(make_hello(client=Origin.VIEW, version="0.0", capabilities=[])).encode()
        + b"\n"
    )
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        assert chunk
        buf += chunk
    welcome_line, _, buf = buf.partition(b"\n")
    assert decode(welcome_line).type == "welcome"

    ok = publish_cdc_report(analysis_name="ip_dma_lint", json_report_path=json_path)
    assert ok is True

    # Skip lifecycle events (such as the publisher's peer_joined) until the diagnostics_set arrives.
    env = None
    for _ in range(8):
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            assert chunk
            buf += chunk
        event_line, _, buf = buf.partition(b"\n")
        env = decode(event_line)
        if env.type == "diagnostics_set":
            break
    assert env is not None and env.type == "diagnostics_set"
    assert env.payload["source"] == "rb-cdc:ip_dma_lint"
    assert len(env.payload["items"]) == 1
    item = env.payload["items"][0]
    assert item["code"] == "CDC-001"
    assert item["instance_path"] == "top.u_dma"
    assert item["file"] == "/abs/x.sv"
    assert item["line"] == 7

    sock.close()


def test_publish_cdc_report_empty_violations_is_a_clear(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    """Empty items[] is the `cleared source` signal for a clean re-run."""

    json_path = tmp_path / "cdc.json"
    json_path.write_text(json.dumps(_minimal_report([])))

    ok = publish_cdc_report(analysis_name="ip_dma_lint", json_report_path=json_path)
    assert ok is True


def test_publish_cdc_report_silently_skips_when_no_hub(tmp_path: Path, monkeypatch):
    """With no hub.json and no $RTL_BUDDY_HUB, publishing returns False without raising."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)

    json_path = tmp_path / "cdc.json"
    json_path.write_text(json.dumps(_minimal_report([])))

    ok = publish_cdc_report(analysis_name="ip_dma_lint", json_report_path=json_path)
    assert ok is False


def test_publish_cdc_report_silently_skips_when_report_missing(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    ok = publish_cdc_report(
        analysis_name="ip_dma_lint",
        json_report_path=tmp_path / "nope.json",
    )
    assert ok is False


def test_publish_cdc_report_silently_skips_on_malformed_json(
    threaded_hub: _ThreadedHub, discovery_root: Path, tmp_path: Path
):
    json_path = tmp_path / "cdc.json"
    json_path.write_text("{ not valid json")
    ok = publish_cdc_report(analysis_name="ip_dma_lint", json_report_path=json_path)
    assert ok is False
