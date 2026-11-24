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
│ --run-tag               TEXT     namespace this invocation's power output under      │
│                                  artefacts/.runs/<tag>/ with its own tree lock and   │
│                                  log; a netlist-source: pnr run reads the P&R run    │
│                                  `rb pnr --run-tag <tag>` wrote, from a copy taken   │
│                                  into that tree                                      │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
