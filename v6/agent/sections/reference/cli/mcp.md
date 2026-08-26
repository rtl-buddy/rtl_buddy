## mcp

```text
Usage: rtl-buddy mcp [OPTIONS]

 serve the design knowledge graph and hierarchy queries over the Model Context Protocol
 (stdio); needs the 'mcp' extra

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --graph             TEXT  graph.json to serve (default <project                      │
│                           root>/artefacts/graph)                                     │
│ --overlay           TEXT  results-overlay.json to join                               │
│ --root              TEXT  project root to serve; default is discovered from cwd,     │
│                           which is what an agent host's spawn gives you              │
│ --design-dir        TEXT  directory searched for models.yaml                         │
│ --frontend          TEXT  viewer parser frontend (verible|slang)                     │
│ --tool              TEXT  path to the rtl-buddy-view binary                          │
│                           [default: rtl-buddy-view]                                  │
│ --list-tools              print the tool schemas and exit instead of serving         │
│ --help                    Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
