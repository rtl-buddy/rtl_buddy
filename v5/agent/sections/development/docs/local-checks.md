## Local Checks

Run the docs checks before opening a PR that touches docs:

```bash
uv run python scripts/check_docs_frontmatter.py --check
uv run --group docs mkdocs build --strict
```

For CLI help changes, regenerate the CLI reference first:

```bash
uv run python scripts/gen_cli_reference.py
```
