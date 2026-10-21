## Open the transaction notebook

```bash
rb axi-profile notebook my_test
rb axi-profile notebook my_test --port 2718
rb axi-profile notebook my_test --headless
```

The notebook needs the per-test Parquet file from `run --emit-txns-parquet` and a `marimo` executable. A missing input fails with the command or extra that creates it.

The default mode runs in the foreground with the packaged notebook template. `--headless` disables the marimo token so the loopback-only hub can open the URL. `--daemon` is accepted but still runs in the foreground; use a hub-launched notebook when the caller must return immediately.
