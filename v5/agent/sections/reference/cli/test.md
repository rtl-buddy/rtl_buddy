## test

```text
Usage: rtl-buddy test [OPTIONS] [TEST_NAME]                                            
                                                                                        
 run a simple test                                                                      
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   test_name      [TEST_NAME]  name of test [default: (run all tests)]                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config                  -c      TEXT  test_config.yaml to use                 │
│                                              [default: tests.yaml]                   │
│ --list                                       list tests in the selected test-config  │
│                                              and exit                                │
│ --coverage-merge                             merge coverage across selected tests;   │
│                                              uses raw merge for summary/html and     │
│                                              info-process for Coverview              │
│ --coverage-merge-raw                         use raw Verilator merge for merged      │
│                                              summary/html/Coverview                  │
│ --coverage-merge-info-process                use info-process merge for merged       │
│                                              summary/Coverview; HTML merge is not    │
│                                              supported                               │
│ --coverage-html                              generate merged LCOV HTML output in     │
│                                              coverage_merge.html                     │
│ --coverage-coverview                         generate Coverview zip output from      │
│                                              coverage info                           │
│ --coverage-dir-summary                 TEXT  append coverage summary lines for       │
│                                              repo-relative directory prefixes; may   │
│                                              be repeated                             │
│ --coverage-dir-summary-file            TEXT  file containing repo-relative directory │
│                                              prefixes, one per line                  │
│ --rnd-new                      -n            use a randomly generated seed instead   │
│                                              of root config seed                     │
│ --rnd-last                     -l            reuse last generated seed               │
│ --help                                       Show this message and exit.             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
