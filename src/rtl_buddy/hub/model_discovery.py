"""Model discovery for ``rb hub start --model NAME``.

Walks the project tree for ``models.yaml`` files, aggregates the
named entries, and resolves a single match for the user-supplied
``--model NAME`` (with optional ``--models-file`` override).

Cross-file name collisions are explicit errors that point the user
at ``--models-file PATH`` for disambiguation. This matches the
contract spelled out in issue #167.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from serde.yaml import from_yaml

from ..config.model import ModelConfigFile, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Directories we never descend into during the discovery walk. Build
# artefacts and VCS metadata can contain copied YAML files (e.g.
# example fixtures vendored under tests/), which would otherwise show
# up as spurious matches.
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
class ModelMatch:
    """One ``--model NAME`` candidate during discovery."""

    models_file: Path
    model_name: str


def discover_models_files(root: Path) -> list[Path]:
    """Return every ``models.yaml`` reachable under ``root``.

    Skips conventional build/VCS directories so vendored fixtures
    don't pollute the candidate set. Order is deterministic
    (alphabetical) so collision-error messages don't churn between
    invocations.
    """

    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in place so os.walk skips our excludes.
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "models.yaml" in filenames:
            results.append(Path(dirpath) / "models.yaml")
    return results


def _read_model_names(path: Path) -> list[str]:
    """Best-effort name extraction without full ``ModelConfigLoader``
    validation — the discovery walk shouldn't fatal-error on a
    sibling project's malformed models.yaml that the user isn't
    asking about. Returns ``[]`` on parse failure (logged at DEBUG).
    """

    try:
        data = from_yaml(ModelConfigFile, path.read_text())
    except Exception as exc:
        log_event(
            logger,
            logging.DEBUG,
            "hub.model_discovery.parse_skipped",
            path=str(path),
            error=str(exc),
        )
        return []
    return [m.name for m in data.models]


def find_matches(models_files: list[Path], model_name: str) -> list[ModelMatch]:
    """Return every ``(models_file, model_name)`` pair where ``models_file``
    contains an entry named ``model_name``.
    """

    matches: list[ModelMatch] = []
    for mf in models_files:
        if model_name in _read_model_names(mf):
            matches.append(ModelMatch(models_file=mf, model_name=model_name))
    return matches


def resolve_model(
    root: Path,
    model_name: str,
    *,
    models_file: Path | None = None,
) -> tuple[Path, "ModelConfigLoader"]:
    """Resolve ``model_name`` to the owning ``models.yaml`` + loader.

    - ``models_file`` set → load directly, no discovery walk.
    - ``models_file`` unset → walk the project root for every
      ``models.yaml``. Error on zero or multiple matches.

    Returns ``(models_yaml_path, ModelConfigLoader)``. The loader's
    duplicate-name guard runs as part of construction (per #169).
    """

    if models_file is not None:
        if not models_file.is_file():
            raise FatalRtlBuddyError(f"--models-file {models_file}: not a file")
        loader = ModelConfigLoader(str(models_file))
        # Trigger lookup so a missing model name surfaces with the
        # loader's own diagnostic.
        loader.get_model(model_name)
        return models_file, loader

    models_files = discover_models_files(root)
    if not models_files:
        raise FatalRtlBuddyError(
            f"no models.yaml found under {root}; "
            f"use --models-file PATH to point at one explicitly"
        )

    matches = find_matches(models_files, model_name)
    if len(matches) == 0:
        # Build a sample list of names from every file so the user
        # can see if they had a typo.
        sample: list[str] = []
        for mf in models_files:
            for n in _read_model_names(mf):
                sample.append(
                    f"{mf.relative_to(root) if mf.is_relative_to(root) else mf}::{n}"
                )
        log_event(
            logger,
            logging.ERROR,
            "hub.model_discovery.not_found",
            model=model_name,
            root=str(root),
            candidates=sample,
        )
        candidates_msg = (
            "\n  ".join(sample) if sample else "(no models defined in any file)"
        )
        raise FatalRtlBuddyError(
            f"model {model_name!r} not found in any models.yaml under {root}.\n"
            f"  candidates:\n  {candidates_msg}"
        )

    if len(matches) > 1:
        paths_msg = "\n  ".join(str(m.models_file) for m in matches)
        log_event(
            logger,
            logging.ERROR,
            "hub.model_discovery.ambiguous",
            model=model_name,
            root=str(root),
            matches=[str(m.models_file) for m in matches],
        )
        raise FatalRtlBuddyError(
            f"model {model_name!r} matches multiple models.yaml files; "
            f"pass --models-file PATH to disambiguate:\n  {paths_msg}"
        )

    chosen = matches[0]
    loader = ModelConfigLoader(str(chosen.models_file))
    loader.get_model(model_name)  # validate
    return chosen.models_file, loader
