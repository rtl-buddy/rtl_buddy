from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.resources import files as _pkg_files


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class DocsSection:
  title: str
  slug: str


@dataclass(frozen=True)
class DocsPage:
  slug: str
  title: str
  summary: str
  sections: list[DocsSection]
  content: str

  def to_list_item(self) -> dict:
    return {
      "slug": self.slug,
      "title": self.title,
      "summary": self.summary,
    }

  def to_show_payload(self) -> dict:
    return {
      "slug": self.slug,
      "title": self.title,
      "summary": self.summary,
      "sections": [asdict(section) for section in self.sections],
      "content": self.content,
    }


def _clean_heading_text(text: str) -> str:
  return re.sub(r"\s+#*$", "", text.strip())


def _slugify_heading(text: str) -> str:
  slug = text.strip().lower()
  slug = re.sub(r"`([^`]*)`", r"\1", slug)
  slug = re.sub(r"[^a-z0-9 -]", "", slug)
  slug = re.sub(r"\s+", "-", slug)
  slug = re.sub(r"-{2,}", "-", slug).strip("-")
  return slug


def _extract_frontmatter(content: str) -> dict[str, str]:
  if not content.startswith("---\n"):
    return {}
  end = content.find("\n---\n", 4)
  if end == -1:
    return {}
  result = {}
  for line in content[4:end].splitlines():
    if ":" in line and not line.startswith(" "):
      key, _, value = line.partition(":")
      result[key.strip()] = value.strip().strip("\"'")
  return result


def _extract_title(lines: list[str], fallback_slug: str) -> str:
  for line in lines:
    match = _HEADING_RE.match(line.strip())
    if match and match.group(1) == "#":
      return _clean_heading_text(match.group(2))
  leaf = fallback_slug.rsplit("/", 1)[-1]
  return leaf.replace("-", " ").title()


def _extract_sections(lines: list[str]) -> list[DocsSection]:
  sections: list[DocsSection] = []
  in_code_block = False
  for line in lines:
    stripped = line.strip()
    if stripped.startswith("```"):
      in_code_block = not in_code_block
      continue
    if in_code_block:
      continue
    match = _HEADING_RE.match(stripped)
    if not match or match.group(1) != "##":
      continue
    title = _clean_heading_text(match.group(2))
    sections.append(DocsSection(title=title, slug=_slugify_heading(title)))
  return sections


def _extract_section_content(content: str, anchor: str) -> str | None:
  lines = content.splitlines(keepends=True)
  in_code_block = False
  collecting = False
  section_lines: list[str] = []

  for line in lines:
    stripped = line.strip()
    if stripped.startswith("```"):
      in_code_block = not in_code_block

    if not in_code_block:
      match = _HEADING_RE.match(stripped)
      if match and match.group(1) == "##":
        title = _clean_heading_text(match.group(2))
        if collecting:
          break
        if _slugify_heading(title) == anchor:
          collecting = True

    if collecting:
      section_lines.append(line)

  if not section_lines:
    return None
  return "".join(section_lines).rstrip()


def _walk(node, prefix: str, results: list[tuple[str, str]]) -> None:
  for child in node.iterdir():
    name = child.name
    if name.startswith("."):
      continue
    child_prefix = f"{prefix}/{name}" if prefix else name
    if child.is_dir():
      _walk(child, child_prefix, results)
    elif name.endswith(".md"):
      results.append((child_prefix[:-3], child.read_text()))


def _iter_docs() -> list[tuple[str, str]]:
  results: list[tuple[str, str]] = []
  _walk(_pkg_files("rtl_buddy").joinpath("docs"), "", results)
  return sorted(results)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, DocsPage]:
  pages: dict[str, DocsPage] = {}
  for slug, content in _iter_docs():
    lines = content.splitlines()
    fm = _extract_frontmatter(content)
    page = DocsPage(
      slug=slug,
      title=_extract_title(lines, slug),
      summary=fm.get("description", ""),
      sections=_extract_sections(lines),
      content=content,
    )
    pages[slug] = page
  return pages


def list_pages() -> list[DocsPage]:
  return sorted(_catalog().values(), key=lambda page: page.slug)


def get_page(slug: str) -> DocsPage | None:
  return _catalog().get(slug)


def get_section(slug: str, anchor: str) -> dict | None:
  page = get_page(slug)
  if page is None:
    return None
  section_content = _extract_section_content(page.content, anchor)
  if section_content is None:
    return None
  section_title = next(
    (s.title for s in page.sections if s.slug == anchor),
    anchor,
  )
  return {
    "slug": slug,
    "section": anchor,
    "title": page.title,
    "section_title": section_title,
    "content": section_content,
  }
