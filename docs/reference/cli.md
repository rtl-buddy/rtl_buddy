---
description: Auto-generated CLI reference for all rtl-buddy commands and their options.
---

# CLI Reference

This page is auto-generated from `rtl-buddy --help` output.
Run `python scripts/gen_cli_reference.py` from the repo root to regenerate it.

<!-- AUTO-GENERATED: do not edit below this line manually -->

## rtl-buddy

```text
Usage: rtl-buddy [OPTIONS] COMMAND [ARGS]...                                           
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --debug               -D                                     Print rtl_buddy debug   │
│                                                              details to console      │
│ --verbose             -v                                     Print execution details │
│                                                              to console              │
│ --machine                                                    Emit machine-oriented   │
│                                                              logs and plain console  │
│                                                              output                  │
│ --color                   --no-color                         Logs without ANSI color │
│                                                              codes                   │
│                                                              [default: color]        │
│ --builder-mode        -M                TEXT                 Override default        │
│                                                              builder_mode            │
│ --builder             -B                TEXT                 Override platform       │
│                                                              default builder         │
│ --early-stop          -E                [pre|comp|sim|post]  Run step to stop early  │
│                                                              at                      │
│ --version                                                    Prints version          │
│ --install-completion                                         Install completion for  │
│                                                              the current shell.      │
│ --show-completion                                            Show completion for the │
│                                                              current shell, to copy  │
│                                                              it or customize the     │
│                                                              installation.           │
│ --help                                                       Show this message and   │
│                                                              exit.                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ test        run a simple test                                                        │
│ randtest    repeat a test with multiple random seeds                                 │
│ regression  run rtl regression                                                       │
│ filelist    generate filelists using models.yaml                                     │
│ verible     run verible cmd                                                          │
│ skill       manage the rtl_buddy agent skill                                         │
│ docs        browse bundled documentation                                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

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

## randtest

```text
Usage: rtl-buddy randtest [OPTIONS] TEST_NAME [RND_CNT]                                
                                                                                        
 repeat a test with multiple random seeds                                               
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT       name of test [default: (run all tests)] [required]    │
│      rnd_cnt        [RND_CNT]  number of random iterations to test [default: 2]      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config  -c      TEXT     test_config.yaml to use [default: tests.yaml]        │
│ --rnd-rpt      -r      INTEGER  repeat iteration number from previous run            │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

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

## filelist

```text
Usage: rtl-buddy filelist [OPTIONS] MODEL_NAME [OUTPUT_PATH]                           
                                                                                        
 generate filelists using models.yaml                                                   
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name       TEXT           name of model [required]                        │
│      output_path      [OUTPUT_PATH]  Output filename [default: run.f]                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config  -c      TEXT  model_config.yaml to use [default: models.yaml]        │
│ --unroll        -u            Recursively unroll -F in filelists                     │
│ --flatten       -f            Remove path to a file, leaving just the filename       │
│ --strip         -s            Remove option part of a line                           │
│ --deduplicate   -d            Remove duplicates                                      │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## verible

```text
Usage: rtl-buddy verible [OPTIONS] CMD [VERIBLE_ARGS]...                               
                                                                                        
 run verible cmd                                                                        
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    cmd               TEXT               Verible cmd [required]                     │
│      verible_args      [VERIBLE_ARGS]...                                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## skill

```text
Usage: rtl-buddy skill [OPTIONS] COMMAND [ARGS]...                                     
                                                                                        
 manage the rtl_buddy agent skill                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ install          Install the bundled rtl_buddy skill.                                │
│ uninstall        Remove the installed rtl_buddy skill files from the selected scope. │
│ status           Report whether the skill is installed and whether it matches the    │
│                  current package version.                                            │
│ print-gitignore  Print the gitignore lines for project-level skill installs.         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## docs

```text
Usage: rtl-buddy docs [OPTIONS] COMMAND [ARGS]...                                      
                                                                                        
 browse bundled documentation                                                           
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ list  list bundled documentation pages                                               │
│ show  show a bundled documentation page                                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
