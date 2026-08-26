## Local docs access

Use the bundled docs commands first when you need CLI or YAML reference:

```bash
rtl-buddy docs list
rtl-buddy docs show agents
rtl-buddy --machine docs show reference/yaml
```

`docs list` shows each page's slug, title, and summary. `docs show --machine` returns lightweight metadata plus the canonical Markdown for the selected page. GitHub Pages remains a convenient human-facing fallback.
