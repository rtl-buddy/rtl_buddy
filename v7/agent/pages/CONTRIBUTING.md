---
description: Setup, authoring rules, and validation required for rtl_buddy contributions.
---

# Contributing

The guides below are the source of truth. Link to them from contributor and agent files instead of copying their rules.

## Environment Setup

[Environment Setup](development/setup.md) covers cloning the repository, installing dependencies, and running local checks.

## Development Guidelines

Read [Engineering Guidelines](development/guidelines.md) before changing a public contract, dependency, command execution, logging, the release workflow, or the bundled skill.

## Documentation Guidelines

Read [Documentation Guidelines](development/docs.md) before editing `docs/`.

## Validation

Run the narrowest checks that prove the change:

- Docs only: `uv run python scripts/check_docs_frontmatter.py --check` and `npm run build`.
- CLI help: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`, then check the docs build.
- Runtime: add focused tests and run the affected subset. Run the full suite for shared contracts or command dispatch.

If a check cannot run locally, say which one and why in the PR.
