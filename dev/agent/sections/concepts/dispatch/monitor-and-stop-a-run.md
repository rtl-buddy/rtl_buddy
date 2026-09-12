## Monitor and stop a run

At normal verbosity dispatch prints:

- suite submission lines with build and simulation job IDs;
- progress when counts change and at `progress-interval` heartbeats;
- a line when each suite drains;
- a warning with outstanding IDs when `max-wait` expires.

Set `progress-interval: 0` to suppress console progress; events remain in the head log. On timeout or interrupt, the head cancels the outstanding fleet.

The poll that decides a fleet has drained asks `squeue` for every state in which a job is still alive — `PENDING` through the held `REQUEUE_HOLD`, `RESV_DEL_HOLD` and `SPECIAL_EXIT`, and `STOPPED`, whose job keeps its CPUs — because a state missing from that filter makes a live job invisible: the wait would return while it was still queued, the collector would score its absent envelope as a failure, and nothing would cancel it. Two states squeue lists as non-terminal are deliberately **out** of it, `PREEMPTED` and `REVOKED`: both are results whose record Slurm keeps until it is purged, so waiting on one would delay collection and the license-queue retry for as long as the site's purge takes, and under a finite `max-wait` fail a run whose jobs had all ended. A preempted job is where the collector expects it — retry treats `PREEMPTED` as an allocation lost, beside `TIMEOUT` and `NODE_FAIL`, and re-submits it — and a preemption configured to requeue moves the job on to `REQUEUED`, which the filter does ask for, so only a terminally preempted job drains. A revoked record is a federation sibling whose job started on another cluster and will never progress here. The build-job dedup probe asks for those two as well, since naming a predecessor whose record is still in the queue costs nothing there; the two filters are derived from one list — the probe's set is the drain set plus the retained results — so they cannot drift apart. A `squeue` too old to know one of those names refuses the query and says which name it refuses; rtl_buddy drops that one and asks again (`dispatch.wait_states_narrowed`, DEBUG), since a state its Slurm does not have is a state no job can be in. Only a refusal that names nothing drops the filter entirely, leaving squeue's own narrower default of pending, running and completing — logged as a WARNING (`dispatch.wait_states_unfiltered`), because a job held in another state can then be reported finished early. Both are remembered for **that cluster** alone, and both events name it: a federation can run several Slurm versions, and applying one cluster's rejection to the rest would poll a newer cluster with a filter too narrow to see its own held job. A poll that fails for any other reason — a controller timeout, an unreachable cluster — is not an answer about the jobs at all: they stay outstanding, `dispatch.wait_poll_failed` records it (WARNING the first time per cluster, DEBUG after, so a wedged `squeue` cannot fill the console), and the next poll asks again, so a `squeue` that never answers ends in the `max-wait` failure rather than in a silent early collection. `Invalid job id specified` is the one error that does mean completion: it is what squeue answers once every one of those ids has aged out of the queue.

Logs are separated by process:

| Process | rtl_buddy log | Related files |
|---|---|---|
| Head | `<suite>/rtl_buddy.log` | Console output |
| Simulation | `artefacts/<test>/dispatch/rtl_buddy-<tag>.log` | `result-<tag>.json`, `slurm-<tag>.log` or `local-parallel-<tag>.log` |
| Build | `artefacts/.dispatch/build-rtl_buddy-<pid>.log` | `build-result-<pid>.json`, `build-<pid>.log` |

`<tag>` is the run ID or `single`; `<pid>` is the head process ID. Failure descriptions point to the relevant worker and scheduler logs.

When a dispatched regression lists multiple test configs from the same
directory, each config gets a stable namespace below `.dispatch`, for example
`artefacts/.dispatch/tests-7a91c2d4e6f8/`. Its plan, build files, array
manifests, scripts, and scheduler logs use that directory; test configs whose
directory is not shared keep the flat layout in the table. The namespace is a
filesystem-safe config stem plus a hash of the resolved config path.

Per-test outputs remain `artefacts/<test>/`. If two co-located configs expand
tests onto the same per-test directory, the regression exits before submitting
any jobs. Rename one test or put the configs in separate directories.

`build-result-<pid>.json` carries the build job's `built` and `failed` test names and a `builds` list with one record per planned config: `test`, `builder`, `duration_sec`, `reused`, and `group` (the suite-relative path of the output the compile writes — the shared `artefacts/.shared-builds/obj_dir_<key>` directory, or an unshared build's own executable). Equal `group` values identify one single-writer output: a shared compile, or several configs pinned to one executable by `builder-simv:`. Where sharing is unsupported every test's output is its own, so distinct `group` values there say nothing about the compile keys. A config that never reached a builder still gets a record, with null timings. A record for a build that **succeeded** — compiled, reused, or adopted from a same-key sibling — also carries `fingerprint_sha`, the digest of the inputs its stamp recorded, taken over the same content-decides comparison the stamp uses, so a byte-identical input matches whatever its timestamp says; a gated simulation job whose stamp check fails compares its own digest against it to report whether the disagreement is over the same inputs. A record for a build that **failed** carries up to four more fields: `returncode` (the builder's exit status), `fingerprint_sha` (the same digest, of the inputs that compile failed on), `error_tail` (the last non-blank lines of its transcript, or the worker's exception when no builder ran) and `transcript` (suite-relative). `returncode` plus a `fingerprint_sha` matching its own inputs is what a gated simulation job requires before declining its own recompile — a failure recorded without a returncode never reached a builder, and one whose fingerprint differs was a different compile, so both still get the retry — and `error_tail` is what puts the real compile error in the run summary. At collect the head folds the build job's own `sacct` row into the same file under `telemetry`, and copies each test's compile record into that test's `result-<tag>.json`, where [`rb graph results`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#results-overlay) surfaces it. Both are best-effort: an envelope written by an older build job simply has no `builds` key, and an annotation that cannot be written leaves the result itself untouched.
