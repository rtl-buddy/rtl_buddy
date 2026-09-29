"""`blocks:` entries: hardened blocks that a synthesis or P&R run instances.

Each entry pairs an instanced module name with the `harden: true` P&R run whose abstract stands in for it.
"""

import os
from dataclasses import dataclass

from serde import field, serde

from ..errors import FatalRtlBuddyError


@serde
class BlockRefFile:
    name: str
    pnr: str
    pnr_path: str | None = field(rename="pnr-path", default=None)


@dataclass(frozen=True)
class BlockRef:
    """One `blocks:` entry, with its `pnr-path` made absolute."""

    name: str
    pnr_run: str
    pnr_suite_path: str


def load_block_refs(
    where: str,
    entries: list[BlockRefFile],
    config_dir: str,
    *,
    default_pnr_path: str | None,
) -> list[BlockRef]:
    """Validate a run's `blocks:` list and resolve each `pnr-path`.

    ``default_pnr_path`` is the `pnr.yaml` used when an entry has no `pnr-path`; ``None`` makes `pnr-path` required.
    """
    refs: list[BlockRef] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        at = f"{where}: blocks[{index}]"
        if not entry.name:
            raise FatalRtlBuddyError(f"{at}: missing 'name' (the instanced module)")
        if not entry.pnr:
            raise FatalRtlBuddyError(
                f"{at}: missing 'pnr' (the harden: true P&R run for {entry.name!r})"
            )
        if entry.name in seen:
            raise FatalRtlBuddyError(f"{at}: block {entry.name!r} is listed twice")
        seen.add(entry.name)
        if entry.pnr_path is not None:
            suite = os.path.normpath(os.path.join(config_dir, entry.pnr_path))
        elif default_pnr_path is not None:
            suite = default_pnr_path
        else:
            raise FatalRtlBuddyError(
                f"{at}: missing 'pnr-path' (the pnr.yaml that defines {entry.pnr!r})"
            )
        refs.append(BlockRef(name=entry.name, pnr_run=entry.pnr, pnr_suite_path=suite))
    return refs
