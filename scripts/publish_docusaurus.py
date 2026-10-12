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


_ALIAS_REDIRECT = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Redirecting</title>
  <link rel="canonical" href="{url}">
  <meta http-equiv="refresh" content="0; url={url}">
  <script>window.location.replace({url_json} + location.search + location.hash)</script>
</head>
<body>Redirecting to <a href="{url}">{url}</a>...</body>
</html>
"""


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _alias_url(relative: Path, version: str) -> str:
    """Relative URL from an alias copy of ``relative`` to the same page under ``version``.

    A Docusaurus build is served with ``trailingSlash: true``, so ``a/b/index.html``
    is addressed as ``a/b/``; any other file keeps its name.
    """
    up = "../" * len(relative.parts)
    if relative.name == "index.html":
        page = relative.parent.as_posix()
        page = "" if page == "." else page + "/"
    else:
        page = relative.as_posix()
    return f"{up}{version}/{page}"


def _write_alias_tree(source: Path, target: Path, version: str) -> None:
    """Mirror ``source`` under ``target`` as redirects into ``pages_dir/<version>``.

    A Docusaurus build is compiled for one ``baseUrl``; served from another
    prefix its client-side router renders "Page Not Found" over every page.
    So the alias tree holds a redirect stub per HTML page (path, query and
    fragment preserved) and a plain copy of every other file, which keeps
    ``latest/llms.txt`` and ``latest/agent/catalog.json`` fetchable by agents.
    """
    if target.exists():
        shutil.rmtree(target)
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        if relative.parts[0] in {"assets", "img"}:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".html":
            url = _alias_url(relative, version)
            destination.write_text(
                _ALIAS_REDIRECT.format(url=url, url_json=json.dumps(url))
            )
        else:
            shutil.copy2(path, destination)


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
    """Copy a build into ``pages_dir/<version>``, leaving other versions in place.

    With ``update_latest``, also point the ``latest`` alias tree and the root
    redirect at it. The alias is a tree of redirects, not a second copy of the
    build: a build is compiled for one ``baseUrl`` and does not route from
    another prefix.
    """
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
        _write_alias_tree(build_dir, pages_dir / "latest", version)
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
