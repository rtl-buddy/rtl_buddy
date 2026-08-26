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
