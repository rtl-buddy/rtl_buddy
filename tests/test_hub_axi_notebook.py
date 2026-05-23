"""Tests for the ``/api/axi-profile/notebook`` hub HTTP endpoint
and its underlying launcher.

The endpoint spawns ``rb axi-profile notebook --headless`` (which
forks marimo). We can't run the real marimo in CI — and don't need
to — so the launcher's subprocess is monkeypatched to a fake that
prints a URL line on stdout. That exercises every code path
(stdout reader, URL regex, timeout, validation) without depending
on the [notebook] extra being installed in the test env.

The route-level tests poke ``_handle_axi_notebook`` directly with
a stub ``ServerConnection`` rather than spinning up the full
websockets server — the goal is to lock the route's contract
(query parse, status codes, JSON body), not the IO plumbing.
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
    """A barely-valid suite_dir — tests.yaml present so validation
    doesn't reject it."""
    suite = tmp_path / "verif" / "demo"
    suite.mkdir(parents=True)
    (suite / "tests.yaml").write_text(
        "rtl-buddy-filetype: test_config\ntestbenches: []\ntests: []\n"
    )
    return suite


def _fake_marimo(tmp_path: Path, *, url: str | None, exit_after: bool = False) -> Path:
    """Drop a shell script that mimics ``marimo edit``'s startup:
    optionally print a URL line, optionally exit. Returned path is
    executable so subprocess.Popen can run it."""
    sh = tmp_path / "fake_rb"
    body = ["#!/usr/bin/env bash", "echo 'Update available 0.23.7 -> 0.23.8'"]
    if url:
        body.append(f"echo 'URL: {url}'")
    if not exit_after:
        # Block forever so the URL-reader doesn't hit EOF.
        body.append("sleep 60")
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
    """Happy path: a fake "marimo" prints the URL on stdout and the
    launcher returns it without waiting for the process to exit."""
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
    # Clean up the background fake_marimo so it doesn't hang around
    # 60s after the test exits.
    try:
        os.kill(result.pid, 9)
    except ProcessLookupError:
        pass


def test_launch_raises_when_subprocess_exits_before_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-on-startup path: subprocess exits before printing the
    URL line. Launcher surfaces a 500 with marimo's exit code in
    the message so the SPA's error toast is actionable."""
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
    """Watchdog path: subprocess runs forever and never prints a URL.
    Should kill the subprocess and return a 504."""
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
    """Setup-error path: marimo binary not installed. Surfaces 503
    with the [notebook] extra install hint."""
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


# ---------------------------------------------------------------------------
# Route-level smoke
# ---------------------------------------------------------------------------


class _StubConnection:
    """Just enough of websockets' ServerConnection to satisfy
    ``_http_response`` — which is the only thing the route handler
    calls on it."""

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
    """End-to-end at the route layer: with a working fake-marimo, the
    handler returns a 200 + JSON containing the URL the SPA needs."""
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
    # Background fake_marimo cleanup.
    try:
        os.kill(payload["pid"], 9)
    except ProcessLookupError:
        pass
