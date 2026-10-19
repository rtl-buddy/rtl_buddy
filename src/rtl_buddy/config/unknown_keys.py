"""Unknown-key checks on raw YAML mappings that pyserde would drop silently.

A block is described by a :class:`KeySpec`: the serde class that reads it and the specs of its nested mappings. :func:`find_unknown_keys` walks the raw YAML against the spec and :func:`reject_unknown_keys` raises one :class:`FatalRtlBuddyError` naming every unknown key, its location and the valid keys. Run both before the typed load, so a misspelt key is reported instead of vanishing or surfacing as a different error.
"""

import dataclasses
import difflib
import logging
from typing import Any, Mapping, NamedTuple

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


def serde_keys(cls) -> tuple[str, ...]:
    """The YAML keys a serde class reads: each field's ``rename``, else its name."""
    return tuple(
        f.metadata.get("serde_rename", f.name) for f in dataclasses.fields(cls)
    )


class KeySpec(NamedTuple):
    """A block's serde class and the specs of its nested mapping blocks, by YAML key.

    ``many`` marks a block that is a list of mappings, each read by ``cls``.
    """

    cls: type
    nested: Mapping[str, "KeySpec"] = {}
    many: bool = False


class UnknownKey(NamedTuple):
    location: str
    key: str
    suggestion: str | None
    valid: tuple[str, ...]

    def message(self) -> str:
        hint = f", did you mean {self.suggestion!r}?" if self.suggestion else ""
        return (
            f"{self.location}: unknown key {self.key!r}{hint} "
            f"(valid keys: {', '.join(self.valid)})"
        )


def entry_label(kind: str, entry: Mapping[str, Any], index: int) -> str:
    """``kind 'name'`` for a named list entry, else ``kind #index``."""
    name = entry.get("name")
    return f"{kind} {name!r}" if isinstance(name, str) else f"{kind} #{index}"


def find_unknown_keys(raw: Any, spec: KeySpec, *, label: str, path: str = "") -> list:
    """Every key in the raw mapping ``raw``, and in its nested blocks, that ``spec`` does not read.

    ``label`` names the block in messages; ``path`` is the dotted position of a nested block under it. A value of the wrong shape is left to the typed load.
    """
    if not isinstance(raw, dict):
        return []
    location = f"{label}: {path}" if path else label
    valid = serde_keys(spec.cls)
    found = []
    for key in raw:
        if key in valid:
            continue
        # YAML 1.1 reads an unquoted `on` or `yes` key as a boolean.
        name = str(key)
        close = difflib.get_close_matches(name, valid, n=1)
        found.append(UnknownKey(location, name, close[0] if close else None, valid))
    for key, child in spec.nested.items():
        value = raw.get(key)
        here = f"{path}.{key}" if path else key
        if child.many:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    found += find_unknown_keys(
                        item, child, label=label, path=f"{here}[{index}]"
                    )
        else:
            found += find_unknown_keys(value, child, label=label, path=here)
    return found


def find_unknown_entry_keys(entries: Any, spec: KeySpec, *, kind: str) -> list:
    """:func:`find_unknown_keys` over each mapping of a list, labelled ``kind 'name'``."""
    if not isinstance(entries, list):
        return []
    found = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            found += find_unknown_keys(
                entry, spec, label=entry_label(kind, entry, index)
            )
    return found


def reject_unknown_keys(found: list, *, path: str) -> None:
    """Log each unknown key at ERROR and raise one FatalRtlBuddyError naming them all; do nothing when ``found`` is empty."""
    if not found:
        return
    for unknown in found:
        log_event(
            logger,
            logging.ERROR,
            "config.unknown_key_rejected",
            path=str(path),
            block=unknown.location,
            key=unknown.key,
            suggestion=unknown.suggestion,
            known=list(unknown.valid),
        )
    raise FatalRtlBuddyError(
        "\n".join(f"{path}: {unknown.message()}" for unknown in found)
    )
