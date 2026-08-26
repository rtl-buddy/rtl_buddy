## Docs

The docs site lives under `docs/` and is built with MkDocs Material. Two checks run in CI on every docs change:

```bash
uv run python scripts/check_docs_frontmatter.py --check
uv run --group docs mkdocs build --strict
```

To preview the site locally:

```bash
uv run --group docs mkdocs serve
```

Then open <http://127.0.0.1:8000>.

If you change CLI help strings in `src/rtl_buddy/rtl_buddy.py`, regenerate the CLI reference page:

```bash
uv run python scripts/gen_cli_reference.py
```

`docs/reference/cli.md` is auto-generated and should not be edited by hand. The docs build also regenerates it via a hook in `mkdocs.yml`; CI auto-commits drift.
