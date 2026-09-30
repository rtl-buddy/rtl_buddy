## Skip the model when nothing will read it

Per-test attribution grows with points times tests, and for a large toggle-instrumented suite it can dominate the run's output and post-dispatch time. `--coverage-model` on `test` and `regression` chooses how much to write:

- `full` (default): every point with per-test hit counts.
- `totals`: every point and hit count, without per-test attribution.
- `none`: no `coverage-model.json`; a model left by an earlier run is removed.

Console summaries, merges and the directory and source summaries are unchanged. Use `none` for a CI job that records the suite figure and discards its artefacts:

```bash
rb -M cov regression --coverage-merge --coverage-model none
```

`rb cov` and the hub `/cov` pane need a model. Under `totals` they show points and totals without attribution; under `none` they exit with an error.
