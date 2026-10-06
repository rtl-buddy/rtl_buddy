## Read a failed merge

The one-line summary uses two tokens for a missing number:

| Token | Meaning |
|---|---|
| `UNSP` | The metric was never measured: not instrumented, or not representable in the source. An LCOV `.info` has no toggle, expression or functional coverage. |
| `FAIL` | The metric was measured, but the only tool run that produced it failed. |

Under `--coverage-merge` and `--coverage-merge-raw`, `verilator_coverage --write` is the only source of toggle, expression and functional coverage. If it is killed, runs out of memory or exits non-zero, those metrics read `FAIL`. Line and branch still report when `use-lcov` or `--coverage-html` is on; otherwise they read `FAIL` too.

```text
Merged Coverage: L:0.92 B:0.95 T:FAIL F:FAIL
Coverage merge FAILED: verilator_coverage --write wrote no merged database, so toggle, expression, functional read FAIL (measurement lost), not UNSP (not instrumented) — see the coverage.merge.failed event
```

The run exits 1, even when every test passed. Results and saved coverage are still written, and the `coverage.merge.failed` event carries the tool's return code and output. If the cause is memory or a killed process, rerun the coverage command on a compute node instead of the submit host; under `--dispatch slurm` the merge already runs in its own job, so raise its reservation in `cfg-dispatch.coverage` (see [Run the coverage tail as a job](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#run-the-coverage-tail-as-a-job)). A merge stopped by `cfg-coverage` `merge-timeout` fails the same way. Machine output reports the failure as `merge_failed`.

A dispatched coverage tail that leaves no answer (refused submission, killed job, `max-wait` expiry) is reported as `Coverage tail FAILED` and `tail_failed` in machine output, with the job id and log. No model or manifest is written, every test result is, and the run exits 1. The previous run's `cov_dir/manifest.json` and `coverage-model.json` are removed before the job is submitted, so `rb cov summary`, the MCP coverage tools and the hub never present them as this run's.
