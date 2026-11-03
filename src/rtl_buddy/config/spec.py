import datetime
import logging
import math
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

    def meta_json(self) -> dict[str, Any]:
        """`meta` with values JSON can carry, for machine output."""
        return _json_safe(self.meta)

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


_WARNED_KEYS: set[tuple[str, str, str]] = set()


def _warn_unknown_keys(raw: Any, path: str) -> None:
    """Warn once per process for each key that no level of ``specs.yaml`` reads; the load drops it."""
    if not isinstance(raw, dict):
        return
    abspath = os.path.abspath(path)

    def check(mapping: Any, cls: type, where: str, hint: str = "") -> None:
        if not isinstance(mapping, dict):
            return
        known = serde_keys(cls)
        for key in mapping:
            if key in known or (abspath, where, str(key)) in _WARNED_KEYS:
                continue
            _WARNED_KEYS.add((abspath, where, str(key)))
            log_event(
                logger,
                logging.WARNING,
                "spec_config.unknown_key",
                path=path,
                where=where,
                key=str(key),
                hint=hint,
            )

    check(raw, SpecConfigFile, "top level")
    blocks = raw.get("blocks")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        block_where = f"block {block.get('name')!r}"
        check(block, SpecBlock, block_where)
        items = block.get("coverage-items")
        for item in items if isinstance(items, list) else []:
            check(
                item,
                SpecCoverageItem,
                f"coverage item {item.get('id') if isinstance(item, dict) else None!r} in {block_where}",
                hint="put project metadata under 'meta:'",
            )


def _drop_null_meta(raw: Any) -> None:
    """Remove ``meta:`` keys written empty or as ``~``, which mean no metadata."""
    blocks = raw.get("blocks") if isinstance(raw, dict) else None
    for block in blocks if isinstance(blocks, list) else []:
        items = block.get("coverage-items") if isinstance(block, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("meta", 0) is None:
                del item["meta"]


def _json_safe(value: Any) -> Any:
    """`value` with dates as ISO strings and other non-JSON types, NaN and infinities included, as strings."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(_json_safe(k)): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


class SpecConfig:
    """Loaded `specs.yaml`; `path` is absolute."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        try:
            with open(path, "r") as f:
                raw = load_yaml(f.read(), path)
            _drop_null_meta(raw)
            data = config_from_data(SpecConfigFile, raw)
            _warn_unknown_keys(raw, path)
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
