"""Unit tests for the shared symlink-aware directory walk (#564).

The discovery walks pin their own behaviour through their own manifests;
these pin the helper itself, so the contract every caller relies on —
links followed, a boundary obeyed, each directory once, and the caller's
own pruning still reaching ``os.walk`` — is asserted where it is written
rather than inferred from its callers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rtl_buddy.fs_walk import artefact_layout_boundary, may_follow_link, walk_unique


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Link ``link`` at directory ``target``, or skip where it cannot.

    The link is the whole subject of these tests, so a platform that
    refuses to make one has nothing to assert rather than a failure to
    report.
    """
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

    Asserted on rather than the logical paths wherever *which* of two
    routes a directory is reported through is directory-order luck: the
    invariant is that each one is reported once, not which name it wears.
    """
    return [
        os.path.realpath(dirpath)
        for dirpath, _dirnames, _filenames in walk_unique(root, **kwargs)
    ]


def test_walks_into_a_symlinked_directory_by_default(tmp_path):
    """``os.walk``'s default is to stop at a link, which is the whole bug:
    a suite's ``artefacts/`` is an ordinary symlink onto scratch storage."""
    root = tmp_path / "repo"
    root.mkdir()
    elsewhere = tmp_path / "scratch" / "artefacts"
    (elsewhere / "run").mkdir(parents=True)
    _symlink_or_skip(root / "artefacts", elsewhere)

    assert _walked(root) == {".", "artefacts", "artefacts/run"}


def test_a_declined_link_is_pruned_from_the_yielded_dirnames(tmp_path):
    """The boundary is applied before the caller sees the triple, so a
    declined link is neither descended into nor offered as a subdirectory —
    a caller cannot forget to ask."""
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
    """The other half of the same predicate, asked through the walk: the
    supported layout is admitted while ``vendor/`` above is not."""
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
    """Two links pointing at each other's parent make an unbounded walk out
    of ``followlinks=True``. Admitting a directory once by its real path is
    what ends it — neither link is an ancestor of the root, so no boundary
    would refuse them, and the test hanging is the failure mode this
    guards."""
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
    """What a caller reporting one result per directory needs: the same
    directory behind two names is one directory."""
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
    """The ``dirnames`` list handed out is the walk's own, so every caller's
    skip set keeps working — the property that let the discovery walks keep
    their pruning unchanged."""
    root = tmp_path / "repo"
    (root / "keep" / "deeper").mkdir(parents=True)
    (root / ".git" / "objects").mkdir(parents=True)

    walked = set()
    for dirpath, dirnames, _filenames in walk_unique(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        walked.add(Path(dirpath).relative_to(root).as_posix())

    assert walked == {".", "keep", "keep/deeper"}


def test_a_missing_root_walks_nothing(tmp_path):
    """``os.walk``'s own answer for an unreadable root, unchanged: an empty
    walk rather than an error, so a project with no tree there is not a
    crash."""
    assert list(walk_unique(tmp_path / "absent")) == []


def test_may_follow_link_refuses_a_link_onto_an_ancestor(tmp_path):
    """The predicate's second guard, asserted directly: an ``artefacts``
    component is not enough if the link circles back over the project."""
    root = tmp_path / "repo"
    artefacts = root / "verif" / "blk" / "artefacts"
    artefacts.mkdir(parents=True)
    _symlink_or_skip(artefacts / "up", tmp_path)
    link = str(artefacts / "up")

    assert not may_follow_link(
        link, ("verif", "blk", "artefacts", "up"), os.path.realpath(root)
    )
    # And nothing outside the layout is admitted at all.
    assert not may_follow_link(link, ("vendor",), os.path.realpath(root))
