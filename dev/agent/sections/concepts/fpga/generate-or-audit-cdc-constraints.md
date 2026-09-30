## Generate or audit CDC constraints

Generate CDC timing exceptions from an analyzed crossing set, or audit an existing XDC:

```bash
rb cdc <name> --emit-constraints --format xdc -o constraints/cdc.xdc
rb cdc <name> --check-xdc constraints/board.xdc
```

Add generated constraints to the run's `xdc` list. `--check-xdc` audits CDC exceptions only; Vivado still validates pins, placement and electrical rules.

Xilinx XPM CDC macros need an `rtl-buddy-cdc` that recognizes the XPM family. Register other known synchronizer primitives with the CDC tool's `--sync-primitive MODULE` extra argument. Use the XDC audit's recognition override only when the engine cannot model a legitimate custom macro.
