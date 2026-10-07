import logging

logger = logging.getLogger(__name__)
import pprint
import os

from serde import serde, field
from .yaml_loader import config_from_yaml
from typing import Literal
from .suite import SuiteConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


@serde
class RegConfigFile:
    """Parsed `reg_config` YAML file."""

    rtl_buddy_filetype: Literal["reg_config"] = field(rename="rtl-buddy-filetype")
    test_configs: list[str] = field(rename="test-configs", default_factory=list)


class RegConfig:
    """A named regression: the suites listed in one `reg_config` file."""

    def __init__(self, name: str, path: str) -> None:
        """Load the regression config at `path`; suite paths resolve relative to it.

        Raises FatalRtlBuddyError if the file or any suite fails to load.
        """
        self.name = name
        self.path = path
        self.suite_configs = []
        try:
            with open(path, "r") as file:
                data = config_from_yaml(RegConfigFile, file.read(), path)
                self.suite_configs = [
                    SuiteConfig(os.path.join(os.path.dirname(self.path), suite_path))
                    for suite_path in data.test_configs
                ]
        except FatalRtlBuddyError:
            # SuiteConfig already named the failing file; do not re-wrap.
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "regression_config.load_failed",
                name=self.name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{self.name}: failed to load "{path}"') from e

    def get_name(self):
        return self.name

    def get_path(self):
        return self.path

    def get_suite_configs(self):
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
