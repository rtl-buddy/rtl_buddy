## Slurm retry reuses artifact paths

A retry overwrites the first attempt's simulation capture and per-job rtl_buddy log. Only `slurm-<tag>-retry<N>.log` stays per attempt, so diagnose retries from the scheduler logs. `max-wait` applies to each attempt. A `--begin` in `sbatch-args` overrides the retry delay, so remove it when you use retry backoff.
