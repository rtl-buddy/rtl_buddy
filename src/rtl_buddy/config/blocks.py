"""`blocks:` — the hardened blocks a synthesis or P&R run instances (#95).

Each entry names a module as it is instanced in the run's netlist and the
`harden: true` P&R run whose abstract stands in for it. The same shape is
accepted on a `synth.yaml` entry (which needs the block's Liberty model) and
on a `pnr.yaml` run (which needs its LEF, Liberty and GDS); resolution to the
abstract's files is `rtl_buddy.tools.pnr_abstract.resolve_blocks`.
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

    ``default_pnr_path`` is the file a `pnr-path`-less entry refers to: the
    run's own `pnr.yaml` for a P&R run, and ``None`` — `pnr-path` required —
    for a synthesis entry, which lives in a different file by definition.
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
