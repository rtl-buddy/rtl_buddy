## Local docs access

The wheel ships the full Markdown docs site alongside the CLI, exposed through:

```bash
rtl-buddy docs list
rtl-buddy docs show agents
rtl-buddy --machine docs show reference/yaml
```

This is the recommended reference surface for agents: the docs are local (no network), and their content always matches the installed version of `rtl_buddy`. `docs list` enumerates each page's slug, title, and description; `docs show` returns the canonical Markdown for a single page. GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> remains available as a human-facing fallback.

Note one exception to the [machine-mode envelope](https://rtl-buddy.github.io/rtl_buddy/v5/agents/#machine-mode) below: under `--machine`, `docs show` prints the raw page payload as a **bare JSON object** (not wrapped in the standard `{command, exit_code, meta, payload}` envelope), because its whole purpose is to hand you the page content directly. `docs list` does use the standard envelope — the page list is under `payload.pages`.
