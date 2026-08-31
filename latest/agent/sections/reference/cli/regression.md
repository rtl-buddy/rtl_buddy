## regression

```text
Usage: rtl-buddy regression [OPTIONS]

 run rtl regression

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config                   -c      TEXT     path to regressions.yaml             │
│                                                 [default: (Use ./regression.yaml if  │
│                                                 present, otherwise root_config.yaml  │
│                                                 reg-cfg-path)]                       │
│ --reg-level                    -l      INTEGER  regression level to stop at          │
│                                                 [default: 0]                         │
│ --start-level                  -s      INTEGER  regression level to start at         │
│                                                 [default: 0]                         │
│ --coverage-merge                                merge coverage across regression     │
│                                                 tests; uses raw merge for            │
│                                                 summary/html and info-process for    │
│                                                 Coverview                            │
│ --coverage-merge-raw                            use raw Verilator merge for merged   │
│                                                 summary/html/Coverview               │
│ --coverage-merge-info-process                   use info-process merge for merged    │
│                                                 summary/Coverview; HTML merge is not │
│                                                 supported                            │
│ --coverage-html                                 generate merged LCOV HTML output in  │
│                                                 coverage_merge.html                  │
│ --coverage-coverview                            generate Coverview zip output from   │
│                                                 coverage info                        │
│ --coverage-per-test                             package one Coverview dataset per    │
│                                                 test in regression mode              │
│ --coverage-dir-summary                 TEXT     append coverage summary lines for    │
│                                                 repo-relative directory prefixes;    │
│                                                 may be repeated                      │
│ --coverage-dir-summary-file            TEXT     file containing repo-relative        │
│                                                 directory prefixes, one per line     │
│ --share-build                                   reuse one compiled simv across tests │
│                                                 with identical compile inputs        │
│                                                 (Verilator builders only)            │
│ --rebuild                                       recompile even when a valid build    │
│                                                 already exists (implies nothing      │
│                                                 about --share-build)                 │
│ --dispatch                             TEXT     execution backend for test runs      │
│                                                 (local, local-parallel, slurm)       │
│                                                 [default: (cfg-dispatch backend,     │
│                                                 else local)]                         │
│ --jobs                         -j      INTEGER  concurrent jobs for --dispatch       │
│                                                 local-parallel                       │
│                                                 [default: (cfg-dispatch jobs, else   │
│                                                 min(4, cpu count))]                  │
│ --help                                          Show this message and exit.          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
