---
description: How to set up a local rtl_buddy development environment — clone, dependencies, lint, tests, docs, and build verification.
---

# Development Environment Setup

This page covers the local environment for maintainers and contributors working **on** `rtl_buddy`. End-user install of the published wheel is documented in [Installation](../install.md).

## Prerequisites

- Python 3.11 or later (matches the floor in `pyproject.toml`).
- `uv` — see <https://docs.astral.sh/uv/> for install instructions.
- `git`.

`uv` owns the project environment. The repo uses `pyproject.toml` plus a committed `uv.lock`; do not maintain a hand-rolled `requirements.txt`.

External EDA tools (Verilator, Yosys, Verible, OpenROAD, etc.) are only required when running the matching `rb` subcommand. Day-to-day Python and docs work needs none of them. See [Installation](../install.md#external-tools-by-feature) for the full feature-to-dependency matrix.

## Clone And Sync

```bash
git clone https://github.com/rtl-buddy/rtl_buddy.git
cd rtl_buddy
uv sync --group dev
```

`uv sync --group dev` installs the package plus the composite `dev` dependency group (lint, test, docs). The resulting environment lives in `.venv/`; `uv run <cmd>` and `./venv/bin/python -m rtl_buddy …` both reach it.

Verify the install:

```bash
uv run rb --version
```

## Pre-Commit Hook

Install the `pre-commit` hook once so Ruff runs automatically on every commit:

```bash
uv tool install pre-commit
pre-commit install
```

To refresh the pinned hook version:

```bash
pre-commit autoupdate
```

CI enforces both `ruff check` and `ruff format --check` via `.github/workflows/lint.yml`, so it pays to catch issues at commit time.

## Lint And Format

```bash
uv run ruff check          # lint
uv run ruff format         # format in place
uv run ruff format --check # check only (what CI runs)
```

## Tests

The pytest suite under `tests/` is the primary correctness gate. CI runs it on every push and PR via `.github/workflows/test.yml`.

```bash
uv run pytest                                    # full suite
uv run pytest tests/test_cli_with_fixture.py     # one file
uv run pytest -k "list"                          # by keyword
uv run pytest --cov                              # with coverage summary
uv run pytest --cov --cov-report=term-missing    # show uncovered lines
uv run pytest --cov --cov-report=html            # write htmlcov/index.html
```

Coverage configuration lives in `[tool.coverage.*]` in `pyproject.toml` (source = `src/rtl_buddy`, excludes the bundled `skill/` and `docs/`). `pytest.ini` does not enable `--cov` by default so plain `pytest` stays fast; pass `--cov` explicitly when you want a coverage run.

## Docs

The docs site lives under `docs/` and is built with MkDocs Material. Two checks run in CI on every docs change:

```bash
uv run python scripts/check_docs_frontmatter.py --check
uv run --group docs mkdocs build --strict
```

To preview the site locally:

```bash
uv run --group docs mkdocs serve
```

Then open <http://127.0.0.1:8000>.

If you change CLI help strings in `src/rtl_buddy/rtl_buddy.py`, regenerate the CLI reference page:

```bash
uv run python scripts/gen_cli_reference.py
```

`docs/reference/cli.md` is auto-generated and should not be edited by hand. The docs build also regenerates it via a hook in `mkdocs.yml`; CI auto-commits drift.

## Building Wheels And Sdists

`rtl_buddy` uses `hatchling` plus `hatch-vcs`, so the version is derived from the latest git tag. Local builds work with `uv build`:

```bash
uv build              # both wheel and sdist
uv build --wheel
uv build --sdist
```

Artifacts land under `dist/`. The wheel ships `src/rtl_buddy/` plus the `docs/` tree (via `force-include`); the sdist ships the same source plus `README.md`, `LICENSE`, and `pyproject.toml`. Dev/CI files (`tests/`, `scripts/`, `.github/`, `mkdocs.yml`, `uv.lock`, agent guides, pre-commit config) are excluded — keep them that way when changing `[tool.hatch.build.targets.*]`.

## Validating Against The Project Template

For changes that affect end-user behavior, validate against the [rtl-buddy-project-template](https://github.com/rtl-buddy/rtl-buddy-project-template) before opening a PR. The template's `dev/local-rtl-buddy` branch swaps the PyPI pin for an editable path dependency on a sibling `rtl_buddy/` checkout, so you can iterate locally without publishing.

Typical loop:

```bash
# In a sibling clone of rtl-buddy-project-template:
git worktree add .worktrees/dev-local dev/local-rtl-buddy
cd .worktrees/dev-local
uv sync                              # picks up ../../../rtl_buddy editable
uv run rb regression -c regression.yaml
```

The template's `AGENTS.md` documents the same standing-branch convention; do not push that branch back to `main`.

## Where To Go Next

- [Engineering Guidelines](guidelines.md) — public contracts, execution contexts, path ownership, subprocesses, logging, errors, releases, and issue triage.
- [Documentation Guidelines](docs.md) — frontmatter, page structure, generated pages, and docs validation.
- [Contributing](../CONTRIBUTING.md) — the contributor entry point that links to both.
