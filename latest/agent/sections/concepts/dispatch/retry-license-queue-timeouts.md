## Retry license-queue timeouts

Retry is disabled until `retry.attempts` is nonzero. It applies to simulation jobs only and retries a missing result only when evidence identifies a VCS license wait:

- Slurm state is `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED`; `FAILED` and `CANCELLED` are not retried.
- Captured output ends in license-queue banner content after the last `-licqueue` marker.
- The suite build job succeeded, so the shared-build stamp is available.

For `local-parallel`, queue evidence is sufficient because there is no scheduler state. Build jobs are never retried.

Delay for retry number `n` is `min(backoff-max-sec, backoff-sec * 2^(n-1))`, multiplied by jitter. Slurm holds retries with `--begin`; local-parallel holds them outside the worker pool. User `sbatch-args` occur last, so a user `--begin` overrides retry backoff. Two flags are exceptions, both on the build job and for the same reason — the shared-build dedup must not be droppable by accident. Its `--dependency` is emitted after `sbatch-args` and carries the configured expression (the *last* one, since that is the one Slurm obeys), and its `--job-name` is emitted after them too, because that name is what `--dependency=singleton` serialises on. So `sbatch-args` cannot rename the build job; a `--job-name` / `-J` there still renames simulation jobs.

`max-wait` bounds each collection round, not the total run, and excludes the requested backoff. An exhausted retry remains a failure. A retry submission failure logs `dispatch.retry_abandoned` and preserves the already-scored run.

Each retry gets a scheduler log named `slurm-<tag>-retry<N>.log`. Test capture files are reused and truncated by the next attempt; `dispatch.retry` and `dispatch.result_missing` in `rtl_buddy.log` are the durable reason trail.

Scheduler-side license gating with Slurm `Licenses=` and `--licenses=<name>:1` is preferable when available because jobs wait without consuming an allocation.

<a id="watching-a-run"></a>
