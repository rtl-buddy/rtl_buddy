#!/usr/bin/env python3
"""Generate src/rtl_buddy/docs_data.json from docs/*.md."""

import argparse
import difflib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
OUTPUT = REPO_ROOT / "src" / "rtl_buddy" / "docs_data.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

from rtl_buddy.docs_access import build_catalog_from_docs_root  # noqa: E402


def generate() -> str:
  payload = {"pages": build_catalog_from_docs_root(DOCS_ROOT)}
  return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--check", action="store_true", help="Exit non-zero if committed file differs from generated output")
  args = parser.parse_args()

  content = generate()
  if args.check:
    committed = OUTPUT.read_text()
    if committed == content:
      raise SystemExit(0)
    diff = difflib.unified_diff(
      committed.splitlines(keepends=True),
      content.splitlines(keepends=True),
      fromfile="src/rtl_buddy/docs_data.json (committed)",
      tofile="src/rtl_buddy/docs_data.json (generated)",
    )
    sys.stdout.writelines(diff)
    raise SystemExit(1)

  OUTPUT.write_text(content)
  print(f"Written: {OUTPUT}")


if __name__ == "__main__":
  main()
