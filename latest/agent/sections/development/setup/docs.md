## Docs

```bash
uv run python scripts/check_docs_frontmatter.py --check
npm ci
npm run build
npm run start
```

`npm run start` previews the Docusaurus site at <http://localhost:3000/>.
The build also exports `llms.txt`, `llms-full.txt`, a JSON page catalog, raw
Markdown pages, and section-level Markdown under `build/agent/`. These static
resources mirror `rb docs` for networked agents while the CLI remains the
installed-version, offline interface.

`npm run build:all` additionally rebuilds every published major from that
major's latest stable Git tag. CI validates this on pull requests and uses it
for stable release deployment after the new tag exists. That release build
refreshes `dev` from the latest `origin/main` snapshot and stable versions from
their tags, so the new major appears in navigation without regressing newer
development content.

For CLI help changes, regenerate the reference before building:

```bash
uv run python scripts/gen_cli_reference.py
```

Do not edit `docs/reference/cli.md` by hand. See [Documentation Guidelines](https://rtl-buddy.github.io/rtl_buddy/v6/development/docs/).
