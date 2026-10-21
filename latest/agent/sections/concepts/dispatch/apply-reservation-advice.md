## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and reports suggested edits. It never changes configuration. Disable advice with `rightsize: {report: false}`; it requires accounting data. Query `sacct` without `-X` for step rows.

Advice uses each test's peak across this invocation:

- Below `over-threshold`, reduce the reservation. Lower the threshold to report fewer reductions.
- Above `near-limit`, or after `TIMEOUT` or `OUT_OF_MEMORY`, raise it. Increases appear first because under-reservation fails work.
- Suggestions are peak times `margin`, floored at 5 minutes and 128 MiB.
- Simulation time advice requires Verilator; VCS license waits distort elapsed time. Memory advice needs a run longer than the accounting interval, except after an OOM kill.

Apply the explicit `edit_hint`, rerun, and confirm the finding clears. Use representative levels and seeds; a smoke run cannot size a nightly reservation.

Machine output always includes all findings in `payload.reservation_advice`:

- `edit_hint.file` is absolute; `edit_hint.path` names the controlling YAML field. Environment overrides use `path: env` without a file. Never derive the target from a test name: inheritance and modes can point elsewhere.
- `phase` is `sim`, `compile+sim`, `compile`, or `verilate`.
- Inapplicable `reg_level`, `allocated`, `suggested_total`, and `aggregate_delta` are omitted; treat absence as null. Regression levels are per finding; build jobs have no simulation-level filter. Findings have no `event` field.

A nonempty list logs one INFO `rightsize.summary` with `total` and direction `counts` (`raise`, `reduce`). Individual `rightsize.advice` events require `--debug`.
