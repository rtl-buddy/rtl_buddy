import logging
logger = logging.getLogger(__name__)
import pprint
import os

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal
from .test import TestbenchConfig, TestConfig, TestConfigFile
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

@serde
class SuiteConfigFile:
  filetype: Literal['test_config'] = field(rename='rtl-buddy-filetype')
  testbenches: list[TestbenchConfig]
  tests: list[TestConfigFile]

class SuiteConfig:
  """
  Config for a suite of tests.

  Attributes:
    path (str): Path to the suite configuration file.
    tests (dict[str, TestConfig]): Test configs in suite, grouped by test name.
  """

  def __init__(self, path):
    data = None
    try:
      with open(path, 'r') as file:
        data = from_yaml(SuiteConfigFile, file.read())
    except Exception as e:
      log_event(logger, logging.ERROR, "suite_config.load_failed", path=path, error=e)
      raise FatalRtlBuddyError(f'failed to load "{path}"') from e

    tbs = {}
    self.tests = {}
    self.path = path

    if data is not None:
      try:
        tbs = { tb.get_name(): tb for tb in data.testbenches }
      except Exception as e:
        log_event(logger, logging.ERROR, "suite_config.testbench_malformed", path=path, error=e)
        raise FatalRtlBuddyError(f"{path}: Testbench section malformed") from e

      config_dir = os.path.dirname(path)
      try:
        self.tests = { test.name: test.initialise(config_dir, tbs) for test in data.tests }
      except KeyError:
        log_event(logger, logging.ERROR, "suite_config.testbench_missing", path=path)
        raise FatalRtlBuddyError(f"{path}: Requested testbench missing")
      except Exception as e:
        log_event(logger, logging.ERROR, "suite_config.tests_malformed", path=path, error=e)
        raise FatalRtlBuddyError(f"{path}: Tests section malformed") from e

  def get_tests(self, test_name=None):
    """
    Retrieves tests, optionally based on name.

    Args:
      test_name (str|None): (optional) Name of test to retrieve.
    Returns:
      tests (list[TestConfig]): List of tests.
    """
    if test_name is not None:
      if test_name not in self.tests.keys():
        log_event(logger, logging.ERROR, "suite_config.test_missing", path=self.path, test=test_name)
        raise FatalRtlBuddyError(f"test_name {test_name} not found in suite {self.path}")
      else:
        return [self.tests[test_name]]
    else:
      return self.tests.values()

  def get_test_names(self):
    """
    Retrieve all configured test names in declaration order.

    Returns:
      list[str]: Test names from the loaded suite config.
    """
    return list(self.tests.keys())

  def get_path(self):
    """
    Retrieve config path.

    Returns:
      path (str): Path of suite config.
    """
    return self.path

  def __str__(self):
    return pprint.pformat(self)
