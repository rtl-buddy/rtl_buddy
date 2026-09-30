## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an [expected-failure](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/expected-failures/) marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No verdict: a successful early stop (exits 0) or an unknown outcome (exits 1) |

| Exit code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP` or an early-stop `NA` |
| 1 | A test or tool-flow failure, an unknown `NA`, a strict `XPASS`, or a failed coverage merge |
| 2 | Fatal configuration or environment error |

The exit code is coarse. Under `--machine`, read `payload.results` for per-test verdicts. A setup or compile failure, a sim killed at `sim_timeout` and a job the scheduler lost all exit 1. A requested coverage merge that produced nothing also exits 1 even when every test passed; see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#read-a-failed-merge).
