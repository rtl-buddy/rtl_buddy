import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from rtl_buddy.docs_access import get_page, list_pages


REPO_ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "export_agent_docs", REPO_ROOT / "scripts/export_agent_docs.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _export(output, docs, *, version="v2"):
    return MODULE.export(
        output,
        docs_dir=docs,
        version=version,
        site_url="https://rtl-buddy.github.io",
        base_url=f"/rtl_buddy/{version}/",
    )


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
        timeout=30,
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

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Test documentation\n")
    _export(output, docs)
    assert not stale.exists()
    catalog = json.loads((output / "agent/catalog.json").read_text())
    assert [page["slug"] for page in catalog["pages"]] == ["index"]


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

    _export(output, docs)
    catalog = json.loads((output / "agent/catalog.json").read_text())
    assert [page["slug"] for page in catalog["pages"]] == ["index"]
    assert catalog["pages"][0]["description"] == "Historical release documentation."
    assert (output / "agent/pages/index.md").read_text() == content


def test_section_export_rebases_relative_links_and_preserves_code(tmp_path):
    docs = tmp_path / "docs"
    (docs / "concepts").mkdir(parents=True)
    content = """# Example

## Linked section

[Sibling](sibling.md#details)
[Parent](../install.md)
[Same page](#linked-section)
[External](https://example.com/docs)

```markdown
[Literal](sibling.md)
```
"""
    (docs / "concepts/example.md").write_text(content)
    (docs / "concepts/sibling.md").write_text("# Sibling\n")
    (docs / "install.md").write_text("# Install\n")
    output = tmp_path / "agent-static"

    _export(output, docs)
    section = (output / "agent/sections/concepts/example/linked-section.md").read_text()
    assert (
        "https://rtl-buddy.github.io/rtl_buddy/v2/concepts/sibling/#details" in section
    )
    assert "https://rtl-buddy.github.io/rtl_buddy/v2/install/" in section
    assert (
        "https://rtl-buddy.github.io/rtl_buddy/v2/concepts/example/#linked-section"
        in section
    )
    assert "[External](https://example.com/docs)" in section
    assert "[Literal](sibling.md)" in section
