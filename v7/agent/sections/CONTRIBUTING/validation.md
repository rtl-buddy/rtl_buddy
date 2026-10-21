## Validation

Run the narrowest checks that prove the change:

- Docs only: `uv run python scripts/check_docs_frontmatter.py --check` and `npm run build`.
- CLI help: regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`, then check the docs build.
- Runtime: add focused tests and run the affected subset. Run the full suite for shared contracts or command dispatch.

If a check cannot run locally, say which one and why in the PR.
