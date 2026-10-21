## graph

```text
Usage: rtl-buddy graph [OPTIONS] COMMAND [ARGS]...

 build the design knowledge graph

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ build    extract every tier and merge them into artefacts/graph/graph.json           │
│ results  refresh artefacts/graph/results-overlay.json — last status, seed and        │
│          artefact paths per test node; graph.json is not touched                     │
│ query    keyword search over graph.json with neighbourhood expansion and the results │
│          overlay joined in                                                           │
│ path     shortest chain of edges between two graph nodes                             │
│ explain  one node's attributes, every edge on it, and its last result                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
