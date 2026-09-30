"""Tests for the shared symlink-aware directory walk: links followed, boundary obeyed,
each directory once, caller pruning preserved.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rtl_buddy.fs_walk import artefact_layout_boundary, may_follow_link, walk_unique


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Link ``link`` at directory ``target``, or skip where the platform cannot."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - POSIX CI
        pytest.skip("platform does not support directory symlinks")


def _walked(root, **kwargs) -> set[str]:
    """The walk's directories, as paths relative to ``root``."""
    return {
        Path(dirpath).relative_to(root).as_posix()
        for dirpath, _dirnames, _filenames in walk_unique(root, **kwargs)
    }


def _reals(root, **kwargs) -> list[str]:
    """The real path of every directory the walk yields, in walk order.

    Used where which of two routes reports a directory depends on directory order;
    the invariant is that each is reported once.
    """
    return [
        os.path.realpath(dirpath)
        for dirpath, _dirnames, _filenames in walk_unique(root, **kwargs)
    ]


def test_walks_into_a_symlinked_directory_by_default(tmp_path):
    """The walk follows a symlinked directory by default, as a suite's ``artefacts/``
    symlink onto scratch storage requires.
    """
    root = tmp_path / "repo"
    root.mkdir()
    elsewhere = tmp_path / "scratch" / "artefacts"
    (elsewhere / "run").mkdir(parents=True)
    _symlink_or_skip(root / "artefacts", elsewhere)

    assert _walked(root) == {".", "artefacts", "artefacts/run"}


def test_a_declined_link_is_pruned_from_the_yielded_dirnames(tmp_path):
    """A declined link is neither descended into nor offered as a subdirectory."""
    root = tmp_path / "repo"
    (root / "artefacts").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "nested").mkdir(parents=True)
    _symlink_or_skip(root / "vendor", elsewhere)

    seen = {}
    for dirpath, dirnames, _filenames in walk_unique(
        root, may_follow=artefact_layout_boundary(root)
    ):
        seen[Path(dirpath).relative_to(root).as_posix()] = sorted(dirnames)

    assert seen == {".": ["artefacts"], "artefacts": []}


def test_a_link_inside_the_artefact_layout_is_followed(tmp_path):
    """The supported artefact layout is admitted while ``vendor/`` is not."""
    root = tmp_path / "repo"
    suite = root / "verif" / "blk"
    suite.mkdir(parents=True)
    elsewhere = tmp_path / "scratch" / "artefacts"
    (elsewhere / "cov_dir").mkdir(parents=True)
    _symlink_or_skip(suite / "artefacts", elsewhere)

    assert _walked(root, may_follow=artefact_layout_boundary(root)) == {
        ".",
        "verif",
        "verif/blk",
        "verif/blk/artefacts",
        "verif/blk/artefacts/cov_dir",
    }


def test_a_cycle_terminates_and_yields_each_directory_once(tmp_path):
    """Two links pointing at each other's parent terminate and yield each directory
    once, because a directory is admitted once by real path.
    """
    root = tmp_path / "repo"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    _symlink_or_skip(root / "a" / "to_b", root / "b")
    _symlink_or_skip(root / "b" / "to_a", root / "a")

    reals = _reals(root)

    assert len(reals) == len(set(reals))
    assert set(reals) == {
        os.path.realpath(root),
        os.path.realpath(root / "a"),
        os.path.realpath(root / "b"),
    }


def test_a_directory_reachable_two_ways_is_yielded_once(tmp_path):
    """A directory reachable under two names is yielded once."""
    root = tmp_path / "repo"
    (root / "real" / "inner").mkdir(parents=True)
    _symlink_or_skip(root / "mirror", root / "real")

    reals = _reals(root)

    assert len(reals) == len(set(reals))
    assert set(reals) == {
        os.path.realpath(root),
        os.path.realpath(root / "real"),
        os.path.realpath(root / "real" / "inner"),
    }


def test_caller_side_pruning_still_reaches_os_walk(tmp_path):
    """The ``dirnames`` list handed out is the walk's own, so a caller's pruning still
    reaches ``os.walk``.
    """
    root = tmp_path / "repo"
    (root / "keep" / "deeper").mkdir(parents=True)
    (root / ".git" / "objects").mkdir(parents=True)

    walked = set()
    for dirpath, dirnames, _filenames in walk_unique(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        walked.add(Path(dirpath).relative_to(root).as_posix())

    assert walked == {".", "keep", "keep/deeper"}


def test_a_missing_root_walks_nothing(tmp_path):
    """A missing root yields an empty walk, not an error."""
    assert list(walk_unique(tmp_path / "absent")) == []


def test_may_follow_link_refuses_a_link_onto_an_ancestor(tmp_path):
    """A link named ``artefacts`` that circles back over the project is refused."""
    root = tmp_path / "repo"
    artefacts = root / "verif" / "blk" / "artefacts"
    artefacts.mkdir(parents=True)
    _symlink_or_skip(artefacts / "up", tmp_path)
    link = str(artefacts / "up")

    assert not may_follow_link(
        link, ("verif", "blk", "artefacts", "up"), os.path.realpath(root)
    )
    # Nothing outside the layout is admitted.
    assert not may_follow_link(link, ("vendor",), os.path.realpath(root))
