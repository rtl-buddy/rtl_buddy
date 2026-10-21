## Stop after a stage

`-E` / `--early-stop` stops after `pre`, `comp`, `sim` or `post`:

```bash
rb -E comp test smoke
```

A successful early stop reports `NA` with `early_stop: true` in its result row and exits 0. A stage failure still reports `FAIL` and exits 1. An `NA` without that marker is an unknown outcome and exits 1. `NA` is never evidence the DUT passed.
