## Stop after a stage

Use the global `-E` or `--early-stop` option with `pre`, `comp`, `sim`, or `post`:

```bash
rb -E comp test smoke
```

A successful stop before a terminal verdict reports `NA` and exits 0; its result row carries `early_stop: true` (in `--machine` output too) so tooling can tell it apart. A stage failure still reports `FAIL` and exits 1. An `NA` that was not asked for — no verdict in the transcript — is an unknown outcome, carries no marker, and exits 1. Treat `NA` as requiring inspection, not evidence that the DUT passed.
