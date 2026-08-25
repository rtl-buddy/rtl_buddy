import logging

logger = logging.getLogger(__name__)
import pprint
import os

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal
from .test import TestbenchConfig, TestConfigFile
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


@serde
class SuiteConfigFile:
    filetype: Literal["test_config"] = field(rename="rtl-buddy-filetype")
    testbenches: list[TestbenchConfig]
    tests: list[TestConfigFile]
    builder: str | None = None


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
            with open(path, "r") as file:
                data = from_yaml(SuiteConfigFile, file.read())
        except Exception as e:
            log_event(
                logger, logging.ERROR, "suite_config.load_failed", path=path, error=e
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        tbs = {}
        self.tests = {}
        self.path = path

        if data is not None:
            # Fail loud on duplicate testbench / test names — the
            # dict-comprehensions below would silently overwrite the
            # first entry with the last, hiding the user's typo.
            seen_tbs: dict[str, int] = {}
            for idx, tb in enumerate(data.testbenches):
                tb_name = tb.get_name()
                if tb_name in seen_tbs:
                    log_event(
                        logger,
                        logging.ERROR,
                        "suite_config.duplicate_testbench",
                        path=path,
                        name=tb_name,
                        first_index=seen_tbs[tb_name],
                        second_index=idx,
                    )
                    raise FatalRtlBuddyError(
                        f"{path}: duplicate testbench name {tb_name!r}"
                    )
                seen_tbs[tb_name] = idx
            seen_tests: dict[str, int] = {}
            for idx, t in enumerate(data.tests):
                if t.name in seen_tests:
                    log_event(
                        logger,
                        logging.ERROR,
                        "suite_config.duplicate_test",
                        path=path,
                        name=t.name,
                        first_index=seen_tests[t.name],
                        second_index=idx,
                    )
                    raise FatalRtlBuddyError(f"{path}: duplicate test name {t.name!r}")
                seen_tests[t.name] = idx

            try:
                tbs = {tb.get_name(): tb for tb in data.testbenches}
            except FatalRtlBuddyError:
                raise
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "suite_config.testbench_malformed",
                    path=path,
                    error=e,
                )
                raise FatalRtlBuddyError(f"{path}: Testbench section malformed") from e

            config_dir = os.path.dirname(path)
            try:
                self.tests = {
                    test.name: test.initialise(config_dir, tbs, data.builder)
                    for test in data.tests
                }
            except KeyError:
                log_event(
                    logger, logging.ERROR, "suite_config.testbench_missing", path=path
                )
                raise FatalRtlBuddyError(f"{path}: Requested testbench missing")
            except FatalRtlBuddyError:
                raise
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "suite_config.tests_malformed",
                    path=path,
                    error=e,
                )
                raise FatalRtlBuddyError(f"{path}: Tests section malformed") from e

    def get_tests(self, test_name=None):
        """
        Retrieves tests, optionally based on one or more names.

        Args:
          test_name (str|iterable[str]|None): (optional) Test name(s) to retrieve.
        Returns:
          tests (list[TestConfig]): List of tests.
        """
        if test_name is not None:
            test_names = [test_name] if isinstance(test_name, str) else list(test_name)
            if len(test_names) != len(set(test_names)):
                duplicate = next(
                    name
                    for index, name in enumerate(test_names)
                    if name in test_names[:index]
                )
                raise FatalRtlBuddyError(
                    f"duplicate test name {duplicate!r} in test selection"
                )

            missing = [name for name in test_names if name not in self.tests]
            if missing:
                log_event(
                    logger,
                    logging.ERROR,
                    "suite_config.test_missing",
                    path=self.path,
                    test=missing[0],
                )
                if len(missing) == 1:
                    message = f"test_name {missing[0]} not found in suite {self.path}"
                else:
                    message = f"test_names {', '.join(missing)} not found in suite {self.path}"
                raise FatalRtlBuddyError(message)
            return [self.tests[name] for name in test_names]
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
