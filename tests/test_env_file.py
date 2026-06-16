"""Tests for project-local env defaults (.rtl-buddy/.env)."""

import os

import pytest

from rtl_buddy.config.env_file import (
    ENV_FILE_RELPATH,
    apply_env_file,
    parse_env_file,
)
from rtl_buddy.errors import FatalRtlBuddyError


# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------


def test_parse_basic_pairs(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=bar\nBAZ=qux quux\n")
    assert parse_env_file(f) == {"FOO": "bar", "BAZ": "qux quux"}


def test_parse_skips_blanks_and_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text("\n# comment\n  \nFOO=bar\n   # indented comment\n")
    assert parse_env_file(f) == {"FOO": "bar"}


def test_parse_tolerates_export_prefix(tmp_path):
    f = tmp_path / ".env"
    f.write_text("export FOO=bar\n")
    assert parse_env_file(f) == {"FOO": "bar"}


def test_parse_strips_matching_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=\"with spaces\"\nB='single'\nC=\"unbalanced'\n")
    assert parse_env_file(f) == {
        "A": "with spaces",
        "B": "single",
        "C": "\"unbalanced'",
    }


def test_parse_empty_value_allowed(tmp_path):
    f = tmp_path / ".env"
    f.write_text("EMPTY=\n")
    assert parse_env_file(f) == {"EMPTY": ""}


def test_parse_no_interpolation(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=$HOME/x\n")
    assert parse_env_file(f) == {"A": "$HOME/x"}


def test_parse_value_may_contain_equals(tmp_path):
    f = tmp_path / ".env"
    f.write_text("ARGS=-D X=1\n")
    assert parse_env_file(f) == {"ARGS": "-D X=1"}


def test_parse_missing_equals_fails_loud(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=ok\nJUSTAWORD\n")
    with pytest.raises(FatalRtlBuddyError, match=r"\.env:2.*JUSTAWORD"):
        parse_env_file(f)


def test_parse_empty_key_fails_loud(tmp_path):
    f = tmp_path / ".env"
    f.write_text("=value\n")
    with pytest.raises(FatalRtlBuddyError, match=r"\.env:1"):
        parse_env_file(f)


# ---------------------------------------------------------------------------
# apply_env_file
# ---------------------------------------------------------------------------


def _write_project_env(tmp_path, text):
    env_path = tmp_path / ENV_FILE_RELPATH
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(text)
    return env_path


def test_apply_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    assert apply_env_file(tmp_path) == {}


def test_apply_sets_absent_vars(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("RB_TEST_ENVFILE_A", None)
    _write_project_env(tmp_path, "RB_TEST_ENVFILE_A=hello\n")
    assert apply_env_file(tmp_path) == {"RB_TEST_ENVFILE_A": "hello"}
    assert os.environ["RB_TEST_ENVFILE_A"] == "hello"


def test_apply_never_overrides_process_env(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ["RB_TEST_ENVFILE_B"] = "from-shell"
    _write_project_env(tmp_path, "RB_TEST_ENVFILE_B=from-file\n")
    assert apply_env_file(tmp_path) == {}
    assert os.environ["RB_TEST_ENVFILE_B"] == "from-shell"


def test_apply_is_idempotent_first_value_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("RB_TEST_ENVFILE_C", None)
    env_path = _write_project_env(tmp_path, "RB_TEST_ENVFILE_C=first\n")
    assert apply_env_file(tmp_path) == {"RB_TEST_ENVFILE_C": "first"}
    env_path.write_text("RB_TEST_ENVFILE_C=second\n")
    assert apply_env_file(tmp_path) == {}
    assert os.environ["RB_TEST_ENVFILE_C"] == "first"


def test_apply_logs_info_only_when_vars_injected(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("RB_TEST_ENVFILE_D", None)
    _write_project_env(tmp_path, "RB_TEST_ENVFILE_D=x\n")

    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.config.env_file"):
        apply_env_file(tmp_path)  # injects -> INFO
        apply_env_file(tmp_path)  # nothing new -> DEBUG

    levels = [
        r.levelno
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "env_file.applied"
    ]
    assert levels == [logging.INFO, logging.DEBUG]


def test_apply_feeds_slang_plugin_resolver(tmp_path, monkeypatch):
    """End-to-end through the consumer that motivated the feature."""
    from rtl_buddy.tools.synth_yosys import SLANG_PLUGIN_ENV, resolve_plugin_path

    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop(SLANG_PLUGIN_ENV, None)
    _write_project_env(tmp_path, f"{SLANG_PLUGIN_ENV}=/tools/slang.so\n")
    apply_env_file(tmp_path)
    assert resolve_plugin_path(None, None) == "/tools/slang.so"
