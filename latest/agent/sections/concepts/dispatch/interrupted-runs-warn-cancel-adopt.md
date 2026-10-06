## Interrupted runs: warn, cancel, adopt

If the head process dies (late `Ctrl-C`, dropped SSH session, login node reboot), its Slurm jobs keep running. Every `--dispatch slurm` run checks the suite's `artefacts/.dispatch/` records at start-up for other runs whose jobs are still queued. `--orphans` (or `cfg-dispatch.orphans`) chooses what to do with them:

| Policy | Effect |
|---|---|
| `warn` (default) | Logs `dispatch.orphans_found` with the run and job IDs, then submits a fresh fleet. The old jobs keep running. |
| `cancel` | Cancels the old jobs and waits until the queue no longer holds them, then submits. If jobs remain after 30 seconds, nothing is submitted and the run fails with `dispatch.orphans_cancel_failed` naming the IDs to cancel by hand. |
| `adopt` | Submits nothing. Waits on the old jobs, collects their results, and reports them as this run's (`dispatch.orphans_adopted`). |

A run that died while still submitting can never be adopted. `--orphans` has no effect for `local` and `local-parallel`; `--orphans adopt` there is a fatal error.

A head can also die while it waits on its [coverage tail job](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#run-the-coverage-tail-as-a-job), which keeps writing `cov_dir/`. `rb test`, `rb randtest` and `rb regression` check the command root's `artefacts/.dispatch/coverage/` records at start-up too:

- `cancel` cancels that job before submitting anything, as for a fleet.
- `warn` logs `dispatch.coverage_orphan_found` and leaves it running. This run's coverage tail then waits for it to leave the queue before writing `cov_dir/` (`coverage.tail_awaiting_orphan`). If `max-wait` expires first, this run's tail fails as `tail_failed` (exit 1) and the job is left running; it may still write its own manifest and model into `cov_dir/` afterwards.
- `adopt` logs the same warning, but a coverage job cannot be adopted: the head that would read its answer is gone. `adopt` still needs an interrupted fleet to collect, so with only a coverage job left the run stops with the usual `--orphans adopt found no interrupted run` error. When it does adopt a fleet, the coverage tail waits as under `warn`.
- Under `--run-tag` the records live in that tag's `artefacts/.runs/<tag>/.dispatch/coverage/`, while `cov_dir/` is shared by every tag. A coverage job orphaned under another tag, or untagged, is not seen.
- Without a Slurm backend (`local`, `local-parallel`, and always for a `rb randtest -r` replay) nothing is checked: the tail writes `cov_dir/` even if an earlier Slurm run's coverage job is still running there.

`adopt` needs exactly one orphan recorded from the same invocation: the same test config, tests, run IDs, per-test plan, builder options, `--extra-sim-timeout`, `--rebuild`, and reservations. Any difference is a fatal error naming the test and field. A `rb randtest` run that draws fresh seeds can never be adopted, and two orphans with live jobs are also fatal.
