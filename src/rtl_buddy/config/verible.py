import logging
logger = logging.getLogger(__name__)
import pprint
import os

from dataclasses import dataclass
from serde import serde, field
from ..logging_utils import log_event

@dataclass
class VeribleConfig:
  """
  Configuration for running Verible within the test suite

  Attributes:
    name (str): Unique verible identifier.
    path (str): Path to the directory containing Verible executables.
    extra_args (dict[str, list[str]]): List of arguments to be supplied to verible, grouped by command.
  """
  name: str
  path: str
  extra_args: dict[str, list[str]]
  available: bool

  def get_name(self):
    """
    Retrieve the value of name.

    Returns:
      name (str): The value of name.
    """
    return self.name

  def get_extra_args(self, cmd:str) -> list[str]:
    """
    Retrieve the extra_args associated with a command.

    Args:
      cmd (str): The command.
    Returns:
      extra_args (list[str]): The list of extra_args associated with the command. If none are found, returns an empty array.
    """
    return self.extra_args[cmd] if cmd in self.extra_args else []

  def get_exe_path(self, exe_name):
    """
    Retrieves the full path to a Verible executable.

    Returns:
      path (str): The path.
    """
    return os.path.join(self.path, exe_name)

  def __str__(self):
    return pprint.pformat(self)

@serde
class VeribleConfigFile:
  name: str
  path: str
  extra_args: dict[str, list[str]]

  def initialise(self) -> VeribleConfig:
    res = VeribleConfig(self.name, self.path, self.extra_args, False)
    if not os.path.exists(self.path):
      log_event(logger, logging.DEBUG, "verible.path_missing", name=res.get_name(), path=self.path)
    else:
      res.available = True

    return res
