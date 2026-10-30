## axi-profile

```text
Usage: rtl-buddy axi-profile [OPTIONS] COMMAND [ARGS]...

 profile AXI interconnect performance via rtl-buddy-axi-profiler

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ run          ingest a test's FST and emit per-test axi-perf.json                     │
│ discover     parse RTL to (re)generate the model's axi-bundles.yaml manifest         │
│ gen-monitor  emit the SV bind-style AXI monitor for the model's testbench            │
│ notebook     launch the packaged marimo notebook against a test's per-txn parquet    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
