## elab-regression

```text
Usage: rtl-buddy elab-regression [OPTIONS]

 run named model elaboration profiles

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT                  elab_regression.yaml to use              │
│                                             [default: (Use ./elab_regression.yaml if │
│                                             present, otherwise root_config.yaml      │
│                                             elab-reg-cfg-path)]                      │
│ --reg-level   -l      INTEGER RANGE [x>=0]  regression level to stop at [default: 0] │
│ --dispatch            TEXT                  execution backend (local,                │
│                                             local-parallel, slurm)                   │
│ --jobs        -j      INTEGER               local-parallel process count             │
│ --help                                      Show this message and exit.              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
