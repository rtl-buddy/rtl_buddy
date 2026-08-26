## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns findings in `payload.reservation_advice`. It never edits configuration.

Advice is calculated per test using the peak across runs in this invocation:

- utilization below `over-threshold` suggests a reduction;
- utilization above `near-limit`, `TIMEOUT`, or `OUT_OF_MEMORY` suggests an increase;
- suggestions use peak times `margin`, with floors of 5 minutes and 128 MiB;
- time advice is limited to Verilator because VCS license wait distorts elapsed time;
- memory advice is suppressed when the longest run is shorter than the accounting sample interval, except that an out-of-memory state still suggests an increase;
- `phase` is `sim` or `compile+sim` and `edit_hint` identifies the configuration field that actually controlled the allocation.

Advice records the run count and regression level; do not use a smoke run to shrink a nightly reservation. Apply the provided `edit_hint`, rerun, and confirm the finding clears. Disable reports with `rightsize: {report: false}`. Without `sacct` accounting, dispatch completes but emits no advice.

To inspect accounting manually, query step rows without `sacct -X`:

```bash
sacct -j <jobid> --format=JobID,Elapsed,MaxRSS
```
