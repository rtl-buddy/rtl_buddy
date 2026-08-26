## Open the transaction notebook

```bash
rb axi-profile notebook my_test
rb axi-profile notebook my_test --port 2718
rb axi-profile notebook my_test --headless
```

The notebook requires the canonical per-test Parquet file from `run --emit-txns-parquet` and a `marimo` executable. Missing inputs fail with the exact command or extra needed to create them.

Foreground mode opens the packaged notebook template. `--headless` disables the marimo token so the loopback-only hub can open the URL. `--daemon` is accepted but runs in the foreground; use hub-managed launch when the caller must return immediately.
