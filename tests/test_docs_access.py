import json
import subprocess
import sys

from rtl_buddy.docs_access import (
  _extract_frontmatter,
  _extract_section_content,
  get_page,
  get_section,
  list_pages,
)


def test_frontmatter_summary_extracted():
  content = "---\ndescription: A clear summary.\n---\n\n# Title\n\nBody text.\n"
  assert _extract_frontmatter(content)["description"] == "A clear summary."


def test_frontmatter_missing_gives_empty_dict():
  content = "# Title\n\nBody text.\n"
  assert _extract_frontmatter(content) == {}


def test_extract_section_content_basic():
  content = (
    "# Guide\n\n"
    "Intro.\n\n"
    "## Setup\n\n"
    "Setup text.\n\n"
    "## Usage\n\n"
    "Usage text.\n"
  )
  result = _extract_section_content(content, "setup")
  assert result is not None
  assert result.startswith("## Setup")
  assert "Setup text." in result
  assert "## Usage" not in result


def test_extract_section_content_code_fence_safe():
  content = (
    "# Guide\n\n"
    "## Real Section\n\n"
    "```\n"
    "## fake heading inside fence\n"
    "```\n\n"
    "More text.\n\n"
    "## Next Section\n\n"
    "Next text.\n"
  )
  result = _extract_section_content(content, "real-section")
  assert result is not None
  assert "fake heading inside fence" in result
  assert "Next Section" not in result


def test_extract_section_content_unknown_anchor():
  content = "# Guide\n\n## Setup\n\nText.\n"
  assert _extract_section_content(content, "does-not-exist") is None


def test_list_pages_contains_expected_metadata():
  pages = list_pages()

  assert any(page.slug == "agents" and page.summary for page in pages)
  assert any(page.slug == "reference/yaml" and page.title == "YAML Formats" for page in pages)


def test_get_page_returns_expected_sections():
  page = get_page("agents")

  assert page is not None
  assert page.title == "For Agents"
  assert any(section.title == "Agent Skill Install" for section in page.sections)


def test_get_section_returns_section_payload():
  result = get_section("agents", "local-docs-access")

  assert result is not None
  assert result["slug"] == "agents"
  assert result["section"] == "local-docs-access"
  assert result["section_title"] == "Local docs access"
  assert result["content"].startswith("## Local docs access")


def test_get_section_unknown_anchor_returns_none():
  assert get_section("agents", "does-not-exist") is None


def test_get_section_unknown_page_returns_none():
  assert get_section("does-not-exist", "some-anchor") is None


def test_docs_list_machine_output():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "--machine", "docs", "list"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 0
  payload = json.loads(result.stdout)
  assert any(page["slug"] == "agents" for page in payload["pages"])


def test_docs_show_machine_output():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "--machine", "docs", "show", "agents"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 0
  payload = json.loads(result.stdout)
  assert payload["slug"] == "agents"
  assert payload["title"] == "For Agents"
  assert payload["summary"]
  assert any(section["title"] == "Agent Skill Install" for section in payload["sections"])
  assert "# For Agents" in payload["content"]


def test_docs_show_section_machine_output():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "--machine", "docs", "show", "agents#local-docs-access"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 0
  payload = json.loads(result.stdout)
  assert payload["slug"] == "agents"
  assert payload["section"] == "local-docs-access"
  assert payload["section_title"] == "Local docs access"
  assert payload["content"].startswith("## Local docs access")


def test_docs_show_unknown_slug_is_clean_error():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "docs", "show", "does-not-exist"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 1
  assert "Unknown docs page" in result.stderr
  assert "Traceback" not in result.stderr


def test_docs_show_unknown_anchor_is_clean_error():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "docs", "show", "agents#does-not-exist"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 1
  assert "Unknown section" in result.stderr
  assert "Traceback" not in result.stderr
