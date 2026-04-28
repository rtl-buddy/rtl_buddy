import logging
import os

from ..config.spec import SpecBlock, SpecConfig
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
  """Walk `root` and load every specs.yaml with rtl-buddy-filetype: spec_config."""
  configs = []
  for path in _walk_yaml_files(root, "specs.yaml"):
    try:
      cfg = SpecConfig(path)
      configs.append(cfg)
      log_event(logger, logging.DEBUG, "spec_trace.found_spec", path=path, blocks=len(cfg.get_blocks()))
    except FatalRtlBuddyError:
      log_event(logger, logging.WARNING, "spec_trace.spec_load_failed", path=path)
  return configs


def all_spec_blocks(spec_configs: list[SpecConfig]) -> list[tuple[SpecConfig, SpecBlock]]:
  """Flatten all (SpecConfig, SpecBlock) pairs across all loaded spec configs."""
  return [(cfg, block) for cfg in spec_configs for block in cfg.get_blocks()]


def discover_model_configs(root: str) -> list[tuple[str, ModelConfig]]:
  """Walk `root`, load every models.yaml, return (models_yaml_path, ModelConfig) pairs."""
  results = []
  for path in _walk_yaml_files(root, "models.yaml"):
    try:
      loader = ModelConfigLoader(path)
      for model in loader.models:
        model.path = path
        results.append((path, model))
    except FatalRtlBuddyError:
      log_event(logger, logging.WARNING, "spec_trace.models_load_failed", path=path)
  return results


def discover_suite_tests(root: str) -> list[tuple[str, object]]:
  """Walk `root`, load every tests.yaml, return (tests_yaml_path, TestConfig) pairs."""
  results = []
  for path in _walk_yaml_files(root, "tests.yaml"):
    try:
      suite = SuiteConfig(path)
      for test in suite.get_tests():
        results.append((path, test))
    except FatalRtlBuddyError:
      log_event(logger, logging.WARNING, "spec_trace.suite_load_failed", path=path)
  return results


def build_coverage_map(suite_tests: list[tuple[str, object]]) -> dict[str, list[tuple[str, str]]]:
  """
  Build a map of coverage-item-id → [(tests_yaml_path, test_name), ...].
  Only includes tests that have a non-empty `covers` list.
  """
  cov_map: dict[str, list[tuple[str, str]]] = {}
  for tests_path, test in suite_tests:
    covers = getattr(test, 'covers', None) or []
    for cov_id in covers:
      cov_map.setdefault(cov_id, []).append((tests_path, test.name))
  return cov_map


def build_spec_to_models_map(
  spec_configs: list[SpecConfig],
  model_entries: list[tuple[str, ModelConfig]],
) -> dict[str, list[tuple[str, str]]]:
  """
  Map "spec_path::block_name" → [(models_yaml_path, model_name), ...] for models
  that reference a spec via their `spec:` field.

  A model is matched to a specific block by name (model.name == block.name).
  If the referenced spec file has only one block, that block is used regardless of name.
  """
  # build lookup: absolute spec path → SpecConfig
  spec_path_to_cfg = {cfg.get_path(): cfg for cfg in spec_configs}

  # initialise result keyed by "path::block_name"
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
    # match by name; fall back to single-block file
    matched = cfg.get_block(model.name)
    if matched is None and len(blocks) == 1:
      matched = blocks[0]
    if matched is not None:
      key = f"{cfg.get_path()}::{matched.name}"
      result.setdefault(key, []).append((models_path, model.name))

  return result
