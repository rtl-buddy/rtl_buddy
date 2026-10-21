## graph query

```text
Usage: rtl-buddy graph query [OPTIONS] QUESTION

 keyword search over graph.json with neighbourhood expansion and the results overlay
 joined in

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    question      TEXT  what to look for — an identifier or a plain question, e.g.  │
│                          "A-COV-1" or "which tests exercise blk_a"                   │
│                          [required]                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --type                             TEXT     restrict to one node type (module, test, │
│                                             coverage_item, ...)                      │
│ --tier                             TEXT     restrict to one tier                     │
│                                             (design|config|binding)                  │
│ --limit                            INTEGER  maximum matches to report [default: 10]  │
│ --depth                            INTEGER  hops of neighbourhood expansion around   │
│                                             each match (0 disables; maximum 3)       │
│                                             [default: 1]                             │
│ --max-neighbors                    INTEGER  neighbours reported per match; anything  │
│                                             beyond is counted in neighbors_truncated │
│                                             rather than dropped silently             │
│                                             [default: 25]                            │
│ --results          --no-results             join the regression-results overlay onto │
│                                             every node                               │
│                                             [default: results]                       │
│ --expand                                    full node summaries for every neighbour  │
│                                             instead of the lean id/label/type        │
│                                             references                               │
│ --graph                            TEXT     graph.json to query (default <project    │
│                                             root>/artefacts/graph)                   │
│ --overlay                          TEXT     results-overlay.json to join (default:   │
│                                             beside graph.json)                       │
│ --help                                      Show this message and exit.              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
