"""Optional ``cfg-tools`` block in ``root_config.yaml``.

Each entry pins a minimum version for a tool manifest name (e.g. ``verible``); ``rb tool-check`` overlays the pins on :mod:`rtl_buddy.tool_manifest`. An entry with ``platform:`` (a ``cfg-platforms[].os``) applies only there and beats an unqualified entry for the same tool.
"""

from dataclasses import dataclass

from serde import field, serde


@serde
class ToolVersionConfigFile:
    name: str
    min_version: str | None = field(rename="min-version", default=None)
    #: ``cfg-platforms[].os`` this pin applies to; unset means every platform.
    platform: str | None = None


@dataclass
class ToolVersionConfig:
    name: str
    min_version: str | None = None
    platform: str | None = None

    @classmethod
    def from_file(cls, cfg: ToolVersionConfigFile) -> "ToolVersionConfig":
        return cls(name=cfg.name, min_version=cfg.min_version, platform=cfg.platform)
