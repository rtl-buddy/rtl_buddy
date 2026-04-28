import logging
import os
import pprint

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


@serde
class SpecCoverageItem:
  """
  A single functional coverage item within a block specification.

  Attributes:
    id (str): Unique identifier for this coverage item (e.g. "AFIFO-COV-01").
    desc (str): Human-readable description of what this item covers.
  """
  id: str
  desc: str

  def __str__(self):
    return pprint.pformat(self)


@serde
class SpecBlock:
  """
  A single block entry within a specs.yaml file.

  Attributes:
    name (str): Unique block identifier.
    desc (str): Human-readable block description.
    docs (list[str]): Paths to markdown spec documents, relative to the specs.yaml directory.
    coverage_items (list[SpecCoverageItem]): Functional coverage items for this block.
  """
  name: str
  desc: str
  docs: list[str] = field(default_factory=list)
  coverage_items: list[SpecCoverageItem] = field(rename='coverage-items', default_factory=list)

  def get_coverage_item_ids(self) -> list[str]:
    return [item.id for item in self.coverage_items]

  def __str__(self):
    return pprint.pformat(self)


@serde
class SpecConfigFile:
  """
  Representation of a 'spec_config' file (specs.yaml).

  Attributes:
    rtl_buddy_filetype: Must be 'spec_config'.
    blocks (list[SpecBlock]): One or more block specifications.
  """
  rtl_buddy_filetype: Literal['spec_config'] = field(rename='rtl-buddy-filetype')
  blocks: list[SpecBlock] = field(default_factory=list)


class SpecConfig:
  """
  Loaded specification file containing one or more block specs.

  Attributes:
    path (str): Absolute path to the specs.yaml file.
    blocks (list[SpecBlock]): Block specifications in this file.
  """

  def __init__(self, path: str) -> None:
    self.path = os.path.abspath(path)
    try:
      with open(path, 'r') as f:
        data = from_yaml(SpecConfigFile, f.read())
    except Exception as e:
      log_event(logger, logging.ERROR, "spec_config.load_failed", path=path, error=e)
      raise FatalRtlBuddyError(f'failed to load spec config "{path}"') from e

    self.blocks = data.blocks

  def get_path(self) -> str:
    return self.path

  def get_blocks(self) -> list[SpecBlock]:
    return self.blocks

  def get_block(self, name: str) -> SpecBlock | None:
    for block in self.blocks:
      if block.name == name:
        return block
    return None

  def __str__(self):
    return pprint.pformat(self)
