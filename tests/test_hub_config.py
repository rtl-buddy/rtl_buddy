"""Tests for ``rtl_buddy.hub.config`` — TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy.hub.config import (
    DEFAULT_TB_PREFIX,
    HubConfig,
    HubConfigError,
    SignalAlias,
    default_config_path,
    load_hub_config,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_file_yields_defaults(tmp_path: Path):
    cfg = load_hub_config(None)
    assert cfg == HubConfig()
    assert cfg.hub.listen_port == 0
    assert cfg.mapping.tb_prefix == DEFAULT_TB_PREFIX

    cfg2 = load_hub_config(tmp_path / "absent.toml")
    assert cfg2 == HubConfig()


def test_round_trip_minimal(tmp_path: Path):
    path = _write(
        tmp_path / "hub.toml",
        """
[hub]
listen_port = 54321
http_port = 54322
log_path = ".rtl-buddy/hub.log"

[mapping]
tb_prefix = "tb.dut."
""",
    )
    cfg = load_hub_config(path)
    assert cfg.hub.listen_port == 54321
    assert cfg.hub.http_port == 54322
    assert cfg.mapping.tb_prefix == "tb.dut."
    assert cfg.source_path == path


def test_signal_aliases_parsed(tmp_path: Path):
    path = _write(
        tmp_path / "hub.toml",
        """
[mapping]
tb_prefix = "tb.dut."
signal_aliases = [
  { wave = "tb.dut.foo_pre_renamed", view = "top.foo" },
  { wave = "tb.dut.bar_pre_renamed", view = "top.bar" },
]
""",
    )
    cfg = load_hub_config(path)
    assert cfg.mapping.signal_aliases == (
        SignalAlias(wave="tb.dut.foo_pre_renamed", view="top.foo"),
        SignalAlias(wave="tb.dut.bar_pre_renamed", view="top.bar"),
    )


def test_unknown_section_rejected(tmp_path: Path):
    path = _write(
        tmp_path / "hub.toml",
        """
[adapters.surfer]
port = 1234
""",
    )
    with pytest.raises(HubConfigError):
        load_hub_config(path)


def test_bad_port_rejected(tmp_path: Path):
    path = _write(
        tmp_path / "hub.toml",
        """
[hub]
listen_port = -1
""",
    )
    with pytest.raises(HubConfigError):
        load_hub_config(path)


def test_alias_without_wave_field_rejected(tmp_path: Path):
    path = _write(
        tmp_path / "hub.toml",
        """
[mapping]
signal_aliases = [
  { view = "top.foo" }
]
""",
    )
    with pytest.raises(HubConfigError):
        load_hub_config(path)


def test_invalid_toml_yields_clear_error(tmp_path: Path):
    path = _write(tmp_path / "hub.toml", "[unclosed-table")
    with pytest.raises(HubConfigError, match="invalid TOML"):
        load_hub_config(path)


def test_default_config_path_layout(tmp_path: Path):
    assert default_config_path(tmp_path) == tmp_path / ".rtl-buddy" / "hub.toml"
