import logging

logger = logging.getLogger(__name__)
import pprint
import os

from dataclasses import dataclass
from serde import serde
from ..logging_utils import log_event


@dataclass
class VeribleConfig:
    """
    Configuration for running Verible within the test suite

    Attributes:
      name (str): Unique verible identifier.
      path (str): Path to the Verible executable directory.
      extra_args (dict[str, list[str]]): List of arguments to be supplied to verible, grouped by command.
      root_cfg_path (str): Directory of the root configuration
    """

    name: str
    path: str
    extra_args: dict[str, list[str]]
    root_cfg_path: str
    available: bool

    def get_name(self):
        """
        Retrieve the value of name.

        Returns:
          name (str): The value of name.
        """
        return self.name

    def get_extra_args(self, cmd: str) -> list[str]:
        """
        Retrieve the extra_args associated with a command.

        Args:
          cmd (str): The command.
        Returns:
          extra_args (list[str]): The list of extra_args associated with the command. If none are found, returns an empty array.
        """
        # logger.info(pprint.pformat(self.cfg))
        return self.extra_args[cmd] if cmd in self.extra_args else []

    def get_path(self):
        """
        Retrieves the path to the Verible executable directory.

        Returns:
          path (str): The path.
        """
        return os.path.join(os.path.dirname(self.root_cfg_path), self.path)

    def get_exe_path(self, exe_name):
        """
        Retrieves the path to the Verible executable.

        Returns:
          path (str): The path.
        """
        return os.path.join(self.get_path(), exe_name)

    def __str__(self):
        return pprint.pformat(self)


@serde
class VeribleConfigFile:
    name: str
    path: str
    extra_args: dict[str, list[str]]

    def initialise(self, root_cfg_path: str) -> VeribleConfig:
        res = VeribleConfig(self.name, self.path, self.extra_args, root_cfg_path, False)
        full_path = res.get_path()
        if not os.path.exists(full_path):
            log_event(
                logger,
                logging.DEBUG,
                "verible.path_missing",
                name=res.get_name(),
                path=full_path,
            )
        else:
            res.available = True

        return res
