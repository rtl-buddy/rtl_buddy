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
│ nvim-install       install/update the unified rtl-buddy-nvim editor plugin (hub +    │
│                    wave annotation)                                                  │
│ wave-install-nvim  alias for nvim-install                                            │
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

## wave-fpv

```text
Usage: rtl-buddy wave-fpv [OPTIONS] VERIF_NAME                                         
                                                                                        
 open SymbiYosys counterexample VCD for a failed FPV verification                       
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    verif_name      TEXT  name of FPV verification to open CEX for [required]       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --fpv-config  -c      TEXT  fpv.yaml to use [default: fpv.yaml]                      │
│ --surfer              TEXT  cfg-surfer entry name [default: surfer-default]          │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## nvim-install

```text
Usage: rtl-buddy nvim-install [OPTIONS]                                                
                                                                                        
 install/update the unified rtl-buddy-nvim editor plugin (hub + wave annotation)        
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --force               remove any existing install and re-clone                       │
│ --update              sync an existing install to the pinned revision                │
│ --ref           TEXT  override the pinned rtl-buddy-nvim git ref (tag/branch)        │
│ --source        TEXT  override the rtl-buddy-nvim repo URL or local path (for        │
│                       offline/dev installs)                                          │
│ --no-lsp              omit the verible-verilog-ls autostart from the managed setup   │
│ --help                Show this message and exit.                                    │
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

## power

```text
Usage: rtl-buddy power [OPTIONS] [POWER_NAME]                                          
                                                                                        
 run power analysis                                                                     
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   power_name      [POWER_NAME]  name of power run                                    │
│                                 [default: (run all entries in the suite)]            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --power-config  -c      TEXT     power.yaml to use [default: power.yaml]             │
│ --list                           list power runs in the selected config and exit     │
│ --reg-level     -l      INTEGER  run only entries with reglvl at or below this value │
│                                  [default: 0]                                        │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## power-regression

```text
Usage: rtl-buddy power-regression [OPTIONS]                                            
                                                                                        
 run power analysis regression                                                          
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT     path to power_regression.yaml                         │
│                                [default: (Use ./power_regression.yaml if present)]   │
│ --reg-level   -l      INTEGER  power regression level to stop at [default: 0]        │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## saif

```text
Usage: rtl-buddy saif [OPTIONS] TRACE OUTPUT                                           
                                                                                        
 convert FST/VCD trace to SAIF v2.0                                                     
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    trace       TEXT  path to input FST or VCD trace [required]                     │
│ *    output      TEXT  path to write SAIF v2.0 file [required]                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
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

## axi-profile discover

```text
Usage: rtl-buddy axi-profile discover [OPTIONS] MODEL_NAME                             
                                                                                        
 parse RTL to (re)generate the model's axi-bundles.yaml manifest                        
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name      TEXT  model from models.yaml [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config  -c      TEXT  models.yaml to use [default: models.yaml]              │
│ --output        -o      TEXT  output path for axi-bundles.yaml (default: the model's │
│                               `axi_bundles:` from models.yaml when set, else         │
│                               artefacts/axi/<model>/axi-bundles.yaml)                │
│ --amend                 TEXT  existing axi-bundles.yaml to merge user edits from     │
│                               (deferred to a follow-up; warns if passed)             │
│ --tool                  TEXT  path to the axi-profiler binary                        │
│                               [default: axi-profiler]                                │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## axi-profile gen-monitor

```text
Usage: rtl-buddy axi-profile gen-monitor [OPTIONS] MODEL_NAME                          
                                                                                        
 emit the SV bind-style AXI monitor for the model's testbench                           
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name      TEXT  model from models.yaml [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config    -c      TEXT     models.yaml to use [default: models.yaml]         │
│ --output          -o      TEXT     output path for the generated SV monitor          │
│                                    (default: the model's `axi_monitor_out:` from     │
│                                    models.yaml)                                      │
│ --time-precision          TEXT     IEEE-1800 timeprecision atom (1ns / 100ps / 1ps / │
│                                    ...). Must match the testbench's `timeprecision.  │
│ --buffer-cap              INTEGER  Per-bundle FIFO depth cap. Drained only at        │
│                                    $finish.                                          │
│ --tool                    TEXT     path to the axi-profiler binary                   │
│                                    [default: axi-profiler]                           │
│ --help                             Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## axi-profile notebook

```text
Usage: rtl-buddy axi-profile notebook [OPTIONS] TEST_NAME                              
                                                                                        
 launch the packaged marimo notebook against a test's per-txn parquet                   
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT  test from tests.yaml [required]                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config  -c              TEXT     tests.yaml to use [default: tests.yaml]      │
│ --port                         INTEGER  TCP port for marimo's edit server (default:  │
│                                         OS-assigned)                                 │
│ --foreground       --daemon             Run marimo in the foreground (default).      │
│                                         --daemon is accepted but currently falls     │
│                                         back to foreground; background detach is a   │
│                                         follow-up.                                   │
│                                         [default: foreground]                        │
│ --headless                              Forward `--headless --no-token` to marimo.   │
│                                         Used by the hub-initiated 'Open in marimo'   │
│                                         flow (Phase 2 of the marimo umbrella) — the  │
│                                         SPA opens the URL itself, so marimo          │
│                                         shouldn't auto-pop a browser and the auth    │
│                                         token is disabled for the loopback-only      │
│                                         handoff.                                     │
│ --marimo                       TEXT     path to the marimo binary (default: 'marimo' │
│                                         on PATH)                                     │
│                                         [default: marimo]                            │
│ --help                                  Show this message and exit.                  │
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

## verible lint

```text
Usage: rtl-buddy verible lint [OPTIONS] [VERIBLE_ARGS]...                              
                                                                                        
 run verible-verilog-lint                                                               
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   verible_args      [VERIBLE_ARGS]...                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## verible syntax

```text
Usage: rtl-buddy verible syntax [OPTIONS] [VERIBLE_ARGS]...                            
                                                                                        
 run verible-verilog-syntax                                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   verible_args      [VERIBLE_ARGS]...                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## verible format

```text
Usage: rtl-buddy verible format [OPTIONS] [VERIBLE_ARGS]...                            
                                                                                        
 run verible-verilog-format                                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   verible_args      [VERIBLE_ARGS]...                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## verible preprocessor

```text
Usage: rtl-buddy verible preprocessor [OPTIONS] [VERIBLE_ARGS]...                      
                                                                                        
 run verible-verilog-preprocessor                                                       
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   verible_args      [VERIBLE_ARGS]...                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## verible filelist

```text
Usage: rtl-buddy verible filelist [OPTIONS]                                            
                                                                                        
 generate verible.filelist from models.yaml so verible-verilog-ls can resolve           
 cross-file symbols                                                                     
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model           TEXT  Model name(s) to include. May be repeated. Default: union of │
│                         every model declared in any models.yaml under the project    │
│                         root.                                                        │
│ --output  -o      TEXT  Output path. Defaults to <project_root>/verible.filelist so  │
│                         verible-verilog-ls auto-discovers it.                        │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## mut

```text
Usage: rtl-buddy mut [OPTIONS] COMMAND [ARGS]...                                       
                                                                                        
 mutation testing                                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ list   enumerate mutation candidate sites without mutating                           │
│ run    generate mutants, score against an FPV proof, report                          │
│ score  recompute mutation score from a saved report                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## mut list

```text
Usage: rtl-buddy mut list [OPTIONS]                                                    
                                                                                        
 enumerate mutation candidate sites without mutating                                    
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --mut-config  -c      TEXT  mut.yaml to use [default: mut.yaml]                      │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## mut run

```text
Usage: rtl-buddy mut run [OPTIONS]                                                     
                                                                                        
 generate mutants, score against an FPV proof, report                                   
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --mut-config  -c      TEXT  mut.yaml to use [default: mut.yaml]                      │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## mut score

```text
Usage: rtl-buddy mut score [OPTIONS] REPORT                                            
                                                                                        
 recompute mutation score from a saved report                                           
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    report      TEXT  path to a mut_report.json from a previous run [required]      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub

```text
Usage: rtl-buddy hub [OPTIONS] COMMAND [ARGS]...                                       
                                                                                        
 manage the rtl-buddy-hub daemon                                                        
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ start                  start the rtl-buddy-hub daemon for this project               │
│ stop                   ask the running hub to shut down                              │
│ status                 print the running hub's discovery record                      │
│ log                    tail the hub log                                              │
│ install-launchagent    install the macOS LaunchAgent so the hub auto-starts at login │
│ uninstall-launchagent  remove the macOS LaunchAgent                                  │
│ config                 hub.toml utilities                                            │
│ send                   One-shot peer for the running rtl-buddy-hub. Connects as      │
│                        origin=cli.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub start

```text
Usage: rtl-buddy hub start [OPTIONS]                                                   
                                                                                        
 start the rtl-buddy-hub daemon for this project                                        
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --foreground       --daemon                                    Run in the foreground │
│                                                                (default).            │
│                                                                [default: foreground] │
│ --serve-viewer     --no-serve-viewer                           Also serve the viewer │
│                                                                HTTP+WebSocket layer  │
│                                                                at the http_port.     │
│                                                                When no               │
│                                                                --viewer-bundle is    │
│                                                                given, the hub        │
│                                                                auto-discovers the    │
│                                                                SPA shipped by        │
│                                                                rtl-buddy-view (if    │
│                                                                installed) and falls  │
│                                                                back to a placeholder │
│                                                                page if neither is    │
│                                                                available.            │
│                                                                [default:             │
│                                                                no-serve-viewer]      │
│ --viewer-bundle                         PATH                   Override the          │
│                                                                auto-discovered SPA   │
│                                                                with this path        │
│                                                                (directory containing │
│                                                                index.html, or a path │
│                                                                to a single           │
│                                                                index.html). Use this │
│                                                                when iterating on the │
│                                                                SPA from a checkout — │
│                                                                the auto-discovered   │
│                                                                bundle ships with the │
│                                                                installed wheel and   │
│                                                                won't reflect         │
│                                                                uncommitted viewer/   │
│                                                                changes. Only used    │
│                                                                with --serve-viewer.  │
│ --listen-port                           INTEGER RANGE          TCP port for adapter  │
│                                         [0<=x<=65535]          peers (nvim, rb       │
│                                                                wave). Overrides      │
│                                                                .listen_port from     │
│                                                                hub.toml. 0 =         │
│                                                                OS-assigned. Pin to a │
│                                                                specific number so    │
│                                                                peers' discovery      │
│                                                                records stay stable   │
│                                                                across restarts.      │
│ --http-port                             INTEGER RANGE          HTTP/WS port for the  │
│                                         [0<=x<=65535]          browser-side SPA.     │
│                                                                Overrides .http_port  │
│                                                                from hub.toml. 0 =    │
│                                                                OS-assigned. Pin to a │
│                                                                specific number so    │
│                                                                the SPA URL stays the │
│                                                                same across restarts. │
│                                                                Only used with        │
│                                                                --serve-viewer.       │
│ --model                                 TEXT                   Generate view.json on │
│                                                                hub start for this    │
│                                                                model name (looked up │
│                                                                in models.yaml).      │
│                                                                Replaces the legacy   │
│                                                                workflow of running   │
│                                                                `rb hier <model>      │
│                                                                --format json -o      │
│                                                                .rtl-buddy/view.json` │
│                                                                manually before each  │
│                                                                hub start. When unset │
│                                                                the hub falls back to │
│                                                                .view_json from       │
│                                                                hub.toml. Requires    │
│                                                                --serve-viewer.       │
│ --models-file                           PATH                   Explicit models.yaml  │
│                                                                that owns the --model │
│                                                                entry. Skips the      │
│                                                                project-tree          │
│                                                                discovery walk. Use   │
│                                                                this to disambiguate  │
│                                                                when the same model   │
│                                                                name exists in more   │
│                                                                than one models.yaml. │
│ --axi-perf-from                         PATH                   Path to an            │
│                                                                axi-perf.json (output │
│                                                                of `rb axi-profile    │
│                                                                run`). The hub bakes  │
│                                                                its                   │
│                                                                per-bundle/interconn… │
│                                                                throughput overlay    │
│                                                                into every generated  │
│                                                                view.json AND records │
│                                                                the source's          │
│                                                                test/suite_dir so the │
│                                                                SPA's 'Open in        │
│                                                                marimo' button skips  │
│                                                                its prompt. Use the   │
│                                                                canonical             │
│                                                                <suite>/artefacts/ax… │
│                                                                layout so the         │
│                                                                test/suite_dir        │
│                                                                derivation lands.     │
│                                                                Only used with        │
│                                                                --serve-viewer.       │
│ --help                                                         Show this message and │
│                                                                exit.                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub stop

```text
Usage: rtl-buddy hub stop [OPTIONS]                                                    
                                                                                        
 ask the running hub to shut down                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub status

```text
Usage: rtl-buddy hub status [OPTIONS]                                                  
                                                                                        
 print the running hub's discovery record                                               
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub log

```text
Usage: rtl-buddy hub log [OPTIONS]                                                     
                                                                                        
 tail the hub log                                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --lines  -n                 INTEGER  Trailing lines to print before following.       │
│                                      [default: 50]                                   │
│          -f  --no-follow             Follow the log (tail -f). [default: no-follow]  │
│ --help                               Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub install-launchagent

```text
Usage: rtl-buddy hub install-launchagent [OPTIONS]                                     
                                                                                        
 install the macOS LaunchAgent so the hub auto-starts at login                          
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub uninstall-launchagent

```text
Usage: rtl-buddy hub uninstall-launchagent [OPTIONS]                                   
                                                                                        
 remove the macOS LaunchAgent                                                           
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub config

```text
Usage: rtl-buddy hub config [OPTIONS] COMMAND [ARGS]...                                
                                                                                        
 hub.toml utilities                                                                     
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ validate  schema-check .rtl-buddy/hub.toml                                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub config validate

```text
Usage: rtl-buddy hub config validate [OPTIONS]                                         
                                                                                        
 schema-check .rtl-buddy/hub.toml                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --path        PATH  Override the default project hub.toml path.                      │
│ --help              Show this message and exit.                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send

```text
Usage: rtl-buddy hub send [OPTIONS] COMMAND [ARGS]...                                  
                                                                                        
 One-shot peer for the running rtl-buddy-hub. Connects as origin=cli.                   
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ select         Broadcast selection_changed{instance_path}.                           │
│ signal         Broadcast signal_selected{signal, wave_scope}.                        │
│ cursor         Broadcast cursor_time_changed{t_fs}.                                  │
│ scope          Broadcast scope_changed{wave_scope}.                                  │
│ open           Broadcast source_focused{file, line, col}.                            │
│ diagnose       Push a diagnostics_set bundle for SOURCE. Each ITEM is                │
│                <file>:<line>:<severity>:<code>:<message>. --clear sends an empty set │
│                (clears any cached diagnostics from SOURCE). Use --instance to attach │
│                a view.json instance_path hint that consumers (the SPA's on-canvas    │
│                badge layer in particular) use as a fast path instead of the          │
│                file+line resolver.                                                   │
│ state          Snapshot the hub's cached state (active model, selection, cursor,     │
│                scope, peers).                                                        │
│ wave-add       Ask the wave peer (surfer) to add one or more signals to the view.    │
│ wave-cursor    Ask the wave peer (surfer) to move its cursor to T_FS.                │
│ wave-scope     Ask the wave peer (surfer) to switch its active scope without         │
│                populating the variable panel (maps to WCP set_scope).                │
│ wave-pan       Pan surfer's viewport to center on T_FS (zoom unchanged). Maps to WCP │
│                set_viewport_to.                                                      │
│ wave-zoom      Zoom + pan surfer to fit [START_FS, END_FS]. Maps to WCP              │
│                set_viewport_range.                                                   │
│ wave-zoom-fit  Zoom surfer out to fit the whole waveform. Maps to WCP zoom_to_fit.   │
│ view-pan       Ask the view peer (SPA) to pan/center on INSTANCE_PATH.               │
│ overlay        Flip an overlay's enabled state on the SPA. Built-in NAMES are        │
│                'clock', 'reset', 'axi-perf', 'wave'; an unknown name is a no-op. Use │
│                --on / --off (default --on). Useful for agents or scripted demos that │
│                want to direct the user's attention to a specific overlay layer       │
│                without a UI click.                                                   │
│ capture        Ask the view peer (SPA) to snapshot the current graph and write it to │
│                --out. Graph-only — surrounding panels are not captured. Useful for   │
│                agents that want to look at what the user is seeing without a browser │
│                screenshot tool.                                                      │
│ open-source    Ask the src peer (nvim) to open FILE at line+col.                     │
│ resolve        resolve coordinates via the hub's view.json + tb_prefix mapping       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send select

```text
Usage: rtl-buddy hub send select [OPTIONS] INSTANCE_PATH                               
                                                                                        
 Broadcast selection_changed{instance_path}.                                            
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    instance_path      TEXT  view.json instance_path, e.g. top.u_fifo.u_wr_ptr      │
│                               [required]                                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send signal

```text
Usage: rtl-buddy hub send signal [OPTIONS] SIGNAL                                      
                                                                                        
 Broadcast signal_selected{signal, wave_scope}.                                         
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    signal      TEXT  signal name, e.g. wr_ptr_q [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --wave-scope        TEXT  surfer/VCD scope owning the signal, e.g. tb.dut.u_fifo  │
│                              [required]                                              │
│    --help                    Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send cursor

```text
Usage: rtl-buddy hub send cursor [OPTIONS] T_FS                                        
                                                                                        
 Broadcast cursor_time_changed{t_fs}.                                                   
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    t_fs      INTEGER  cursor time in femtoseconds (decimal integer) [required]     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send scope

```text
Usage: rtl-buddy hub send scope [OPTIONS] WAVE_SCOPE                                   
                                                                                        
 Broadcast scope_changed{wave_scope}.                                                   
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    wave_scope      TEXT  surfer/VCD scope, e.g. tb.dut.u_fifo [required]           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send open

```text
Usage: rtl-buddy hub send open [OPTIONS] SPEC                                          
                                                                                        
 Broadcast source_focused{file, line, col}.                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    spec      TEXT  file:line[:col], e.g. design/dma/dma.sv:42:7 [required]         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send diagnose

```text
Usage: rtl-buddy hub send diagnose [OPTIONS] SOURCE [ITEMS]...                         
                                                                                        
 Push a diagnostics_set bundle for SOURCE. Each ITEM is                                 
 <file>:<line>:<severity>:<code>:<message>. --clear sends an empty set (clears any      
 cached diagnostics from SOURCE). Use --instance to attach a view.json instance_path    
 hint that consumers (the SPA's on-canvas badge layer in particular) use as a fast path 
 instead of the file+line resolver.                                                     
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    source      TEXT        producer key (e.g. 'rtl-buddy-cdc', 'claude-analysis'); │
│                              latest-writer-wins per source on the hub's cache        │
│                              [required]                                              │
│      items       [ITEMS]...  <file>:<line>:<sev>:<code>:<msg> ...                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --clear                 Send an empty items list (clears SOURCE).                    │
│ --instance        TEXT  Optional view.json instance_path to attach to every ITEM in  │
│                         this push. Use when the producer knows which instance a      │
│                         finding pertains to (most one-shot agent calls do); skip for │
│                         batch lint output where each item lives at a different       │
│                         file:line.                                                   │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send state

```text
Usage: rtl-buddy hub send state [OPTIONS]                                              
                                                                                        
 Snapshot the hub's cached state (active model, selection, cursor, scope, peers).       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-add

```text
Usage: rtl-buddy hub send wave-add [OPTIONS] VARIABLES...                              
                                                                                        
 Ask the wave peer (surfer) to add one or more signals to the view.                     
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    variables      VARIABLES...  fully-scoped variable names, e.g.                  │
│                                   tb.dut.u_fifo.wr_ptr_q                             │
│                                   [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-cursor

```text
Usage: rtl-buddy hub send wave-cursor [OPTIONS] T_FS                                   
                                                                                        
 Ask the wave peer (surfer) to move its cursor to T_FS.                                 
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    t_fs      INTEGER  cursor time in femtoseconds (decimal integer) [required]     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-scope

```text
Usage: rtl-buddy hub send wave-scope [OPTIONS] WAVE_SCOPE                              
                                                                                        
 Ask the wave peer (surfer) to switch its active scope without populating the variable  
 panel (maps to WCP set_scope).                                                         
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    wave_scope      TEXT  surfer/VCD scope [required]                               │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-pan

```text
Usage: rtl-buddy hub send wave-pan [OPTIONS] T_FS                                      
                                                                                        
 Pan surfer's viewport to center on T_FS (zoom unchanged). Maps to WCP set_viewport_to. 
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    t_fs      INTEGER  center time in femtoseconds [required]                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-zoom

```text
Usage: rtl-buddy hub send wave-zoom [OPTIONS] START_FS END_FS                          
                                                                                        
 Zoom + pan surfer to fit [START_FS, END_FS]. Maps to WCP set_viewport_range.           
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    start_fs      INTEGER  range start in femtoseconds [required]                   │
│ *    end_fs        INTEGER  range end in femtoseconds [required]                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send wave-zoom-fit

```text
Usage: rtl-buddy hub send wave-zoom-fit [OPTIONS]                                      
                                                                                        
 Zoom surfer out to fit the whole waveform. Maps to WCP zoom_to_fit.                    
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send view-pan

```text
Usage: rtl-buddy hub send view-pan [OPTIONS] INSTANCE_PATH                             
                                                                                        
 Ask the view peer (SPA) to pan/center on INSTANCE_PATH.                                
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    instance_path      TEXT  view.json instance_path [required]                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send overlay

```text
Usage: rtl-buddy hub send overlay [OPTIONS] NAME                                       
                                                                                        
 Flip an overlay's enabled state on the SPA. Built-in NAMES are 'clock', 'reset',       
 'axi-perf', 'wave'; an unknown name is a no-op. Use --on / --off (default --on).       
 Useful for agents or scripted demos that want to direct the user's attention to a      
 specific overlay layer without a UI click.                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  overlay name [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --on      --off      Enable (default) or disable the named overlay. [default: on]    │
│ --help               Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send capture

```text
Usage: rtl-buddy hub send capture [OPTIONS]                                            
                                                                                        
 Ask the view peer (SPA) to snapshot the current graph and write it to --out.           
 Graph-only — surrounding panels are not captured. Useful for agents that want to look  
 at what the user is seeing without a browser screenshot tool.                          
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --out      -o      PATH                         Destination file. Extension       │
│                                                    determines format if --format     │
│                                                    omitted.                          │
│                                                    [required]                        │
│    --format   -f      TEXT                         png (default) or svg. Inferred    │
│                                                    from --out suffix if not given.   │
│    --scale            FLOAT RANGE [0.1<=x<=8.0]    PNG upscale factor (1.0 =         │
│                                                    native). Ignored for SVG.         │
│                                                    [default: 1.0]                    │
│    --timeout          FLOAT RANGE [1.0<=x<=120.0]  Seconds to wait for the SPA to    │
│                                                    reply. Large designs may need     │
│                                                    longer.                           │
│                                                    [default: 15.0]                   │
│    --help                                          Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send open-source

```text
Usage: rtl-buddy hub send open-source [OPTIONS] SPEC                                   
                                                                                        
 Ask the src peer (nvim) to open FILE at line+col.                                      
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    spec      TEXT  file:line[:col], e.g. design/dma/dma.sv:42:7 [required]         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send resolve

```text
Usage: rtl-buddy hub send resolve [OPTIONS] COMMAND [ARGS]...                          
                                                                                        
 resolve coordinates via the hub's view.json + tb_prefix mapping                        
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ view-to-wave    instance_path → wave_scope                                           │
│ wave-to-view    wave_scope → instance_path                                           │
│ signal-to-view  signal + wave_scope → driver instance_path(s) and driven port        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send resolve view-to-wave

```text
Usage: rtl-buddy hub send resolve view-to-wave [OPTIONS] INSTANCE_PATH                 
                                                                                        
 instance_path → wave_scope                                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    instance_path      TEXT  view.json instance_path [required]                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send resolve wave-to-view

```text
Usage: rtl-buddy hub send resolve wave-to-view [OPTIONS] WAVE_SCOPE                    
                                                                                        
 wave_scope → instance_path                                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    wave_scope      TEXT  surfer/VCD wave_scope [required]                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## hub send resolve signal-to-view

```text
Usage: rtl-buddy hub send resolve signal-to-view [OPTIONS] SIGNAL                      
                                                                                        
 signal + wave_scope → driver instance_path(s) and driven port                          
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    signal      TEXT  signal name (e.g. wr_ptr_q) [required]                        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --wave-scope        TEXT  enclosing wave scope [required]                         │
│    --help                    Show this message and exit.                             │
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

## skill install

```text
Usage: rtl-buddy skill install [OPTIONS]                                               
                                                                                        
 Install the bundled rtl_buddy skill.                                                   
                                                                                        
 Default scope is user-level (`~/.claude/skills/rtl_buddy/` and                         
 `~/.codex/skills/rtl_buddy/`). Use `--project` to install into the                     
 discovered project root instead; project-level copies take precedence                  
 over user-level when both exist. Use `--dir PATH` to write a single                    
 `PATH/rtl_buddy/SKILL.md` directly, bypassing the `.claude`/`.agents`                  
 layout entirely.                                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --project                   install into the discovered project root instead of the  │
│                             user home                                                │
│ --root                PATH  explicit target root (implies project-level layout)      │
│ --dir                 PATH  write a single flat target at <DIR>/rtl_buddy/SKILL.md,  │
│                             bypassing the .claude/.agents/.codex layout              │
│ --no-claude                 skip writing the Claude Code target                      │
│ --no-codex                  skip writing the Codex target                            │
│ --no-gitignore              skip updating .gitignore on project-level installs       │
│ --dry-run                   print what would be written and exit                     │
│ --force                     overwrite even when content matches                      │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## skill uninstall

```text
Usage: rtl-buddy skill uninstall [OPTIONS]                                             
                                                                                        
 Remove the installed rtl_buddy skill files from the selected scope.                    
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --project                uninstall from the discovered project root instead of the   │
│                          user home                                                   │
│ --root             PATH  explicit target root (implies project-level layout)         │
│ --no-claude              skip the Claude Code target                                 │
│ --no-codex               skip the Codex target                                       │
│ --help                   Show this message and exit.                                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## skill status

```text
Usage: rtl-buddy skill status [OPTIONS]                                                
                                                                                        
 Report whether the skill is installed and whether it matches the current package       
 version.                                                                               
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --project              report status for the discovered project root instead of the  │
│                        user home                                                     │
│ --root           PATH  explicit target root (implies project-level layout)           │
│ --help                 Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## skill view

```text
Usage: rtl-buddy skill view [OPTIONS]                                                  
                                                                                        
 Print the bundled rtl_buddy skill to stdout.                                           
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## skill print-gitignore

```text
Usage: rtl-buddy skill print-gitignore [OPTIONS]                                       
                                                                                        
 Print the gitignore lines for project-level skill installs.                            
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
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

## docs list

```text
Usage: rtl-buddy docs list [OPTIONS]                                                   
                                                                                        
 list bundled documentation pages                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## docs show

```text
Usage: rtl-buddy docs show [OPTIONS] SLUG                                              
                                                                                        
 show a bundled documentation page                                                      
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    slug      TEXT  MkDocs path slug or slug#section-anchor, for example            │
│                      concepts/root-config or agents#local-docs-access                │
│                      [required]                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
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

## spec list

```text
Usage: rtl-buddy spec list [OPTIONS]                                                   
                                                                                        
 list all spec blocks discovered in the project                                         
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --spec-dir        TEXT  Directory to search for specs.yaml files                     │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## spec check-design

```text
Usage: rtl-buddy spec check-design [OPTIONS]                                           
                                                                                        
 show which spec blocks have design models referencing them                             
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --spec-dir          TEXT  Directory to search for specs.yaml files                   │
│ --design-dir        TEXT  Directory to search for models.yaml files                  │
│ --help                    Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

## spec check-coverage

```text
Usage: rtl-buddy spec check-coverage [OPTIONS]                                         
                                                                                        
 show which spec coverage items are addressed by tests                                  
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --spec-dir         TEXT  Directory to search for specs.yaml files                    │
│ --verif-dir        TEXT  Directory to search for tests.yaml files                    │
│ --help                   Show this message and exit.                                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
