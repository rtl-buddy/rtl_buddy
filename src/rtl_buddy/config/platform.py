import logging

logger = logging.getLogger(__name__)
import pprint

from dataclasses import dataclass, field as dc_field
from serde import serde
from .rtl import RtlBuilderConfig
from .verible import VeribleConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


#: Optional tool blocks a ``cfg-platforms`` entry may route, keyed by the platform's YAML key.
#: Each value is ``(RootConfig attribute holding the block's entries, YAML block name)``.
#:
#: ``builder`` and ``verible`` are resolved eagerly into :class:`PlatformConfig`, not routed here.
#: ``cfg-*-tools`` blocks are not routable because the flow's ``tool:`` name also selects the backend.
#: Pin a per-platform binary with the candidate list ``tool:`` accepts; see :mod:`rtl_buddy.config.toolpath`.
PLATFORM_TOOL_BLOCKS: dict[str, tuple[str, str]] = {
    "surfer": ("surfer_cfgs", "cfg-surfer"),
}


@dataclass
class PlatformConfig:
    """A resolved platform entry: target OS, supported unames, builder and verible configs.

    ``routed`` maps a :data:`PLATFORM_TOOL_BLOCKS` key to the entry this platform selects; unmentioned blocks are absent and keep their global default.
    """

    os: str
    unames: list[str]
    builder: RtlBuilderConfig
    verible: VeribleConfig
    routed: dict[str, str] = dc_field(default_factory=dict)

    def get_os(self) -> str:
        """Target OS of the platform."""
        return self.os

    def get_builder(self) -> RtlBuilderConfig:
        """Builder config for the platform."""
        return self.builder

    def get_verible(self) -> VeribleConfig:
        """Verible config for the platform."""
        return self.verible

    def get_routed_tool(self, block: str) -> str | None:
        """Entry name this platform routes for ``block`` (a :data:`PLATFORM_TOOL_BLOCKS` key), or None."""
        return self.routed.get(block)

    def get_routed_tools(self) -> dict[str, str]:
        """All routed ``block -> entry name`` pairs for this platform."""
        return dict(self.routed)

    def __str__(self) -> str:
        return pprint.pformat(self)


@serde
class PlatformConfigFile:
    os: str
    unames: list[str]
    builder: str | None
    verible: str
    surfer: str | None = None

    def get_routed_names(self) -> dict[str, str]:
        """Configured ``block -> entry name`` routing, skipping unset blocks."""
        return {
            block: name
            for block in PLATFORM_TOOL_BLOCKS
            # YAML keys are hyphenated; the pyserde attributes are not.
            if (name := getattr(self, block.replace("-", "_"), None))
        }

    def validate_routing(self, tool_blocks: dict[str, dict]) -> None:
        """Fail if this entry routes a block to an entry that is not configured.

        :class:`~rtl_buddy.config.root.RootConfig` calls it for every ``cfg-platforms`` entry at load, not only the matching one.
        """
        for block, entry_name in self.get_routed_names().items():
            available = tool_blocks.get(block) or {}
            if entry_name not in available:
                _, yaml_block = PLATFORM_TOOL_BLOCKS[block]
                log_event(
                    logger,
                    logging.ERROR,
                    "platform.tool_missing",
                    block=block,
                    entry=entry_name,
                    os=self.os,
                    available=", ".join(sorted(available)),
                )
                raise FatalRtlBuddyError(
                    f'cfg-platforms[{self.os}].{block}: "{entry_name}" '
                    f"not in {yaml_block} "
                    f"(available: {sorted(available)})"
                )

    def initialise(
        self,
        builders: dict[str, RtlBuilderConfig],
        veribles: dict[str, VeribleConfig],
        builder_override: str | None,
    ) -> PlatformConfig:
        """Resolve this platform entry against the root config's blocks.

        ``builders`` and ``veribles`` are the ``cfg-rtl-builder`` and ``cfg-verible`` entries by name; ``builder_override`` is the ``--builder`` CLI value or None.
        Routing is validated separately by :meth:`validate_routing`.
        """
        builder = None
        if self.builder is not None:
            if self.builder not in builders:
                log_event(
                    logger,
                    logging.ERROR,
                    "platform.builder_missing",
                    builder=self.builder,
                    os=self.os,
                )
                raise FatalRtlBuddyError(f'"{self.builder}" not in root config')

            builder = builders[self.builder]

        if builder_override is not None:
            log_event(
                logger,
                logging.INFO,
                "platform.builder_override",
                builder=builder_override,
                configured_builder=self.builder,
                os=self.os,
            )
            if builder_override not in builders:
                log_event(
                    logger,
                    logging.ERROR,
                    "platform.builder_override_missing",
                    builder=builder_override,
                    os=self.os,
                )
                raise FatalRtlBuddyError(
                    f'Builder override "{builder_override}" is not in root config.'
                )

            builder = builders[builder_override]

        if builder is None:
            log_event(logger, logging.ERROR, "platform.builder_unset", os=self.os)
            raise FatalRtlBuddyError(
                "Both builder and builder_override are not set. Builder is None"
            )

        if self.verible not in veribles:
            log_event(
                logger,
                logging.ERROR,
                "platform.verible_missing",
                verible=self.verible,
                os=self.os,
            )
            raise FatalRtlBuddyError(f'"{self.verible}" not in verible config')

        routed = self.get_routed_names()
        if routed:
            log_event(
                logger,
                logging.DEBUG,
                "platform.tool_routing",
                os=self.os,
                routing=", ".join(f"{k}={v}" for k, v in sorted(routed.items())),
            )

        return PlatformConfig(
            self.os, self.unames, builder, veribles[self.verible], routed
        )

    def get_os(self) -> str:
        """Target OS of the platform."""
        return self.os

    def get_unames(self) -> list[str]:
        """The unames the platform supports."""
        return self.unames
