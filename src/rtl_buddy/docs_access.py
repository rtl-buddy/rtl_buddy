from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path


_DOCS_DATA = "docs_data.json"
_SUMMARY_RE = re.compile(r"(.+?[.!?])(?:\s|$)")
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


def _first_sentence(text: str) -> str:
  collapsed = " ".join(text.split())
  if not collapsed:
    return ""
  match = _SUMMARY_RE.match(collapsed)
  return match.group(1).strip() if match else collapsed


def _first_paragraph(lines: list[str]) -> str:
  paragraph: list[str] = []
  in_code_block = False

  for raw_line in lines:
    line = raw_line.rstrip()
    stripped = line.strip()

    if stripped.startswith("```"):
      in_code_block = not in_code_block
      continue
    if in_code_block:
      continue
    if not stripped:
      if paragraph:
        break
      continue
    if stripped.startswith("#"):
      continue
    if stripped.startswith(("|", "-", "*", "1.", "2.", "3.", "4.", "5.")):
      continue
    paragraph.append(stripped)

  return " ".join(paragraph).strip()


def _extract_title(lines: list[str], fallback_slug: str) -> str:
  for line in lines:
    match = _HEADING_RE.match(line.strip())
    if match and match.group(1) == "#":
      return _clean_heading_text(match.group(2))
  leaf = fallback_slug.rsplit("/", 1)[-1]
  return leaf.replace("-", " ").title()


def _extract_sections(lines: list[str]) -> list[DocsSection]:
  sections: list[DocsSection] = []
  for line in lines:
    match = _HEADING_RE.match(line.strip())
    if not match or match.group(1) != "##":
      continue
    title = _clean_heading_text(match.group(2))
    sections.append(DocsSection(title=title, slug=_slugify_heading(title)))
  return sections


def _slug_for_path(path: Path, docs_root: Path) -> str:
  relpath = path.relative_to(docs_root)
  return str(relpath.with_suffix("")).replace("\\", "/")


def build_catalog_from_docs_root(docs_root: Path) -> list[dict]:
  pages: list[dict] = []
  for path in sorted(docs_root.rglob("*.md")):
    if path.name.startswith("."):
      continue
    content = path.read_text()
    lines = content.splitlines()
    slug = _slug_for_path(path, docs_root)
    title = _extract_title(lines, slug)
    summary = _first_sentence(_first_paragraph(lines))
    sections = [asdict(section) for section in _extract_sections(lines)]
    pages.append({
      "slug": slug,
      "title": title,
      "summary": summary,
      "sections": sections,
      "content": content,
    })
  return pages


def _load_catalog_payload() -> dict:
  text = files("rtl_buddy").joinpath(_DOCS_DATA).read_text()
  payload = json.loads(text)
  pages = payload.get("pages")
  if not isinstance(pages, list):
    raise RuntimeError("docs_data.json is missing the pages array")
  return payload


@lru_cache(maxsize=1)
def _catalog() -> dict[str, DocsPage]:
  pages: dict[str, DocsPage] = {}
  for item in _load_catalog_payload()["pages"]:
    page = DocsPage(
      slug=item["slug"],
      title=item["title"],
      summary=item["summary"],
      sections=[DocsSection(**section) for section in item.get("sections", [])],
      content=item["content"],
    )
    pages[page.slug] = page
  return pages


def list_pages() -> list[DocsPage]:
  return sorted(_catalog().values(), key=lambda page: page.slug)


def get_page(slug: str) -> DocsPage | None:
  return _catalog().get(slug)
