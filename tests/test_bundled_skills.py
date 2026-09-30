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
    assert "unknown `NA`" in primary
    assert "including an intentional early-stop `NA`" in primary

    graph = _bundled_skill_text("rtl-buddy-graph")
    # The hub-gated `phys_focus` tool must be described as hub-gated.
    assert "`phys_focus` is served only when a live hub" in graph
    assert "soft miss" in graph
    assert "instance_join" in graph
    # POWER attribution answers Liberty-cell questions only; a flat netlist's top is not a second answer.
    assert "flat netlist" not in graph
    assert "changes the hierarchy rather than the namespace" in graph
    # "Nothing else" must not imply that an RTL name gets no synthesis row.
    assert "and nothing else" not in graph
    assert "still gets its synthesis row" in graph
    # The skill must say how to ask for the complete list.
    assert "`limit: 0` asks for the complete one" in graph

    tests = _bundled_skill_text("rtl-buddy-test")
    assert "result.json" in tests
    assert "multi-select when available" in tests
    assert "VCS/Icarus report no header dependencies" in tests
    assert "configured extra compile" in tests
