#!/usr/bin/env python3
"""Resolve supported Verilator and rtl_buddy versions for the build workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def select_verilator(tags: list[str]) -> list[str]:
    """Return the last 3 even-numbered Verilator release tags, sorted."""
    seen: dict[tuple[int, int], str] = {}
    for tag in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)", tag)
        if m and int(m[2]) % 2 == 0:
            major, patch = int(m[1]), int(m[2])
            seen[(major, patch)] = f"v{major}.{patch:03d}"
    return [v for _, v in sorted(seen.items())][-3:]


def select_rtl_buddy(tags: list[str]) -> list[str]:
    """Return the last 5 semver tags for each of the latest 2 major versions."""
    per_major: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    seen: set[tuple[int, int, int]] = set()
    for tag in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
        if not m:
            continue
        key = int(m[1]), int(m[2]), int(m[3])
        if key not in seen:
            seen.add(key)
            per_major[key[0]].append(key)
    result = []
    for major in sorted(per_major)[-2:]:
        for t in sorted(per_major[major])[-5:]:
            result.append(f"v{t[0]}.{t[1]}.{t[2]}")
    return result


def normalize_verilator(tags: list[str]) -> list[str]:
    """Normalize and deduplicate manually specified Verilator tags."""
    seen: dict[tuple[int, int], str] = {}
    for tag in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)", tag)
        if not m:
            raise ValueError(f"Unsupported Verilator tag format: {tag}")
        major, patch = int(m[1]), int(m[2])
        seen[(major, patch)] = f"v{major}.{patch:03d}"
    return [v for _, v in sorted(seen.items())]


def normalize_rtl_buddy(tags: list[str]) -> list[str]:
    """Normalize and deduplicate manually specified rtl_buddy tags."""
    seen: dict[tuple[int, int, int], str] = {}
    for tag in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
        if not m:
            raise ValueError(f"Unsupported rtl_buddy tag format: {tag}")
        key = int(m[1]), int(m[2]), int(m[3])
        seen[key] = f"v{key[0]}.{key[1]}.{key[2]}"
    return [v for _, v in sorted(seen.items())]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verilator-tags-file")
    parser.add_argument("--rtl-buddy-tags-file")
    parser.add_argument("--manual-verilator")
    parser.add_argument("--manual-rtl-buddy")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    def read_tags(path: str | None) -> list[str]:
        if not path:
            return []
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]

    def split(raw: str | None) -> list[str]:
        return [t for t in re.split(r"[\s,]+", raw.strip()) if t] if raw else []

    try:
        verilator = normalize_verilator(split(args.manual_verilator)) if args.manual_verilator else select_verilator(read_tags(args.verilator_tags_file))
        rtl_buddy = normalize_rtl_buddy(split(args.manual_rtl_buddy)) if args.manual_rtl_buddy else select_rtl_buddy(read_tags(args.rtl_buddy_tags_file))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not verilator:
        print("No supported Verilator versions found.", file=sys.stderr)
        return 1
    if not rtl_buddy:
        print("No supported rtl_buddy versions found.", file=sys.stderr)
        return 1

    outputs = {
        "verilator_versions": json.dumps(verilator),
        "rtl_buddy_versions": json.dumps(rtl_buddy),
        "latest_verilator": verilator[-1],
        "latest_rtl_buddy": rtl_buddy[-1],
    }
    print(json.dumps(outputs, indent=2))

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            for key, value in outputs.items():
                f.write(f"{key}={value}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
