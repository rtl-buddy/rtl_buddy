"""Loader for ``<project_root>/.rtl-buddy/hub.toml``.

The file is optional and has a ``[hub]`` block (ports, log path) and a ``[mapping]`` block (testbench prefix, signal aliases, ``view.json`` path). Unknown top-level sections raise :class:`HubConfigError`; unknown keys inside known sections are ignored.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


HUB_CONFIG_FILENAME = "hub.toml"
DEFAULT_TB_PREFIX = "tb.dut."
DEFAULT_LOG_PATH = ".rtl-buddy/hub.log"


class HubConfigError(Exception):
    """``hub.toml`` is unreadable or invalid."""


@dataclass(frozen=True, slots=True)
class SignalAlias:
    """Rewrite of a wave path to a view path, applied before :attr:`HubMappingConfig.tb_prefix` is stripped."""

    wave: str
    view: str


@dataclass(frozen=True, slots=True)
class HubServerConfig:
    """``[hub]`` block: ports and log path."""

    listen_port: int = 0
    http_port: int = 0
    log_path: str = DEFAULT_LOG_PATH


@dataclass(frozen=True, slots=True)
class HubMappingConfig:
    """``[mapping]`` block: wave-to-view path translation."""

    tb_prefix: str = DEFAULT_TB_PREFIX
    signal_aliases: tuple[SignalAlias, ...] = field(default_factory=tuple)
    view_json: str | None = None
    """``view.json`` path relative to the project root; ``None`` means ``.rtl-buddy/view.json``."""


@dataclass(frozen=True, slots=True)
class HubConfig:
    """Parsed ``hub.toml``."""

    hub: HubServerConfig = field(default_factory=HubServerConfig)
    mapping: HubMappingConfig = field(default_factory=HubMappingConfig)
    source_path: Path | None = None

    @property
    def listen_port(self) -> int:
        return self.hub.listen_port

    @property
    def tb_prefix(self) -> str:
        return self.mapping.tb_prefix


_KNOWN_SECTIONS = frozenset({"hub", "mapping"})


def load_hub_config(path: Path | None) -> HubConfig:
    """Read a ``hub.toml`` into a :class:`HubConfig`.

    Returns the defaults when ``path`` is ``None`` or does not exist. Raises :class:`HubConfigError` on invalid content.
    """

    if path is None or not path.exists():
        return HubConfig()

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise HubConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise HubConfigError(f"{path}: invalid TOML — {exc}") from exc

    unknown = sorted(set(raw) - _KNOWN_SECTIONS)
    if unknown:
        raise HubConfigError(
            f"{path}: unknown section(s) {unknown}; expected any of {sorted(_KNOWN_SECTIONS)}"
        )

    hub_block = _parse_hub_block(raw.get("hub", {}), path)
    mapping_block = _parse_mapping_block(raw.get("mapping", {}), path)
    return HubConfig(hub=hub_block, mapping=mapping_block, source_path=path)


def _parse_hub_block(raw: dict[str, Any], path: Path) -> HubServerConfig:
    defaults = HubServerConfig()

    listen_port = raw.get("listen_port", defaults.listen_port)
    if not isinstance(listen_port, int) or listen_port < 0 or listen_port > 65535:
        raise HubConfigError(
            f"{path}: [hub].listen_port must be an integer in [0, 65535], got {listen_port!r}"
        )

    http_port = raw.get("http_port", defaults.http_port)
    if not isinstance(http_port, int) or http_port < 0 or http_port > 65535:
        raise HubConfigError(
            f"{path}: [hub].http_port must be an integer in [0, 65535], got {http_port!r}"
        )

    log_path = raw.get("log_path", defaults.log_path)
    if not isinstance(log_path, str) or not log_path:
        raise HubConfigError(
            f"{path}: [hub].log_path must be a non-empty string, got {log_path!r}"
        )

    return replace(
        defaults, listen_port=listen_port, http_port=http_port, log_path=log_path
    )


def _parse_mapping_block(raw: dict[str, Any], path: Path) -> HubMappingConfig:
    defaults = HubMappingConfig()

    tb_prefix = raw.get("tb_prefix", defaults.tb_prefix)
    if not isinstance(tb_prefix, str):
        raise HubConfigError(
            f"{path}: [mapping].tb_prefix must be a string, got {tb_prefix!r}"
        )

    view_json = raw.get("view_json", defaults.view_json)
    if view_json is not None and (not isinstance(view_json, str) or not view_json):
        raise HubConfigError(
            f"{path}: [mapping].view_json must be a non-empty string, got {view_json!r}"
        )

    aliases_raw = raw.get("signal_aliases", [])
    if not isinstance(aliases_raw, list):
        raise HubConfigError(
            f"{path}: [mapping].signal_aliases must be a list of tables, got {type(aliases_raw).__name__}"
        )

    aliases: list[SignalAlias] = []
    for idx, item in enumerate(aliases_raw):
        if not isinstance(item, dict):
            raise HubConfigError(
                f"{path}: [mapping].signal_aliases[{idx}] must be a table, got {type(item).__name__}"
            )
        wave = item.get("wave")
        view = item.get("view")
        if not isinstance(wave, str) or not wave:
            raise HubConfigError(
                f"{path}: [mapping].signal_aliases[{idx}].wave must be a non-empty string"
            )
        if not isinstance(view, str) or not view:
            raise HubConfigError(
                f"{path}: [mapping].signal_aliases[{idx}].view must be a non-empty string"
            )
        aliases.append(SignalAlias(wave=wave, view=view))

    return HubMappingConfig(
        tb_prefix=tb_prefix,
        signal_aliases=tuple(aliases),
        view_json=view_json,
    )


def default_config_path(project_root: Path) -> Path:
    """Return the ``hub.toml`` path inside ``project_root``."""

    return project_root / ".rtl-buddy" / HUB_CONFIG_FILENAME


__all__ = [
    "HUB_CONFIG_FILENAME",
    "DEFAULT_TB_PREFIX",
    "DEFAULT_LOG_PATH",
    "HubConfig",
    "HubConfigError",
    "HubServerConfig",
    "HubMappingConfig",
    "SignalAlias",
    "load_hub_config",
    "default_config_path",
]
