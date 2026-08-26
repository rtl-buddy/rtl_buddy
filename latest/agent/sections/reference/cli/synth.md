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
