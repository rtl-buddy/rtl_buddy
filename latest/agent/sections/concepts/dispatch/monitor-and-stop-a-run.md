## Monitor and stop a run

At normal verbosity the console shows submission lines with job IDs, progress at each `progress-interval`, a line when each suite drains, and a warning with outstanding IDs when `max-wait` expires. Set `progress-interval: 0` to silence progress; events stay in the log.

On timeout or `Ctrl-C`, the head cancels the outstanding jobs. If jobs survive `scancel`, `dispatch.orphans_cancel_failed` names them, and the next run finds them as orphans.

Logs are separated by process:

| Process | rtl_buddy log | Related files |
|---|---|---|
| Head | `<suite>/rtl_buddy.log` | Console output |
| Simulation | `artefacts/<test>/dispatch/rtl_buddy-<tag>.log` | `result-<tag>.json`, `slurm-<tag>.log` or `local-parallel-<tag>.log` |
| Verilate | `artefacts/.dispatch/verilate-rtl_buddy-<pid>.log` | `verilate-<pid>.log` |
| Build | `artefacts/.dispatch/build-rtl_buddy-<pid>.log` | `build-<pid>.log` |

`<tag>` is the run ID or `single`; `<pid>` is the head process ID. In `.dispatch`, every `<pid>` is followed by `-<token>`, for example `build-rtl_buddy-<pid>-<token>.log`; use a glob.

- Regressions that list several test configs from one directory get a namespace directory under `.dispatch`, such as `artefacts/.dispatch/tests-7a91c2d4e6f8/`. If two configs expand tests onto the same per-test directory, the regression exits before submitting; rename one test or separate the configs.
- `--run-tag <name>` moves these paths into `artefacts/.runs/<name>/`. The shared build directory stays put, so tagged runs reuse one build. See [Namespace concurrent runs](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/#namespace-concurrent-runs).
- Each test's `result-<tag>.json` includes its build record, which [`rb graph results`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#results-overlay) shows.
