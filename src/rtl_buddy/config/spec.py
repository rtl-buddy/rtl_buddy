import logging
import os
import pprint

from serde import serde, field
from .unknown_keys import serde_keys
from .yaml_loader import config_from_data, load_yaml
from typing import Any, Literal

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


@serde
class SpecCoverageItem:
    """One functional coverage item; `id` is unique, e.g. "AFIFO-COV-01". `meta` is free-form project metadata."""

    id: str
    desc: str
    meta: dict[str, Any] = field(default_factory=dict)

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


_WARNED_ITEM_KEYS: set[tuple[str, str, str, str]] = set()


def _warn_unknown_item_keys(raw: Any, path: str) -> None:
    """Warn once per process for each key on a coverage item that is neither a field nor ``meta``."""
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        return
    known = serde_keys(SpecCoverageItem)
    for block in raw["blocks"]:
        items = block.get("coverage-items") if isinstance(block, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in item:
                if key in known:
                    continue
                token = (
                    os.path.abspath(path),
                    str(block.get("name")),
                    str(item.get("id")),
                    str(key),
                )
                if token in _WARNED_ITEM_KEYS:
                    continue
                _WARNED_ITEM_KEYS.add(token)
                log_event(
                    logger,
                    logging.WARNING,
                    "spec_config.unknown_item_key",
                    path=path,
                    block=block.get("name"),
                    item=item.get("id"),
                    key=str(key),
                )


class SpecConfig:
    """Loaded `specs.yaml`; `path` is absolute."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        try:
            with open(path, "r") as f:
                raw = load_yaml(f.read(), path)
            data = config_from_data(SpecConfigFile, raw)
            _warn_unknown_item_keys(raw, path)
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
