---
description: Entry point for contributing to rtl_buddy, with links to the detailed development and documentation guidelines maintainers follow.
---

# Contributing

This page is the entry point for contributors and maintainers.
Detailed rules live under the Development section so they are visible in the docs site, available through `rb docs show`, and not duplicated across agent-only files.

## Environment Setup

Start with [Environment Setup](development/setup.md) for cloning, `uv sync`, pre-commit hooks, running the test suite, and building the wheel/sdist locally.

## Development Guidelines

Read [Engineering Guidelines](development/guidelines.md) before changing runtime behavior, command execution, YAML loading, subprocess wrappers, logging, errors, release mechanics, or the bundled agent skill.

Those rules define the public contracts maintainers should preserve.
When the current implementation differs from a guideline, treat the mismatch as behavior debt: fix it in the same change when it is in scope, or document the exception and add a follow-up issue.

## Documentation Guidelines

Read [Documentation Guidelines](development/docs.md) before adding or editing files under `docs/`.

Documentation changes must keep frontmatter valid, keep generated pages in sync, and avoid duplicating reference material that is already generated from the CLI or schemas.

## Validation

Use the narrowest validation that proves the change:

- Docs-only edits: run `uv run python scripts/check_docs_frontmatter.py --check` and `uv run --group docs mkdocs build --strict`.
- CLI help changes: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py` and check the docs build.
- Runtime behavior changes: add or update focused tests, then run the affected test subset. Broaden to the full suite when shared contracts or command dispatch are touched.

If validation cannot be run locally, say which check was skipped and why in the PR.
