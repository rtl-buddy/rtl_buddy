---
description: Run tests concurrently on one host or Slurm, configure resources and retries, and diagnose dispatched builds and jobs.
---

# Parallel dispatch

Dispatch runs `test`, `randtest`, or regression work in parallel after planning the run and sharing compilations where possible.

```bash
rb regression --dispatch local-parallel
rb regression --dispatch slurm
rb randtest my_test 500 --dispatch slurm
rb test smoke reset_error --dispatch slurm
rb test --filter '^smoke_' --dispatch slurm
```

| Backend | Execution | Concurrency | Resource enforcement | Usage advice |
|---|---|---|---|---|
| `local` | Current process | 1 | None | None |
| `local-parallel` | Subprocesses on this host | `--jobs` or `cfg-dispatch.jobs` | No | No |
| `slurm` | Cluster jobs | Per-array throttle | Yes | From `sacct` |

`local` is the default. `rb test` uses dispatch only when `--dispatch` is given explicitly; it does not inherit `cfg-dispatch.backend`. Other dispatch settings still apply after a backend is selected.

`rb test` accepts the same explicit-name list or regex filter locally and under dispatch, creating one simulation job per selected test. See [Run tests](tests.md#run-tests) for selection order and validation.

Dispatch cannot be combined with `--early-stop`. It implies [`--share-build`](tests.md#sharing-compiled-builds-across-tests), expands sweep hooks once on the head, and skips the per-tree lock in worker jobs.

## Run on one host

`local-parallel` uses one global subprocess pool across all suites and resource groups:

```bash
rb regression --dispatch local-parallel       # min(4, CPU count)
rb regression --dispatch local-parallel -j 8
rb randtest my_test 20 --dispatch local-parallel -j 4
```

The default is `min(4, CPU count)`. Build jobs are prioritized because they unblock their suite. A simulation starts only after its build exits 0; a failed build prevents dependent simulations from starting and makes them dispatch failures.

CPU, memory, and time reservations are not enforced locally. A non-default reservation logs `dispatch.reservations_ignored`; choose `--jobs` for the memory demand of the heaviest concurrent tests. Local runs also produce no reservation advice.

Use `Ctrl-C` to stop the head and its process groups. `SIGKILL` prevents cleanup and may leave child processes running.

## Meet the Slurm requirements

Before using `--dispatch slurm`, provide:

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host, and `scontrol` for the `MaxArraySize` probe below. Run `rb tool-check --explain slurm`.
- A shared filesystem exposing the project, artefacts, and Python environment at identical absolute paths on submit and compute hosts.
- The project's Python environment on compute hosts; workers run `sys.executable -m rtl_buddy`.

The submit process only plans, submits, waits, and collects. Compilation and simulation run on compute nodes.

## Understand build and simulation jobs

For each suite, dispatch:

1. Writes a plan and, when needed, submits one build job.
2. Builds each unique compile key. Compile keys fingerprint sources, flags, defines, and the resolved builder.
3. Groups simulations with identical resolved resources into Slurm arrays and gates them with `afterok` on the build.
4. Collects each worker's `result.json` into the normal summary and exit status.

The build job compiles one key at a time by default. `cfg-dispatch.compile.parallel` raises that to N distinct builds compiled concurrently inside the same job. Concurrency is over distinct compile keys, never over tests: configs sharing a key compile one after another, the later ones short-circuiting on the first one's build stamp, because two builders writing one build directory is what `compile.prebuilt_stamp_invalid` reports. Configs whose resolved `builder-simv` output is one file — an absolute pin, or a relative spelling whose `..` escapes the per-test workspace — are grouped the same way even though their compile directories differ, because the executable they write is one path. A config whose compile fails is still reported per test, and the job still exits 0 so its `afterok` dependents run.

Preprocessing hooks always run serially, and their position relative to compilation depends on `parallel`. At the default `parallel: 1` the job runs `preproc` and then the compile for one config before touching the next, so a hook that regenerates a shared input cannot overwrite what an earlier config is about to compile. Above 1 the compile key is only knowable after that config's `preproc` has run, so every hook runs first and the builders then overlap: raising `parallel` requires that no config's `preproc` mutate another config's inputs. Simulation jobs already require this — each `rb _test-job` re-runs its own `preproc` concurrently on its own node.

Arrays group by resource tuple, not compile key. Tests may share a compiled executable while using different arrays, or share an array while using different builds. `max-jobs-per-array` is a `%N` throttle on each array; total concurrency can approach the throttle multiplied by the number of arrays.

A group larger than the cluster's Slurm `MaxArraySize` is not a legal array, so it is submitted as several. rtl_buddy reads `MaxArraySize` once per run from `scontrol show config`, or from `cfg-dispatch.max-array-size` when that is set, and slices the group into arrays of at most `MaxArraySize - 1` elements — Slurm's largest task index is one below the limit, and rtl_buddy's array elements are numbered from 1. A cluster that also sets `SchedulerParameters=max_array_tasks=N` caps how many tasks one array may hold at all, which can be well below `MaxArraySize` and is reported separately by `scontrol`; the slice size is the smaller of the two, `min(max_array_tasks, MaxArraySize - 1)`, because that parameter is an inclusive count of tasks while `MaxArraySize` is an exclusive index bound. The two ceilings layer independently: `cfg-dispatch.max-array-size` overrides the probed `MaxArraySize`, `cfg-dispatch.max-array-tasks` overrides the probed `max_array_tasks`, and whichever field is left unset still comes from the probe. Pinning only `max-array-size` therefore keeps the cluster's task cap in force instead of hiding it; the probe is skipped entirely only when both are pinned, or when no single cluster can be asked. Either ceiling alone is enough to slice: a site that can state only its task cap — no `scontrol` on the submit host, or a multi-cluster selection — gets arrays of `max-array-tasks` elements even though `MaxArraySize` is unknown. Each slice is its own array with its own manifest and logs under `slice-N/` in the run's dispatch directory, its job name carries a `/N` suffix, and every slice waits on the same build job. The handles are collected as one logical group, so the summary, cancellation, and the reservation advice are unchanged by the split. `max-jobs-per-array` throttles each slice, so a split group's peak concurrency is the throttle multiplied by the number of slices. The probe follows the cluster the jobs go to: when `sbatch-args` selects another one (`-M name`, `-Mname`, `--clusters=name`, or `--clusters name`, last occurrence winning as it does at submit), `scontrol` is asked with the same `-M name`, because an unqualified probe would read the local cluster's limit and submit against a different one. `SBATCH_CLUSTERS` in the environment selects a cluster the same way and is read when the probe runs; `sbatch-args` wins over it, matching Slurm's own precedence. Every job records the cluster that accepted it — `sbatch --parsable` answers `jobid;cluster` for a remote submission, and the selection stands in when it answers with the id alone — because a job id only means anything on its own cluster. Polling, cancellation and accounting are then issued there: one `squeue -M <cluster>` per cluster while the fleet drains — an unqualified poll cannot see a remote slice, and a job it cannot see reads as drained, so collection would start while that slice was still queued — one `scancel -M <cluster>` per cluster the fleet reached (a `--clusters=a,b` group can be spread over both, and the cleanup after a failed slice must reach the slices already queued), and one `sacct -M <cluster>` per cluster as well — accounting rows carry no cluster of their own, so a combined query could not be split back apart and two jobs sharing a number would have their rows merged. Telemetry, the outstanding set and per-suite membership are therefore keyed by job id for a local or single-cluster run, as they always were, and by `<cluster>:<job id>` for a job accepted elsewhere — two clusters can issue the same number, and one entry for both would drop a queued job from the count and report its suite finished early. That key is internal: a `max-wait` failure splits it again and hands you a command Slurm accepts, `squeue -M alpha -j '77_[1-2]'`, with `jobs`, `clusters` and `queries` as separate fields on `dispatch.max_wait_exceeded`. A single-cluster site sees the same bare commands as before. A selection naming more than one cluster — a comma-separated list, or the reserved `all` — is left unknown on purpose: Slurm picks which cluster runs the job at submit, so no single `MaxArraySize` applies, and `scontrol -M all show config` answers with one config block per cluster. Pin `cfg-dispatch.max-array-size` there, or `max-array-tasks` alone if that is the ceiling you know. A resolved limit is recorded at debug level as `dispatch.max_array_size`, with the `cluster` probed, both ceilings (`max_array_size` and `max_array_tasks`) beside the `max_elements` that governed, and `source: config` or `source: scontrol` naming where the **governing** value came from — with the two ceilings on different layers, that is the one that produced the smaller slice. `max_array_tasks` is reported on every path where a cap is known, configured or probed; like every unset field in rtl_buddy's log events it is omitted when none is known, so read its absence as "no task cap known". Only when NEITHER ceiling is known — nothing configured and nothing probed — is the group submitted whole, and an oversized one is then refused by sbatch; `dispatch.max_array_size_unknown` records that in the run log, and the sbatch failure repeats the hint so the fix is on the console that failed the run. An array refused despite being within every limit the probe could see says so too, naming the slice size it used and pointing at whichever ceiling produced it — `cfg-dispatch.max-array-tasks` when the task cap was binding, `cfg-dispatch.max-array-size` otherwise — as the knob to lower. A cluster can enforce a ceiling it does not report, and shrinking the index bound to work around a task cap would state a `MaxArraySize` the cluster does not have. A group that fits in one array is submitted exactly as before, with no `slice-N/` level.

Verilator, VCS, and Icarus can place outputs in a shared compile-key directory. Other builders, and builders with an absolute `builder-simv`, keep the build under the test artefact directory:

- Without shared-capable tests or seed fan-out, no separate build job is submitted; each simulation job compiles in its own directory.
- A fanned-out test still gets a build job to prevent concurrent compiles into the same test directory. Workers use the stamp left by that job.
- A job that compiles **for itself** — one whose builder cannot share a build, so no build job covers it — uses the field-wise maximum of its simulation and compile reservations.

A job gated on a build job keeps its **simulation** reservation. The compile block is not folded into it: that would inflate every gated job in the fan-out, and change which jobs share an array, to pay for a compile that normally does not happen there. What a gated job does when the build's stamp fails to validate depends on why:

- The build job recorded this test's **builder** as having run and exited non-zero **on the same inputs** — the per-build record carries its exit status, and the record's input fingerprint matches the one this job just derived. That fingerprint follows the same content rule the stamp does, so a source whose bytes are unchanged still matches after a `touch`, a re-checkout, or a hook that regenerated it identically — only a real edit counts as "the inputs moved". The job does not recompile. A deterministic compile error fails the same way again, and it would fail under the simulation reservation, so a large elaboration is killed for memory and the summary reports that kill instead of the design error. The row reports the build job's exit status and error lines, and `compile.log` is left as the build job wrote it.
- Anything else — the stamp is **absent or stale** (a moved toolchain, a clock skew, a config the build job never reached), the build job recorded a failure **without** a builder exit status (a preproc or filelist error, a crashed worker — the simulation job re-runs its own preproc, so those can pass here), or the **inputs changed** since the failed build (the fingerprints differ, so the recorded failure may not reproduce). The job recompiles, at its **simulation** size, and writes the transcript to `compile.retry.log` in its **run's** artifact directory (`run-NNNN/` under the test directory, or the test directory itself for a single run) — the retry is one run's recompile, and sibling runs of a fanned-out test share the test directory, so a test-scoped file would be one run's story overwritten and advertised by every sibling. The build job's `compile.log` is never touched. `cfg-dispatch.compile` does not size this path — it sizes only the build job (and the self-compiling jobs above) — so a suite that relies on this recovery for heavy builds must size the applicable simulation reservation (the test or testbench `resources:`, or `cfg-dispatch.resources`) for compilation; better, fix whatever drifts the inputs so the stamp validates and no gated job compiles at all. Read `compile.prebuilt_stamp_invalid` in the job's log to see that the retry happened.

A compile that reuses an existing build says so. `compile.build_reused` names the reused directory and the age of its stamp — once per build directory per process on the console (every reuse still lands in the log file), which on a local run is your terminal and under dispatch is the job's own log — and the test's `compile.log` carries the same breadcrumb, naming the command a rebuild would run. Under dispatch a gated job that reuses writes that breadcrumb into the build job's `compile.log`, keeping the build's own transcript below it; a gated *retry* never writes there at all, which is why it has its own file. A run that compiled nothing is now visible as such instead of only as a missing file. What a stamp validates against is content: every tracked input under the project root is compared by hash, so a `preproc` hook that regenerates a file byte-for-byte reuses the build, while any real edit invalidates it even on a node whose cached `stat` still describes the file as it was.

`--rebuild` compiles even where the stamp validates. It forces at most one rebuild per build directory per invocation, so a suite whose tests share a compile key still compiles once and `compile.rebuild_forced` reports it once. Under dispatch it rides the build job, which is the single writer of the shared directory; gated simulation jobs never carry it, because a whole array forced to compile would land in that one directory at once. A suite that submits no build job — every test compiling in its own directory — passes it to the simulation jobs instead, and a retried job carries whatever its first attempt held. `--rebuild` says nothing about sharing: it neither implies nor suppresses `--share-build`.

One build job per build identity at a time. Each build job is named after what it builds — `rb-build-<hash>` over the suite directory, the planned test names and the builder selection — and before submitting one the head asks `squeue` for its own queued, running, configuring, or suspended jobs of that name. When it finds any, the new build job is submitted with `--dependency=afterany:<ids>` and `dispatch.build_job_deduped` names them on the console. This is the interrupted-run case: a client that was Ctrl-C'd leaves its build job on the cluster, and the re-run's build job would otherwise Verilate into the same `artefacts/.shared-builds/obj_dir_<key>` beside it. Instead it waits for the orphan to finish — holding no allocation while it waits — then revalidates the stamp under the build lock and reuses what the orphan built. `afterany`, so an orphan that fails or is cancelled still releases this run, which then compiles for itself.

The name is not the compile key. Compile keys fingerprint sources, flags and defines, and they are only knowable after a config's `preproc` has run, inside the job — so the head names jobs by what it *can* see. A source edited between two runs of the same suite therefore still makes the second job wait for the first, and it then rebuilds correctly; the cost is queue latency, never a wrong build. In the other direction the name is precise: another suite, another set of tests, or another builder never adopts the wait. The probe is scoped to your own jobs, is time-boxed, and degrades silently — a submit host with no `squeue`, or one that errors, submits exactly as before and records `dispatch.build_dedup_unavailable` at DEBUG. A `--dependency` of your own in `sbatch-args` is composed with rather than replaced. The `local-parallel` backend needs none of this: its jobs are processes on one host, where the build lock already serialises them.

When a build job exists, every dependent is submitted with `--kill-on-invalid-dep=yes`. A failed build therefore removes jobs that could never satisfy `afterok`; collection also cancels any `DependencyNeverSatisfied` remnants. A user-supplied `--kill-on-invalid-dep=no` in `sbatch-args` overrides the default.

A missing result from a scheduler kill, worker crash, or dependency failure is a failed row, not a dropped test. A compile failure for one compile key does not stop unrelated keys; the affected tests report that compile's exit status and error lines, and their simulation jobs do not repeat it.

## Configure dispatch

Set defaults in `root_config.yaml`:

```yaml
cfg-dispatch:
  backend: slurm
  jobs: 4
  resources:
    cpus: 2
    mem: 4G
    time: "01:00:00"
  compile:
    cpus: 8
    mem: 16G
    time: "02:00:00"
    parallel: 4          # distinct builds compiled at once in the build job;
                         # the head reserves up to cpus x parallel (32 here,
                         # capped at the planned test count) and leaves mem
                         # and time exactly as written
  sbatch-args:
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200
  max-array-size: 1001   # the cluster's Slurm MaxArraySize; omit it to read
                         # the value from `scontrol show config`
  poll-interval: 10
  progress-interval: 60
  max-wait: 7200
  retry:
    attempts: 2
    backoff-sec: 60
    backoff-max-sec: 600
    jitter: 0.5
    classifiers: [license-queue]
  rightsize:
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`jobs` controls the single local-parallel pool. `max-jobs-per-array` controls each Slurm array, and `max-array-size` controls how large one array may be before the group is split. See [YAML formats](../reference/yaml.md#root_configyaml) for defaults and validation.

Always quote `time` values. YAML 1.1 can parse an unquoted value such as `4:00:00` as the integer `14400`, changing its meaning. rtl_buddy rejects that form. Quote times in global, compile, testbench, and test reservations.

## Set per-test resources

Reservations resolve field by field in this order: test, testbench, `cfg-dispatch.resources`, built-in defaults.

```yaml
testbenches:
  - name: axi_tb
    resources: {cpus: 2, mem: 8G, time: "00:30:00"}

tests:
  - name: axi_smoke
  - name: axi_soak
    resources: {mem: 24G, time: "04:00:00"}
```

Tests with identical resolved reservations share an array. Compilation normally uses `cfg-dispatch.compile`; when compilation occurs inside a simulation job, that job receives the field-wise maximum of both reservations.

## Set per-suite compile resources

`cfg-dispatch.compile` is one reservation for every suite's build job, so a repo with one large top-level testbench and many leaf-cell benches sizes them all for the largest. A suite that differs states its own reservation at the **top level of its `tests.yaml`**, in the same `{cpus, mem, time}` shape:

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G          # this suite's verilation only; cpus and time inherited

testbenches:
  - name: soc_tb
    ...
```

The compile reservation resolves field by field in this order: suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults. A field the suite omits inherits, so the example above keeps the cluster-wide `cpus: 8` and `time: "02:00:00"` and moves only memory. The block sizes the suite's build job, and — for a builder that cannot share a build — the compile half of the field-wise maximum that sizes each simulation job.

`parallel` is not accepted at suite level. It sizes the build job against the partition's widest node, which is a cluster fact; keep it in `cfg-dispatch.compile`. Note that unknown keys are dropped silently rather than rejected, so a `parallel` written here simply has no effect.

The block is a scheduling fact only. It is not part of the compile fingerprint, so adding or changing it never invalidates a shared build stamp.

Size `compile.time` for the longest batch the build job runs, not for one build. With `compile.parallel: N` the suite's unique compile keys are compiled N at a time, so the job's wall clock is the makespan of a work queue N workers deep: each worker takes the next unbuilt key as it frees up, and the job ends when the last one finishes. `ceil(distinct builds / N)` times the slowest build is a safe upper bound to size against, and it is close to the real figure only when the builds take similar times; a mix of one long build and several short ones finishes nearer the long one alone. At the default `parallel: 1` it is the serial total of every key.

Size `compile.mem` for `parallel` concurrent builds: the head scales only the `cpus` reservation, and N elaborations need roughly N times the memory. Size it from elaboration, not simulation. Large generated structures can make elaboration the memory peak; Slurm reports `OUT_OF_MEMORY`, while local runs may show `Killed`, SIGKILL, or exit 137. Raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`.

VCS license wait under `-licqueue` counts against the Slurm time limit. Give `compile.time` queue headroom; `compile.license_queued` records only completed builds that waited. N concurrent elaborations hold up to N licenses at once, so raising `parallel` multiplies license pressure and can convert compute time into queue time; keep it at or below what the site's license pool can serve.

Dispatch requests `--acctg-freq=task=1` unless `sbatch-args` already supplies it. Keep fine-grained accounting if you want useful memory advice for short jobs.

<a id="retrying-a-license-queue-kill"></a>

## Retry license-queue timeouts

Retry is disabled until `retry.attempts` is nonzero. It applies to simulation jobs only and retries a missing result only when evidence identifies a VCS license wait:

- Slurm state is `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED`; `FAILED` and `CANCELLED` are not retried.
- Captured output ends in license-queue banner content after the last `-licqueue` marker.
- The suite build job succeeded, so the shared-build stamp is available.

For `local-parallel`, queue evidence is sufficient because there is no scheduler state. Build jobs are never retried.

Delay for retry number `n` is `min(backoff-max-sec, backoff-sec * 2^(n-1))`, multiplied by jitter. Slurm holds retries with `--begin`; local-parallel holds them outside the worker pool. User `sbatch-args` occur last, so a user `--begin` overrides retry backoff.

`max-wait` bounds each collection round, not the total run, and excludes the requested backoff. An exhausted retry remains a failure. A retry submission failure logs `dispatch.retry_abandoned` and preserves the already-scored run.

Each retry gets a scheduler log named `slurm-<tag>-retry<N>.log`. Test capture files are reused and truncated by the next attempt; `dispatch.retry` and `dispatch.result_missing` in `rtl_buddy.log` are the durable reason trail.

Scheduler-side license gating with Slurm `Licenses=` and `--licenses=<name>:1` is preferable when available because jobs wait without consuming an allocation.

<a id="watching-a-run"></a>

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

`build-result-<pid>.json` carries the build job's `built` and `failed` test names and a `builds` list with one record per planned config: `test`, `builder`, `duration_sec`, `reused`, and `group` (the suite-relative path of the output the compile writes — the shared `artefacts/.shared-builds/obj_dir_<key>` directory, or an unshared build's own executable). Equal `group` values identify one single-writer output: a shared compile, or several configs pinned to one executable by `builder-simv:`. Where sharing is unsupported every test's output is its own, so distinct `group` values there say nothing about the compile keys. A config that never reached a builder still gets a record, with null timings. A record for a build that **failed** carries up to four more fields: `returncode` (the builder's exit status), `fingerprint_sha` (a digest of the inputs that compile failed on, taken over the same content-decides comparison the stamp uses, so a byte-identical input matches whatever its timestamp says), `error_tail` (the last non-blank lines of its transcript, or the worker's exception when no builder ran) and `transcript` (suite-relative). `returncode` plus a `fingerprint_sha` matching its own inputs is what a gated simulation job requires before declining its own recompile — a failure recorded without a returncode never reached a builder, and one whose fingerprint differs was a different compile, so both still get the retry — and `error_tail` is what puts the real compile error in the run summary. At collect the head folds the build job's own `sacct` row into the same file under `telemetry`, and copies each test's compile record into that test's `result-<tag>.json`, where [`rb graph results`](graph.md#results-overlay) surfaces it. Both are best-effort: an envelope written by an older build job simply has no `builds` key, and an annotation that cannot be written leaves the result itself untouched.

## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns findings in `payload.reservation_advice`. It never edits configuration.

Advice is calculated per test using the peak across runs in this invocation:

- utilization below `over-threshold` suggests a reduction;
- utilization above `near-limit`, `TIMEOUT`, or `OUT_OF_MEMORY` suggests an increase;
- suggestions use peak times `margin`, with floors of 5 minutes and 128 MiB;
- time advice is limited to Verilator because VCS license wait distorts elapsed time;
- memory advice is suppressed when the longest run is shorter than the accounting sample interval, except that an out-of-memory state still suggests an increase;
- `phase` is `sim`, `compile+sim`, or `compile` and `edit_hint` identifies the configuration field that actually controlled the allocation;
- CPU efficiency is measured against the cpus the job **requested**, not the cpus the scheduler handed out (`AllocCPUS`).

The suite's build job gets one row of its own, named `(build job)` with `phase: compile`. It suggests `time` in both directions and `cpus` only downwards, because low CPU efficiency there means build slots idled rather than that more were needed. Its `cpus` suggestion is per build, and it appears only for a job that ran one build at a time: with `compile.parallel` above 1 the job's CPU efficiency also carries idle slots in the tail, which no accounting field separates from a compile that under-used its own CPUs, so the row is withheld with reason `parallel-utilization-ambiguous` rather than advising a reduction that could starve the longest compile. Size `compile.parallel` against the suite's distinct compile keys first, then read the `cpus` row from a `parallel: 1` run. `time` advice is unaffected — concurrent builds take the wall clock of the longest, not of their sum. A `reduce` is withheld when nothing actually compiled (every build reused its stamp), when the head has no per-build records to judge by — `no-build-records`, which covers both a job that left no envelope at all and one whose envelope carries no `builds` list (an older build job, or one whose telemetry could not be serialised) — or when it finished inside one accounting interval; `rightsize.build_advice_withheld` records which, along with how many records it saw and how many seconds of them were real compiling. Without that guard a re-run of an unchanged suite would advise a limit the next real RTL change times out against, cancelling the fan-out behind it.

### Requested cpus versus allocated cpus

A partition with `SelectTypeParameters=NONE` on nodes with `ThreadsPerCore=2` allocates whole cores, so a job submitted with `--cpus-per-task=1` is charged two: `sacct` reports `ReqCPUS=1` and `AllocCPUS=2`. Measured against the allocation a single-threaded simulation can never exceed 0.5 efficiency, and the default `over-threshold: 0.5` would fire on every such test, advising a reduction to the `cpus: 1` the `tests.yaml` already holds — advice no edit can retire.

Efficiency is therefore taken against the request, which is what a `resources.cpus` or `compile.cpus` edit actually moves, and a `reduce` is emitted only when the suggestion is strictly below it. The denominator is the reservation rtl_buddy itself resolved and submitted as `--cpus-per-task` — the request by construction, so it holds on a site whose Slurm also normalises `ReqCPUS` to the rounded figure. Where that is unavailable the fallbacks are `ReqCPUS`, then `AllocCPUS`.

One case withdraws the first of those. `cfg-dispatch.sbatch-args` is appended **after** the generated reservation flags and therefore overrides them, so an argument there — not the resolved reservation — decides what the jobs request. `ReqCPUS` is *tasks × cpus-per-task*, so two families qualify:

| | Options | |
| --- | --- | --- |
| the cpu count | `-c` / `--cpus-per-task` | states the request |
| task and node counts | `-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes` | raise it |

`--ntasks-per-node` is in the second family because sbatch documents it as a *request* when `--ntasks` is absent ("request that ntasks be invoked on each node … meant to be used with the `--nodes` option"), so `--nodes=2 --ntasks-per-node=4` asks for eight tasks. It degrades to a per-node maximum when `--ntasks` is also given — and that option is in the set too, so the pair is caught either way.

The same applies to the **environment**. `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE` and `SBATCH_NODES` are sbatch's documented input variables for those options, and they reach sbatch because the dispatched submit inherits the head's environment — `SBATCH_NTASKS=4` beside the generated `--cpus-per-task=2` requests eight cpus. rtl_buddy reads them at submit time and treats them exactly like the equivalent `sbatch-args` entry. It does **not** sanitize the environment: a site that exports these means them. Command line beats environment, which is sbatch's own precedence, so a variable whose option is already written in `sbatch-args` is not reported — the job did not run with it. An unset or blank variable is not an override at all.

`SBATCH_CPUS_PER_TASK` is the one that looks like it should count and does not, for the same reason as `--cpus-per-gpu`: every submit path states `--cpus-per-task` on the command line, which beats the variable, so it can never take effect.

The `sbatch-args` half is read from the **backend**, not from the suite's `cfg-dispatch`. The backend is instantiated once, from the orchestration `root_config.yaml`, before the suite loop; `root_cfg` is then rebuilt for any suite that walks up to a different one. In a regression spanning project roots the two lists therefore differ, and only the backend's is what `sbatch` receives — so reading the suite's would miss an override the backend really appends, or invent one it does not. The generated reservation flags stay suite-derived, since they come from that suite's own resolved `resources:`; it is the verbatim passthrough that belongs to the backend.

The environment is read **once per suite, before that suite submits anything**, and the result is carried through to its analysis. A regression submits every suite before collecting any, and a sweep hook is `exec()`d in the head process, so a later suite's hook can set or unset `SBATCH_*` in between; re-reading it at analysis would judge an earlier suite's jobs by a later suite's environment. Both the per-test rows and the `(build job)` row use that one snapshot, so the two halves of a suite's advice always describe the same submission.

A **retry** is a fresh `sbatch` from whatever environment the process holds by then, and it is the retry's telemetry the analysis reads — so each resubmission re-reads the overrides for the rows it resubmits. The new values are applied only once that round has been accepted and waited on: a refused `sbatch` or a failed wait leaves the head holding the previous attempt's results and telemetry, which must not be paired with the reservation of an attempt that never ran. Where a round is abandoned the rows keep describing the attempt that produced their numbers.

Retries are also per run, so a test whose seeds did not all retry can end up with runs submitted under different requests. Efficiency is the peak across every run, and one `Reserved` and one `Field` cannot describe two reservations, so the `cpus` row for such a test is **withheld** and `rightsize.cpus_advice_withheld` records it with reason `mixed-cpu-requests` — the same answer `parallel-utilization-ambiguous` gives the build job. `mem` and `time` advice is unaffected, since no cpu argument moves those reservations.

One **combination** counts even though neither half does alone. sbatch documents a second mode for `--ntasks-per-gpu`: "specify the GPUs wanted (e.g. via `--gpus` or `--gres`) without specifying `--ntasks`, and the total task count will be automatically determined". So a GPU count (`--gpus` / `-G`, `--gpus-per-node`, `--gpus-per-socket`, or a `--gres` that asks for gpus) together with `--ntasks-per-gpu`, and no `--ntasks` anywhere, derives *gpus × ntasks-per-gpu* tasks — a task-count override exactly like `--ntasks`. Both halves may come from `sbatch-args` or from `SBATCH_*`, and the advice names the pair, since neither argument alone caused it. With `--ntasks` present the derivation runs the other way (it sets the GPU count instead) and `--ntasks` is already an override, so the pair is not reported.

All spellings are recognised (`--ntasks=4`, `--ntasks 4`, `-n 4`, `-n4`). Within **one** option the last occurrence is the one reported, because that is the one sbatch obeys, and the short and long spellings are the same option — `[-c 4, --cpus-per-task=8]` is one argument written twice, not two. **Across** options there is no winner at all: each distinct option is reported, because they combine rather than supersede one another.

The set is deliberately narrow, because a false positive is not free — it discards a request rtl_buddy knows, retargets the edit hint away from the field that really governs, and disables the compile floor. Four near misses are excluded:

- `--exclusive` and `--overcommit` change what is *allocated*, not what is requested, so `ReqCPUS` still describes the reservation.
- `--threads-per-core` and `-B` / `--extra-node-info` are **node-selection constraints**: they restrict which nodes and hardware threads may be used, while the generated `--cpus-per-task` still states the request. rtl_buddy therefore still knows it, and keeps using it.
- `--ntasks-per-core` and `--ntasks-per-socket` are **placement maxima** ("request the maximum ntasks be invoked on each core/socket … meant to be used with the `--ntasks` option"): they cap where the tasks `--ntasks` asked for may land, and a lone one requests nothing. The `--ntasks` they accompany is in the set, so a real task-count change is still caught.
- `--cpus-per-gpu` is documented as mutually exclusive with `--cpus-per-task`, which every dispatched job carries, so sbatch rejects the pair. A job submitted that way never runs, and there is nothing to right-size. `--ntasks-per-gpu` is left out of the table on its own, since alone it caps placement without requesting anything — but it is not ignored: see below.

Where such an argument or variable is present rtl_buddy records no request for that run's rows or its build job, so the analysis falls back to `ReqCPUS`; a DEBUG line (`rightsize request_from_scheduler`) names what was responsible.

The `edit_hint` follows. An override masks every cpus field the layering could name, so applying a hint that named one would leave the next job's reservation exactly where it was and the finding would return — the same non-retiring advice this whole rule exists to stop. While an override is in force, a `cpus` finding's `edit_hint.path` is `cfg-dispatch.sbatch-args` (with `file` pointing at `root_config.yaml`) and its `note` says which field was superseded, for example:

```
sbatch-args `--cpus-per-task=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.
```

`suggested` is always the whole-job cpu count, but only one shape of override can be handed it: **exactly one `--cpus-per-task`**, as above. The other two shapes cannot, and the note says so rather than giving advice that would not apply.

A lone task or node count is not a cpu count — writing 3 into `--ntasks` asks for three tasks, not three cpus — and it does not supersede the per-task field either: the generated `--cpus-per-task` is still in force, so both are levers and the note names both:

```
`--ntasks=4` multiplies this job's cpu request: the generated --cpus-per-task
from tests[name=wr_single].resources.cpus still applies, so the request is 8
per task x 4 tasks. Suggested value is the whole-job cpu count — lower
tests[name=wr_single].resources.cpus, the task count in sbatch-args, or both;
no single one of them takes it.
```

The `8 per task x 4 tasks` clause is an observation — the scheduler's own request over the flag the head submitted — and is omitted when that division is not exact.

Where several arguments are present they combine by sbatch's own precedence, which the note does **not** attempt to reproduce — with `--ntasks=8 --nodes=2 --ntasks-per-node=4 --cpus-per-task=2` the request is 16, not the product of all four, because `--ntasks` wins and `--ntasks-per-node` degrades to a maximum. The note names them and leaves the arithmetic to the reader, who is the only party that knows which one should shrink:

```
sbatch-args supersedes tests[name=wr_single].resources.cpus: `--ntasks=4` and
`--cpus-per-task=2` set this job's cpu request together. Suggested value is
the whole-job cpu count — decompose it across them per sbatch's own
precedence; no single one of them takes it.
```

An override that came only from the environment names no file at all: its `edit_hint.path` is `env` and there is no `file` key, because a variable lives in nothing an agent can edit. Where an `sbatch-args` entry is also in play the hint keeps pointing there — the command line is what can defeat the variable — and the note names both.

A **direct** cpu override also disables the **compile cpus floor**. That floor exists because a job compiling inside itself is allocated `max(sim, compile)`, so no reduction can take it below the compile side — but a `--cpus-per-task` in `sbatch-args` replaces that generated flag, and sbatch never sees the max. Left in place it clamps every suggestion up to the floor and then discards it for not being below the request, so a genuinely over-reserved run reports nothing at all.

A task or node count does **not** disable it. `--ntasks=2` leaves `--cpus-per-task=8` exactly where it was and asks for two tasks of it, so the floor still holds and the advice may not suggest below it: even one task costs 8 cpus, so a whole-job suggestion of 3 could never be reached and the finding would recur every run. The floor is kept *unscaled* rather than multiplied by the tasks observed — the task count is one of the two levers the advice offers, so 8 really is reachable, by dropping to a single task, and flooring at 8 × 2 would suppress every reduction there is.

The `mem` and `time` floors are untouched throughout, since no cpu argument supersedes them.

Only `cpus` is retargeted: none of these arguments supersedes a `mem` or `time` field, so those findings keep naming the reservation that governs them.

`mem` and `time` advice never had this exposure: both are already measured against `ReqMem` and `TimelimitRaw`, which `sacct` reports from the allocation the override actually produced, so no override can put a stale number in their denominators.

`Reserved` in the table and `reserved` in `payload.reservation_advice` are that requested figure. Where the scheduler gave out more, the allocated number rides along: the table shows `4 (8 allocated)` and the finding carries an `allocated` field, so `squeue` and `sacct` still reconcile. `allocated` is present on **every** finding for a stable key set, and is null except on a `cpus` row whose allocation differs from its request.

Wherever a row's `edit_hint` names the compile reservation, it names whichever file holds the value that won. A field the suite's own `compile` block set is reported as `compile.<field>` in that suite's `tests.yaml`; every other compile field is reported as `cfg-dispatch.compile.<field>` in `root_config.yaml`. Both forms can appear in one run — a suite that overrides only `mem` gets suite-level advice for `mem` and root-level advice for `time`.

This applies to the `(build job)` row and to any `compile+sim` row alike. A builder that compiles inside its own simulation job produces no build job at all, so the compile reservation only ever appears there, inside the field-wise maximum; a `raise` after `OUT_OF_MEMORY` on a field the suite set points at the suite, because raising `cfg-dispatch.compile.mem` would leave the suite's value in force and the same advice would return on the next run. Fields the test's own `resources` governs are unaffected and still name `tests[name=…].resources.<field>`.

Advice records the run count and regression level; do not use a smoke run to shrink a nightly reservation. Apply the provided `edit_hint`, rerun, and confirm the finding clears. Disable reports with `rightsize: {report: false}`. Without `sacct` accounting, dispatch completes but emits no advice.

To inspect accounting manually, query step rows without `sacct -X`:

```bash
sacct -j <jobid> --format=JobID,Elapsed,MaxRSS
```
