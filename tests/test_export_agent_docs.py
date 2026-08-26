import json
import subprocess
import sys
from pathlib import Path

from rtl_buddy.docs_access import get_page, list_pages


REPO_ROOT = Path(__file__).parent.parent


def test_export_matches_bundled_docs_contract(tmp_path):
    output = tmp_path / "agent-static"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/export_agent_docs.py"),
            "--output",
            str(output),
            "--version",
            "v6",
            "--base-url",
            "/rtl_buddy/v6/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    catalog = json.loads((output / "agent/catalog.json").read_text())
    assert catalog["schema_version"] == 1
    assert catalog["rtl_buddy_version"] == "v6"
    assert [page["slug"] for page in catalog["pages"]] == [
        page.slug for page in list_pages()
    ]

    agents = next(page for page in catalog["pages"] if page["slug"] == "agents")
    assert agents["url"].endswith("/rtl_buddy/v6/agent/pages/agents.md")
    assert any(
        section["slug"] == "local-docs-access"
        and section["url"].endswith("/agent/sections/agents/local-docs-access.md")
        for section in agents["sections"]
    )
    assert (output / "agent/pages/agents.md").read_text() == get_page("agents").content
    assert (output / "llms.txt").is_file()
    assert (output / "llms-full.txt").is_file()


def test_export_replaces_stale_output(tmp_path):
    output = tmp_path / "agent-static"
    output.mkdir()
    stale = output / "stale.txt"
    stale.write_text("old")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/export_agent_docs.py"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not stale.exists()


def test_export_uses_requested_historical_docs_tree(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    content = """---
description: Historical release documentation.
---

# Historical

## Old behavior

This behavior belongs to the old release.
"""
    (docs / "index.md").write_text(content)
    output = tmp_path / "agent-static"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/export_agent_docs.py"),
            "--docs-dir",
            str(docs),
            "--output",
            str(output),
            "--version",
            "v2",
            "--base-url",
            "/rtl_buddy/v2/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    catalog = json.loads((output / "agent/catalog.json").read_text())
    assert [page["slug"] for page in catalog["pages"]] == ["index"]
    assert catalog["pages"][0]["description"] == "Historical release documentation."
    assert (output / "agent/pages/index.md").read_text() == content
