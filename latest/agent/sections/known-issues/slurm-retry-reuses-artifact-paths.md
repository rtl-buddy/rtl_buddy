## Slurm retry reuses artifact paths

A retry overwrites the first attempt's simulation capture and per-job rtl_buddy log; only `slurm-<tag>-retry<N>.log` remains attempt-specific. Use scheduler logs and the head's `dispatch.result_missing` event when diagnosing retries.

`max-wait` applies to each attempt, not the whole run, and includes the requested backoff. A later `--begin` in `sbatch-args` overrides rtl_buddy's retry delay because Slurm uses the last duplicate option. Remove custom `--begin` when using retry backoff.
