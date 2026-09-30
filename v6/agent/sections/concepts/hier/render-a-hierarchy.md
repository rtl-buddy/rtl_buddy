## Render a hierarchy

Run from a directory where rtl_buddy can find the configuration, or pass `-c`:

```bash
rb hier demo_top
rb hier demo_top --format mermaid -o demo_top.mmd
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg
rb hier demo_top --format json -o demo_top.hier.json
rb hier demo_top -c design/demo_top/models.yaml
```

The positional name selects a model from `models.yaml`. rtl_buddy builds a stripped, deduplicated filelist from it and passes the model name to the renderer as the top.

| Format | Use |
| --- | --- |
| `tree` | Terminal inspection (default). |
| `dot` | Graphviz input for diagrams. |
| `mermaid` | Mermaid source for Markdown. |
| `json` | Structured data for downstream tools. |

Without `-o` the output goes to stdout and can be piped. With `-o` the renderer writes the file.
