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

See ``docs/concepts/execution-context.md`` for the user-facing description
and ``docs/development/guidelines.md`` for the policy these fields encode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tools.artifact_paths import sanitize_artifact_component


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

    @classmethod
    def for_command(
        cls,
        invocation_cwd: Path,
        primary_config: Path,
        *,
        artifact_root: Path | None = None,
    ) -> "ExecutionContext":
        """Build an :class:`ExecutionContext` for a command.

        ``primary_config`` is the command's ``-c`` argument (e.g.
        ``tests.yaml``, ``synth.yaml``). It is resolved against
        ``invocation_cwd`` and made absolute, then its parent becomes the
        command root.

        ``artifact_root`` is reserved for a future override flag; pass
        ``None`` for the default ``command_root/artefacts`` layout.
        """
        invocation_cwd = Path(invocation_cwd).resolve()
        primary_config = Path(primary_config)
        if not primary_config.is_absolute():
            primary_config = invocation_cwd / primary_config
        primary_config = primary_config.resolve()
        command_root = primary_config.parent
        if artifact_root is None:
            artifact_root = command_root / "artefacts"
        else:
            artifact_root = Path(artifact_root).resolve()
        return cls(
            invocation_cwd=invocation_cwd,
            command_root=command_root,
            artifact_root=artifact_root,
            primary_config=primary_config,
        )

    @classmethod
    def for_dir(
        cls,
        invocation_cwd: Path,
        command_root: Path,
        *,
        artifact_root: Path | None = None,
    ) -> "ExecutionContext":
        """Build an :class:`ExecutionContext` from a directory, not a config file.

        Used by commands whose anchor is naturally a directory (e.g. ``hub``
        at the project root) rather than a YAML config.
        """
        invocation_cwd = Path(invocation_cwd).resolve()
        command_root = Path(command_root).resolve()
        if artifact_root is None:
            artifact_root = command_root / "artefacts"
        else:
            artifact_root = Path(artifact_root).resolve()
        return cls(
            invocation_cwd=invocation_cwd,
            command_root=command_root,
            artifact_root=artifact_root,
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
        """Where ``rtl_buddy.log`` should be written for this command."""
        return self.command_root / "rtl_buddy.log"
