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
│ test               run a simple test                                                 │
│ randtest           repeat a test with multiple random seeds                          │
│ regression         run rtl regression                                                │
│ filelist           generate filelists using models.yaml                              │
│ hier               render module hierarchy via rtl-buddy-view                        │
│ wave               open waveform viewer for a test                                   │
│ wave-fpv           open SymbiYosys counterexample VCD for a failed FPV verification  │
│ wave-install-nvim  install nvim plugin for rb wave annotation                        │
│ synth              run synthesis                                                     │
│ synth-regression   run synthesis regression                                          │
│ pnr                run place-and-route                                               │
│ power              run power analysis                                                │
│ power-regression   run power analysis regression                                     │
│ saif               convert FST/VCD trace to SAIF v2.0                                │
│ cdc                run CDC lint                                                      │
│ cdc-regression     run CDC lint regression                                           │
│ fpv                run formal property verification                                  │
│ fpv-regression     run FPV regression                                                │
│ tool-check         check installed tool dependencies and subcommand readiness        │
│ axi-profile        profile AXI interconnect performance via rtl-buddy-axi-profiler   │
│ verible            verible commands                                                  │
│ mut                mutation testing                                                  │
│ hub                manage the rtl-buddy-hub daemon                                   │
│ skill              manage the rtl_buddy agent skill                                  │
│ docs               browse bundled documentation                                      │
│ spec               spec traceability commands                                        │
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

## hier

```text
Usage: rtl-buddy hier [OPTIONS] NAME                                                   
                                                                                        
 render module hierarchy via rtl-buddy-view                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  with --view dut (default): model name from models.yaml; with    │
│                      --view tb: test name from tests.yaml (the test pins both the    │
│                      model + the testbench top)                                      │
│                      [required]                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config     -c      TEXT  models.yaml to use [default: models.yaml]           │
│ --test-config              TEXT  tests.yaml to use (--view tb) [default: tests.yaml] │
│ --view                     TEXT  what to render: 'dut' (default) renders the model   │
│                                  hierarchy rooted at --top; 'tb' renders the         │
│                                  testbench hierarchy with the DUT called out as a    │
│                                  subtree. With --view tb the positional argument is  │
│                                  a test name.                                        │
│                                  [default: dut]                                      │
│ --format                   TEXT  output format: tree, dot, mermaid, json             │
│                                  [default: tree]                                     │
│ --output           -o      TEXT  write renderer output to file instead of stdout     │
│ --frontend                 TEXT  parser frontend (verible|slang)                     │
│ --cdc-annotations          TEXT  clock-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-domain-map`                                  │
│ --rdc-annotations          TEXT  reset-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-reset-domain-map`                            │
│ --clock-legend                   dot format only: emit a side legend of clock colors │
│ --tool                     TEXT  path to the rtl-buddy-view binary                   │
│                                  [default: rtl-buddy-view]                           │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

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

## verible

```text
Usage: rtl-buddy verible [OPTIONS] COMMAND [ARGS]...                                   
                                                                                        
 verible commands                                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ lint          run verible-verilog-lint                                               │
│ syntax        run verible-verilog-syntax                                             │
│ format        run verible-verilog-format                                             │
│ preprocessor  run verible-verilog-preprocessor                                       │
│ filelist      generate verible.filelist from models.yaml so verible-verilog-ls can   │
│               resolve cross-file symbols                                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## wave

```text
Usage: rtl-buddy wave [OPTIONS] TEST_NAME                                              
                                                                                        
 open waveform viewer for a test                                                        
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT  name of test to open waveform for [required]               │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config     -c      TEXT  tests.yaml to use [default: tests.yaml]              │
│ --surfer                  TEXT  cfg-surfer entry name [default: surfer-default]      │
│ --resim                         force re-run of debug sim even if FST exists         │
│ --focused-signal                annotate only the signal selected via Go to          │
│                                 declaration; default annotates all signals in scope  │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## wave-install-nvim

```text
Usage: rtl-buddy wave-install-nvim [OPTIONS]                                           
                                                                                        
 install nvim plugin for rb wave annotation                                             
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --force          overwrite existing installation                                     │
│ --help           Show this message and exit.                                         │
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
│ view             Print the bundled rtl_buddy skill to stdout.                        │
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

## spec

```text
Usage: rtl-buddy spec [OPTIONS] COMMAND [ARGS]...                                      
                                                                                        
 spec traceability commands                                                             
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ list            list all spec blocks discovered in the project                       │
│ check-design    show which spec blocks have design models referencing them           │
│ check-coverage  show which spec coverage items are addressed by tests                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## synth

```text
Usage: rtl-buddy synth [OPTIONS] [SYNTH_NAME]                                          
                                                                                        
 run synthesis                                                                          
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   synth_name      [SYNTH_NAME]  name of synthesis to run                             │
│                                 [default: (run all syntheses)]                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --synth-config  -c      TEXT  synth.yaml to use [default: synth.yaml]                │
│ --list                        list syntheses in the selected config and exit         │
│ --effort                TEXT  override synthesis effort (must match                  │
│                               cfg-synth-efforts entry)                               │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## synth-regression

```text
Usage: rtl-buddy synth-regression [OPTIONS]                                            
                                                                                        
 run synthesis regression                                                               
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT     path to synth_regression.yaml                         │
│                                [default: (Use ./synth_regression.yaml if present)]   │
│ --reg-level   -l      INTEGER  synthesis regression level to stop at [default: 0]    │
│ --effort              TEXT     override synthesis effort (must match                 │
│                                cfg-synth-efforts entry)                              │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## pnr

```text
Usage: rtl-buddy pnr [OPTIONS] [PNR_NAME]                                              
                                                                                        
 run place-and-route                                                                    
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   pnr_name      [PNR_NAME]  name of pnr run                                          │
│                             [default: (run all entries in the suite)]                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --pnr-config  -c      TEXT     pnr.yaml to use [default: pnr.yaml]                   │
│ --list                         list pnr runs in the selected config and exit         │
│ --reg-level   -l      INTEGER  run only entries with reglvl at or below this value   │
│                                [default: 0]                                          │
│ --gds                          stream out GDS via KLayout after a successful P&R     │
│ --png                          render a PNG of the routed GDS via KLayout (implies   │
│                                --gds)                                                │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## cdc

```text
Usage: rtl-buddy cdc [OPTIONS] [CDC_NAME]                                              
                                                                                        
 run CDC lint                                                                           
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   cdc_name      [CDC_NAME]  name of CDC analysis to run                              │
│                             [default: (run all analyses)]                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --cdc-config  -c      TEXT  cdc.yaml to use [default: cdc.yaml]                      │
│ --list                      list analyses in the selected config and exit            │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## cdc-regression

```text
Usage: rtl-buddy cdc-regression [OPTIONS]                                              
                                                                                        
 run CDC lint regression                                                                
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT     path to cdc_regression.yaml                           │
│                                [default: (Use ./cdc_regression.yaml if present)]     │
│ --reg-level   -l      INTEGER  CDC regression level to stop at [default: 0]          │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## fpv

```text
Usage: rtl-buddy fpv [OPTIONS] [FPV_NAME]                                              
                                                                                        
 run formal property verification                                                       
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   fpv_name      [FPV_NAME]  name of FPV verification to run                          │
│                             [default: (run all verifications)]                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --fpv-config  -c      TEXT  fpv.yaml to use [default: fpv.yaml]                      │
│ --list                      list verifications in the selected config and exit       │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## fpv-regression

```text
Usage: rtl-buddy fpv-regression [OPTIONS]                                              
                                                                                        
 run FPV regression                                                                     
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT     path to fpv_regression.yaml                           │
│                                [default: (Use ./fpv_regression.yaml if present)]     │
│ --reg-level   -l      INTEGER  FPV regression level to stop at [default: 0]          │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## tool-check

```text
Usage: rtl-buddy tool-check [OPTIONS]                                                  
                                                                                        
 check installed tool dependencies and subcommand readiness                             
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --format                                       TEXT  text | json [default: text]     │
│ --required-for                                 TEXT  check only what `rb             │
│                                                      <subcommand>` needs             │
│ --explain                                      TEXT  show install instructions for a │
│                                                      single tool and exit            │
│ --strict                                             exit non-zero if any required   │
│                                                      tool is missing/outdated        │
│ --include-optional    --no-include-optional          include optional tools          │
│                                                      (default: yes)                  │
│                                                      [default: include-optional]     │
│ --probe-versions      --no-probe-versions            run `<tool> --version` to       │
│                                                      capture installed version       │
│                                                      (default: yes)                  │
│                                                      [default: probe-versions]       │
│ --help                                               Show this message and exit.     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
