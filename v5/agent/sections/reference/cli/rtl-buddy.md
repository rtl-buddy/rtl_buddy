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
