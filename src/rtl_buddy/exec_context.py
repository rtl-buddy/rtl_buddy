"""Execution context for one rtl_buddy command invocation.

It holds the paths that anchor outputs and relative arguments; the fields are
described on :class:`ExecutionContext`. See ``docs/concepts/execution-context.md``
for the user-facing description and ``docs/development/guidelines.md`` for the
policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .logging_utils import DEFAULT_FILE_LOG
from .tools.artifact_paths import (
    RUNS_DIRNAME,
    run_artifact_root,
    sanitize_artifact_component,
    validate_run_tag,
)


def _tagged_artifact_root(
    command_root: Path, artifact_root: Path | None, run_tag: str | None
) -> Path:
    """The artefact root for one invocation, with ``run_tag`` applied.

    An explicit ``artifact_root`` is namespaced by the tag too, so a tag always
    selects a separate tree.
    """
    if artifact_root is None:
        return run_artifact_root(command_root, run_tag)
    resolved = Path(artifact_root).resolve()
    if run_tag is None:
        return resolved
    return resolved / RUNS_DIRNAME / validate_run_tag(run_tag)


@dataclass(frozen=True)
class ExecutionContext:
    """Paths that anchor a single command's execution.

    - ``invocation_cwd``: the directory the user ran ``rb`` from. Explicit CLI
      input and output paths resolve against it.
    - ``command_root``: the directory of the command's primary config file.
      Logs and the artefact tree anchor here, whatever the invocation directory.
    - ``artifact_root``: where per-item artefact trees live. Default
      ``command_root/artefacts``; with a run tag,
      ``command_root/artefacts/.runs/<tag>``, so concurrent runs of one suite
      hold different tree locks.
    - ``run_tag``: the optional ``--run-tag`` namespace.

    Construct with :meth:`for_command` or :meth:`for_dir`. The dataclass is
    frozen.
    """

    invocation_cwd: Path
    command_root: Path
    artifact_root: Path
    primary_config: Path | None = None
    run_tag: str | None = None

    @classmethod
    def for_command(
        cls,
        invocation_cwd: Path,
        primary_config: Path,
        *,
        artifact_root: Path | None = None,
        run_tag: str | None = None,
    ) -> "ExecutionContext":
        """Build an :class:`ExecutionContext` for a command.

        ``primary_config`` is the command's ``-c`` argument. It is resolved
        against ``invocation_cwd`` and its parent becomes the command root.
        ``artifact_root`` is reserved for an override flag; ``None`` gives the
        default layout. ``run_tag`` namespaces the layout; ``None`` gives the
        flat tree.
        """
        invocation_cwd = Path(invocation_cwd).resolve()
        primary_config = Path(primary_config)
        if not primary_config.is_absolute():
            primary_config = invocation_cwd / primary_config
        primary_config = primary_config.resolve()
        command_root = primary_config.parent
        artifact_root = _tagged_artifact_root(command_root, artifact_root, run_tag)
        return cls(
            invocation_cwd=invocation_cwd,
            command_root=command_root,
            artifact_root=artifact_root,
            primary_config=primary_config,
            run_tag=run_tag,
        )

    @classmethod
    def for_dir(
        cls,
        invocation_cwd: Path,
        command_root: Path,
        *,
        artifact_root: Path | None = None,
        run_tag: str | None = None,
    ) -> "ExecutionContext":
        """Build an :class:`ExecutionContext` from a directory instead of a config file.

        For commands anchored at a directory, such as ``hub`` at the project root.
        """
        invocation_cwd = Path(invocation_cwd).resolve()
        command_root = Path(command_root).resolve()
        artifact_root = _tagged_artifact_root(command_root, artifact_root, run_tag)
        return cls(
            invocation_cwd=invocation_cwd,
            command_root=command_root,
            artifact_root=artifact_root,
            run_tag=run_tag,
        )

    def artifact_dir(self, *parts: str) -> Path:
        """Return ``artifact_root/<sanitized parts...>`` without creating it.

        Each part is sanitized separately, so ``foo/bar`` does not nest.
        """
        sanitized = [sanitize_artifact_component(p) for p in parts if p]
        return self.artifact_root.joinpath(*sanitized)

    def resolve_input(self, path: str | Path) -> Path:
        """Resolve a user-supplied path against ``invocation_cwd``.

        For explicit CLI input and output arguments. Absolute paths pass through.
        """
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (self.invocation_cwd / p).resolve()

    @property
    def log_path(self) -> Path:
        """Where ``rtl_buddy.log`` is written for this command.

        Beside the command's config, or in the tagged artefact root under
        ``--run-tag``. The first open truncates the file, so concurrent runs
        need separate logs.
        """
        if self.run_tag is None:
            return self.command_root / DEFAULT_FILE_LOG
        return self.artifact_root / DEFAULT_FILE_LOG
