## graph results

```text
Usage: rtl-buddy graph results [OPTIONS]

 refresh artefacts/graph/results-overlay.json — last status, seed and artefact paths
 per test node; graph.json is not touched

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --verif-dir             TEXT  directory searched for tests.yaml                      │
│ --out-dir       -o      TEXT  output directory (default: <project                    │
│                               root>/artefacts/graph)                                 │
│ --graph                 TEXT  graph.json to cross-check ids against (default:        │
│                               <out-dir>/graph.json); read, never written             │
│ --strict                      exit non-zero when an envelope could not be read, a    │
│                               test node has no result, or a result matches no node   │
│ --coverage              TEXT  coverage source to join onto the graph's ids: 'auto'   │
│                               (cov_dir/manifest.json, then the per-test coverage.dat │
│                               databases this scan finds), 'model' (the manifest      │
│                               only), 'none', or a path to a merged LCOV .info file;  │
│                               nothing is re-run                                      │
│                               [default: auto]                                        │
│ --no-coverage                 skip the coverage join (same as --coverage none)       │
│ --cov-dir               TEXT  coverage artefact directory to join from (default: the │
│                               newest cov_dir/ under the project)                     │
│ --cov-manifest          TEXT  coverage manifest.json to join from, instead of        │
│                               discovery                                              │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
