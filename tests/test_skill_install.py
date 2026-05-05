from rtl_buddy.skill_install import _update_gitignore

_SNIPPET = (
    "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
    ".claude/skills/rtl_buddy/\n"
    ".agents/skills/rtl_buddy/\n"
)


def test_gitignore_created_when_missing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "added 2 pattern(s)"
    assert gitignore.exists()
    text = gitignore.read_text()
    assert ".claude/skills/rtl_buddy/" in text
    assert ".agents/skills/rtl_buddy/" in text
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
        ".claude/skills/rtl_buddy/\n"
    )
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "added 1 pattern(s)"
    text = gitignore.read_text()
    assert ".agents/skills/rtl_buddy/" in text
    assert text.count(".claude/skills/rtl_buddy/") == 1
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
    assert ".claude/skills/rtl_buddy/" in text
    assert ".agents/skills/rtl_buddy/" in text


def test_comment_not_duplicated(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "# rtl_buddy skill (materialized by `rtl-buddy skill install --project`)\n"
        ".claude/skills/rtl_buddy/\n"
    )
    _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    text = gitignore.read_text()
    assert text.count("# rtl_buddy skill") == 1


def test_patterns_present_comment_missing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".claude/skills/rtl_buddy/\n.agents/skills/rtl_buddy/\n")
    result = _update_gitignore(gitignore, _SNIPPET, dry_run=False)
    assert result == "already present"
    assert "# rtl_buddy skill" not in gitignore.read_text()
