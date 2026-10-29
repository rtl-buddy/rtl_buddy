## Troubleshoot reservations, arrays, and waits

- **`config.unknown_key_rejected`:** an unknown reservation or `cfg-dispatch` key stops configuration loading with exit code 2. Rename it to the suggested key or remove it. See [Parallel dispatch](https://rtl-buddy.github.io/rtl_buddy/v7/reference/yaml/#parallel-dispatch).
- **`dispatch.reservations_ignored`:** `local-parallel` does not enforce `cpus`, `mem`, or `time`. Lower `--jobs` if memory runs out.
- **`OUT_OF_MEMORY` (Slurm) or `Killed` / exit 137 (local):** raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`. Elaboration of large generated structures can be the memory peak.
- **sbatch refuses an array:** set `cfg-dispatch.max-array-size` or `max-array-tasks`. `dispatch.max_array_size_unknown` (INFO) means no limit could be read.
- **`dispatch.submit_deferred`:** sbatch refused a submission because the cluster's job queue is full (`MaxJobCount`). The head waits 30 s, doubling up to 5 min, and resubmits. It stops 30 min after the first attempt; the run then exits 2 with `sbatch array submit failed (<N> jobs, ...)`, the retry count and sbatch's message, and cancels what it already submitted. Rerun once the queue drains. `rb pnr --dispatch slurm` resubmits a refused stage job the same way, and a cancel or `Ctrl-C` ends its wait at once. Other refusals, and sbatch timeouts, are not retried: a timed-out submission may already be queued.
- **`dispatch.max_wait_exceeded`:** jobs were still outstanding at `max-wait`. Run the printed `squeue` command. The head cancels them.
- **`dispatch.wait_poll_failed`:** `squeue` timed out or the cluster is unreachable. Polling continues until `max-wait`.
- **`dispatch.wait_states_unfiltered`:** an old `squeue` refused the state filter, so a held job can be reported finished early.
- **`dispatch.result_missing`:** a job produced no result. It counts as a failure unless the message says it is being retried.
- **`dispatch.retry_abandoned`:** a retry could not be submitted; the earlier result stays scored.
- **`dispatch.orphans_found` / `dispatch.orphans_cancel_failed` / `dispatch.coverage_orphan_found`:** see [Interrupted runs](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/dispatch/#interrupted-runs-warn-cancel-adopt).
