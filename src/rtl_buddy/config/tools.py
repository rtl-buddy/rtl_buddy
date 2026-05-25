"""Optional ``cfg-tools`` block in ``root_config.yaml``.

Each entry pairs a tool manifest name (e.g. ``verible``, ``yosys``, ``surfer``)
with a project-pinned minimum version. ``rb tool-check`` overlays these on top
of the in-source defaults in :mod:`rtl_buddy.tool_manifest`, so a project can
demand a newer baseline than what rtl_buddy ships with — without forking the
manifest.

The block is intentionally optional and additive. Projects that don't pin
versions get the manifest defaults.
"""

from dataclasses import dataclass

from serde import field, serde


@serde
class ToolVersionConfigFile:
    name: str
    min_version: str | None = field(rename="min-version", default=None)


@dataclass
class ToolVersionConfig:
    name: str
    min_version: str | None = None

    @classmethod
    def from_file(cls, cfg: ToolVersionConfigFile) -> "ToolVersionConfig":
        return cls(name=cfg.name, min_version=cfg.min_version)
