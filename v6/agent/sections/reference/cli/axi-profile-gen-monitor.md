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
