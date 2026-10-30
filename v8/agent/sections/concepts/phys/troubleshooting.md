## Troubleshooting

- **`phys: no phys-manifest.json under <root>` or `in <dir>` (exit 2).** No run has published a model there. Run `rb synth` or `rb power`.
- **Unknown module or instance (exit 2).** The error lists close candidates.
- **`phys: cannot read <file>`, or a manifest or model refused by name.** The JSON is unreadable or has the wrong shape. Rerun the producer. A `null` half is valid.
- **`phys-model.json has no per-module breakdown` (synthesis) or `per-instance breakdown` (power).** The stat dump or instance report was missing. Totals are kept, but `rb phys module` or `instance` has nothing for that half. Rerun the flow.
- **`phys-model.json was not written`.** Publishing failed. Files in that directory belong to an earlier run.
- **`the previous run's module rows` or `per-instance rows could not be withdrawn`.** The run stops so stale rows are not left over new results. Fix the write error and rerun.
- **Power run warns that the halves cannot pair.** The netlist hashes differ or are missing. Rerun the synthesis and the power run against the same netlist.
