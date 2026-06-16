"""Tests for ``rtl_buddy.hub.discovery`` — hub.json lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rtl_buddy.hub.discovery import (
    HubAlreadyRunningError,
    HubDiscoveryError,
    delete_record_if_owner,
    discovery_path,
    env_override,
    find_project_root_with_hub,
    read_record,
    write_record,
)


def test_write_and_read_record_round_trip(tmp_path: Path):
    rec = write_record(
        tmp_path,
        pid=os.getpid(),
        tcp="127.0.0.1:54321",
        server_version="0.1.0",
    )
    assert rec.pid == os.getpid()
    assert discovery_path(tmp_path).exists()

    loaded = read_record(tmp_path)
    assert loaded is not None
    assert loaded.tcp == "127.0.0.1:54321"
    assert loaded.server_version == "0.1.0"
    assert Path(loaded.project_root).resolve() == tmp_path.resolve()
    # started_at is ISO-8601 with 'T' separator and a timezone designator.
    assert "T" in loaded.started_at


def test_read_record_missing_returns_none(tmp_path: Path):
    assert read_record(tmp_path) is None


def test_read_record_corrupt_raises(tmp_path: Path):
    target = discovery_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("not json", encoding="utf-8")
    with pytest.raises(HubDiscoveryError):
        read_record(tmp_path)


def test_read_record_missing_field_raises(tmp_path: Path):
    target = discovery_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"v": 1, "pid": 123}), encoding="utf-8")
    with pytest.raises(HubDiscoveryError, match="missing or malformed field"):
        read_record(tmp_path)


def test_second_write_with_live_pid_refused(tmp_path: Path):
    write_record(tmp_path, pid=os.getpid(), tcp="127.0.0.1:1", server_version="0.1.0")
    with pytest.raises(HubAlreadyRunningError):
        write_record(
            tmp_path, pid=os.getpid(), tcp="127.0.0.1:2", server_version="0.1.0"
        )


def test_second_write_with_dead_pid_overwrites(tmp_path: Path):
    # First write a record with a guaranteed-dead pid (the helper does the
    # liveness check we want to bypass — patch it for clarity).
    target = discovery_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "v": 1,
                "pid": 999999,
                "tcp": "127.0.0.1:9",
                "server_version": "0.0.1",
                "project_root": str(tmp_path),
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    rec = write_record(
        tmp_path, pid=os.getpid(), tcp="127.0.0.1:1", server_version="0.1.0"
    )
    assert rec.pid == os.getpid()


def test_delete_record_if_owner(tmp_path: Path):
    write_record(tmp_path, pid=os.getpid(), tcp="127.0.0.1:1", server_version="0.1.0")
    assert delete_record_if_owner(tmp_path, expected_pid=os.getpid()) is True
    assert read_record(tmp_path) is None


def test_delete_record_if_owner_refuses_wrong_pid(tmp_path: Path):
    write_record(tmp_path, pid=os.getpid(), tcp="127.0.0.1:1", server_version="0.1.0")
    assert delete_record_if_owner(tmp_path, expected_pid=12345) is False
    assert read_record(tmp_path) is not None


def test_find_project_root_walks_up(tmp_path: Path):
    write_record(tmp_path, pid=os.getpid(), tcp="127.0.0.1:1", server_version="0.1.0")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root_with_hub(nested) == tmp_path.resolve()


def test_find_project_root_returns_none_when_absent(tmp_path: Path):
    assert find_project_root_with_hub(tmp_path) is None


def test_env_override_uses_env(monkeypatch):
    monkeypatch.setenv("RTL_BUDDY_HUB", "127.0.0.1:7000")
    assert env_override() == "127.0.0.1:7000"


def test_env_override_unset(monkeypatch):
    monkeypatch.delenv("RTL_BUDDY_HUB", raising=False)
    assert env_override() is None
