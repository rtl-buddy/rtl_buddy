import logging
import os

from ..config.spec import SpecBlock, SpecConfig
from ..config.fpv import FpvRegConfig
from ..config.model import ModelConfig, ModelConfigLoader
from ..config.suite import SuiteConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


def _walk_yaml_files(root: str, filename: str) -> list[str]:
    """Return absolute paths of all files named `filename` under `root`."""
    found = []
    for dirpath, _, files in os.walk(root):
        if filename in files:
            found.append(os.path.abspath(os.path.join(dirpath, filename)))
    return sorted(found)


def discover_spec_configs(root: str) -> list[SpecConfig]:
    """Load every specs.yaml under `root` whose filetype is spec_config."""
    configs = []
    for path in _walk_yaml_files(root, "specs.yaml"):
        try:
            cfg = SpecConfig(path)
            configs.append(cfg)
            log_event(
                logger,
                logging.DEBUG,
                "spec_trace.found_spec",
                path=path,
                blocks=len(cfg.get_blocks()),
            )
        except FatalRtlBuddyError:
            log_event(logger, logging.WARNING, "spec_trace.spec_load_failed", path=path)
    return configs


def all_spec_blocks(
    spec_configs: list[SpecConfig],
) -> list[tuple[SpecConfig, SpecBlock]]:
    """Return every (SpecConfig, SpecBlock) pair across `spec_configs`."""
    return [(cfg, block) for cfg in spec_configs for block in cfg.get_blocks()]


def discover_model_configs(root: str) -> list[tuple[str, ModelConfig]]:
    """Load every models.yaml under `root` as (models_yaml_path, ModelConfig) pairs."""
    results = []
    for path in _walk_yaml_files(root, "models.yaml"):
        try:
            loader = ModelConfigLoader(path)
            for model in loader.models:
                model.path = path
                results.append((path, model))
        except FatalRtlBuddyError:
            log_event(
                logger, logging.WARNING, "spec_trace.models_load_failed", path=path
            )
    return results


def discover_suite_tests(root: str) -> tuple[list[tuple[str, object]], list[str]]:
    """Load every tests.yaml under `root`; return the loaded tests and the paths that failed."""
    results = []
    failures = []
    for path in _walk_yaml_files(root, "tests.yaml"):
        try:
            suite = SuiteConfig(path)
            for test in suite.get_tests():
                results.append((path, test))
        except FatalRtlBuddyError:
            log_event(
                logger, logging.WARNING, "spec_trace.suite_load_failed", path=path
            )
            failures.append(path)
    return results, failures


def discover_fpv_verifications(
    project_root: str,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Return the fpv runs listed in ``<project_root>/fpv_regression.yaml``, plus the paths that failed to load.

    Entries are ``(fpv_yaml_path, FpvConfig)`` pairs, shaped like the ``(tests_yaml_path, TestConfig)``
    pairs :func:`build_coverage_map` consumes. A project without ``fpv_regression.yaml`` yields no
    entries and no failures.
    """
    reg_path = os.path.join(str(project_root), "fpv_regression.yaml")
    if not os.path.isfile(reg_path):
        return [], []
    try:
        reg = FpvRegConfig(name="spec_trace/fpv", path=reg_path)
    except FatalRtlBuddyError as exc:
        log_event(
            logger,
            logging.WARNING,
            "spec_trace.fpv_reg_load_failed",
            path=reg_path,
            error=str(exc),
        )
        return [], [reg_path]
    return [
        (suite.get_path(), verification)
        for suite in reg.get_suite_configs()
        for verification in suite.get_verifications()
    ], []


def build_coverage_map(
    suite_tests: list[tuple[str, object]],
) -> dict[str, list[tuple[str, str]]]:
    """Map each coverage-item id to the ``(tests_yaml_path, test_name)`` pairs that cover it."""
    cov_map: dict[str, list[tuple[str, str]]] = {}
    for tests_path, test in suite_tests:
        covers = getattr(test, "covers", None) or []
        for cov_id in covers:
            cov_map.setdefault(cov_id, []).append((tests_path, test.name))
    return cov_map


def build_spec_to_models_map(
    spec_configs: list[SpecConfig],
    model_entries: list[tuple[str, ModelConfig]],
) -> dict[str, list[tuple[str, str]]]:
    """Map ``"spec_path::block_name"`` to the ``(models_yaml_path, model_name)`` pairs whose `spec:` field references it.

    A model matches the block with its own name; if the spec file has a single block, that block matches regardless of name.
    """
    spec_path_to_cfg = {cfg.get_path(): cfg for cfg in spec_configs}

    result: dict[str, list[tuple[str, str]]] = {}
    for cfg in spec_configs:
        for block in cfg.get_blocks():
            result[f"{cfg.get_path()}::{block.name}"] = []

    for models_path, model in model_entries:
        if model.spec is None:
            continue
        models_dir = os.path.dirname(models_path)
        abs_spec_path = os.path.normpath(os.path.join(models_dir, model.spec))
        cfg = spec_path_to_cfg.get(abs_spec_path)
        if cfg is None:
            continue

        blocks = cfg.get_blocks()
        matched = cfg.get_block(model.name)
        if matched is None and len(blocks) == 1:
            matched = blocks[0]
        if matched is not None:
            key = f"{cfg.get_path()}::{matched.name}"
            result.setdefault(key, []).append((models_path, model.name))

    return result
