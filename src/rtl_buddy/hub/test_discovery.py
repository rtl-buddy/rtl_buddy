"""Test discovery for ``rb hub`` TB-view mode.

Finds ``tests.yaml`` files under the project root, lists their tests with the resolved ``(model, tb)`` pair, and resolves a ``?test=NAME`` request (optionally pinned by ``--tests-file``). Uses the same skip and ordering rules as :mod:`rtl_buddy.hub.model_discovery`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from serde.yaml import from_yaml

from ..config.suite import SuiteConfigFile
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Must match model_discovery._SKIP_DIRS.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "artefacts",
        "build",
        "dist",
        ".tox",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)


@dataclass(frozen=True)
class TestMatch:
    """One ``?test=NAME`` candidate during discovery."""

    tests_file: Path
    test_name: str


@dataclass(frozen=True)
class TestEntry:
    """One test in a ``GET /tests`` listing.

    ``tests_file`` is the absolute path of the owning ``tests.yaml``.
    """

    name: str
    model: str
    tb: str
    tests_file: Path


def discover_tests_files(root: Path) -> list[Path]:
    """Return every ``tests.yaml`` under ``root``, using the walk rules of :func:`model_discovery.discover_models_files`."""

    root_resolved = Path(root).resolve()
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip nested git worktrees (``.git`` as a file, not a dir).
        if Path(dirpath).resolve() != root_resolved and ".git" in filenames:
            git_entry = Path(dirpath) / ".git"
            if git_entry.is_file():
                dirnames.clear()
                continue
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "tests.yaml" in filenames:
            results.append(Path(dirpath) / "tests.yaml")
    return results


def _read_test_entries(path: Path) -> list[TestEntry]:
    """Return the tests in one file without building a full ``SuiteConfig``; an unparseable file yields ``[]``."""

    try:
        data = from_yaml(SuiteConfigFile, path.read_text())
    except Exception as exc:
        log_event(
            logger,
            logging.DEBUG,
            "hub.test_discovery.parse_skipped",
            path=str(path),
            error=str(exc),
        )
        return []
    # Tests that reference an undefined testbench are skipped.
    tb_names = {tb.name for tb in data.testbenches}
    out: list[TestEntry] = []
    for t in data.tests:
        if t.tb not in tb_names:
            continue
        out.append(
            TestEntry(
                name=t.name,
                model=t.model,
                tb=t.tb,
                tests_file=path,
            )
        )
    return out


def list_tests(root: Path, tests_file: Path | None = None) -> list[TestEntry]:
    """List the tests in the project, or only in ``tests_file`` when given.

    Ordered by file path, then by position within each file.
    """

    if tests_file is not None:
        if not tests_file.is_file():
            return []
        files = [tests_file]
    else:
        files = discover_tests_files(root)
    out: list[TestEntry] = []
    for tf in sorted(files):
        out.extend(_read_test_entries(tf))
    return out


def find_matches(tests_files: list[Path], test_name: str) -> list[TestMatch]:
    """Return one match for each file in ``tests_files`` that defines ``test_name``."""

    matches: list[TestMatch] = []
    for tf in tests_files:
        if any(entry.name == test_name for entry in _read_test_entries(tf)):
            matches.append(TestMatch(tests_file=tf, test_name=test_name))
    return matches


def resolve_test(
    root: Path,
    test_name: str,
    *,
    tests_file: Path | None = None,
) -> tuple[Path, TestConfig]:
    """Resolve ``test_name`` to its ``tests.yaml`` and :class:`TestConfig`.

    With ``tests_file`` the file is loaded directly. Otherwise the project is searched, and zero or several matches raise ``FatalRtlBuddyError`` pointing at ``--tests-file``.
    """

    from ..config.suite import SuiteConfig

    if tests_file is not None:
        if not tests_file.is_file():
            raise FatalRtlBuddyError(f"--tests-file {tests_file}: not a file")
        suite = SuiteConfig(str(tests_file))
        test_cfg = list(suite.get_tests(test_name))[0]
        return tests_file, test_cfg

    files = discover_tests_files(root)
    if not files:
        raise FatalRtlBuddyError(
            f"no tests.yaml found under {root}; "
            f"use --tests-file PATH to point at one explicitly"
        )

    matches = find_matches(files, test_name)
    if len(matches) == 0:
        sample: list[str] = []
        for tf in files:
            for entry in _read_test_entries(tf):
                rel = tf.relative_to(root) if tf.is_relative_to(root) else tf
                sample.append(f"{rel}::{entry.name}")
        log_event(
            logger,
            logging.ERROR,
            "hub.test_discovery.not_found",
            test=test_name,
            root=str(root),
            candidates=sample,
        )
        candidates_msg = (
            "\n  ".join(sample) if sample else "(no tests defined in any file)"
        )
        raise FatalRtlBuddyError(
            f"test {test_name!r} not found in any tests.yaml under {root}.\n"
            f"  candidates:\n  {candidates_msg}"
        )

    if len(matches) > 1:
        paths_msg = "\n  ".join(str(m.tests_file) for m in matches)
        log_event(
            logger,
            logging.ERROR,
            "hub.test_discovery.ambiguous",
            test=test_name,
            root=str(root),
            matches=[str(m.tests_file) for m in matches],
        )
        raise FatalRtlBuddyError(
            f"test {test_name!r} matches multiple tests.yaml files; "
            f"pass --tests-file PATH to disambiguate:\n  {paths_msg}"
        )

    chosen = matches[0]
    suite = SuiteConfig(str(chosen.tests_file))
    test_cfg = list(suite.get_tests(test_name))[0]
    return chosen.tests_file, test_cfg
