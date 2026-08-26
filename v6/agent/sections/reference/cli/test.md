## test

```text
Usage: rtl-buddy test [OPTIONS] [TEST_NAME]...

 run a simple test

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   test_name      [TEST_NAME]...  names of tests [default: (run all tests)]           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config                  -c      TEXT     test_config.yaml to use              │
│                                                 [default: tests.yaml]                │
│ --list                                          list tests in the selected           │
│                                                 test-config and exit                 │
│ --filter                               TEXT     case-sensitive Python regex matched  │
│                                                 against configured test names        │
│ --coverage-merge                                merge coverage across selected       │
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
│ --coverage-dir-summary                 TEXT     append coverage summary lines for    │
│                                                 repo-relative directory prefixes;    │
│                                                 may be repeated                      │
│ --coverage-dir-summary-file            TEXT     file containing repo-relative        │
│                                                 directory prefixes, one per line     │
│ --rnd-new                      -n               use a randomly generated seed        │
│                                                 instead of root config seed          │
│ --rnd-last                     -l               reuse last generated seed            │
│ --share-build                                   reuse one compiled simv across tests │
│                                                 with identical compile inputs        │
│                                                 (Verilator builders only)            │
│ --reg-level                            INTEGER  regression level to stop at          │
│ --start-level                          INTEGER  regression level to start at         │
│ --dispatch                             TEXT     execution backend for the test run   │
│                                                 (local, local-parallel, slurm);      │
│                                                 opt-in per run —                     │
│                                                 cfg-dispatch.backend does not        │
│                                                 redirect rb test                     │
│                                                 [default: (local)]                   │
│ --jobs                         -j      INTEGER  concurrent jobs for --dispatch       │
│                                                 local-parallel                       │
│                                                 [default: (cfg-dispatch jobs, else   │
│                                                 min(4, cpu count))]                  │
│ --help                                          Show this message and exit.          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
