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
