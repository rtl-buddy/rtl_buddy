## Subcommand: `run`

```bash
# Ingest the test's FST and emit axi-perf.json
rb axi-profile run my_test

# Also produce the per-txn parquet that the notebook reads
rb axi-profile run my_test --emit-txns-parquet

# Explicit parquet path (implies --emit-txns-parquet)
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet

# Custom output path for axi-perf.json
rb axi-profile run my_test -o /tmp/axi-perf.json

# Override the FST top scope (default = the test's tb name)
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The runner resolves everything from `tests.yaml` and the standard artefact layout:

| Input | Where it comes from |
|-------|---------------------|
| Model | `tests.yaml` → `model:` |
| Manifest | `model.axi_bundles` in `models.yaml` (must exist — run `discover` first) |
| FST trace | `<suite_dir>/artefacts/<test>/dump.fst` (same convention as `rb wave`) |
| Top scope prefix | The test's `testbenches:` entry name in `tests.yaml` |

You only type `rb axi-profile run <test>` — everything else auto-resolves. The `--tb-prefix` override exists for setups where the Verilator wrapper renames the testbench scope; pass an empty string to disable prefix matching entirely.

Pass `--emit-txns-parquet` to also write per-transaction rows to `artefacts/axi/<test>/axi-txns.parquet` — that's the canonical location `rb axi-profile notebook` reads. Requires the `axi-profiler` `[parquet]` extra (pyarrow).
