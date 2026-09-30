## Troubleshooting

Read `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. A failed run deletes `power.rpt` and the netlist copy, so it never leaves an earlier run's numbers.

- **`N instance(s) have no Liberty power data and report 0 W`:** see [Read the unpowered-cell warning](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#read-the-unpowered-cell-warning).
- **`N configured macro input(s) not on disk`:** the run stops. Fix the listed `lib-paths` or `lef-paths` on this entry or the upstream run.
- **`power.spef_rejected`:** the SPEF is unreadable or stale and the estimate was used. Rerun `rb pnr`, then the power run.
- **`phys-model.json has no per-instance breakdown`:** the per-instance report was missing or unreadable. Totals are still recorded and the run passes, but `rb phys instance` has nothing for it.
- **`phys-model.json was not written`:** publishing failed. Any model in the directory belongs to an earlier run.
- **Previous run's per-instance rows could not be withdrawn:** the run stops rather than leave stale rows over cleared reports. Fix the error the message names and rerun.
