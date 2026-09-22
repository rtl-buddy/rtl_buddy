## Read the unpowered-cell warning

An instance whose master no Liberty covers reports `0.00e+00` in all four columns, and nothing in the report distinguishes that from a cell that genuinely burns nothing. A run that finds one says so:

```text
power run "demo_power_macro": 1 instance(s) have no Liberty power data and report 0 W — sram_32x256.
```

The same run's description carries the qualifier, and `--machine` carries `unpowered_cells`, `unpowered_cell_count` and `unpowered_instance_count`. All three are absent from a run that found nothing, so a consumer reading `unpowered_cells` is reading a total that does not cover the whole design.

The verdict does not change. The watts reported are a real measurement of everything that had a library, and a design that carries a LEF-only macro on purpose — an ORFS `fakeram45`, a placeholder — would otherwise have no way to pass. Gate on `unpowered_cell_count` where the flow signs off on power.

A cell is reported only when all three hold: its master is declared in none of the Liberty files the generated script read, its total power is exactly zero, and it is not one of the PDK's `fill-cells`. The second keeps an unusual Liberty spelling from turning a whole standard-cell library into a warning; the first keeps a flop that really does sit at zero out of it; the third keeps the fill instances `filler_placement` left in the routed database out of it, which on the sky130 pipeclean is 26 076 instances against one SRAM. Which instances they are is in `power_instances.rpt` and in `phys-model.json`, with the cell beside each.
