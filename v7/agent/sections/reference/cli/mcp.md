## mcp

```text
Usage: rtl-buddy mcp [OPTIONS]

 serve the knowledge graph, test status, coverage, physical metrics and hierarchy
 queries (plus the live session when a hub runs) over MCP (stdio); needs the 'mcp'
 extra

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --graph             TEXT  graph.json to serve (default <project                      │
│                           root>/artefacts/graph)                                     │
│ --overlay           TEXT  results-overlay.json to join                               │
│ --root              TEXT  project root to serve (default: discovered from cwd)       │
│ --design-dir        TEXT  directory searched for models.yaml                         │
│ --frontend          TEXT  viewer parser frontend (verible|slang)                     │
│ --tool              TEXT  path to the rtl-buddy-view binary                          │
│                           [default: rtl-buddy-view]                                  │
│ --list-tools              print the tool schemas and exit instead of serving         │
│ --help                    Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
