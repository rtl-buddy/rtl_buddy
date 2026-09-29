"""Tests for the ``/api/axi-profile/notebook`` hub endpoint and its launcher.

The launcher's subprocess is monkeypatched to a fake that prints a URL line, so
the stdout reader, URL regex, timeout and validation run without marimo. Route
tests call ``_handle_axi_notebook`` with a stub ``ServerConnection`` to pin the
query parse, status codes and JSON body.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from rtl_buddy.hub import axi_notebook_launcher
from rtl_buddy.hub.axi_notebook_launcher import AxiNotebookLaunchError


def _write_suite(tmp_path: Path) -> Path:
    """A minimal valid suite_dir: tests.yaml present."""
    suite = tmp_path / "verif" / "demo"
    suite.mkdir(parents=True)
    (suite / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\ntestbenches: []\ntests: []\n"
    )
    return suite


def _fake_marimo(tmp_path: Path, *, url: str | None, exit_after: bool = False) -> Path:
    """Drop a Python script mimicking ``marimo edit`` startup: optional URL line, optional exit.

    It is Python rather than bash because the shutdown-cleanup test SIGTERMs the
    process and needs a prompt exit; bash defers signals and was unreliable on busy
    CI runners.
    """
    sh = tmp_path / "fake_marimo.py"
    body = [
        "#!/usr/bin/env python3",
        "import signal, sys, time",
        "print('Update available 0.23.7 -> 0.23.8', flush=True)",
    ]
    if url:
        body.append(f"print('URL: {url}', flush=True)")
    if not exit_after:
        # Block until SIGTERM or SIGINT; the handler exits 0.
        body.append("signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))")
        body.append("signal.signal(signal.SIGINT, lambda *_: sys.exit(0))")
        body.append("time.sleep(60)")
    sh.write_text("\n".join(body) + "\n")
    sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sh


def test_validate_test_name_rejects_shell_metacharacters() -> None:
    with pytest.raises(AxiNotebookLaunchError) as exc:
        axi_notebook_launcher._validate_test_name("foo; rm -rf /")
    assert exc.value.status == 400
    assert "unexpected characters" in str(exc.value)


def test_validate_test_name_accepts_normal_identifiers() -> None:
    assert axi_notebook_launcher._validate_test_name("basic_traffic") == "basic_traffic"
    assert axi_notebook_launcher._validate_test_name("test-1.v2") == "test-1.v2"


def test_validate_suite_dir_rejects_path_traversal(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    with pytest.raises(AxiNotebookLaunchError) as exc:
        axi_notebook_launcher._validate_suite_dir(str(suite), other_root)
    assert exc.value.status == 400
    assert "project_root" in str(exc.value)


def test_validate_suite_dir_rejects_missing_tests_yaml(tmp_path: Path) -> None:
    suite = tmp_path / "noyaml"
    suite.mkdir()
    with pytest.raises(AxiNotebookLaunchError) as exc:
        axi_notebook_launcher._validate_suite_dir(str(suite), tmp_path)
    assert "tests.yaml" in str(exc.value)


def test_validate_suite_dir_accepts_valid_relative_path(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    resolved = axi_notebook_launcher._validate_suite_dir("verif/demo", tmp_path)
    assert resolved == (tmp_path / "verif" / "demo").resolve()


def test_launch_returns_url_when_subprocess_prints_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher returns the URL the fake marimo prints without waiting for exit."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    result = asyncio.run(
        axi_notebook_launcher.launch(
            test="basic", suite_dir=str(suite), project_root=tmp_path, timeout_s=5.0
        )
    )
    assert result.url == "http://localhost:31337"
    assert result.test == "basic"
    assert result.pid > 0
    # Clean up the background fake_marimo.
    try:
        os.kill(result.pid, 9)
    except ProcessLookupError:
        pass


def test_launch_propagates_events_url_to_subprocess_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``events_url`` is exported as ``RB_HUB_EVENTS_URL`` to the subprocess; when
    omitted, no env var leaks."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )
    captured: dict[str, dict[str, str]] = {}
    real_create = axi_notebook_launcher.asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(axi_notebook_launcher.asyncio, "create_subprocess_exec", spy)

    result = asyncio.run(
        axi_notebook_launcher.launch(
            test="basic",
            suite_dir=str(suite),
            project_root=tmp_path,
            timeout_s=5.0,
            events_url="ws://127.0.0.1:7000/api/events/sync",
        )
    )
    assert captured["env"]["RB_HUB_EVENTS_URL"] == "ws://127.0.0.1:7000/api/events/sync"
    try:
        os.kill(result.pid, 9)
    except ProcessLookupError:
        pass

    # Second pass without events_url: env stays clean.
    captured.clear()
    result2 = asyncio.run(
        axi_notebook_launcher.launch(
            test="basic",
            suite_dir=str(suite),
            project_root=tmp_path,
            timeout_s=5.0,
        )
    )
    assert "RB_HUB_EVENTS_URL" not in captured["env"]
    try:
        os.kill(result2.pid, 9)
    except ProcessLookupError:
        pass


def test_launch_raises_when_subprocess_exits_before_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess exiting before the URL surfaces a 500 with marimo's exit code."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url=None, exit_after=True)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    with pytest.raises(AxiNotebookLaunchError) as exc:
        asyncio.run(
            axi_notebook_launcher.launch(
                test="basic",
                suite_dir=str(suite),
                project_root=tmp_path,
                timeout_s=5.0,
            )
        )
    assert exc.value.status == 500
    assert "exited" in str(exc.value)


def test_launch_times_out_when_subprocess_hangs_without_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess that never prints a URL is killed and returns a 504."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url=None)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    with pytest.raises(AxiNotebookLaunchError) as exc:
        asyncio.run(
            axi_notebook_launcher.launch(
                test="basic",
                suite_dir=str(suite),
                project_root=tmp_path,
                timeout_s=0.5,
            )
        )
    assert exc.value.status == 504


def test_launch_503_when_marimo_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing marimo binary surfaces a 503 with the [notebook] extra install hint."""
    _write_suite(tmp_path)
    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: None)
    with pytest.raises(AxiNotebookLaunchError) as exc:
        asyncio.run(
            axi_notebook_launcher.launch(
                test="basic", suite_dir="verif/demo", project_root=tmp_path
            )
        )
    assert exc.value.status == 503
    assert "[notebook]" in str(exc.value)


# Route-level smoke


class _StubConnection:
    """Enough of websockets' ServerConnection for ``_http_response``."""

    request: Any = None


def _make_viewer_server(project_root: Path) -> Any:
    from rtl_buddy.hub.viewer_http import ViewerServer

    return ViewerServer(hub_host="127.0.0.1", hub_port=0, project_root=project_root)


def test_route_returns_400_for_missing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(tmp_path)
    server = _make_viewer_server(tmp_path)
    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")

    resp = asyncio.run(
        server._handle_axi_notebook(_StubConnection(), {"suite_dir": [str(suite)]})
    )
    assert resp.status_code == 400
    assert b"test is required" in resp.body


def test_route_returns_500_when_project_root_unset(tmp_path: Path) -> None:
    from rtl_buddy.hub.viewer_http import ViewerServer

    server = ViewerServer(hub_host="127.0.0.1", hub_port=0, project_root=None)
    resp = asyncio.run(
        server._handle_axi_notebook(
            _StubConnection(), {"test": ["basic"], "suite_dir": ["verif/demo"]}
        )
    )
    assert resp.status_code == 500
    body = json.loads(resp.body)
    assert "project_root" in body["error"]


def test_route_returns_json_url_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a working fake marimo the route returns 200 and JSON with the URL."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")
    server = _make_viewer_server(tmp_path)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    resp = asyncio.run(
        server._handle_axi_notebook(
            _StubConnection(),
            {"test": ["basic"], "suite_dir": [str(suite)]},
        )
    )
    assert resp.status_code == 200
    payload = json.loads(resp.body)
    assert payload["url"] == "http://localhost:31337"
    assert payload["test"] == "basic"
    assert payload["pid"] > 0
    assert payload["reused"] is False
    # Background fake_marimo cleanup.
    try:
        os.kill(payload["pid"], 9)
    except ProcessLookupError:
        pass


# Session reuse + shutdown cleanup


def test_repeat_request_for_same_test_reuses_cached_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat request for the same (test, suite_dir) returns the same url, pid and
    port without a new spawn."""
    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")
    server = _make_viewer_server(tmp_path)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    query = {"test": ["basic"], "suite_dir": [str(suite)]}
    first = json.loads(
        asyncio.run(server._handle_axi_notebook(_StubConnection(), query)).body
    )
    second = json.loads(
        asyncio.run(server._handle_axi_notebook(_StubConnection(), query)).body
    )

    assert first["reused"] is False
    assert second["reused"] is True
    assert second["pid"] == first["pid"]
    assert second["url"] == first["url"]
    assert second["port"] == first["port"]
    assert len(server._axi_notebook_sessions) == 1
    try:
        os.kill(first["pid"], 9)
    except ProcessLookupError:
        pass


def test_cache_drops_stale_entry_and_respawns_when_pid_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache entry whose pid is dead is dropped and respawned.

    ``_is_pid_alive`` is mocked; real SIGKILL and reap timing was flaky in CI.
    """
    from rtl_buddy.hub import viewer_http

    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")
    server = _make_viewer_server(tmp_path)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    query = {"test": ["basic"], "suite_dir": [str(suite)]}
    first = json.loads(
        asyncio.run(server._handle_axi_notebook(_StubConnection(), query)).body
    )
    # Force the stale-cache branch.
    monkeypatch.setattr(viewer_http, "_is_pid_alive", lambda pid: False)

    second = json.loads(
        asyncio.run(server._handle_axi_notebook(_StubConnection(), query)).body
    )
    assert second["reused"] is False
    assert second["pid"] != first["pid"]
    for pid in (first["pid"], second["pid"]):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_shutdown_calls_terminate_on_every_session_and_clears_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ViewerServer.shutdown()`` SIGTERMs every spawned marimo and clears the session cache.

    ``_terminate_pid`` is mocked to record calls; real signal delivery timing was
    flaky under CI load.
    """
    from rtl_buddy.hub import viewer_http

    suite = _write_suite(tmp_path)
    fake = _fake_marimo(tmp_path, url="http://localhost:31337")
    server = _make_viewer_server(tmp_path)

    monkeypatch.setattr(axi_notebook_launcher.shutil, "which", lambda _: "marimo")
    monkeypatch.setattr(
        axi_notebook_launcher,
        "_build_cmd",
        lambda *, suite_dir, test, port: [str(fake)],
    )

    payload = json.loads(
        asyncio.run(
            server._handle_axi_notebook(
                _StubConnection(),
                {"test": ["basic"], "suite_dir": [str(suite)]},
            )
        ).body
    )
    pid = payload["pid"]
    assert os.kill(pid, 0) is None  # spawned + alive

    terminated: list[int] = []
    monkeypatch.setattr(viewer_http, "_terminate_pid", terminated.append)

    asyncio.run(server.shutdown())

    assert server._axi_notebook_sessions == {}
    assert terminated == [pid]

    # Clean up the still-alive fake_marimo; the mock prevented the real SIGTERM.
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def test_terminate_pid_sends_sigterm_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_terminate_pid`` sends SIGTERM to the given pid."""
    import os as _os
    import signal

    from rtl_buddy.hub import viewer_http

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(_os, "kill", lambda p, s: sent.append((p, s)))
    viewer_http._terminate_pid(12345)
    assert sent == [(12345, signal.SIGTERM)]


def test_terminate_pid_swallows_process_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_terminate_pid`` swallows a process that already exited."""
    import os as _os

    from rtl_buddy.hub import viewer_http

    def boom(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(_os, "kill", boom)
    # Should not raise.
    viewer_http._terminate_pid(99999)
