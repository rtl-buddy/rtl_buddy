import logging
logger = logging.getLogger(__name__)
import pprint
import os

from serde import serde, field, SerdeError
from serde.yaml import from_yaml
from typing import Literal
from .suite import SuiteConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

@serde
class RegConfigFile:
  """
  Representation of a "reg_config' file.

  Attributes
    rtl_buddy_filetype (Literal['reg_config']): Config file type. Must be 'reg_config'.
    test_configs (list[str]): List of paths to test configurations.
  """
  rtl_buddy_filetype: Literal['reg_config'] = field(rename='rtl-buddy-filetype')
  test_configs: list[str] = field(rename='test-configs', default_factory=list)

class RegConfig:
  """
  Configuration for a set of regression tests.

  Attributes:
    name (str): Unique regression test identifier.
    path (str): Path to the regression test file.
    test_configs (list[str]): List of paths to test suite files defining tests in the regression test.
  """

  def __init__(self, name:str, path:str) -> None:
    """
    Initialise a RegConfig given a path to a YAML configuration file.

    Args:
      name (str): Unique regression test identifier.
      path (str): Path to the regression test configuration file.
    Raises:
      SystemExitError: If there was an error parsing the file.
    """
    self.name = name
    self.path = path
    self.suite_configs = []
    try:
      with open(path, 'r') as file:
        data = from_yaml(RegConfigFile, file.read())
        self.suite_configs = list(map(lambda suite_path: SuiteConfig(os.path.join(os.path.dirname(self.path), suite_path)), data.test_configs))
    except Exception as e:
      log_event(logger, logging.ERROR, "regression_config.load_failed", name=self.name, path=path, error=e)
      raise FatalRtlBuddyError(f'{self.name}: failed to load "{path}"') from e

  def get_name(self):
    """
    Retrieve the value of name

    Returns:
    name (str): The name of the regression test
    """
    return self.name

  def get_path(self):
    """
    Retrieve the value of path

    Returns
    path (str): The value of path in the regression test
    """
    return self.path

  def get_suite_configs(self):
    """
    Retrieve the value of suite_configs

    Returns
    test_configs (list[SuiteConfig]): The value of suite_configs in the regression test
    """
    return self.suite_configs

  def __str__(self):
    return pprint.pformat(self)
