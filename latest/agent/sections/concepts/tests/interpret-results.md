## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM, or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile, or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an expected-failure marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No real verdict was produced, including a successful early stop |

The shell exit code is a coarse run status. Parse `payload.results` under `--machine` for per-test verdicts.

| Code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP`, or `NA` |
| 1 | At least one real test/tool-flow failure, or a strict `XPASS` |
| 2 | Fatal configuration or environment error |

A strict unexpected pass counts as a failure. See [Expected Failures](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/expected-failures/).
