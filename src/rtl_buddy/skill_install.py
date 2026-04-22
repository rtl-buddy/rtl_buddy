"""`rtl-buddy skill ...` subcommands: materialize the bundled agent skill.

Skill content ships inside the wheel at `rtl_buddy.skill`. There is no
PEP 517 post-install hook, so users run `rtl-buddy skill install` once to
copy `SKILL.md` to the Claude Code / Codex skill directories. Default scope
is user-level; `--project` (or `--root PATH`) opts into project-level, which
Claude Code resolves with higher precedence than user-level.
"""
from __future__ import annotations

import hashlib
from importlib.metadata import version as _pkg_version
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from .config.root import _discover_root_cfg
from .errors import FatalRtlBuddyError


SKILL_DIRNAME = "rtl_buddy"
SKILL_FILENAME = "SKILL.md"
VERSION_MARKER = ".rtl_buddy_skill_version"
PACKAGE_NAME = "rtl-buddy"

app = typer.Typer(help="manage the rtl_buddy agent skill", no_args_is_help=True)


def _package_version() -> str:
    return _pkg_version(PACKAGE_NAME)


def _bundled_skill_text() -> str:
    return _resource_files("rtl_buddy.skill").joinpath(SKILL_FILENAME).read_text()


def _bundled_gitignore_snippet() -> str:
    return _resource_files("rtl_buddy.skill").joinpath("gitignore_snippet.txt").read_text()


def _find_git_root(start: Path) -> Optional[Path]:
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _discover_project_root() -> Path:
    """Find project root via rtl_buddy's own root_config.yaml discovery, then .git."""
    cfg_path = _discover_root_cfg()
    if cfg_path is not None:
        return Path(cfg_path).parent
    git_root = _find_git_root(Path.cwd())
    if git_root is not None:
        return git_root
    raise FatalRtlBuddyError(
        "skill install --project: cannot locate project root "
        "(no root_config.yaml or .git found above cwd). "
        "Pass --root PATH or run from inside a project."
    )


def _resolve_root(project: bool, root: Optional[Path]) -> tuple[str, Path]:
    """Return (scope_label, target_root) for the requested scope.

    scope_label is 'user' or 'project' and drives per-scope target dirs.
    """
    if project and root is not None:
        raise FatalRtlBuddyError("--project and --root are mutually exclusive.")
    if root is not None:
        return "project", root.expanduser().resolve()
    if project:
        return "project", _discover_project_root().resolve()
    return "user", Path.home()


def _targets(scope: str, base: Path, include_claude: bool, include_codex: bool) -> list[tuple[str, Path]]:
    """Return the (label, dir) pairs that should receive a copy of SKILL.md."""
    if scope == "user":
        claude = base / ".claude" / "skills" / SKILL_DIRNAME
        codex = base / ".codex" / "skills" / SKILL_DIRNAME
    else:
        claude = base / ".claude" / "skills" / SKILL_DIRNAME
        codex = base / ".agents" / "skills" / SKILL_DIRNAME

    out: list[tuple[str, Path]] = []
    if include_claude:
        out.append(("claude", claude))
    if include_codex:
        out.append(("codex", codex))
    return out


def _same_content(path: Path, text: str) -> bool:
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(text.encode()).hexdigest()


def _update_gitignore(gitignore_path: Path, snippet: str, *, dry_run: bool) -> str:
    snippet_lines = snippet.strip().splitlines()
    comment_lines = [line for line in snippet_lines if line.startswith("#")]
    pattern_lines = [line for line in snippet_lines if line and not line.startswith("#")]

    existing_text = gitignore_path.read_text() if gitignore_path.is_file() else ""
    existing_lines = {line.strip() for line in existing_text.splitlines()}

    missing = [p for p in pattern_lines if p.strip() not in existing_lines]
    if not missing:
        return "already present"

    lines_to_add = []
    for cl in comment_lines:
        if cl.strip() not in existing_lines:
            lines_to_add.append(cl)
    lines_to_add.extend(missing)

    if dry_run:
        return f"would add {len(missing)} pattern(s) (dry run)"

    if not existing_text:
        prefix = ""
    elif existing_text.endswith("\n"):
        prefix = "\n"
    else:
        prefix = "\n\n"

    gitignore_path.open("a").write(prefix + "\n".join(lines_to_add) + "\n")
    return f"added {len(missing)} pattern(s)"


@app.command("install")
def cmd_install(
    project: Annotated[bool, typer.Option("--project", help="install into the discovered project root instead of the user home")] = False,
    root: Annotated[Optional[Path], typer.Option("--root", help="explicit target root (implies project-level layout)")] = None,
    no_claude: Annotated[bool, typer.Option("--no-claude", help="skip writing the Claude Code target")] = False,
    no_codex: Annotated[bool, typer.Option("--no-codex", help="skip writing the Codex target")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print what would be written and exit")] = False,
    force: Annotated[bool, typer.Option("--force", help="overwrite even when content matches")] = False,
):
    """Install the bundled rtl_buddy skill.

    Default scope is user-level (`~/.claude/skills/rtl_buddy/` and
    `~/.codex/skills/rtl_buddy/`). Use `--project` to install into the
    discovered project root instead; project-level copies take precedence
    over user-level when both exist.
    """
    scope, base = _resolve_root(project, root)
    targets = _targets(scope, base, include_claude=not no_claude, include_codex=not no_codex)
    if not targets:
        raise FatalRtlBuddyError("--no-claude and --no-codex leave nothing to install.")

    skill_text = _bundled_skill_text()
    ver = _package_version()

    typer.echo(f"Scope:   {scope}")
    typer.echo(f"Base:    {base}")
    typer.echo(f"Version: {ver}")
    typer.echo("")

    changed = 0
    unchanged = 0
    for label, target_dir in targets:
        skill_path = target_dir / SKILL_FILENAME
        marker_path = target_dir / VERSION_MARKER
        content_matches = _same_content(skill_path, skill_text)
        marker_matches = marker_path.is_file() and marker_path.read_text().strip() == ver
        needs_write = force or not content_matches or not marker_matches

        action = "write" if needs_write else "skip (up to date)"
        typer.echo(f"  [{label:>6}] {skill_path}  — {action}")

        if needs_write and not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill_text)
            marker_path.write_text(ver + "\n")
            changed += 1
        elif not needs_write:
            unchanged += 1

    typer.echo("")
    if dry_run:
        typer.echo("Dry run — no files written.")
    else:
        typer.echo(f"Wrote {changed} file(s); {unchanged} already up to date.")

    if scope == "project":
        gitignore_path = base / ".gitignore"
        result = _update_gitignore(gitignore_path, _bundled_gitignore_snippet(), dry_run=dry_run)
        typer.echo(f".gitignore: {result}")


@app.command("uninstall")
def cmd_uninstall(
    project: Annotated[bool, typer.Option("--project", help="uninstall from the discovered project root instead of the user home")] = False,
    root: Annotated[Optional[Path], typer.Option("--root", help="explicit target root (implies project-level layout)")] = None,
    no_claude: Annotated[bool, typer.Option("--no-claude", help="skip the Claude Code target")] = False,
    no_codex: Annotated[bool, typer.Option("--no-codex", help="skip the Codex target")] = False,
):
    """Remove the installed rtl_buddy skill files from the selected scope."""
    scope, base = _resolve_root(project, root)
    targets = _targets(scope, base, include_claude=not no_claude, include_codex=not no_codex)

    removed = 0
    for label, target_dir in targets:
        skill_path = target_dir / SKILL_FILENAME
        marker_path = target_dir / VERSION_MARKER
        if skill_path.is_file():
            skill_path.unlink()
            removed += 1
            typer.echo(f"  [{label:>6}] removed {skill_path}")
        if marker_path.is_file():
            marker_path.unlink()
        if target_dir.is_dir() and not any(target_dir.iterdir()):
            target_dir.rmdir()

    if removed == 0:
        typer.echo("Nothing to remove.")


@app.command("status")
def cmd_status(
    project: Annotated[bool, typer.Option("--project", help="report status for the discovered project root instead of the user home")] = False,
    root: Annotated[Optional[Path], typer.Option("--root", help="explicit target root (implies project-level layout)")] = None,
):
    """Report whether the skill is installed and whether it matches the current package version."""
    scope, base = _resolve_root(project, root)
    targets = _targets(scope, base, include_claude=True, include_codex=True)
    current = _package_version()

    typer.echo(f"Scope:   {scope}")
    typer.echo(f"Base:    {base}")
    typer.echo(f"Version: {current} (installed rtl_buddy)")
    typer.echo("")

    for label, target_dir in targets:
        marker = target_dir / VERSION_MARKER
        skill_path = target_dir / SKILL_FILENAME
        if not skill_path.is_file():
            state = "not installed"
        elif marker.is_file():
            on_disk = marker.read_text().strip()
            state = f"installed @ {on_disk}" + ("" if on_disk == current else " (stale — re-run `rtl-buddy skill install`)")
        else:
            state = "installed (version unknown — re-run `rtl-buddy skill install`)"
        typer.echo(f"  [{label:>6}] {target_dir}  — {state}")


@app.command("print-gitignore")
def cmd_print_gitignore():
    """Print the gitignore lines for project-level skill installs."""
    typer.echo(_bundled_gitignore_snippet(), nl=False)
