"""Loader for ``<project_root>/.rtl-buddy/hub.toml``.

The hub config is intentionally tiny (§5 of the protocol spec): a
``[hub]`` block for transport-layer tunables and a ``[mapping]`` block
for the testbench-prefix strip + per-instance aliases that drive
``view ↔ wave`` resolution. Anything else (surfer flags, nvim
keymaps, …) lives in the adapters, not here.

The loader is forgiving by design — the file is optional and defaults
are baked into :class:`HubConfig`. Unknown top-level sections raise
:class:`HubConfigError` so typos are caught at startup, but unknown
keys *inside* known sections are tolerated to keep forward-compat
cheap.
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
    """Raised when the on-disk ``hub.toml`` is malformed or unreadable."""


@dataclass(frozen=True, slots=True)
class SignalAlias:
    """Pre-strip rewrite from a legacy testbench scope to a view path.

    Applied *before* :attr:`HubMappingConfig.tb_prefix` is stripped, so
    the ``wave`` side of the alias is the literal wave path, not a
    post-prefix-strip remnant. See §5 of the protocol spec.
    """

    wave: str
    view: str


@dataclass(frozen=True, slots=True)
class HubServerConfig:
    """``[hub]`` block — transport configuration."""

    listen_port: int = 0
    http_port: int = 0
    log_path: str = DEFAULT_LOG_PATH


@dataclass(frozen=True, slots=True)
class HubMappingConfig:
    """``[mapping]`` block — coordinate translation configuration."""

    tb_prefix: str = DEFAULT_TB_PREFIX
    signal_aliases: tuple[SignalAlias, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class HubConfig:
    """Parsed ``hub.toml`` — handed to the server loop and resolver."""

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
    """Read ``path`` (a ``hub.toml``) and return a :class:`HubConfig`.

    When ``path`` is ``None`` or points at a non-existent file, returns
    a default :class:`HubConfig`. The protocol's design choice is that
    the hub runs with sensible defaults even on a freshly initialised
    project; the file is for overrides, not gating.
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

    return HubMappingConfig(tb_prefix=tb_prefix, signal_aliases=tuple(aliases))


def default_config_path(project_root: Path) -> Path:
    """Where the hub looks for its config inside ``project_root``."""

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
