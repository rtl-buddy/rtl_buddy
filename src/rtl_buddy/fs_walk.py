# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Directory walks that follow symlinks without walking a tree twice.

``os.walk`` does not descend into a symlinked directory unless it is
asked to, and an artefact-discovery walk has to ask: a suite whose
``artefacts/`` is a link onto scratch storage is an ordinary, documented
setup — the one the filelist writer is pinned against — and a walk that
stopped at the link would report a project with no artefacts in it at
all (rtl-buddy/rtl_buddy#564).

Following links costs two risks, and both are paid for here rather than
in each caller. A link can circle, so a directory is admitted once, by
its *real* path, and one already admitted is neither yielded again nor
descended into — which terminates a cycle and de-dupes a directory
reachable by two routes, something a caller reporting one result per
directory needs in any case. And a link can lead somewhere that is not
the project at all: a ``vendor/`` link, or a link to ``$HOME``, drags an
unrelated tree into the walk, which is slow and — worse — reports
someone else's artefacts as this project's own run. That one is a policy
question, so it is asked of the caller's ``may_follow`` predicate, and
:func:`may_follow_link` is the answer both artefact walks give.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .tools.artifact_paths import ARTIFACT_DIRNAME


def may_follow_link(link: str, rel_parts: tuple[str, ...], root_real: str) -> bool:
    """Whether a symlinked directory is part of the artefact layout.

    ``link`` is the link itself, ``rel_parts`` the components of its path
    below the project root (its own basename last), ``root_real`` the
    resolved project root.

    A link is descended into only when
    :data:`~rtl_buddy.tools.artifact_paths.ARTIFACT_DIRNAME` is already a
    component of its path below the project root — its own basename
    (``<suite>/artefacts -> scratch``, the supported case) or an
    ``artefacts`` above it (a run directory inside an ``artefacts/``
    subtree linked out individually). A link whose realpath is the
    project root or an ancestor of it is refused outright, so no admitted
    link can circle back over the whole tree (or over ``/``).

    Public because the hub's ``?dir=`` route decides the same question
    about the same tree (:func:`rtl_buddy.hub.phys_page
    .contained_phys_dir`). A run the walk refused to enter is a run the
    route must refuse to read: two spellings of one boundary would
    eventually disagree, and the disagreement anyone finds first is the
    one where the route is the looser of the two.
    """
    if ARTIFACT_DIRNAME not in rel_parts:
        return False
    link_real = os.path.realpath(link)
    return not (root_real == link_real or root_real.startswith(link_real + os.sep))


def artefact_layout_boundary(root) -> Callable[[str], bool]:
    """:func:`may_follow_link` bound to one project root, for ``may_follow``.

    The binding a whole-project walk needs: the predicate judges a link
    by its position *below the root*, so the root has to come from
    somewhere, and both artefact walks would otherwise restate the same
    three lines.
    """
    root_real = os.path.realpath(root)

    def _may_follow(link: str) -> bool:
        return may_follow_link(link, Path(os.path.relpath(link, root)).parts, root_real)

    return _may_follow


def walk_unique(
    root, *, may_follow: Callable[[str], bool] | None = None
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Yield ``os.walk`` triples, following links, each directory once.

    A drop-in for ``os.walk(root, followlinks=True)``. The ``dirnames``
    list handed out is the walk's own, so a caller still prunes it in
    place — its own skip set, its own naming rules — and the pruning is
    honoured exactly as before.

    ``may_follow`` is asked about each *link* among the directories about
    to be descended into, by its path, and a link it declines is pruned;
    real directories are walked whatever it says. The default follows
    every link, which suits a walk bounded to a directory the caller
    already trusts; a walk over a whole project should pass a boundary —
    :func:`artefact_layout_boundary` — because an arbitrary link under a
    project root need not lead anywhere inside it.
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
