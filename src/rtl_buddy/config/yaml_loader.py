"""The one YAML loader for user-authored config files.

PyYAML's ``safe_load`` keeps the last value of a duplicated mapping key and drops the
earlier ones silently, so a second ``dont-use-cells:`` in a ``cfg-pdks`` entry discards
the first list. :func:`load_yaml` rejects a literal duplicate key with a
:class:`FatalRtlBuddyError` naming the file, the key and both line numbers.

Everything else is ``yaml.safe_load``: same YAML 1.1 resolver (``on:`` still parses as
``True``), same constructors. A key brought in by a ``<<`` merge and then set locally is
the merge idiom, not a duplicate.

:func:`config_from_data` names every required key a mapping leaves out, and the entry
it is missing from, before the typed load.
"""

import dataclasses
import functools
import types
import typing
from os import PathLike
from typing import Any

import yaml
from serde import from_dict
from serde.compat import SerdeError
from serde.yaml import deserialize_yaml_numbers

from ..errors import FatalRtlBuddyError

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _DuplicateKey(Exception):
    def __init__(self, key: str, first_line: int, second_line: int) -> None:
        super().__init__(key, first_line, second_line)
        self.key = key
        self.first_line = first_line
        self.second_line = second_line


class _UniqueKeys:
    """Constructor mixin that refuses a mapping with the same key written twice."""

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


class UniqueKeySafeLoader(_UniqueKeys, yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping with the same key written twice."""


# libyaml's parser, when PyYAML was built with it: several times faster on large generated files.
_FastUniqueKeySafeLoader = (
    type("_FastUniqueKeySafeLoader", (_UniqueKeys, yaml.CSafeLoader), {})
    if hasattr(yaml, "CSafeLoader")
    else UniqueKeySafeLoader
)


def load_yaml(
    text: Any, path: str | PathLike | None = None, *, fast: bool = False
) -> Any:
    """``yaml.safe_load`` of `text` (a string or stream) that rejects duplicate mapping keys.

    Raises FatalRtlBuddyError naming `path`, the key and both line numbers on a duplicate;
    other YAML errors propagate as ``yaml.YAMLError``. `fast` parses with libyaml when
    available, for large machine-written files.
    """
    loader = (_FastUniqueKeySafeLoader if fast else UniqueKeySafeLoader)(text)
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


class MissingKeyError(SerdeError):
    """A config mapping leaves out keys its serde class requires; the message names each one and where it is missing."""


def _entry(location: str, item: Any, index: int) -> str:
    """``<list> 'name'`` for a list entry with a string ``name``, else ``<list>[index]``."""
    name = item.get("name") if isinstance(item, dict) else None
    if isinstance(name, str):
        return f"{location} {name!r}"
    return f"{location}[{index}]"


@functools.cache
def _type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except Exception:
        return {}


def _optional(hint: Any) -> bool:
    return typing.get_origin(hint) in (typing.Union, types.UnionType) and (
        type(None) in typing.get_args(hint)
    )


def missing_keys(hint: Any, data: Any, location: str = "") -> list[str]:
    """The keys that the serde class or type `hint` requires and the raw `data`, or a block nested in it, leaves out, one ``<location>: missing required key(s) '<key>', ...`` per block.

    A required key is a field with no default whose type is not optional, by its YAML name. A list entry is located by its ``name``, else its index. A value of the wrong shape, or read by a field's own deserializer, is left to the typed load.
    """
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        if not isinstance(data, dict):
            return []
        hints = _type_hints(hint)
        if not hints:
            return []
        absent, found = [], []
        for f in dataclasses.fields(hint):
            if f.metadata.get("serde_skip"):
                continue
            key = f.metadata.get("serde_rename", f.name)
            if key not in data:
                if (
                    f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING
                    and not _optional(hints.get(f.name))
                ):
                    absent.append(repr(key))
            elif "serde_deserializer" not in f.metadata:
                inner = f"{location}.{key}" if location else key
                found += missing_keys(hints.get(f.name), data[key], inner)
        if absent:
            where = f"{location}: " if location else ""
            keys = "key" if len(absent) == 1 else "keys"
            found.insert(0, f"{where}missing required {keys} {', '.join(absent)}")
        return found
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        members = [a for a in args if a is not type(None)]
        return missing_keys(members[0], data, location) if len(members) == 1 else []
    if origin is list and args and isinstance(data, list):
        return [
            message
            for index, item in enumerate(data)
            for message in missing_keys(args[0], item, _entry(location, item, index))
        ]
    if origin is dict and len(args) == 2 and isinstance(data, dict):
        return [
            message
            for key, value in data.items()
            for message in missing_keys(
                args[1], value, f"{location}.{key}" if location else str(key)
            )
        ]
    return []


def config_from_data(cls: Any, data: Any) -> Any:
    """``serde.from_dict(cls, data)`` with the options every config file loads with.

    Raises :class:`MissingKeyError` naming every required key `data` leaves out (:func:`missing_keys`) before the typed load, which would report only the first as a bare ``KeyError``.
    """
    missing = missing_keys(cls, data)
    if missing:
        raise MissingKeyError("; ".join(missing))
    return from_dict(
        cls,
        data,
        reuse_instances=False,
        deserialize_numbers=deserialize_yaml_numbers,
    )


def config_from_yaml(cls: Any, text: str, path: str | PathLike | None = None) -> Any:
    """``serde.yaml.from_yaml(cls, text)`` parsed through :func:`load_yaml`."""
    return config_from_data(cls, load_yaml(text, path))
