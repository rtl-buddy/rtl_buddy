## Coverage merge modes

Three merge modes are available, selected by a mutually exclusive flag. Only one may be used per run.

| Flag | Merge method | Outputs |
|------|-------------|---------|
| `--coverage-merge` | Raw for summary/HTML, info-process for Coverview | summary, HTML (if `--coverage-html`), Coverview (if `--coverage-coverview`) |
| `--coverage-merge-raw` | Raw Verilator merge only | summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | summary, Coverview — HTML not supported |

If none of these flags is given, no merging is done and coverage is reported per test.
