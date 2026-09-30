## Local docs access

The wheel includes the docs for its installed version, so they work offline and match the running release:

```bash
rb docs list
rb docs show agents
rb docs show concepts/tests#interpret-results
rb --machine docs list
rb --machine docs show reference/yaml
```

`docs list` returns each page's slug, title, and frontmatter description. `docs show` takes a slug and an optional section anchor. In machine mode `docs list` uses the standard command envelope, while `docs show` prints the page payload as a bare JSON object.

Each published documentation version also has a static network mirror under `dev/` or `v<major>/`:

- `llms.txt` for discovery.
- `agent/catalog.json` for page and section metadata.
- `agent/pages/<slug>.md` for a raw page.
- `agent/sections/<slug>/<anchor>.md` for one section, with relative links rebased to version-pinned pages.
