## axi-profile run

```text
Usage: rtl-buddy axi-profile run [OPTIONS] TEST_NAME                                   
                                                                                        
 ingest a test's FST and emit per-test axi-perf.json                                    
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT  test from tests.yaml [required]                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config             -c      TEXT  tests.yaml to use [default: tests.yaml]      │
│ --output                  -o      TEXT  output path for axi-perf.json (default:      │
│                                         artefacts/axi/<test>/axi-perf.json)          │
│ --tb-prefix                       TEXT  Override the testbench top scope name used   │
│                                         as the hierarchical prefix in the FST.       │
│                                         Default is the test's tb name from           │
│                                         tests.yaml. Pass empty string to disable.    │
│ --emit-txns-parquet                     Also emit a per-transaction parquet artifact │
│                                         at artefacts/axi/<test>/axi-txns.parquet —   │
│                                         the canonical location `rb axi-profile       │
│                                         notebook` reads. Requires the axi-profiler   │
│                                         extra (pyarrow).                             │
│ --emit-txns-parquet-path          TEXT  Explicit path for the per-transaction        │
│                                         parquet artefact. Implies                    │
│                                         --emit-txns-parquet.                         │
│ --tool                            TEXT  path to the axi-profiler binary              │
│                                         [default: axi-profiler]                      │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
