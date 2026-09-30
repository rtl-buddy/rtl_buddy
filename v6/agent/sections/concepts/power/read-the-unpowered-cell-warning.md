## Read the unpowered-cell warning

An instance whose master no Liberty covers reports `0.00e+00` in every column, the same as a cell that burns nothing. A run that finds one warns:

```text
power run "demo_power_macro": 1 instance(s) have no Liberty power data and report 0 W — sram_32x256.
```

`--machine` output adds `unpowered_cells`, `unpowered_cell_count` and `unpowered_instance_count`; all three are absent when nothing was found. To fix it, supply the cell's Liberty as described in [Give hard macros a library](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#give-hard-macros-a-library).

The verdict does not change: the reported watts are a real measurement of everything that had a library, and a design with a LEF-only macro on purpose, such as a `fakeram45` placeholder, could not otherwise pass. Gate on `unpowered_cell_count` if your flow signs off on power. Fill cells listed in the PDK's `fill-cells` are not reported.
