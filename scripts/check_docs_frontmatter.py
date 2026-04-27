#!/usr/bin/env python3
"""Check that all non-generated docs pages have a non-empty description: frontmatter field."""

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
DOCS_ROOT = REPO_ROOT / "docs"

# No pages are exempt — reference/cli.md has its description managed by gen_cli_reference.py.
GENERATED_PAGES: set = set()

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def get_description(content: str) -> str:
  match = _FRONTMATTER_RE.match(content)
  if not match:
    return ""
  for line in match.group(1).splitlines():
    if line.startswith("description:") or line.startswith("description "):
      _, _, value = line.partition(":")
      return value.strip().strip("\"'")
  return ""


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--check", action="store_true",
                      help="Exit non-zero if any page is missing a description")
  args = parser.parse_args()

  offenders: list[str] = []
  for path in sorted(DOCS_ROOT.rglob("*.md")):
    if path in GENERATED_PAGES:
      continue
    if not get_description(path.read_text()):
      offenders.append(str(path.relative_to(REPO_ROOT)))

  if not offenders:
    if args.check:
      print("All docs pages have a description: frontmatter field.")
    sys.exit(0)

  print("The following docs pages are missing a non-empty description: frontmatter field:", file=sys.stderr)
  for page in offenders:
    print(f"  {page}", file=sys.stderr)
  print(
    "\nAdd a description: field to the YAML frontmatter at the top of each file.\n"
    "See docs/CONTRIBUTING.md for the required format.",
    file=sys.stderr,
  )
  sys.exit(1)


if __name__ == "__main__":
  main()
