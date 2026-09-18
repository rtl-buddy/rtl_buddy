"""Execution context for a single rtl_buddy command invocation.

A command's `ExecutionContext` captures the three paths that decide where
outputs land and how relative arguments are resolved:

- ``invocation_cwd`` — the directory the user ran ``rb`` from. Explicit CLI
  input/output paths are resolved against this so the shell behaves normally.
- ``command_root`` — the directory containing the command's primary config
  file (``tests.yaml``, ``synth.yaml``, ``cdc.yaml``, etc.). Orchestration
  logs and the artifact tree are anchored here so the same command produces
  the same layout regardless of where the user invoked it from.
- ``artifact_root`` — the directory under which per-command-item artifact
  trees live. Defaults to ``command_root / "artefacts"``; a future
  ``--artifact-root`` flag can redirect this onto a separate disk without
  affecting any downstream consumer.
- ``run_tag`` — the optional ``--run-tag`` namespace (#541). When set, the
  artifact root moves to ``command_root / "artefacts" / ".runs" / <tag>``,
  which is what lets two runs of one suite hold different tree locks and
  write disjoint paths. Unset is today's layout exactly.

See ``docs/concepts/execution-context.md`` for the user-facing description
and ``docs/development/guidelines.md`` for the policy these fields encode.
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
    """The artefact root for one invocation, tag applied (#541).

    An explicit ``artifact_root`` is resolved and then namespaced too: the
    tag says "this invocation's tree", which has to mean the same thing
    wherever the tree was redirected to, or a future ``--artifact-root``
    would quietly switch concurrent runs back onto one lock.
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

    Construct with :meth:`for_command` from inside a command handler once
    the primary config path has been resolved. The dataclass is frozen so
    downstream code can safely cache references.
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

        ``primary_config`` is the command's ``-c`` argument (e.g.
        ``tests.yaml``, ``synth.yaml``). It is resolved against
        ``invocation_cwd`` and made absolute, then its parent becomes the
        command root.

        ``artifact_root`` is reserved for a future override flag; pass
        ``None`` for the default ``command_root/artefacts`` layout.

        ``run_tag`` namespaces that layout (#541); pass ``None`` for the
        flat tree every untagged run keeps.
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
        """Build an :class:`ExecutionContext` from a directory, not a config file.

        Used by commands whose anchor is naturally a directory (e.g. ``hub``
        at the project root) rather than a YAML config.
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

        Each part is sanitized independently so test names like
        ``foo/bar`` don't accidentally create nested directories.
        """
        sanitized = [sanitize_artifact_component(p) for p in parts if p]
        return self.artifact_root.joinpath(*sanitized)

    def resolve_input(self, path: str | Path) -> Path:
        """Resolve a user-supplied path against ``invocation_cwd``.

        Use this for explicit CLI input/output arguments (e.g. ``-o
        report.svg``) so shell behavior matches user expectations.
        Absolute paths pass through unchanged.
        """
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (self.invocation_cwd / p).resolve()

    @property
    def log_path(self) -> Path:
        """Where ``rtl_buddy.log`` should be written for this command.

        Beside the command's config, as it always has been — except under
        a ``--run-tag``, where it moves into the tagged artefact root. A
        file log is opened for *writing* and the first open truncates it,
        so two concurrent runs of one suite sharing
        ``<suite>/rtl_buddy.log`` would erase each other's record: the same
        failure #437 fixed for dispatched jobs, reached by a second route.
        A tagged run's log therefore belongs to its own tree.
        """
        if self.run_tag is None:
            return self.command_root / DEFAULT_FILE_LOG
        return self.artifact_root / DEFAULT_FILE_LOG
