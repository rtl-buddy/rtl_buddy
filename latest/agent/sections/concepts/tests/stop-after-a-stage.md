## Stop after a stage

Use the global `-E` or `--early-stop` option with `pre`, `comp`, `sim`, or `post`:

```bash
rb -E comp test smoke
```

A successful stop before a terminal verdict reports `NA` and exits 0. A stage failure still reports `FAIL` and exits 1. Treat `NA` as requiring inspection, not evidence that the DUT passed.
