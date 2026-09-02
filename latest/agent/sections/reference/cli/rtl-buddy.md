## rtl-buddy

```text
Usage: rtl-buddy [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --debug               -D                                      Print rtl_buddy debug  │
│                                                               details to console     │
│ --verbose             -v                                      Print execution        │
│                                                               details to console     │
│ --machine                                                     Emit machine-oriented  │
│                                                               logs and plain console │
│                                                               output                 │
│ --color                   --no-color                          Logs without ANSI      │
│                                                               color codes            │
│                                                               [default: color]       │
│ --builder-mode        -M                TEXT                  Override default       │
│                                                               builder_mode           │
│ --builder             -B                TEXT                  Override platform      │
│                                                               default builder        │
│ --extra-sim-timeout                     INTEGER RANGE [x>=0]  Seconds to add to      │
│                                                               every test's           │
│                                                               sim_timeout,           │
│                                                               overriding the         │
│                                                               builder's              │
│                                                               extra-sim-timeout      │
│ --early-stop          -E                [pre|comp|sim|post]   Run step to stop early │
│                                                               at                     │
│ --version                                                     Prints version         │
│ --install-completion                                          Install completion for │
│                                                               the current shell.     │
│ --show-completion                                             Show completion for    │
│                                                               the current shell, to  │
│                                                               copy it or customize   │
│                                                               the installation.      │
│ --help                                                        Show this message and  │
│                                                               exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ test               run a simple test                                                 │
│ randtest           repeat a test with multiple random seeds                          │
│ regression         run rtl regression                                                │
│ elab               elaborate a model with pyslang                                    │
│ elab-regression    run named model elaboration profiles                              │
│ filelist           generate filelists using models.yaml                              │
│ hier               render module hierarchy via rtl-buddy-view                        │
│ hier-query         query the module hierarchy via rtl-buddy-view (find-module,       │
│                    subtree, instances-of, port-connections, source-snippet); JSON on │
│                    stdout                                                            │
│ mcp                serve the design knowledge graph and hierarchy queries over the   │
│                    Model Context Protocol (stdio); needs the 'mcp' extra             │
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
│ fpga               run FPGA implementation (synth + place + route)                   │
│ fpga-regression    run FPGA implementation regression                                │
│ saif               convert FST/VCD trace to SAIF v2.0                                │
│ lint               run style lint (verible)                                          │
│ lint-regression    run style lint regression                                         │
│ fpv                run formal property verification                                  │
│ fpv-regression     run FPV regression                                                │
│ tool-check         check installed tool dependencies and subcommand readiness        │
│ graph              build the design knowledge graph                                  │
│ cov                query coverage artefacts already on disk                          │
│ axi-profile        profile AXI interconnect performance via rtl-buddy-axi-profiler   │
│ verible            verible commands                                                  │
│ mut                mutation testing                                                  │
│ hub                manage the rtl-buddy-hub daemon                                   │
│ skill              manage the rtl_buddy agent skill                                  │
│ docs               browse bundled documentation                                      │
│ spec               spec traceability commands                                        │
│ xplr               design-space exploration experiment ledger (agent-facing)         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
