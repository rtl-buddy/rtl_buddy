"""Optional ``cfg-tools`` block in ``root_config.yaml``.

Each entry pairs a tool manifest name (e.g. ``verible``, ``yosys``, ``surfer``)
with a project-pinned minimum version. ``rb tool-check`` overlays these on top
of the in-source defaults in :mod:`rtl_buddy.tool_manifest`, so a project can
demand a newer baseline than what rtl_buddy ships with — without forking the
manifest.

The block is intentionally optional and additive. Projects that don't pin
versions get the manifest defaults.

An entry may also carry ``platform:`` naming a ``cfg-platforms[].os``, in
which case it applies only on that platform and beats an unqualified
entry for the same tool. That is what lets a project pin Linux to a
shared tool tree at ``5.050`` while macOS takes Homebrew's ``5.049``,
instead of having to declare the lowest floor any platform can satisfy.
"""

from dataclasses import dataclass

from serde import field, serde


@serde
class ToolVersionConfigFile:
    name: str
    min_version: str | None = field(rename="min-version", default=None)
    #: Optional ``cfg-platforms[].os`` this pin applies to. Unset means
    #: every platform. A project that pins one platform's toolchain to a
    #: shared tool tree can then state the real floor there instead of
    #: the lowest floor any platform can satisfy (#439).
    platform: str | None = None


@dataclass
class ToolVersionConfig:
    name: str
    min_version: str | None = None
    platform: str | None = None

    @classmethod
    def from_file(cls, cfg: ToolVersionConfigFile) -> "ToolVersionConfig":
        return cls(name=cfg.name, min_version=cfg.min_version, platform=cfg.platform)
