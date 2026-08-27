## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns findings in `payload.reservation_advice`. It never edits configuration.

Advice is calculated per test using the peak across runs in this invocation:

- utilization below `over-threshold` suggests a reduction;
- utilization above `near-limit`, `TIMEOUT`, or `OUT_OF_MEMORY` suggests an increase;
- suggestions use peak times `margin`, with floors of 5 minutes and 128 MiB;
- time advice is limited to Verilator because VCS license wait distorts elapsed time;
- memory advice is suppressed when the longest run is shorter than the accounting sample interval, except that an out-of-memory state still suggests an increase;
- `phase` is `sim`, `compile+sim`, or `compile` and `edit_hint` identifies the configuration field that actually controlled the allocation.

The suite's build job gets one row of its own, named `(build job)` with `phase: compile`. It suggests `time` in both directions and `cpus` only downwards, because low CPU efficiency there means build slots idled rather than that more were needed. Its `cpus` suggestion is per build, and it appears only for a job that ran one build at a time: with `compile.parallel` above 1 the job's CPU efficiency also carries idle slots in the tail, which no accounting field separates from a compile that under-used its own CPUs, so the row is withheld with reason `parallel-utilization-ambiguous` rather than advising a reduction that could starve the longest compile. Size `compile.parallel` against the suite's distinct compile keys first, then read the `cpus` row from a `parallel: 1` run. `time` advice is unaffected — concurrent builds take the wall clock of the longest, not of their sum. A `reduce` is withheld when nothing actually compiled (every build reused its stamp), when the head has no per-build records to judge by — `no-build-records`, which covers both a job that left no envelope at all and one whose envelope carries no `builds` list (an older build job, or one whose telemetry could not be serialised) — or when it finished inside one accounting interval; `rightsize.build_advice_withheld` records which, along with how many records it saw and how many seconds of them were real compiling. Without that guard a re-run of an unchanged suite would advise a limit the next real RTL change times out against, cancelling the fan-out behind it.

Advice records the run count and regression level; do not use a smoke run to shrink a nightly reservation. Apply the provided `edit_hint`, rerun, and confirm the finding clears. Disable reports with `rightsize: {report: false}`. Without `sacct` accounting, dispatch completes but emits no advice.

To inspect accounting manually, query step rows without `sacct -X`:

```bash
sacct -j <jobid> --format=JobID,Elapsed,MaxRSS
```
