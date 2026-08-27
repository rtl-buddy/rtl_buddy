## Results Overlay

`rb graph results` writes current run state to `artefacts/graph/results-overlay.json` without modifying structural `graph.json`:

```bash
rb graph results
rb graph results --strict
```

Entries are keyed by `test:<suite dir>#<test name>` and contain the latest result status, seed, timestamp, and paths to artefacts that exist. The timestamp is the result envelope's file modification time, so refreshing unchanged inputs is byte-stable.

An entry also carries an optional `compile` block with `duration_sec`, `builder`, and `reused` when the run's result envelope records one. A local run records its own. A dispatched run produces two envelopes that disagree on purpose: the simulation job writes `artefacts/<test>/result.json` with its own `compile` block, which says `reused: true` and near-zero duration because the shared build already produced the `simv`, and at collect the head overwrites the `compile` block in `artefacts/<test>/dispatch/result-<tag>.json` with the build job's record. The overlay takes the newest envelope, which is the head's, so a dispatched run reports the shared build's compile — the one that did the work. The exception is a simulation job that had to rebuild because the prebuilt stamp did not validate (`compile.prebuilt_stamp_invalid`): the overlay still reports the build job's record, not the recompile that actually produced that run's `simv`. Its values are read from the envelope, never measured at overlay time, and the block is absent when the envelope says nothing about the compile, so byte-stability holds for a refresh with nothing rerun.

Result status comes from each run's `result.json`, not from log parsing. A test directory with artefacts but no result envelope is retained as `UNKNOWN`. Random-test iterations remain available under `runs`; the newest iteration supplies the entry's top-level status.

When cross-checking against `graph.json`:

- `missing` identifies graph test nodes with no result.
- `unmatched` identifies results with no declared test node, such as generated sweep names.
- `problems` identifies unreadable result data.

These are reported normally and become failures with `--strict`. A missing or unreadable overlay does not prevent structural queries.
