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

This installs the package and the lint and test groups. Add `--extra graph-extract` when working on the optional graph binding tier or matching the CI test environment.

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
uv run pytest --collect-only -q              # collection and case count
uv run pytest --durations=25                 # runtime without coverage overhead
uv run pytest --cov --cov-report=term-missing
```

Coverage is opt-in; plain `pytest` stays fast. Coverage includes Python subprocesses and enforces a combined statement/branch floor of 87%. CI also checks an 80% floor for each elaboration and Tcl worker, so missing subprocess measurements fail visibly. For a focused coverage investigation, select the affected source with `--cov=rtl_buddy.<module>` and set an appropriate `--cov-fail-under` for that subset; the full-suite floor applies to CI.

CI uses managed Python 3.12 and the graph extractor extra:

```bash
uv sync --managed-python --python 3.12 --group dev --extra graph-extract
uv run --managed-python --python 3.12 pytest --durations=25
```

Compare timings using the same interpreter, optional dependencies and tool availability. Read setup as well as call durations. Optional EDA tests probe their tools when selected and skip when prerequisites are absent; inspect skips with `-rs`. Keep a full run when changing shared fixtures or test infrastructure. Test authoring and deletion rules are in [Engineering Guidelines](guidelines.md#test-design).

## Docs

```bash
uv run python scripts/check_docs_frontmatter.py --check
npm ci
npm run build
npm run start
```

`npm run start` previews the Docusaurus site at <http://localhost:3000/>.

The build also writes `llms.txt`, `llms-full.txt`, a JSON page catalog, raw Markdown pages, and section-level Markdown under `build/agent/`. These static resources mirror `rb docs` for networked agents; the CLI remains the offline, installed-version interface.

`npm run build:all` also rebuilds every published major from that major's latest stable Git tag. CI runs it on pull requests and for stable release deployment after the new tag exists. It refreshes `dev` from the latest `origin/main` snapshot and stable versions from their tags.

For CLI help changes, regenerate the reference before building:

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
