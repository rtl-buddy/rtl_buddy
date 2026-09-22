## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM, or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile, or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an expected-failure marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No verdict was produced: either a successful early stop (exits 0) or an unknown outcome (exits 1) |

The shell exit code is a coarse run status. Parse `payload.results` under `--machine` for per-test verdicts.

| Code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP`, or an early-stop `NA` |
| 1 | At least one real test/tool-flow failure, an unknown `NA`, a strict `XPASS`, or a failed coverage merge |
| 2 | Fatal configuration or environment error |

A strict unexpected pass counts as a failure, and a marker never covers a failure that happened instead of a verdict — a setup or compile failure, a sim killed at `sim_timeout`, a dispatched job the scheduler lost — so those still exit 1. See [Expected Failures](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/expected-failures/).

A requested coverage merge that produced nothing also exits 1, even when every test passed, because the run reported an incomplete measurement. Artefacts and results are written first; see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#read-a-failed-merge).
