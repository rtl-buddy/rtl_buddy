# rtl_buddy — Agent Guide

## Purpose

This repository is the source of truth for the `rtl_buddy` Python CLI. It
contains the implementation, tests, documentation, packaged agent skills, and
release configuration.

This file is a stable orientation guide. Detailed engineering, documentation,
review, skill, template, and release rules live in the linked development
documents and should be updated there rather than duplicated here.

## Read First

Use the guide that matches the work:

- [Contributing](docs/CONTRIBUTING.md) — contributor entry point.
- [Environment Setup](docs/development/setup.md) — local installation and
  development commands.
- [Engineering Guidelines](docs/development/guidelines.md) — runtime
  contracts, paths, artifacts, dependencies, logging, errors, validation,
  issue triage, and releases.
- [Documentation Guidelines](docs/development/docs.md) — documentation
  structure, ownership, generated pages, and docs validation.
- [Code Reviews](docs/development/reviews.md) — review scope, evidence, and
  guideline selection.
- [Bundled Skill Guidelines](docs/development/bundled-skills.md) — skill
  content, packaging, installation, and lifecycle checks.
- [Project Template Guidelines](docs/development/project-template.md) —
  downstream example coverage and validation.

For agent-facing CLI usage, use [Agent use of rtl-buddy](docs/agents.md). For
user-facing command and configuration reference, use the generated
[CLI reference](docs/reference/cli.md) and [YAML reference](docs/reference/yaml.md).

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

The `rtl-buddy-project-template` repository is a downstream validation target,
not a source-of-truth copy of this implementation. Follow the project-template
guidelines when a change affects user-visible behavior.

## Normal Development Loop

1. Check the repository status and current branch. Use a dedicated worktree
   for feature work and preserve unrelated changes.
2. Read the applicable development guide before changing a public contract.
3. Set up or refresh the environment with `uv sync --group dev`; use `uv run`
   for Python commands.
4. Make the smallest coherent implementation, test, and documentation change.
5. Run the narrowest checks that prove the change, then broaden them when a
   shared contract or command-dispatch path is affected.
6. Validate user-visible behavior against the project template when required.
7. After validation, commit the scoped change and open a review-ready PR using
   the repository's review and release conventions.

Common local checks are:

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
```

For documentation changes, also run the frontmatter check and Docusaurus
build described in [Documentation Guidelines](docs/development/docs.md). For
CLI help changes, regenerate `docs/reference/cli.md` with
`uv run python scripts/gen_cli_reference.py` before building the docs.

## Source-of-Truth Boundaries

- Put runtime and CLI behavior in `src/rtl_buddy/`, with tests in `tests/`.
- Put user and maintainer documentation in `docs/`; keep each rule, schema,
  and workflow in its canonical page.
- Edit CLI help in the implementation and regenerate
  `docs/reference/cli.md`; do not hand-edit generated output.
- Put bundled skill changes in `src/rtl_buddy/skill/`. The skill family ships
  in the wheel; there is no separate skill source repository.
- When behavior, configuration, dependencies, or workflows change, follow
  the required downstream updates in the Engineering and Project Template
  Guidelines.

## Working Agreements

- Preserve public CLI, configuration, artifact, machine-output, logging, and
  skill contracts unless the change explicitly updates their tests and docs.
- Prefer targeted changes over broad refactors.
- Keep comments and agent guidance concise; link to canonical documentation
  instead of copying it.
- Do not merge, force-push, or change unrelated work without explicit approval.
