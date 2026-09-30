## Exclude cells with dont-use-cells

`dont-use-cells` is a list of cell names or patterns on the PDK, read by both `rb pnr` and `rb synth`. P&R applies it before the floorplan, so no placement, repair or CTS step chooses a listed cell. Synthesis passes it to Yosys and, on the OpenROAD backend, to resynthesis.

- Write one pattern per entry. Only `*` and `?` are allowed. An entry with whitespace or a Tcl metacharacter (`[ ] { } $ " \ ;`) is rejected when the config loads.
- A synth or P&R platform can add its own list, appended to the PDK's. A platform's list reaches only its own flow; the PDK's reaches both.

With any cell excluded, the flow checks the routed design after detailed routing, before any output is written. It prints each offending instance to `pnr.log` as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run, which catches a cell the synthesis netlist already instantiated. An `xfail:` marker can excuse it.

A pattern that matches no Liberty cell excludes nothing. OpenROAD reports `STA-0122 cell '<pattern>' not found` and rb logs a warning naming the pattern.
