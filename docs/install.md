---
description: How to install rtl_buddy into a project using uv, including prerequisites and verification steps.
---

# Installation

`rtl_buddy` is available on PyPI and installed into your project environment with `uv`.

## Prerequisites

- Python 3.11 or later
- `uv`
- Simulation tool on `PATH`: Verilator (macOS/Linux) or VCS (Linux)
- Optional Verible binaries if you want to use `uv run rb verible ...`
- Optional system-level coverage tools:
  - `lcov` for `.info` export and HTML reports
  - Antmicro `coverview` for Coverview package generation

`rtl_buddy` can be used with different project-specific tool setups, but the primary supported flows are Verilator and VCS. Basic Verible command integration exists; broader first-class Verible and PeakRDL workflows are on the roadmap.

## Install Into A Project With `uv`

Add `rtl_buddy` to your project environment:

```bash
uv add rtl_buddy
```

Then verify the install:

```bash
uv run rb --version
```

## Updating

To move a project to a newer `rtl_buddy` version:

```bash
uv add rtl_buddy@latest
uv sync
```

Commit the resulting lockfile change in your project repo.

## Installing A Pre-release

Pre-release versions follow PEP 440 (`2.3.0rc1`, `2.3.0rc2`, …). They are published to PyPI but excluded from the default resolver — an unqualified range like `>=2.2.0` will not pull one in.

To install a specific pre-release, pin it exactly:

```bash
uv add "rtl_buddy==2.3.0rc1"
```

Or in `pyproject.toml`:

```toml
dependencies = ["rtl_buddy==2.3.0rc1"]
```

This works without any `--pre` flag because the exact version is specified.

## Set Up The Agent Skill

`rtl_buddy` ships an agent skill for Claude Code and Codex. After installing `rtl_buddy`, run once per machine:

```bash
uv run rtl-buddy skill install
```

This writes `SKILL.md` to `~/.claude/skills/rtl_buddy/` and `~/.codex/skills/rtl_buddy/`. Agents pick it up automatically. Re-run after upgrading `rtl_buddy` to refresh the content.

To install at project scope instead (overrides the user-level copy for that project):

```bash
uv run rtl-buddy skill install --project
```

See [For Agents](agents.md) for scope semantics and `.gitignore` guidance.
