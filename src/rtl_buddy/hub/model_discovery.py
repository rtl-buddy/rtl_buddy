"""Model discovery for ``rb hub start --model NAME``.

Walks the project for ``models.yaml`` files and resolves one match for the model name, with an optional ``--models-file`` override. Cross-file name collisions are errors that point at ``--models-file PATH``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..config.yaml_loader import config_from_yaml

from ..config.model import ModelConfigFile, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Directories the walk never enters: build output and VCS metadata can hold
# copied YAML that would show up as spurious matches.
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
    """Return every ``models.yaml`` under ``root``, in alphabetical order.

    Skips build and VCS directories and nested git worktrees, whose
    ``models.yaml`` files duplicate the parent's and would collide.
    """

    root_resolved = Path(root).resolve()
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # A nested worktree has ``.git`` as a file; the starting root's ``.git``
        # is a directory and must not be pruned.
        if Path(dirpath).resolve() != root_resolved and ".git" in filenames:
            git_entry = Path(dirpath) / ".git"
            if git_entry.is_file():
                dirnames.clear()
                continue
        # In place, so os.walk skips the excluded directories.
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "models.yaml" in filenames:
            results.append(Path(dirpath) / "models.yaml")
    return results


def _read_model_names(path: Path) -> list[str]:
    """Model names in a ``models.yaml``, without full validation.

    Returns ``[]`` on parse failure (logged at DEBUG), so a malformed file the
    user did not ask about does not fail discovery.
    """

    try:
        data = config_from_yaml(ModelConfigFile, path.read_text(), path)
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
    """Return a match for every file that has an entry named ``model_name``."""

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
    """Resolve ``model_name`` to its ``models.yaml`` and loader.

    With ``models_file`` set, load it directly. Otherwise walk ``root`` and
    require exactly one match. Returns ``(models_yaml_path, ModelConfigLoader)``.
    """

    if models_file is not None:
        if not models_file.is_file():
            raise FatalRtlBuddyError(f"--models-file {models_file}: not a file")
        loader = ModelConfigLoader(str(models_file))
        # Surfaces a missing model name with the loader's own diagnostic.
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
        # Candidates for the error message, to expose typos.
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
