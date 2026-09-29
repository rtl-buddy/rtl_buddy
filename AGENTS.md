# rtl_buddy — Agent Guide

## Purpose

This repository is the source of truth for the `rtl_buddy` Python CLI: implementation, tests, documentation, packaged agent skills, and release configuration.

This file is a stable orientation guide. Detailed rules live in the development documents below; update them there, not here.

## Read First

- [Contributing](docs/CONTRIBUTING.md) — contributor entry point.
- [Environment Setup](docs/development/setup.md) — installation and development commands.
- [Engineering Guidelines](docs/development/guidelines.md) — runtime contracts, paths, artifacts, dependencies, logging, errors, validation, issue triage, releases.
- [Documentation Guidelines](docs/development/docs.md) — documentation structure, ownership, generated pages, validation.
- [Code Reviews](docs/development/reviews.md) — review scope, evidence, guideline selection.
- [Bundled Skill Guidelines](docs/development/bundled-skills.md) — skill content, packaging, installation, lifecycle checks.
- [Project Template Guidelines](docs/development/project-template.md) — downstream example coverage and validation.

For agent-facing CLI usage, see [Agent use of rtl-buddy](docs/agents.md). For commands and configuration, see the generated [CLI reference](docs/reference/cli.md) and the [YAML reference](docs/reference/yaml.md).

## Repository Map

| Path | Role |
| --- | --- |
| `src/rtl_buddy/` | CLI implementation and packaged resources. |
| `src/rtl_buddy/skill/` | Source of truth for the bundled skill family. |
| `tests/` | Unit, integration, contract, and packaging tests. |
| `docs/` | User and maintainer documentation; the development pages own detailed policy. |
| `scripts/` | Documentation, packaging, and development helpers. |
| `.github/` | CI, release, issue, and pull-request configuration. |
| `pyproject.toml` | Package metadata, dependencies, entry points, and tool configuration. |

The `rtl-buddy-project-template` repository is a downstream validation target, not a copy of this implementation. Follow the project-template guidelines when a change affects user-visible behavior.

## Development Loop

1. Check the branch and status. Use a dedicated worktree for feature work and preserve unrelated changes.
2. Read the applicable development guide before changing a public contract.
3. Run `uv sync --group dev`; use `uv run` for Python commands.
4. Make the smallest coherent implementation, test, and documentation change.
5. Run the narrowest checks that prove the change. Broaden them when a shared contract or command-dispatch path is affected.
6. Validate user-visible behavior against the project template when required.
7. Commit the scoped change and open a review-ready PR following the repository's review and release conventions.

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
```

For documentation changes, also run the frontmatter check and Docusaurus build described in [Documentation Guidelines](docs/development/docs.md). For CLI help changes, run `uv run python scripts/gen_cli_reference.py` before building the docs.

## Source-of-Truth Boundaries

- Runtime and CLI behavior lives in `src/rtl_buddy/`, with tests in `tests/`.
- Documentation lives in `docs/`; each rule, schema, and workflow has one canonical page.
- CLI help is edited in the implementation; `docs/reference/cli.md` is generated.
- Bundled skills live in `src/rtl_buddy/skill/` and ship in the wheel.
- When behavior, configuration, dependencies, or workflows change, make the downstream updates required by the Engineering and Project Template Guidelines.

## Working Agreements

- Preserve public CLI, configuration, artifact, machine-output, logging, and skill contracts unless the change updates their tests and docs.
- Prefer targeted changes over broad refactors.
- Keep comments and agent guidance concise; link to canonical documentation instead of copying it.
- Do not merge, force-push, or change unrelated work without explicit approval.

## Code Review Rules

The review procedure and guideline routing are in [Code Reviews](docs/development/reviews.md). Review comprehensively so repeated reviews are not needed.
