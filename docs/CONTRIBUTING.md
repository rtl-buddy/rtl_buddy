---
description: Setup, authoring rules, and validation required for rtl_buddy contributions.
---

# Contributing

Use the linked guides as the source of truth; do not duplicate their rules in contributor or agent files.

## Environment Setup

Follow [Environment Setup](development/setup.md) to clone the repository, install dependencies, and run local checks.

## Development Guidelines

Read [Engineering Guidelines](development/guidelines.md) before changing a public contract, dependency, command execution, logging, release workflow, or the bundled skill.

## Documentation Guidelines

Read [Documentation Guidelines](development/docs.md) before editing `docs/`.

## Validation

Run the narrowest checks that prove the change:

- Docs-only edits: run `uv run python scripts/check_docs_frontmatter.py --check` and `npm run build`.
- CLI help changes: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py` and check the docs build.
- Runtime changes: add focused tests and run the affected subset. Run the full suite for shared contracts or command dispatch.

If validation cannot be run locally, say which check was skipped and why in the PR.
