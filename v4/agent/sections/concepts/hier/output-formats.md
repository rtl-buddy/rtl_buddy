## Output formats

`--format` selects one of four renderers:

| Format | What you get |
|--------|--------------|
| `tree` (default) | ASCII tree, ideal for terminal inspection |
| `dot` | Graphviz DOT source — pipe through `dot -Tsvg` / `-Tpng` for graphics |
| `mermaid` | Mermaid diagram source — paste into Markdown that renders Mermaid |
| `json` | Structured JSON (schema_version, tool.\*, design.top, nodes, edges) for programmatic consumption |

When `-o`/`--output` is not set, the renderer's stdout passes through to your terminal so `rb hier x --format dot | dot -Tsvg -o x.svg` works as a one-liner.
