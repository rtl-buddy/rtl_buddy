## Validation

Use the narrowest validation that proves the change:

- Docs-only edits: run `uv run python scripts/check_docs_frontmatter.py --check` and `uv run --group docs mkdocs build --strict`.
- CLI help changes: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py` and check the docs build.
- Runtime behavior changes: add or update focused tests, then run the affected test subset. Broaden to the full suite when shared contracts or command dispatch are touched.

If validation cannot be run locally, say which check was skipped and why in the PR.
