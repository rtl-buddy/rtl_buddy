import json
import subprocess
import sys
from pathlib import Path

from rtl_buddy.docs_access import build_catalog_from_docs_root, get_page, list_pages


def test_catalog_builder_extracts_summary_and_sections(tmp_path):
  docs_root = tmp_path / "docs"
  docs_root.mkdir()
  (docs_root / "guide.md").write_text(
    "# Guide\n\n"
    "Guide summary sentence. More detail after that.\n\n"
    "## Setup\n\n"
    "Setup text.\n\n"
    "### Deep Detail\n\n"
    "Ignored subsection.\n"
  )

  pages = build_catalog_from_docs_root(docs_root)

  assert pages == [{
    "slug": "guide",
    "title": "Guide",
    "summary": "Guide summary sentence.",
    "sections": [{"title": "Setup", "slug": "setup"}],
    "content": (docs_root / "guide.md").read_text(),
  }]


def test_list_pages_contains_expected_metadata():
  pages = list_pages()

  assert any(page.slug == "agents" and page.summary for page in pages)
  assert any(page.slug == "reference/yaml" and page.title == "YAML Formats" for page in pages)


def test_get_page_returns_expected_sections():
  page = get_page("agents")

  assert page is not None
  assert page.title == "For Agents"
  assert any(section.title == "Agent Skill Install" for section in page.sections)


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
  assert payload["content"].startswith("# For Agents")


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


def test_docs_data_matches_repo_docs():
  repo_root = Path(__file__).resolve().parents[1]
  pages = build_catalog_from_docs_root(repo_root / "docs")
  packaged = [
    page.to_show_payload()
    for page in list_pages()
  ]

  assert packaged == pages
