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
  trees live. Defaults to ``command_root / "artefacts"``; ``--run-tag``
  moves it to ``command_root/artefacts/.runs/<tag>`` so concurrent runs in
  one checkout get separate trees (#541), and an explicit ``artifact_root``
  can redirect it onto a separate disk without affecting any downstream
  consumer.

See ``docs/concepts/execution-context.md`` for the user-facing description
and ``docs/development/guidelines.md`` for the policy these fields encode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tools.artifact_paths import sanitize_artifact_component, suite_artifact_root


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
    #: The ``--run-tag`` in force, already normalized, or ``None``. Carried
    #: so a command that builds artefact paths for a *different* suite than
    #: its own root (a regression walking its suites, `rb graph results`
    #: scanning them) can namespace those the same way.
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

        ``run_tag`` namespaces the artefact tree so two runs in one
        checkout do not share it (#541). ``artifact_root`` overrides the
        layout outright and wins over ``run_tag``; pass ``None`` for both to
        get the default ``command_root/artefacts``.
        """
        invocation_cwd = Path(invocation_cwd).resolve()
        primary_config = Path(primary_config)
        if not primary_config.is_absolute():
            primary_config = invocation_cwd / primary_config
        primary_config = primary_config.resolve()
        command_root = primary_config.parent
        if artifact_root is None:
            artifact_root = suite_artifact_root(command_root, run_tag)
        else:
            artifact_root = Path(artifact_root).resolve()
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
        if artifact_root is None:
            artifact_root = suite_artifact_root(command_root, run_tag)
        else:
            artifact_root = Path(artifact_root).resolve()
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

        Under a ``--run-tag`` it follows the artefact tree (#541). Two heads
        sharing one command root would otherwise share this file, and the
        handler truncates a path on its first open in each process, so the
        second run to start would erase the first one's log.
        """
        if self.run_tag is None:
            return self.command_root / "rtl_buddy.log"
        return self.artifact_root / "rtl_buddy.log"
