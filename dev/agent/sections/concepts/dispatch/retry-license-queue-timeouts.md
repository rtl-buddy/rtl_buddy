## Retry license-queue timeouts

Retry is off until `retry.attempts` is nonzero. It applies to simulation jobs only and only when the evidence points at a VCS license wait: the Slurm state is `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED`, the captured output ends in license-queue banner text after the last `-licqueue` marker, and the suite's build job succeeded. For `local-parallel`, the queue text alone is enough.

Retry delay grows as `backoff-sec * 2^(n-1)` up to `backoff-max-sec`, with jitter; a `--begin` in `sbatch-args` overrides it. `max-wait` bounds each collection round, not the whole run. Each retry writes `slurm-<tag>-retry<N>.log`, and `rtl_buddy.log` gives the reason. An exhausted retry stays a failure.

Where available, prefer Slurm `Licenses=` with `--licenses=<name>:1`, so jobs wait without holding an allocation. VCS waits under `-licqueue` count against the time limit, so give `compile.time` headroom and keep `parallel` within the license pool.
