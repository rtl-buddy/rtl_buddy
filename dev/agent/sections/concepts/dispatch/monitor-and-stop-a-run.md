## Monitor and stop a run

At normal verbosity dispatch prints:

- suite submission lines with build and simulation job IDs;
- progress when counts change and at `progress-interval` heartbeats;
- a line when each suite drains;
- a warning with outstanding IDs when `max-wait` expires.

Set `progress-interval: 0` to suppress console progress; events remain in the head log. On timeout or interrupt, the head cancels the outstanding fleet.

Logs are separated by process:

| Process | rtl_buddy log | Related files |
|---|---|---|
| Head | `<suite>/rtl_buddy.log` | Console output |
| Simulation | `artefacts/<test>/dispatch/rtl_buddy-<tag>.log` | `result-<tag>.json`, `slurm-<tag>.log` or `local-parallel-<tag>.log` |
| Build | `artefacts/.dispatch/build-rtl_buddy-<pid>.log` | `build-result-<pid>.json`, `build-<pid>.log` |

`<tag>` is the run ID or `single`; `<pid>` is the head process ID. Failure descriptions point to the relevant worker and scheduler logs.

`build-result-<pid>.json` carries the build job's `built` and `failed` test names and a `builds` list with one record per planned config: `test`, `builder`, `duration_sec`, `reused`, and `group` (the suite-relative path of the output the compile writes — the shared `artefacts/.shared-builds/obj_dir_<key>` directory, or an unshared build's own executable). Equal `group` values identify one single-writer output: a shared compile, or several configs pinned to one executable by `builder-simv:`. Where sharing is unsupported every test's output is its own, so distinct `group` values there say nothing about the compile keys. A config that never reached a builder still gets a record, with null timings. A record for a build that **failed** carries up to four more fields: `returncode` (the builder's exit status), `fingerprint_sha` (a digest of the inputs that compile failed on), `error_tail` (the last non-blank lines of its transcript, or the worker's exception when no builder ran) and `transcript` (suite-relative). `returncode` plus a `fingerprint_sha` matching its own inputs is what a gated simulation job requires before declining its own recompile — a failure recorded without a returncode never reached a builder, and one whose fingerprint differs was a different compile, so both still get the retry — and `error_tail` is what puts the real compile error in the run summary. At collect the head folds the build job's own `sacct` row into the same file under `telemetry`, and copies each test's compile record into that test's `result-<tag>.json`, where [`rb graph results`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#results-overlay) surfaces it. Both are best-effort: an envelope written by an older build job simply has no `builds` key, and an annotation that cannot be written leaves the result itself untouched.
