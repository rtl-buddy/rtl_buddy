import logging

logger = logging.getLogger(__name__)
import pprint

from dataclasses import dataclass, field as dc_field
from serde import serde, field
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
#: run), while these blocks are optional and resolved on demand by the
#: matching ``RootConfig.get_*`` accessor. Everything else about them is
#: the same indirection — the platform entry names an entry in the block,
#: and an unrouted block keeps its pre-#439 global behaviour.
PLATFORM_TOOL_BLOCKS: dict[str, tuple[str, str]] = {
    "surfer": ("surfer_cfgs", "cfg-surfer"),
    "synth-tools": ("synth_tool_cfgs", "cfg-synth-tools"),
    "pnr-tools": ("pnr_tool_cfgs", "cfg-pnr-tools"),
    "power-tools": ("power_tool_cfgs", "cfg-power-tools"),
    "cdc-tools": ("cdc_tool_cfgs", "cfg-cdc-tools"),
    "fpv-tools": ("fpv_tool_cfgs", "cfg-fpv-tools"),
    "fpga-tools": ("fpga_tool_cfgs", "cfg-fpga-tools"),
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
    synth_tools: str | None = field(rename="synth-tools", default=None)
    pnr_tools: str | None = field(rename="pnr-tools", default=None)
    power_tools: str | None = field(rename="power-tools", default=None)
    cdc_tools: str | None = field(rename="cdc-tools", default=None)
    fpv_tools: str | None = field(rename="fpv-tools", default=None)
    fpga_tools: str | None = field(rename="fpga-tools", default=None)

    def get_routed_names(self) -> dict[str, str]:
        """Configured ``block -> entry name`` routing, skipping unset blocks."""
        raw = {
            "surfer": self.surfer,
            "synth-tools": self.synth_tools,
            "pnr-tools": self.pnr_tools,
            "power-tools": self.power_tools,
            "cdc-tools": self.cdc_tools,
            "fpv-tools": self.fpv_tools,
            "fpga-tools": self.fpga_tools,
        }
        return {block: name for block, name in raw.items() if name}

    def initialise(
        self,
        builders: dict[str, RtlBuilderConfig],
        veribles: dict[str, VeribleConfig],
        builder_override: str | None,
        tool_blocks: dict[str, dict] | None = None,
    ) -> PlatformConfig:
        """Resolve this platform entry against the root config's blocks.

        Args:
          builders: ``cfg-rtl-builder`` entries by name.
          veribles: ``cfg-verible`` entries by name.
          builder_override: ``--builder`` CLI override, or None.
          tool_blocks: Available entries per :data:`PLATFORM_TOOL_BLOCKS`
            key, used to validate the optional routing. Omitted (None)
            skips validation — callers that only need builder/verible
            (e.g. older tests) are unaffected.
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
        if tool_blocks is not None:
            for block, entry_name in routed.items():
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
