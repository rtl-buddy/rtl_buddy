import logging

logger = logging.getLogger(__name__)
import pprint
import os

from serde import serde, field
from .yaml_loader import config_from_yaml, load_yaml
from typing import Any, Literal
from .dispatch import (
    RESOURCES_BLOCK,
    SUITE_COMPILE_BLOCK,
    TESTBENCH_COMPILE_BLOCK,
    SuiteCompileFile,
    validate_compile_block,
    warn_unknown_block_keys,
)
from .test import (
    TestbenchConfig,
    TestConfigFile,
    validate_preproc_sets_plusdefines,
)
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


@serde
class SuiteConfigFile:
    filetype: Literal["test_config"] = field(rename="rtl-buddy-filetype")
    testbenches: list[TestbenchConfig]
    tests: list[TestConfigFile]
    builder: str | None = None
    # Layers over cfg-dispatch.compile field by field. A dedicated class keeps an unset `parallel` (None) distinct from `parallel: 1`.
    compile: SuiteCompileFile | None = None
    # Suite default for each test's `preproc-sets-plusdefines`; Any so the load error names the key.
    preproc_sets_plusdefines: Any = field(
        rename="preproc-sets-plusdefines", default=None
    )


def _entry_label(kind: str, entry: dict, idx: int) -> str:
    name = entry.get("name")
    return f"{kind} {name!r}" if isinstance(name, str) else f"{kind} #{idx}"


def warn_unknown_reservation_keys(raw, path) -> list[str]:
    """Warn about unknown keys in a tests.yaml's ``compile:`` and ``resources:`` blocks, which serde drops; return them.

    Covers the suite ``compile:`` block and each testbench's ``resources:`` and ``compile:`` and each test's ``resources:``. A malformed shape is left to the typed load.
    """
    if not isinstance(raw, dict):
        return []
    found = warn_unknown_block_keys(
        raw.get("compile"), SUITE_COMPILE_BLOCK, path=path, block="compile"
    )
    for kind, section, blocks in (
        (
            "testbench",
            "testbenches",
            (("resources", RESOURCES_BLOCK), ("compile", TESTBENCH_COMPILE_BLOCK)),
        ),
        ("test", "tests", (("resources", RESOURCES_BLOCK),)),
    ):
        entries = raw.get(section)
        if not isinstance(entries, list):
            continue
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            label = _entry_label(kind, entry, idx)
            for key, spec in blocks:
                found += warn_unknown_block_keys(
                    entry.get(key), spec, path=path, block=f"{label} {key}"
                )
    return found


class SuiteConfig:
    """A loaded suite file: `tests` maps test name to TestConfig; `compile` is the suite-level dispatch compile block or None."""

    def __init__(self, path):
        data = None
        try:
            with open(path, "r") as file:
                text = file.read()
            # Before the typed load, whose validation can be fatal: the warning names a misspelt key.
            warn_unknown_reservation_keys(load_yaml(text, path), path)
            data = config_from_yaml(SuiteConfigFile, text, path)
        except Exception as e:
            log_event(
                logger, logging.ERROR, "suite_config.load_failed", path=path, error=e
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        tbs = {}
        self.tests = {}
        self.path = path
        self.compile = None

        if data is not None:
            # Validated at load: an unquoted `time: 4:00:00` parses as an integer.
            try:
                self.compile = validate_compile_block(data.compile)
            except FatalRtlBuddyError as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "suite_config.compile_invalid",
                    path=path,
                    error=e,
                )
                raise FatalRtlBuddyError(f"{path}: {e}") from e
            validate_preproc_sets_plusdefines(data.preproc_sets_plusdefines, where=path)

            # The dict comprehensions below would silently keep the last duplicate.
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
                    test.name: test.initialise(
                        config_dir, tbs, data.builder, data.preproc_sets_plusdefines
                    )
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
        """Return the tests, or only those named by `test_name` (a name or an iterable of names), in the order given.

        Raises FatalRtlBuddyError for an unknown or repeated name.
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
        """Return all test names in declaration order."""
        return list(self.tests.keys())

    def get_compile(self):
        """Return the validated ``compile:`` block, or None."""
        return self.compile

    def get_path(self):
        return self.path

    def __str__(self):
        return pprint.pformat(self)
