## Render a hierarchy

Run from a directory where rtl_buddy can find the relevant configuration, or pass `-c` explicitly:

```bash
rb hier demo_top
rb hier demo_top --format mermaid -o demo_top.mmd
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg
rb hier demo_top --format json -o demo_top.hier.json
rb hier demo_top -c design/demo_top/models.yaml
```

The positional name selects a model from `models.yaml`. rtl_buddy builds a stripped, deduplicated filelist from that model and passes its name as the renderer top.

Available formats are:

| Format | Use |
| --- | --- |
| `tree` | Terminal inspection; default. |
| `dot` | Graphviz input for diagrams. |
| `mermaid` | Mermaid source for Markdown. |
| `json` | Structured data for downstream tools. |

Without `-o`, renderer output goes to stdout and can be piped. With `-o`, the renderer writes the requested file.
