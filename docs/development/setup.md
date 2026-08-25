---
description: Set up rtl_buddy for local development and run its lint, test, docs, and package checks.
---

# Development Environment Setup

This page is for contributors working on `rtl_buddy`. For the published package, see [Installation](../install.md).

## Prerequisites

- Python 3.11 or later.
- [`uv`](https://docs.astral.sh/uv/).
- `git`.

`uv` manages the environment from `pyproject.toml` and `uv.lock`. External EDA tools are required only for their matching commands; see [Installation](../install.md#external-tools-by-feature).

## Clone And Sync

```bash
git clone https://github.com/rtl-buddy/rtl_buddy.git
cd rtl_buddy
uv sync --group dev
```

This installs the package and the lint, test, and docs groups. Add `--extra graph-extract` only when working on the optional graph binding tier.

Verify the install:

```bash
uv run rb --version
```

## Pre-Commit Hook

Install the Ruff pre-commit hook once:

```bash
uv tool install pre-commit
pre-commit install
```

## Lint And Format

```bash
uv run ruff check
uv run ruff format --check
uv run ruff format          # rewrite files
```

## Tests

```bash
uv run pytest                                # full suite
uv run pytest tests/test_cli_with_fixture.py # one file
uv run pytest -k "list"                      # by keyword
uv run pytest --cov --cov-report=term-missing
```

Coverage is opt-in; plain `pytest` stays fast.

## Docs

```bash
uv run python scripts/check_docs_frontmatter.py --check
uv run --group docs mkdocs build --strict
uv run --group docs mkdocs serve
```

`mkdocs serve` previews the site at <http://127.0.0.1:8000>. For CLI help changes, regenerate the reference before building:

```bash
uv run python scripts/gen_cli_reference.py
```

Do not edit `docs/reference/cli.md` by hand. See [Documentation Guidelines](docs.md).

## Building Wheels And Sdists

```bash
uv build
uv build --wheel
uv build --sdist
```

Artifacts land in `dist/`. Inspect wheel and sdist contents when changing `[tool.hatch.build.targets.*]`; both must include the package and docs, not tests or development configuration.

## Validating Against The Project Template

For end-user behavior changes, use the template's local-development worktree. Point its editable `rtl_buddy` source at the exact feature worktree being tested.

```bash
# In a sibling clone of rtl-buddy-project-template:
git worktree add .worktrees/dev-local dev/local-rtl-buddy
cd .worktrees/dev-local
uv sync
uv run rb regression -c regression.yaml
```

Follow the template's `AGENTS.md`. Keep `dev/local-rtl-buddy` local and do not merge it to `main`.

## Authoring Rules

- [Engineering Guidelines](guidelines.md)
- [Documentation Guidelines](docs.md)
