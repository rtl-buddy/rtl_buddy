#!/usr/bin/env python3
"""Resolve supported Verilator and rtl_buddy versions for the build workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable


VERILATOR_RE = re.compile(r"^v?(\d+)\.(\d+)$")
RTL_BUDDY_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def read_tags(path: str | None) -> list[str]:
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


def parse_manual_versions(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [token for token in re.split(r"[\s,]+", raw.strip()) if token]


def normalize_verilator(tag: str) -> tuple[int, int, str]:
    match = VERILATOR_RE.fullmatch(tag)
    if not match:
        raise ValueError(f"Unsupported Verilator tag format: {tag}")
    major, patch = (int(part) for part in match.groups())
    return major, patch, f"v{major}.{patch:03d}"


def normalize_rtl_buddy(tag: str) -> tuple[int, int, int, str]:
    match = RTL_BUDDY_RE.fullmatch(tag)
    if not match:
        raise ValueError(f"Unsupported rtl_buddy tag format: {tag}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch, f"v{major}.{minor}.{patch}"


def select_verilator_versions(tags: Iterable[str]) -> list[str]:
    seen: dict[tuple[int, int], str] = {}
    for tag in tags:
        match = VERILATOR_RE.fullmatch(tag)
        if not match:
            continue
        major, patch = (int(part) for part in match.groups())
        if patch % 2 != 0:
            continue
        seen[(major, patch)] = f"v{major}.{patch:03d}"

    selected = [value for _, value in sorted(seen.items())]
    return selected[-3:]


def select_rtl_buddy_versions(tags: Iterable[str]) -> list[str]:
    per_major: dict[int, list[tuple[int, int, int, str]]] = defaultdict(list)
    seen: set[tuple[int, int, int]] = set()

    for tag in tags:
        match = RTL_BUDDY_RE.fullmatch(tag)
        if not match:
            continue
        major, minor, patch = (int(part) for part in match.groups())
        key = (major, minor, patch)
        if key in seen:
            continue
        seen.add(key)
        per_major[major].append((major, minor, patch, f"v{major}.{minor}.{patch}"))

    selected: list[str] = []
    for major in sorted(per_major)[-2:]:
        versions = sorted(per_major[major])
        selected.extend(version for _, _, _, version in versions[-5:])
    return selected


def normalize_manual_verilator(tags: Iterable[str]) -> list[str]:
    versions = sorted({normalize_verilator(tag) for tag in tags})
    return [version for _, _, version in versions]


def normalize_manual_rtl_buddy(tags: Iterable[str]) -> list[str]:
    versions = sorted({normalize_rtl_buddy(tag) for tag in tags})
    return [version for _, _, _, version in versions]


def write_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verilator-tags-file")
    parser.add_argument("--rtl-buddy-tags-file")
    parser.add_argument("--manual-verilator")
    parser.add_argument("--manual-rtl-buddy")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    manual_verilator = parse_manual_versions(args.manual_verilator)
    manual_rtl_buddy = parse_manual_versions(args.manual_rtl_buddy)

    try:
        verilator_versions = (
            normalize_manual_verilator(manual_verilator)
            if manual_verilator
            else select_verilator_versions(read_tags(args.verilator_tags_file))
        )
        rtl_buddy_versions = (
            normalize_manual_rtl_buddy(manual_rtl_buddy)
            if manual_rtl_buddy
            else select_rtl_buddy_versions(read_tags(args.rtl_buddy_tags_file))
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not verilator_versions:
        print("No supported Verilator versions found.", file=sys.stderr)
        return 1
    if not rtl_buddy_versions:
        print("No supported rtl_buddy versions found.", file=sys.stderr)
        return 1

    outputs = {
        "verilator_versions": json.dumps(verilator_versions),
        "rtl_buddy_versions": json.dumps(rtl_buddy_versions),
        "latest_verilator": verilator_versions[-1],
        "latest_rtl_buddy": rtl_buddy_versions[-1],
    }

    write_output(args.github_output, outputs)
    print(json.dumps(outputs, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
