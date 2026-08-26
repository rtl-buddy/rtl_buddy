## Validation

Run the narrowest checks that prove the change:

- Docs-only edits: run `uv run python scripts/check_docs_frontmatter.py --check` and `npm run build`.
- CLI help changes: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py` and check the docs build.
- Runtime changes: add focused tests and run the affected subset. Run the full suite for shared contracts or command dispatch.

If validation cannot be run locally, say which check was skipped and why in the PR.
