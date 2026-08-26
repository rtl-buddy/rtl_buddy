## Generate or audit CDC constraints

Generate CDC timing exceptions from an analyzed crossing set, or check an existing XDC:

```bash
rb cdc <name> --emit-constraints --format xdc -o constraints/cdc.xdc
rb cdc <name> --check-xdc constraints/board.xdc
```

Add generated constraints to the run's `xdc` list. `--check-xdc` audits CDC exceptions only; Vivado remains responsible for pin, placement, and electrical validation.

Xilinx XPM CDC macros require a compatible `rtl-buddy-cdc` that recognizes the XPM family. Register other known synchronizer primitives through the CDC tool's `--sync-primitive MODULE` extra argument. Use the XDC audit's recognition override only when the engine cannot model a legitimate custom macro.
