from typer.testing import CliRunner

from rtl_buddy.skill_install import (
    LEGACY_SKILL_DIRNAME,
    SKILL_DIRNAME,
    SKILL_FILENAME,
    VERSION_MARKER,
    _bundled_gitignore_snippet,
    _update_gitignore,
    app,
)

runner = CliRunner()

_SNIPPET = (
    "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
    ".claude/skills/rtl-buddy/\n"
    ".agents/skills/rtl-buddy/\n"
)


def test_gitignore_created_when_missing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "added 2 pattern(s)"
    assert gitignore.exists()
    text = gitignore.read_text()
    assert ".claude/skills/rtl-buddy/" in text
    assert ".agents/skills/rtl-buddy/" in text
    assert "# rtl_buddy skill" in text


def test_already_present(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_SNIPPET)
    mtime = gitignore.stat().st_mtime
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "already present"
    assert gitignore.stat().st_mtime == mtime


def test_partial_update(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
        ".claude/skills/rtl-buddy/\n"
    )
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "added 1 pattern(s)"
    text = gitignore.read_text()
    assert ".agents/skills/rtl-buddy/" in text
    assert text.count(".claude/skills/rtl-buddy/") == 1
    assert text.count("# rtl_buddy skill") == 1


def test_dry_run_no_write(tmp_path):
    gitignore = tmp_path / ".gitignore"
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=True)
    assert result == "would add 2 pattern(s) (dry run)"
    assert not gitignore.exists()


def test_dry_run_already_present(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_SNIPPET)
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=True)
    assert result == "already present"


def test_no_trailing_newline(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.log")
    _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    text = gitignore.read_text()
    assert text.startswith("*.log\n")
    assert ".claude/skills/rtl-buddy/" in text
    assert ".agents/skills/rtl-buddy/" in text


def test_comment_not_duplicated(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
        ".claude/skills/rtl-buddy/\n"
    )
    _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    text = gitignore.read_text()
    assert text.count("# rtl_buddy skill") == 1


def test_patterns_present_comment_missing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".claude/skills/rtl-buddy/\n.agents/skills/rtl-buddy/\n")
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "already present"
    assert "# rtl_buddy skill" not in gitignore.read_text()


def test_install_dir_flat_target(tmp_path):
    result = runner.invoke(app, ["install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    skill = tmp_path / SKILL_DIRNAME / SKILL_FILENAME
    assert skill.is_file()
    # flat layout: no .claude / .agents intermediate dirs
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".agents").exists()


def test_install_dir_mutually_exclusive_with_project(tmp_path):
    result = runner.invoke(app, ["install", "--dir", str(tmp_path), "--project"])
    assert result.exit_code != 0
    assert "mutually exclusive" in str(result.exception)


def test_install_no_gitignore_skips_gitignore(tmp_path):
    result = runner.invoke(app, ["install", "--root", str(tmp_path), "--no-gitignore"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".gitignore").exists()
    assert ".gitignore:" not in result.output


def test_install_project_writes_gitignore_by_default(tmp_path):
    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitignore").is_file()


def test_skill_dirname_matches_frontmatter_name():
    """Agent Skills spec: `name:` must equal the containing directory name."""
    from rtl_buddy.skill_install import _bundled_skill_text

    for line in _bundled_skill_text().splitlines():
        if line.startswith("name:"):
            assert line.split(":", 1)[1].strip() == SKILL_DIRNAME
            break
    else:  # pragma: no cover - frontmatter always has a name
        raise AssertionError("SKILL.md frontmatter has no `name:` field")


def test_bundled_snippet_patterns_use_skill_dirname():
    text = _bundled_gitignore_snippet()
    assert f".claude/skills/{SKILL_DIRNAME}/" in text
    assert f".agents/skills/{SKILL_DIRNAME}/" in text
    assert f"skills/{LEGACY_SKILL_DIRNAME}/" not in text


def _legacy_install(parent, version="0.0.1"):
    """Materialize a legacy-named install (ours: carries the version marker)."""
    legacy = parent / LEGACY_SKILL_DIRNAME
    legacy.mkdir(parents=True)
    (legacy / SKILL_FILENAME).write_text("---\nname: rtl-buddy\n---\nold\n")
    (legacy / VERSION_MARKER).write_text(version + "\n")
    return legacy


def test_install_user_scope_uses_hyphenated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    result = runner.invoke(app, ["install"])
    assert result.exit_code == 0, result.output
    for parent in (
        tmp_path / ".claude" / "skills",
        tmp_path / ".codex" / "skills",
    ):
        assert (parent / SKILL_DIRNAME / SKILL_FILENAME).is_file()
        assert (parent / SKILL_DIRNAME / VERSION_MARKER).is_file()
        assert not (parent / LEGACY_SKILL_DIRNAME).exists()


def test_install_project_scope_uses_hyphenated_dir(tmp_path):
    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".claude" / "skills" / SKILL_DIRNAME / SKILL_FILENAME).is_file()
    assert (tmp_path / ".agents" / "skills" / SKILL_DIRNAME / SKILL_FILENAME).is_file()
    text = (tmp_path / ".gitignore").read_text()
    assert f".claude/skills/{SKILL_DIRNAME}/" in text
    assert f".agents/skills/{SKILL_DIRNAME}/" in text


def test_install_migrates_legacy_dir(tmp_path):
    parents = [
        tmp_path / ".claude" / "skills",
        tmp_path / ".agents" / "skills",
    ]
    for parent in parents:
        _legacy_install(parent)

    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "removed (legacy name)" in result.output
    assert "Migrated 2 legacy" in result.output
    for parent in parents:
        assert not (parent / LEGACY_SKILL_DIRNAME).exists()
        assert (parent / SKILL_DIRNAME / SKILL_FILENAME).is_file()


def test_install_dry_run_does_not_migrate(tmp_path):
    legacy = _legacy_install(tmp_path / ".claude" / "skills")
    result = runner.invoke(app, ["install", "--root", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would remove (legacy name)" in result.output
    assert legacy.is_dir()


def test_install_leaves_foreign_legacy_dir_alone(tmp_path):
    """A `rtl_buddy/` dir without our marker belongs to the user, not us."""
    legacy = tmp_path / ".claude" / "skills" / LEGACY_SKILL_DIRNAME
    legacy.mkdir(parents=True)
    (legacy / SKILL_FILENAME).write_text("hand-written\n")

    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "legacy name" not in result.output
    assert (legacy / SKILL_FILENAME).read_text() == "hand-written\n"


def test_install_dir_migrates_legacy_dir(tmp_path):
    legacy = _legacy_install(tmp_path)
    result = runner.invoke(app, ["install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not legacy.exists()
    assert (tmp_path / SKILL_DIRNAME / SKILL_FILENAME).is_file()


def test_install_preserves_unrelated_files_in_legacy_dir(tmp_path):
    legacy = _legacy_install(tmp_path / ".claude" / "skills")
    (legacy / "notes.md").write_text("mine\n")
    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (legacy / SKILL_FILENAME).exists()
    assert not (legacy / VERSION_MARKER).exists()
    assert (legacy / "notes.md").is_file()


def test_status_reports_legacy_install(tmp_path):
    _legacy_install(tmp_path / ".claude" / "skills")
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "installed at legacy path" in result.output
    assert "re-run `rtl-buddy skill install` to migrate" in result.output
    # the codex target has nothing at either spelling
    assert "not installed" in result.output


def test_status_reports_installed_for_current_dirname(tmp_path):
    runner.invoke(app, ["install", "--root", str(tmp_path)])
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "not installed" not in result.output
    assert "installed at legacy path" not in result.output
    assert "installed @" in result.output


def test_status_ignores_foreign_legacy_dir(tmp_path):
    legacy = tmp_path / ".claude" / "skills" / LEGACY_SKILL_DIRNAME
    legacy.mkdir(parents=True)
    (legacy / SKILL_FILENAME).write_text("hand-written\n")
    result = runner.invoke(app, ["status", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "legacy path" not in result.output
    assert result.output.count("not installed") == 2


def test_uninstall_removes_current_dirname(tmp_path):
    runner.invoke(app, ["install", "--root", str(tmp_path)])
    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".claude" / "skills" / SKILL_DIRNAME).exists()
    assert not (tmp_path / ".agents" / "skills" / SKILL_DIRNAME).exists()


def test_uninstall_removes_legacy_dirname(tmp_path):
    _legacy_install(tmp_path / ".claude" / "skills")
    _legacy_install(tmp_path / ".agents" / "skills")
    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Nothing to remove." not in result.output
    assert not (tmp_path / ".claude" / "skills" / LEGACY_SKILL_DIRNAME).exists()
    assert not (tmp_path / ".agents" / "skills" / LEGACY_SKILL_DIRNAME).exists()


def test_uninstall_removes_both_spellings(tmp_path):
    runner.invoke(app, ["install", "--root", str(tmp_path)])
    _legacy_install(tmp_path / ".claude" / "skills")
    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    skills = tmp_path / ".claude" / "skills"
    assert not (skills / SKILL_DIRNAME).exists()
    assert not (skills / LEGACY_SKILL_DIRNAME).exists()


def test_uninstall_nothing_to_remove(tmp_path):
    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Nothing to remove." in result.output
