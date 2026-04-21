#!/usr/bin/env python3
"""Generate docs/reference/cli.md from rtl-buddy --help output."""

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT = REPO_ROOT / "docs" / "reference" / "cli.md"
SUBCOMMANDS = ["test", "randtest", "regression", "filelist", "verible", "skill", "docs"]

HEADER = """\
# CLI Reference

This page is auto-generated from `rtl-buddy --help` output.
Run `python scripts/gen_cli_reference.py` from the repo root to regenerate it.

<!-- AUTO-GENERATED: do not edit below this line manually -->"""


def run_help(*args):
    cmd = ["rtl-buddy", *args, "--help"]
    env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
    env["COLUMNS"] = "88"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        raise RuntimeError("rtl-buddy not found in PATH")
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr}")
    plain = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', result.stdout)
    return plain.strip()


def generate():
    parts = [HEADER, f"## rtl-buddy\n\n```text\n{run_help()}\n```"]
    for sub in SUBCOMMANDS:
        parts.append(f"## {sub}\n\n```text\n{run_help(sub)}\n```")
    return "\n\n".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if committed file differs from generated output")
    args = parser.parse_args()

    try:
        content = generate()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.check:
        committed = OUTPUT.read_text()
        if content == committed:
            sys.exit(0)
        diff = difflib.unified_diff(
            committed.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile="docs/reference/cli.md (committed)",
            tofile="docs/reference/cli.md (generated)",
        )
        sys.stdout.writelines(diff)
        sys.exit(1)

    OUTPUT.write_text(content)
    print(f"Written: {OUTPUT}")


def on_pre_build(config):
    """MkDocs hook: regenerate cli.md before each build."""
    import logging
    log = logging.getLogger("mkdocs")
    try:
        content = generate()
        existing = OUTPUT.read_text() if OUTPUT.exists() else ""
        if content != existing:
            OUTPUT.write_text(content)
            log.info("gen_cli_reference: updated docs/reference/cli.md")
    except RuntimeError as e:
        log.warning(f"gen_cli_reference: skipped ({e}), using committed cli.md")


if __name__ == "__main__":
    main()
