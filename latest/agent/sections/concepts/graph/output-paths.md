## Output Paths

The stable outputs are:

```text
artefacts/graph/
├── graph.json
├── graph-meta.json
├── results-overlay.json
├── design/<model>/graph.json
├── config/graph.json
├── binding/graph.json
└── bind/graph.json
```

`graph-meta.json` records the build fingerprint, input hashes, tool versions, tier status, failures, skipped items, stitch points, dangling targets, and id collisions. Per-testbench and per-run design exports are nested under `design/<model>/tb/` and `design/<model>/run/`. Their generated filelists and renderer logs live under `artefacts/hier/`.

Volatile results, seeds, timestamps, and artefact paths belong only in the overlay. Consumers may join them in memory but must not write the annotated document over `graph.json`.
