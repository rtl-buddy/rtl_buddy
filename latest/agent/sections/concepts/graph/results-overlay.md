## Results Overlay

`rb graph results` writes current run state to `artefacts/graph/results-overlay.json` without modifying structural `graph.json`:

```bash
rb graph results
rb graph results --strict
```

Entries are keyed by `test:<suite dir>#<test name>` and contain the latest result status, seed, timestamp, and paths to artefacts that exist. The timestamp is the result envelope's file modification time, so refreshing unchanged inputs is byte-stable.

Result status comes from each run's `result.json`, not from log parsing. A test directory with artefacts but no result envelope is retained as `UNKNOWN`. Random-test iterations remain available under `runs`; the newest iteration supplies the entry's top-level status.

When cross-checking against `graph.json`:

- `missing` identifies graph test nodes with no result.
- `unmatched` identifies results with no declared test node, such as generated sweep names.
- `problems` identifies unreadable result data.

These are reported normally and become failures with `--strict`. A missing or unreadable overlay does not prevent structural queries.
