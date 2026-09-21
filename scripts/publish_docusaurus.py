#!/usr/bin/env python3
"""Copy one Docusaurus build into the versioned GitHub Pages tree."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


_VERSION_RE = re.compile(r"(?:dev|v[0-9]+)")
_REDIRECT = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Redirecting</title>
  <meta http-equiv="refresh" content="0; url=latest/">
  <script>window.location.replace("latest/" + location.search + location.hash)</script>
</head>
<body>Redirecting to <a href="latest/">latest documentation</a>...</body>
</html>
"""


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _version_key(item: dict) -> tuple[int, int]:
    version = item["version"]
    if version == "dev":
        return (0, 0)
    return (1, -int(version[1:]))


def publish(
    build_dir: Path,
    pages_dir: Path,
    *,
    version: str,
    update_latest: bool,
) -> None:
    """Publish a build without disturbing other version directories."""
    build_dir = build_dir.resolve()
    pages_dir = pages_dir.resolve()
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid documentation version: {version}")
    if not (build_dir / "index.html").is_file():
        raise ValueError(f"Docusaurus build has no index.html: {build_dir}")
    if not (pages_dir / ".git").exists():
        raise ValueError(f"pages directory is not a git worktree: {pages_dir}")

    _replace_tree(build_dir, pages_dir / version)
    if update_latest:
        _replace_tree(build_dir, pages_dir / "latest")
        (pages_dir / "index.html").write_text(_REDIRECT)
    (pages_dir / ".nojekyll").touch()

    manifest_path = pages_dir / "versions.json"
    versions = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    versions = [item for item in versions if item.get("version") != version]
    if update_latest:
        for item in versions:
            item["aliases"] = [
                alias for alias in item.get("aliases", []) if alias != "latest"
            ]
    versions.append(
        {
            "version": version,
            "title": version,
            "aliases": ["latest"] if update_latest else [],
        }
    )
    versions.sort(key=_version_key)
    manifest_path.write_text(json.dumps(versions, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--pages-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--update-latest", action="store_true")
    args = parser.parse_args()
    publish(
        args.build_dir,
        args.pages_dir,
        version=args.version,
        update_latest=args.update_latest,
    )


if __name__ == "__main__":
    main()
