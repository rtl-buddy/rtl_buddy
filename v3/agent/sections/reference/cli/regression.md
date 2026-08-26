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
│ --help                                          Show this message and exit.          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
