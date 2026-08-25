from typer.testing import CliRunner

from rtl_buddy.skill_install import (
    LEGACY_SKILL_DIRNAME,
    SKILL_DIRNAME,
    SKILL_DIRNAMES,
    SKILL_FILENAME,
    SPECIALIST_SKILL_DIRNAMES,
    VERSION_MARKER,
    _bundled_skill_text,
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
    for skill_name in SKILL_DIRNAMES:
        assert (tmp_path / skill_name / SKILL_FILENAME).is_file()
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


def test_bundled_snippet_patterns_use_skill_dirname():
    text = _bundled_gitignore_snippet()
    for skill_name in SKILL_DIRNAMES:
        assert f".claude/skills/{skill_name}/" in text
        assert f".agents/skills/{skill_name}/" in text
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
        for skill_name in SKILL_DIRNAMES:
            assert (parent / skill_name / SKILL_FILENAME).is_file()
            assert (parent / skill_name / VERSION_MARKER).is_file()
        assert not (parent / LEGACY_SKILL_DIRNAME).exists()


def test_install_project_scope_uses_hyphenated_dir(tmp_path):
    result = runner.invoke(app, ["install", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for parent in (
        tmp_path / ".claude" / "skills",
        tmp_path / ".agents" / "skills",
    ):
        for skill_name in SKILL_DIRNAMES:
            assert (parent / skill_name / SKILL_FILENAME).is_file()
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
    assert result.output.count("not installed") == 2 * len(SKILL_DIRNAMES)


def test_uninstall_removes_current_dirname(tmp_path):
    runner.invoke(app, ["install", "--root", str(tmp_path)])
    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for parent in (
        tmp_path / ".claude" / "skills",
        tmp_path / ".agents" / "skills",
    ):
        for skill_name in SKILL_DIRNAMES:
            assert not (parent / skill_name).exists()


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


def test_install_upgrades_primary_only_install_to_family(tmp_path):
    for parent in (
        tmp_path / ".claude" / "skills",
        tmp_path / ".agents" / "skills",
    ):
        primary = parent / SKILL_DIRNAME
        primary.mkdir(parents=True)
        (primary / SKILL_FILENAME).write_text(_bundled_skill_text())
        (primary / VERSION_MARKER).write_text("0.0.1\n")

    result = runner.invoke(app, ["install", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    for parent in (
        tmp_path / ".claude" / "skills",
        tmp_path / ".agents" / "skills",
    ):
        for skill_name in SKILL_DIRNAMES:
            assert (parent / skill_name / SKILL_FILENAME).is_file()


def test_status_reports_missing_specialists_for_primary_only_install(tmp_path):
    primary = tmp_path / ".claude" / "skills" / SKILL_DIRNAME
    primary.mkdir(parents=True)
    (primary / SKILL_FILENAME).write_text(_bundled_skill_text())
    (primary / VERSION_MARKER).write_text("0.0.1\n")

    result = runner.invoke(app, ["status", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output.count("not installed") == (2 * len(SKILL_DIRNAMES) - 1)
    for skill_name in SPECIALIST_SKILL_DIRNAMES:
        assert skill_name in result.output


def test_install_refuses_unmanaged_specialist_directory(tmp_path):
    foreign = tmp_path / ".claude" / "skills" / SPECIALIST_SKILL_DIRNAMES[0]
    foreign.mkdir(parents=True)
    (foreign / SKILL_FILENAME).write_text("hand-written\n")

    result = runner.invoke(app, ["install", "--root", str(tmp_path)])

    assert result.exit_code != 0
    assert "Refusing to overwrite unmanaged skill directory" in str(result.exception)
    assert (foreign / SKILL_FILENAME).read_text() == "hand-written\n"
    assert not (tmp_path / ".agents").exists()


def test_install_preflights_specialist_file_conflict_atomically(tmp_path):
    conflict = tmp_path / ".agents" / "skills" / SPECIALIST_SKILL_DIRNAMES[-1]
    conflict.parent.mkdir(parents=True)
    conflict.write_text("not a directory\n")

    result = runner.invoke(app, ["install", "--root", str(tmp_path)])

    assert result.exit_code != 0
    assert "Refusing to overwrite unmanaged skill directory" in str(result.exception)
    assert conflict.read_text() == "not a directory\n"
    assert not (tmp_path / ".claude").exists()
    assert not (
        tmp_path / ".agents" / "skills" / SKILL_DIRNAME / SKILL_FILENAME
    ).exists()


def test_uninstall_preserves_unmanaged_specialist_directory(tmp_path):
    foreign = tmp_path / ".claude" / "skills" / SPECIALIST_SKILL_DIRNAMES[0]
    foreign.mkdir(parents=True)
    (foreign / SKILL_FILENAME).write_text("hand-written\n")

    result = runner.invoke(app, ["uninstall", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (foreign / SKILL_FILENAME).read_text() == "hand-written\n"


def test_install_family_dry_run_writes_nothing(tmp_path):
    result = runner.invoke(app, ["install", "--root", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    for skill_name in SKILL_DIRNAMES:
        assert f"/{skill_name}/{SKILL_FILENAME}" in result.output
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".agents").exists()


def test_gitignore_drops_the_pre_rename_patterns(tmp_path):
    """`.gitignore` is the one tracked file the #434 rename touches, so the
    dead patterns come out rather than accumulating under the same comment."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
        ".claude/skills/rtl_buddy/\n"
        ".agents/skills/rtl_buddy/\n"
    )

    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)

    text = gitignore.read_text()
    assert ".claude/skills/rtl_buddy/" not in text
    assert ".agents/skills/rtl_buddy/" not in text
    assert ".claude/skills/rtl-buddy/" in text
    assert ".agents/skills/rtl-buddy/" in text
    # The comment was already there, so it is not duplicated.
    assert text.count("# rtl_buddy skill") == 1
    assert "removed 2 legacy pattern(s)" in result


def test_gitignore_leaves_hand_edited_legacy_lines_alone(tmp_path):
    """Only an exact match on the shipped pre-rename text is ours to remove."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        ".claude/skills/rtl_buddy/   # keep: vendored by hand\n"
        "!.agents/skills/rtl_buddy/keepme\n"
    )

    _update_gitignore(gitignore, _SNIPPET, dry_run=False)

    text = gitignore.read_text()
    assert ".claude/skills/rtl_buddy/   # keep: vendored by hand" in text
    assert "!.agents/skills/rtl_buddy/keepme" in text


def test_gitignore_prunes_legacy_even_when_new_patterns_are_present(tmp_path):
    """The early "already present" return must not skip the prune."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_SNIPPET + ".claude/skills/rtl_buddy/\n")

    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)

    assert ".claude/skills/rtl_buddy/" not in gitignore.read_text()
    assert result == "removed 1 legacy pattern(s)"


def test_gitignore_dry_run_reports_the_prune_without_writing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    original = _SNIPPET + ".claude/skills/rtl_buddy/\n"
    gitignore.write_text(original)

    result = _update_gitignore(gitignore, _SNIPPET, dry_run=True)

    assert gitignore.read_text() == original
    assert "would remove 1 legacy pattern(s)" in result
    assert "(dry run)" in result
