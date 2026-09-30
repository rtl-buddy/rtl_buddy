## Interrupted runs: warn, cancel, adopt

If the head process dies (late `Ctrl-C`, dropped SSH session, login node reboot), its Slurm jobs keep running. Every `--dispatch slurm` run checks the suite's `artefacts/.dispatch/` records at start-up for other runs whose jobs are still queued. `--orphans` (or `cfg-dispatch.orphans`) chooses what to do with them:

| Policy | Effect |
|---|---|
| `warn` (default) | Logs `dispatch.orphans_found` with the run and job IDs, then submits a fresh fleet. The old jobs keep running. |
| `cancel` | Cancels the old jobs and waits until the queue no longer holds them, then submits. If jobs remain after 30 seconds, nothing is submitted and the run fails with `dispatch.orphans_cancel_failed` naming the IDs to cancel by hand. |
| `adopt` | Submits nothing. Waits on the old jobs, collects their results, and reports them as this run's (`dispatch.orphans_adopted`). |

A run that died while still submitting can never be adopted. `--orphans` has no effect for `local` and `local-parallel`; `--orphans adopt` there is a fatal error.

`adopt` needs exactly one orphan recorded from the same invocation: the same test config, tests, run IDs, per-test plan, builder options, `--extra-sim-timeout`, `--rebuild`, and reservations. Any difference is a fatal error naming the test and field. A `rb randtest` run that draws fresh seeds can never be adopted, and two orphans with live jobs are also fatal.
