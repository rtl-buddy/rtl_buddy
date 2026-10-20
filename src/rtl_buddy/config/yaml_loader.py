"""The one YAML loader for user-authored config files.

PyYAML's ``safe_load`` keeps the last value of a duplicated mapping key and drops the
earlier ones silently, so a second ``dont-use-cells:`` in a ``cfg-pdks`` entry discards
the first list. :func:`load_yaml` rejects a literal duplicate key with a
:class:`FatalRtlBuddyError` naming the file, the key and both line numbers.

Everything else is ``yaml.safe_load``: same YAML 1.1 resolver (``on:`` still parses as
``True``), same constructors. A key brought in by a ``<<`` merge and then set locally is
the merge idiom, not a duplicate.
"""

from os import PathLike
from typing import Any

import yaml
from serde import from_dict
from serde.yaml import deserialize_yaml_numbers

from ..errors import FatalRtlBuddyError

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _DuplicateKey(Exception):
    def __init__(self, key: str, first_line: int, second_line: int) -> None:
        super().__init__(key, first_line, second_line)
        self.key = key
        self.first_line = first_line
        self.second_line = second_line


class UniqueKeySafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping with the same key written twice."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            # Before flatten_mapping splices `<<` entries into node.value: only literal keys count.
            seen: dict[Any, yaml.Node] = {}
            for key_node, _ in node.value:
                if key_node.tag == _MERGE_TAG or not isinstance(
                    key_node, yaml.ScalarNode
                ):
                    continue
                key = self.construct_object(key_node)
                try:
                    first = seen.get(key)
                except TypeError:
                    continue
                if first is None:
                    seen[key] = key_node
                else:
                    raise _DuplicateKey(
                        key_node.value,
                        first.start_mark.line + 1,
                        key_node.start_mark.line + 1,
                    )
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: Any, path: str | PathLike | None = None) -> Any:
    """``yaml.safe_load`` of `text` (a string or stream) that rejects duplicate mapping keys.

    Raises FatalRtlBuddyError naming `path`, the key and both line numbers on a duplicate;
    other YAML errors propagate as ``yaml.YAMLError``.
    """
    loader = UniqueKeySafeLoader(text)
    try:
        return loader.get_single_data()
    except _DuplicateKey as dup:
        where = str(path) if path is not None else "<yaml>"
        raise FatalRtlBuddyError(
            f"{where}: duplicate key '{dup.key}' at line {dup.second_line} "
            f"(first defined at line {dup.first_line}); YAML would keep only the last value"
        ) from None
    finally:
        loader.dispose()


def config_from_yaml(cls: Any, text: str, path: str | PathLike | None = None) -> Any:
    """``serde.yaml.from_yaml(cls, text)`` parsed through :func:`load_yaml`."""
    return from_dict(
        cls,
        load_yaml(text, path),
        reuse_instances=False,
        deserialize_numbers=deserialize_yaml_numbers,
    )
