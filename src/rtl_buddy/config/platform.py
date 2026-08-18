import logging

logger = logging.getLogger(__name__)
import pprint

from dataclasses import dataclass, field as dc_field
from serde import serde
from .rtl import RtlBuilderConfig
from .verible import VeribleConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


#: Tool blocks a ``cfg-platforms`` entry may route, keyed by the YAML key
#: on the platform entry. The value is ``(RootConfig attribute holding
#: that block's entries, YAML block name)``.
#:
#: ``builder`` and ``verible`` are *not* here: they are resolved eagerly
#: into :class:`PlatformConfig` objects (a platform without either cannot
#: run), while ``surfer`` is optional and resolved on demand by
#: :meth:`RootConfig.get_surfer_cfg`. Everything else about it is the same
#: indirection — the platform entry names an entry in the block, and an
#: unrouted block keeps its pre-#439 global behaviour.
#:
#: The ``cfg-*-tools`` blocks are deliberately **not** routable (#439).
#: Routing only means something for a block whose active entry is chosen
#: by rtl-buddy: ``builder``, ``verible`` and ``surfer`` all are. A
#: ``cfg-*-tools`` entry is chosen per run by the flow YAML's ``tool:``,
#: and that name is simultaneously the *backend selector* — ``openroad``
#: picks the OpenROAD P&R backend, ``yosys`` the Yosys synthesis backend,
#: and ``rb power`` looks the name up in a backend registry. A platform
#: cannot therefore redirect one of those entries without either being
#: ignored (the flow named an entry, so routing never applies) or
#: breaking backend dispatch (the routed name is not a backend). Pinning a
#: ``cfg-*-tools`` binary per platform is done in the entry itself, with
#: the candidate list ``tool:`` accepts — the first candidate that exists
#: wins, so a Linux tool-tree path and a Homebrew path can sit in the same
#: committed entry and each platform takes the one it has. See
#: :mod:`rtl_buddy.config.toolpath`.
PLATFORM_TOOL_BLOCKS: dict[str, tuple[str, str]] = {
    "surfer": ("surfer_cfgs", "cfg-surfer"),
}


@dataclass
class PlatformConfig:
    """
    Configuration entry defining a single test platoform.

    Attributes:
      os (str): Target OS of platform.
      unames (list[str]): List of supported unames for the platform.
      builder (str | None): Name of builder configuration associated with the platform.
      verible (str): Name of verible configuration associated with the platform.
      routed (dict[str, str]): Entry name this platform selects in each
        optional tool block, keyed by :data:`PLATFORM_TOOL_BLOCKS` key.
        Blocks the platform does not mention are absent, and their
        accessors keep their global default.
    """

    os: str
    unames: list[str]
    builder: RtlBuilderConfig
    verible: VeribleConfig
    routed: dict[str, str] = dc_field(default_factory=dict)

    def get_os(self) -> str:
        """
        Retrieve the value of os.

        Returns:
          os (str): The value of os
        """
        return self.os

    def get_builder(self) -> RtlBuilderConfig:
        """
        Get the value of builder

        Returns:
          builder (RtlBuilderConfig): The value of builder.
        """
        return self.builder

    def get_verible(self) -> VeribleConfig:
        """
        Get the value of verible.

        Returns:
          verible_name (str): The value of verible.
        """
        return self.verible

    def get_routed_tool(self, block: str) -> str | None:
        """
        Entry name this platform routes for ``block``.

        Args:
          block (str): A :data:`PLATFORM_TOOL_BLOCKS` key, e.g. ``"surfer"``.
        Returns:
          name (str | None): The routed entry name, or None when this
            platform does not route the block (the block stays global).
        """
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
        """Configured ``block -> entry name`` routing, skipping unset blocks.

        Read off :data:`PLATFORM_TOOL_BLOCKS` rather than a hand-written
        list, so adding a routable block is one edit: declare the field
        here and the entry there, and routing, validation and the
        accessors all follow.
        """
        return {
            block: name
            for block in PLATFORM_TOOL_BLOCKS
            # YAML keys are hyphenated, the pyserde attribute is not.
            if (name := getattr(self, block.replace("-", "_"), None))
        }

    def validate_routing(self, tool_blocks: dict[str, dict]) -> None:
        """Fail if this entry routes a block to an entry that is not configured.

        Called by :class:`~rtl_buddy.config.root.RootConfig` for *every*
        ``cfg-platforms`` entry at load, not just the one whose ``unames``
        matched: a typo in the Linux entry is otherwise invisible to a
        macOS developer and only becomes fatal on the CI host, which is
        the worst place to find it (#439). That sweep covers the matched
        entry too, so :meth:`initialise` does not re-check.
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

        Routing is *not* validated here: :meth:`validate_routing` has
        already run over every entry at load, and repeating it for the
        matched one only duplicates the work and the error.

        Args:
          builders: ``cfg-rtl-builder`` entries by name.
          veribles: ``cfg-verible`` entries by name.
          builder_override: ``--builder`` CLI override, or None.
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
        """
        Retrieve the value of os.

        Returns:
          os (str): The value of os
        """
        return self.os

    def get_unames(self) -> list[str]:
        """
        Retrieve the value of unames, the list of unames supported by the platform.

        Returns:
          unames (list[str]): The value of unames.
        """
        return self.unames
