#!/usr/bin/env python3
"""Export the bundled docs contract as static, versioned agent resources."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from rtl_buddy.docs_access import (
    DocsPage,
    _extract_frontmatter,
    _extract_section_content,
    _extract_sections,
    _extract_title,
)


REPO_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT = REPO_ROOT / ".docusaurus-agent-static"
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<prefix>!?\[[^\]]*\]\()(?P<destination>[^)\s]+)(?P<suffix>\))"
)


def _base_url(value: str) -> str:
    return "/" + "/".join(part for part in value.split("/") if part) + "/"


def _url(site_url: str, base_url: str, path: str) -> str:
    return f"{site_url.rstrip('/')}{base_url}{path.lstrip('/')}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _human_path(slug: str) -> str:
    if slug == "index":
        return ""
    if slug.endswith("/index"):
        return f"{slug.removesuffix('/index')}/"
    return f"{slug}/"


def _rebase_section_links(
    content: str,
    *,
    page_slug: str,
    site_url: str,
    base_url: str,
) -> str:
    """Preserve link targets after moving a section away from its source page."""

    def replace(match: re.Match[str]) -> str:
        destination = match.group("destination")
        parsed = urlsplit(destination)
        if parsed.scheme or parsed.netloc or destination.startswith("/"):
            return match.group(0)

        if not parsed.path:
            target_path = _human_path(page_slug)
        elif parsed.path.endswith(".md"):
            parent = posixpath.dirname(page_slug)
            target_slug = posixpath.normpath(posixpath.join(parent, parsed.path))
            target_path = _human_path(target_slug.removesuffix(".md"))
        else:
            return match.group(0)

        rebased = _url(site_url, base_url, target_path)
        if parsed.query:
            rebased += f"?{parsed.query}"
        if parsed.fragment:
            rebased += f"#{parsed.fragment}"
        return f"{match.group('prefix')}{rebased}{match.group('suffix')}"

    lines = []
    in_code_block = False
    for line in content.splitlines(keepends=True):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            lines.append(line)
        elif in_code_block:
            lines.append(line)
        else:
            lines.append(_MARKDOWN_LINK_RE.sub(replace, line))
    return "".join(lines)


def _list_pages(docs_dir: Path) -> list[DocsPage]:
    pages = []
    for path in sorted(docs_dir.rglob("*.md")):
        content = path.read_text()
        slug = path.relative_to(docs_dir).with_suffix("").as_posix()
        lines = content.splitlines()
        pages.append(
            DocsPage(
                slug=slug,
                title=_extract_title(lines, slug),
                summary=_extract_frontmatter(content).get("description", ""),
                sections=_extract_sections(lines),
                content=content,
            )
        )
    return pages


def export(
    output: Path,
    *,
    docs_dir: Path,
    version: str,
    site_url: str,
    base_url: str,
) -> dict:
    """Write static agent resources and return the catalog payload."""
    output = output.resolve()
    if output == Path(output.anchor):
        raise ValueError("refusing to replace a filesystem root")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    pages_payload = []
    full_parts = [
        f"# RTL Buddy documentation ({version})",
        "",
        "Canonical Markdown for this published RTL Buddy documentation version.",
    ]

    for page in _list_pages(docs_dir):
        page_path = f"agent/pages/{page.slug}.md"
        human_path = _human_path(page.slug)
        sections_payload = []

        _write(output / page_path, page.content)
        full_parts.extend(
            ["", "---", "", f"<!-- source: {page.slug} -->", "", page.content]
        )

        for section in page.sections:
            section_content = _extract_section_content(page.content, section.slug)
            if section_content is None:
                raise RuntimeError(f"section disappeared: {page.slug}#{section.slug}")
            section_path = f"agent/sections/{page.slug}/{section.slug}.md"
            _write(
                output / section_path,
                _rebase_section_links(
                    section_content,
                    page_slug=page.slug,
                    site_url=site_url,
                    base_url=base_url,
                )
                + "\n",
            )
            sections_payload.append(
                {
                    "slug": section.slug,
                    "title": section.title,
                    "url": _url(site_url, base_url, section_path),
                    "human_url": _url(
                        site_url, base_url, f"{human_path}#{section.slug}"
                    ),
                }
            )

        pages_payload.append(
            {
                "slug": page.slug,
                "title": page.title,
                "description": page.summary,
                "url": _url(site_url, base_url, page_path),
                "human_url": _url(site_url, base_url, human_path),
                "sections": sections_payload,
            }
        )

    catalog = {
        "schema_version": 1,
        "rtl_buddy_version": version,
        "pages": pages_payload,
    }
    _write(
        output / "agent/catalog.json",
        json.dumps(catalog, indent=2, sort_keys=True) + "\n",
    )

    llms_lines = [
        f"# RTL Buddy {version}",
        "",
        "> Version-pinned documentation for the RTL Buddy CLI and its agent interfaces.",
        "",
        "Use the catalog for page and section-level retrieval:",
        "",
        f"- [Agent catalog]({_url(site_url, base_url, 'agent/catalog.json')})",
        "",
        "## Documentation pages",
        "",
    ]
    for page in pages_payload:
        llms_lines.append(f"- [{page['title']}]({page['url']}): {page['description']}")
    _write(output / "llms.txt", "\n".join(llms_lines) + "\n")
    _write(output / "llms-full.txt", "\n".join(full_parts).rstrip() + "\n")
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=Path(os.environ.get("DOCS_PATH", REPO_ROOT / "docs")),
    )
    parser.add_argument("--version", default=os.environ.get("DOCS_VERSION", "dev"))
    parser.add_argument(
        "--site-url",
        default=os.environ.get("DOCS_SITE_URL", "https://rtl-buddy.github.io"),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DOCS_BASE_URL", "/rtl_buddy/dev/"),
    )
    args = parser.parse_args()

    catalog = export(
        args.output,
        docs_dir=args.docs_dir.resolve(),
        version=args.version,
        site_url=args.site_url,
        base_url=_base_url(args.base_url),
    )
    print(f"Exported {len(catalog['pages'])} pages to {args.output}")


if __name__ == "__main__":
    main()
