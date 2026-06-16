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

# Top-level commands, in the same order `rtl-buddy --help` lists them. Each
# command is rendered, then the generator auto-discovers and recurses into any
# subcommands it exposes (so command groups like `axi-profile`, `mut`, `hub`,
# `verible`, `skill`, `spec`, and `docs` get a section per subcommand without
# this list having to enumerate them).
SUBCOMMANDS = [
    "test",
    "randtest",
    "regression",
    "filelist",
    "hier",
    "hier-query",
    "wave",
    "wave-fpv",
    "nvim-install",
    "synth",
    "synth-regression",
    "pnr",
    "power",
    "power-regression",
    "saif",
    "cdc",
    "cdc-regression",
    "fpv",
    "fpv-regression",
    "tool-check",
    "axi-profile",
    "verible",
    "mut",
    "hub",
    "skill",
    "docs",
    "spec",
]

HEADER = """\
---
description: Auto-generated CLI reference for all rtl-buddy commands and their options.
---

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
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    return plain.strip()


def extract_subcommands(help_text):
    """Parse subcommand names out of a Typer/Rich ``Commands`` help panel.

    Returns the command names in the order they appear, or an empty list if the
    command is a leaf (no ``Commands`` panel). Command rows start one space
    after the box border; wrapped description lines are indented further and are
    skipped, so multi-line descriptions do not produce phantom commands.
    """
    cmds = []
    in_panel = False
    for line in help_text.splitlines():
        if not in_panel:
            if "Commands" in line and ("╭" in line or "┌" in line):
                in_panel = True
            continue
        if "╰" in line or "└" in line:
            break
        m = re.match(r"^[│|] (\S+)\s", line)
        if m:
            cmds.append(m.group(1))
    return cmds


def emit_command(path, parts):
    """Render ``path`` (a list of command words) and recurse into subcommands.

    A subcommand whose ``--help`` fails (e.g. it needs a connection) is skipped
    rather than aborting the whole reference.
    """
    try:
        help_text = run_help(*path)
    except RuntimeError as e:
        if len(path) == 1:
            raise
        print(f"Warning: skipping `{' '.join(path)}` ({e})", file=sys.stderr)
        return
    parts.append(f"## {' '.join(path)}\n\n```text\n{help_text}\n```")
    for child in extract_subcommands(help_text):
        emit_command(path + [child], parts)


def generate():
    parts = [HEADER, f"## rtl-buddy\n\n```text\n{run_help()}\n```"]
    for sub in SUBCOMMANDS:
        emit_command([sub], parts)
    return "\n\n".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if committed file differs from generated output",
    )
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
