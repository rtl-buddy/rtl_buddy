import logging

logger = logging.getLogger(__name__)
import pprint

from dataclasses import dataclass
from serde import serde
from .rtl import RtlBuilderConfig
from .verible import VeribleConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


@dataclass
class PlatformConfig:
    """
    Configuration entry defining a single test platoform.

    Attributes:
      os (str): Target OS of platform.
      unames (list[str]): List of supported unames for the platform.
      builder (str | None): Name of builder configuration associated with the platform.
      verible (str): Name of verible configuration associated with the platform.
    """

    os: str
    unames: list[str]
    builder: RtlBuilderConfig
    verible: VeribleConfig

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

    def __str__(self) -> str:
        return pprint.pformat(self)


@serde
class PlatformConfigFile:
    os: str
    unames: list[str]
    builder: str | None
    verible: str

    def initialise(
        self,
        builders: dict[str, RtlBuilderConfig],
        veribles: dict[str, VeribleConfig],
        builder_override: str | None,
    ) -> PlatformConfig:
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

        return PlatformConfig(self.os, self.unames, builder, veribles[self.verible])

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
