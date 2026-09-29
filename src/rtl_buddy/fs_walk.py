# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Directory walks that follow symlinks without walking a tree twice.

A directory is admitted once by its real path, which ends symlink cycles and
de-dupes directories reachable by two routes. A caller-supplied ``may_follow``
predicate decides which links to enter; :func:`may_follow_link` is the policy
both artefact walks use.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .tools.artifact_paths import ARTIFACT_DIRNAME


def may_follow_link(link: str, rel_parts: tuple[str, ...], root_real: str) -> bool:
    """Whether a symlinked directory is part of the artefact layout.

    ``link`` is the link path, ``rel_parts`` the components of its path below
    the project root (its own basename last), ``root_real`` the resolved
    project root. A link is followed only when
    :data:`~rtl_buddy.tools.artifact_paths.ARTIFACT_DIRNAME` is already a
    component of that path: the link itself (``<suite>/artefacts -> scratch``)
    or an ancestor. A link resolving to the project root or one of its
    ancestors is refused, so no link can circle back over the tree.

    Public because the hub's ``?dir=`` route applies the same boundary
    (:func:`rtl_buddy.hub.phys_page.contained_phys_dir`). A run the walk
    refuses must also be unreadable through the route.
    """
    if ARTIFACT_DIRNAME not in rel_parts:
        return False
    link_real = os.path.realpath(link)
    return not (root_real == link_real or root_real.startswith(link_real + os.sep))


def artefact_layout_boundary(root) -> Callable[[str], bool]:
    """:func:`may_follow_link` bound to one project root, for ``may_follow``."""
    root_real = os.path.realpath(root)

    def _may_follow(link: str) -> bool:
        return may_follow_link(link, Path(os.path.relpath(link, root)).parts, root_real)

    return _may_follow


def walk_unique(
    root, *, may_follow: Callable[[str], bool] | None = None
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Yield ``os.walk`` triples, following links, each directory once.

    Drop-in for ``os.walk(root, followlinks=True)``; callers still prune
    ``dirnames`` in place. ``may_follow`` is asked about each symlinked
    directory by path, and a declined link is pruned; real directories are
    always walked. The default follows every link, so a walk over a whole
    project should pass :func:`artefact_layout_boundary`.
    """
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        if may_follow is not None:
            dirnames[:] = [
                d
                for d in dirnames
                if not os.path.islink(os.path.join(dirpath, d))
                or may_follow(os.path.join(dirpath, d))
            ]
        yield dirpath, dirnames, filenames
