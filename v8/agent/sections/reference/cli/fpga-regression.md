## fpga-regression

```text
Usage: rtl-buddy fpga-regression [OPTIONS]

 run FPGA implementation regression

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --reg-config  -c      TEXT     path to fpga_regression.yaml                          │
│                                [default: (Use ./fpga_regression.yaml if present,     │
│                                otherwise root_config.yaml fpga-reg-cfg-path)]        │
│ --reg-level   -l      INTEGER  FPGA regression level to stop at [default: 0]         │
│ --bitstream                    generate bitstreams after route (write_bitstream);    │
│                                off by default — a smoke/timing regression doesn't    │
│                                need bitgen                                           │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
