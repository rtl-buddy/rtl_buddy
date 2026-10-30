## graph explain

```text
Usage: rtl-buddy graph explain [OPTIONS] NODE

 one node's attributes, every edge on it, and its last result

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    node      TEXT  node id, or a bare unambiguous name [required]                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --results    --no-results          join the regression-results overlay onto every    │
│                                    node                                              │
│                                    [default: results]                                │
│ --expand                           full node summaries for every edge peer instead   │
│                                    of the lean id/label/type references              │
│ --graph                      TEXT  graph.json to query                               │
│ --overlay                    TEXT  results-overlay.json to join                      │
│ --help                             Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
