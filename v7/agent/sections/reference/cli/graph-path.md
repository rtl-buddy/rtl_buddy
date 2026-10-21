## graph path

```text
Usage: rtl-buddy graph path [OPTIONS] SOURCE TARGET

 shortest chain of edges between two graph nodes

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    source      TEXT  start node id, or a bare unambiguous name [required]          │
│ *    target      TEXT  end node id, or a bare unambiguous name [required]            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --directed     --undirected             follow edge direction; undirected by default │
│                                         because edge direction encodes role, not     │
│                                         reachability                                 │
│                                         [default: undirected]                        │
│ --max-paths                    INTEGER  shortest paths to report [default: 3]        │
│ --results      --no-results             join the regression-results overlay onto     │
│                                         every node                                   │
│                                         [default: results]                           │
│ --graph                        TEXT     graph.json to query                          │
│ --overlay                      TEXT     results-overlay.json to join                 │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
