"""Structural guardrails for the agent skills shipped in the wheel."""

from rtl_buddy.skill_install import SKILL_DIRNAMES, _bundled_skill_text


def _frontmatter_value(text: str, key: str) -> str:
    for line in text.splitlines()[1:]:
        if line == "---":
            break
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"SKILL.md frontmatter has no `{key}:` field")


def test_bundled_skills_are_lean_and_spec_named():
    descriptions = set()
    for skill_name in SKILL_DIRNAMES:
        text = _bundled_skill_text(skill_name)
        assert len(text.splitlines()) <= 60, skill_name
        assert len(text.encode()) < 8 * 1024, skill_name
        assert _frontmatter_value(text, "name") == skill_name
        description = _frontmatter_value(text, "description")
        assert description
        assert description not in descriptions
        descriptions.add(description)


def test_bundled_skills_keep_critical_operational_guidance():
    for skill_name in SKILL_DIRNAMES:
        assert "rb --version" in _bundled_skill_text(skill_name), skill_name

    primary = _bundled_skill_text("rtl-buddy")
    assert "payload.results" in primary
    assert "`rb docs show` is" in primary
    assert "bare JSON" in primary
    assert "strict `XPASS`" in primary
    assert "including `NA`/`XFAIL`" in primary

    tests = _bundled_skill_text("rtl-buddy-test")
    assert "result.json" in tests
    assert "multi-select when available" in tests
    assert "VCS/Icarus may not report header dependencies" in tests
    assert "configured extra compile" in tests
