## fpga

```text
Usage: rtl-buddy fpga [OPTIONS] [FPGA_NAME]

 run FPGA implementation (synth + place + route)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   fpga_name      [FPGA_NAME]  name of fpga run                                       │
│                               [default: (run all entries in the suite)]              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --fpga-config  -c      TEXT     fpga.yaml to use [default: fpga.yaml]                │
│ --list                          list fpga runs in the selected config and exit       │
│ --reg-level    -l      INTEGER  run only entries with reglvl at or below this value  │
│                                 [default: 0]                                         │
│ --bitstream                     generate a bitstream after route (write_bitstream);  │
│                                 off by default — a smoke/timing run doesn't need     │
│                                 bitgen                                               │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
