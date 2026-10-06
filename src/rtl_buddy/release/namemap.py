"""The obfuscation name map: original identifier to released identifier.

The file format is Verible's ``--save_map`` / ``--load_map`` dictionary: one
``original obfuscated`` pair per line. A pair whose two names are equal is an
identity entry, a name the release keeps.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import FatalRtlBuddyError

_VERSION_PART = re.compile(r"(\d+)|(\D+)")


@dataclass
class NameMap:
    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "NameMap":
        entries: dict[str, str] = {}
        with open(path) as f:
            for lineno, raw in enumerate(f, 1):
                parts = raw.split()
                if not parts:
                    continue
                if len(parts) != 2:
                    raise FatalRtlBuddyError(
                        f"{path}:{lineno}: malformed name-map line {raw.strip()!r}; "
                        "expected 'original obfuscated'"
                    )
                entries[parts[0]] = parts[1]
        return cls(entries)

    def save(self, path: str | os.PathLike) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            for key in sorted(self.entries):
                f.write(f"{key} {self.entries[key]}\n")
        os.replace(tmp, path)

    def renamed(self) -> dict[str, str]:
        """The entries that change a name."""
        return {k: v for k, v in self.entries.items() if k != v}

    def check_injective(self) -> None:
        """Fail when two original names map to one released name."""
        seen: dict[str, str] = {}
        for key, value in self.entries.items():
            other = seen.setdefault(value, key)
            if other != key:
                raise FatalRtlBuddyError(
                    f"name map is not one-to-one: '{other}' and '{key}' both map "
                    f"to '{value}'"
                )


def seed_map(preserve: set[str], previous: NameMap | None) -> tuple[NameMap, list[str]]:
    """Build the map the obfuscator starts from.

    Every preserved name maps to itself. Renamed entries of ``previous`` carry
    over, so a name keeps its released spelling from one release to the next,
    except where the name is now preserved or its old spelling now collides with
    a preserved name. Those are dropped and returned.
    """
    entries = {name: name for name in preserve}
    dropped: list[str] = []
    if previous is not None:
        for key, value in previous.renamed().items():
            if key in preserve or value in preserve:
                dropped.append(key)
                continue
            entries[key] = value
    return NameMap(entries), sorted(dropped)


def version_key(version: str) -> tuple:
    """Sort key for release versions: numeric parts compare as numbers (``1.10`` > ``1.9``)."""
    return tuple(
        (0, int(num)) if num else (1, text)
        for num, text in _VERSION_PART.findall(version)
    )


def previous_map(map_dir: Path, current_version: str) -> Path | None:
    """The map of the newest release older than ``current_version`` in ``map_dir``, if any."""
    if not map_dir.is_dir():
        return None
    current = version_key(current_version)
    older = [p for p in map_dir.glob("*.map") if version_key(p.stem) < current]
    if not older:
        return None
    return max(older, key=lambda p: version_key(p.stem))
