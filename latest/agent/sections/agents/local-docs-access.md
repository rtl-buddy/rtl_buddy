## Local docs access

The wheel includes the docs for its installed version:

```bash
rb docs list
rb docs show agents
rb docs show concepts/tests#interpret-results
rb --machine docs list
rb --machine docs show reference/yaml
```

`docs list` returns each page's slug, title, and frontmatter description. `docs show` accepts a slug and optional section anchor.

In machine mode, `docs list` uses the standard command envelope. `docs show` is the exception: it prints the page payload as a bare JSON object so consumers can use the content directly.

Every published Docusaurus version also exposes a static agent surface. Use
`llms.txt` for discovery, `agent/catalog.json` for structured page and section
metadata, `agent/pages/<slug>.md` for a raw page, or
`agent/sections/<slug>/<anchor>.md` for one bounded section. These URLs are
versioned under `dev/` or `v<major>/`; they are the network-accessible mirror,
not a replacement for the installed-version and offline guarantees above.
Relative links in bounded sections are rebased to version-pinned human pages so
they retain the same targets after extraction.
