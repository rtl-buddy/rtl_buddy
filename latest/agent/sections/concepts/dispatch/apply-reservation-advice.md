## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns the findings in `payload.reservation_advice`. rtl_buddy never edits configuration. Turn the report off with `rightsize: {report: false}`. Without `sacct` accounting there is no advice.

Advice is per test, from the peak across runs in this invocation:

- Utilization below `over-threshold` suggests a reduction. A lower value reports fewer tests: `0.3` lists only tests that used under a third of what they asked for.
- Utilization above `near-limit`, a `TIMEOUT`, or an `OUT_OF_MEMORY` suggests an increase. These are listed first because under-reservation costs failed work.
- Suggestions are peak times `margin`, with floors of 5 minutes and 128 MiB.
- Time advice is given for Verilator only, because VCS license waits distort elapsed time. Memory advice is skipped for runs shorter than the accounting sample interval, except after an out-of-memory kill.
- `phase` is `sim`, `compile+sim`, `compile`, or `verilate`, and `edit_hint` names the field that controlled the allocation.

Do not use a smoke run to shrink a nightly reservation. Apply the `edit_hint`, rerun, and confirm the finding clears. Query `sacct` without `-X` to see step rows.
