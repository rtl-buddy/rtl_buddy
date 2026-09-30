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

The run exits 1, even when every test passed. Results and saved coverage are still written, and the `coverage.merge.failed` event carries the tool's return code and output. If the cause is memory or a killed process, rerun the coverage command on a compute node instead of the submit host. Machine output reports the failure as `merge_failed`.
