"""Test discovery for ``rb hub`` TB-view mode (#99 / 6b).

Walks the project tree for ``tests.yaml`` files, aggregates the
named test entries with their resolved ``(model, tb)`` pair, and
resolves a single match for an SPA-supplied ``?test=NAME`` request
(with optional ``--tests-file`` pin).

Mirrors :mod:`rtl_buddy.hub.model_discovery` for shape and skip
rules — same conventional build/VCS dir exclusions, same git-
worktree pruning, same alphabetical determinism. Keeping the two
discoveries in lockstep makes the cross-file collision error
messages familiar to anyone who's already debugged a ``--model``
ambiguity.
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


# Mirror model_discovery._SKIP_DIRS exactly — vendored fixtures,
# build outputs, VCS metadata. Keep these two lists in sync; any
# divergence will surface as "model X has has_cdc but test Y can't
# be found" surprises.
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
    """A single advertised test for ``GET /tests`` listings.

    Carries the resolved ``model`` and ``tb`` names so the SPA can
    annotate each option and skip an extra round-trip per click. The
    ``tests_file`` is the absolute path to the ``tests.yaml`` that
    owns the entry; the SPA uses it as the picker's stable key.
    """

    name: str
    model: str
    tb: str
    tests_file: Path


def discover_tests_files(root: Path) -> list[Path]:
    """Return every ``tests.yaml`` reachable under ``root``.

    Same walk rules as :func:`model_discovery.discover_models_files`.
    """

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
    """Best-effort enumeration without running the full SuiteConfig
    initialiser. We need three fields per test (``name``, ``model``,
    ``tb``); the rest of the test config (sweep scripts, plusargs,
    UVM block) is irrelevant to the picker and might fail to
    deserialize on a project's stale ``tests.yaml``. Falling back to
    [] on parse error keeps the discovery walk robust to broken
    sibling projects, matching :func:`model_discovery._read_model_names`.
    """

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
    # Map ``testbench`` field on each test to the actual TB name; the
    # serde-side TestConfigFile keeps ``tb`` as a string reference,
    # not a resolved object. Tests whose tb reference is missing are
    # dropped — the same SuiteConfig initialiser would have raised
    # ``testbench_missing``, so silently skipping here keeps the
    # listing useful even when the suite has a broken test.
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
    """Enumerate every test in the project. With ``tests_file``
    set, only that file is scanned (matches ``--tests-file`` pin
    semantics on the CLI). Stable ordering: by tests-file path then
    by test name within each file.
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
    """Return every ``(tests_file, test_name)`` pair where ``tests_file``
    contains an entry named ``test_name``.
    """

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
    """Resolve ``test_name`` to the owning ``tests.yaml`` + resolved
    :class:`TestConfig`.

    - ``tests_file`` set → load directly via SuiteConfig, no walk.
    - ``tests_file`` unset → walk the project root; error on zero or
      multiple matches with a guide pointing at ``--tests-file``.

    Same shape as :func:`model_discovery.resolve_model` so the hub's
    HTTP layer can share its error-translation logic.
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
