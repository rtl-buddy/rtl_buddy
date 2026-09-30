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
    """One functional coverage item; `id` is unique, e.g. "AFIFO-COV-01"."""

    id: str
    desc: str

    def __str__(self):
        return pprint.pformat(self)


@serde
class SpecBlock:
    """One block entry in `specs.yaml`; `docs` paths are relative to that file."""

    name: str
    desc: str
    docs: list[str] = field(default_factory=list)
    coverage_items: list[SpecCoverageItem] = field(
        rename="coverage-items", default_factory=list
    )

    def get_coverage_item_ids(self) -> list[str]:
        return [item.id for item in self.coverage_items]

    def __str__(self):
        return pprint.pformat(self)


@serde
class SpecConfigFile:
    """Parsed `spec_config` YAML file (`specs.yaml`)."""

    rtl_buddy_filetype: Literal["spec_config"] = field(rename="rtl-buddy-filetype")
    blocks: list[SpecBlock] = field(default_factory=list)


class SpecConfig:
    """Loaded `specs.yaml`; `path` is absolute."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        try:
            with open(path, "r") as f:
                data = from_yaml(SpecConfigFile, f.read())
        except Exception as e:
            log_event(
                logger, logging.ERROR, "spec_config.load_failed", path=path, error=e
            )
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
