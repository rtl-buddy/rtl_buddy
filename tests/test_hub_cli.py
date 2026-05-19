"""CLI smoke tests for ``rb hub`` subcommands.

These exercise option parsing, exit codes, and the read-side of the
discovery + config layers. The server loop itself lands in PR 2 of
rtl-buddy/rtl_buddy#115 and is exercised separately.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.hub import discovery
from rtl_buddy.hub.cli import app as hub_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp dir with the minimum project markers: a ``.git`` directory.

    The hub CLI uses ``discover_project_root()`` which accepts either
    ``root_config.yaml`` or ``.git``; we use ``.git`` to keep the
    fixture self-contained.
    """

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_help_lists_subcommands(runner: CliRunner):
    result = runner.invoke(hub_app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "start" in result.output
    assert "stop" in result.output
    assert "status" in result.output
    assert "log" in result.output
    assert "config" in result.output


def test_config_help_lists_validate(runner: CliRunner):
    result = runner.invoke(hub_app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "validate" in result.output


def test_config_validate_missing_file_is_ok(runner: CliRunner, project_root: Path):
    result = runner.invoke(hub_app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "defaults" in result.output


def test_config_validate_good_file(runner: CliRunner, project_root: Path):
    cfg_dir = project_root / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.toml").write_text(
        '[hub]\nlisten_port = 0\n[mapping]\ntb_prefix = "tb.dut."\n',
        encoding="utf-8",
    )
    result = runner.invoke(hub_app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "ok:" in result.output
    assert "tb.dut." in result.output


def test_config_validate_bad_file(runner: CliRunner, project_root: Path):
    cfg_dir = project_root / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.toml").write_text("[unknown_section]\nx = 1\n", encoding="utf-8")
    result = runner.invoke(hub_app, ["config", "validate"])
    assert result.exit_code == 1, result.output
    assert "unknown" in result.output.lower()


def test_status_no_hub(runner: CliRunner, project_root: Path):
    result = runner.invoke(hub_app, ["status"])
    assert result.exit_code == 1, result.output
    assert "no hub running" in result.output


def test_status_running_hub(runner: CliRunner, project_root: Path):
    discovery.write_record(
        project_root,
        pid=os.getpid(),
        tcp="127.0.0.1:54321",
        server_version="0.1.0",
    )
    result = runner.invoke(hub_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "RUNNING" in result.output
    assert "127.0.0.1:54321" in result.output


def test_status_stale_record(runner: CliRunner, project_root: Path):
    # Use a pid that is virtually guaranteed to be dead.
    cfg_dir = project_root / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.json").write_text(
        '{"v": 1, "pid": 999999, "tcp": "127.0.0.1:1",'
        ' "server_version": "0.1.0",'
        f' "project_root": "{project_root}",'
        ' "started_at": "2026-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    result = runner.invoke(hub_app, ["status"])
    assert result.exit_code == 1, result.output
    assert "STALE" in result.output


def test_stop_with_no_hub(runner: CliRunner, project_root: Path):
    result = runner.invoke(hub_app, ["stop"])
    assert result.exit_code == 1, result.output
    assert "no hub running" in result.output


def test_stop_clears_stale_record(runner: CliRunner, project_root: Path):
    cfg_dir = project_root / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.json").write_text(
        '{"v": 1, "pid": 999999, "tcp": "127.0.0.1:1",'
        ' "server_version": "0.1.0",'
        f' "project_root": "{project_root}",'
        ' "started_at": "2026-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    result = runner.invoke(hub_app, ["stop"])
    assert result.exit_code == 1, result.output
    assert not (cfg_dir / "hub.json").exists()


def test_start_runs_preflight_only_when_blocked(runner: CliRunner, project_root: Path):
    """A live hub already registered must block start without entering the loop.

    Writing a record for this process's own PID is the cheap way to
    trip the `HubAlreadyRunningError` path — it short-circuits before
    `loop.serve` is reached, so the test doesn't need to actually run
    the asyncio loop in the CliRunner.
    """

    discovery.write_record(
        project_root,
        pid=os.getpid(),
        tcp="127.0.0.1:65000",
        server_version="0.0.0+test",
    )
    result = runner.invoke(hub_app, ["start"])
    assert result.exit_code != 0
    assert result.exception is not None
    assert "already running" in str(result.exception)


def test_log_missing_file(runner: CliRunner, project_root: Path):
    result = runner.invoke(hub_app, ["log"])
    assert result.exit_code == 1, result.output
    assert "log not found" in result.output


def test_log_prints_tail(runner: CliRunner, project_root: Path):
    cfg_dir = project_root / ".rtl-buddy"
    cfg_dir.mkdir()
    (cfg_dir / "hub.log").write_text("line-a\nline-b\nline-c\n", encoding="utf-8")
    result = runner.invoke(hub_app, ["log", "-n", "2"])
    assert result.exit_code == 0, result.output
    assert "line-b" in result.output
    assert "line-c" in result.output
    assert "line-a" not in result.output
