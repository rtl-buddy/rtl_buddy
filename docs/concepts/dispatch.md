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

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host. Run `rb tool-check --explain slurm`.
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

Verilator, VCS, and Icarus can place outputs in a shared compile-key directory. Other builders, and builders with an absolute `builder-simv`, keep the build under the test artefact directory:

- Without shared-capable tests or seed fan-out, no separate build job is submitted; each simulation job compiles in its own directory.
- A fanned-out test still gets a build job to prevent concurrent compiles into the same test directory. Workers use the stamp left by that job.
- A job that compiles **for itself** — one whose builder cannot share a build, so no build job covers it — uses the field-wise maximum of its simulation and compile reservations.

A job gated on a build job keeps its **simulation** reservation. The compile block is not folded into it: that would inflate every gated job in the fan-out, and change which jobs share an array, to pay for a compile that normally does not happen there. What a gated job does when the build's stamp fails to validate depends on why:

- The build job recorded this test's **builder** as having run and exited non-zero **on the same inputs** — the per-build record carries its exit status, and the record's input fingerprint matches the one this job just derived. That fingerprint follows the same content rule the stamp does, so a source whose bytes are unchanged still matches after a `touch`, a re-checkout, or a hook that regenerated it identically — only a real edit counts as "the inputs moved". The job does not recompile. A deterministic compile error fails the same way again, and it would fail under the simulation reservation, so a large elaboration is killed for memory and the summary reports that kill instead of the design error. The row reports the build job's exit status and error lines, and `compile.log` is left as the build job wrote it.
- Anything else — the stamp is **absent or stale** (a moved toolchain, a clock skew, a config the build job never reached), the build job recorded a failure **without** a builder exit status (a preproc or filelist error, a crashed worker — the simulation job re-runs its own preproc, so those can pass here), or the **inputs changed** since the failed build (the fingerprints differ, so the recorded failure may not reproduce). The job recompiles, at its **simulation** size, and writes the transcript to `compile.retry.log` in its **run's** artifact directory (`run-NNNN/` under the test directory, or the test directory itself for a single run) — the retry is one run's recompile, and sibling runs of a fanned-out test share the test directory, so a test-scoped file would be one run's story overwritten and advertised by every sibling. The build job's `compile.log` is never touched. `cfg-dispatch.compile` does not size this path — it sizes only the build job (and the self-compiling jobs above) — so a suite that relies on this recovery for heavy builds must size the applicable simulation reservation (the test or testbench `resources:`, or `cfg-dispatch.resources`) for compilation; better, fix whatever drifts the inputs so the stamp validates and no gated job compiles at all. Read `compile.prebuilt_stamp_invalid` in the job's log to see that the retry happened.

A compile that reuses an existing build says so. `compile.build_reused` names the reused directory and the age of its stamp — once per build directory per process on the console (every reuse still lands in the log file), which on a local run is your terminal and under dispatch is the job's own log — and the test's `compile.log` carries the same breadcrumb, naming the command a rebuild would run. Under dispatch a gated job that reuses writes that breadcrumb into the build job's `compile.log`, keeping the build's own transcript below it; a gated *retry* never writes there at all, which is why it has its own file. A run that compiled nothing is now visible as such instead of only as a missing file. What a stamp validates against is content: every tracked input under the project root is compared by hash, so a `preproc` hook that regenerates a file byte-for-byte reuses the build, while any real edit invalidates it even on a node whose cached `stat` still describes the file as it was.

`--rebuild` compiles even where the stamp validates. It forces at most one rebuild per build directory per invocation, so a suite whose tests share a compile key still compiles once and `compile.rebuild_forced` reports it once. Under dispatch it rides the build job, which is the single writer of the shared directory; gated simulation jobs never carry it, because a whole array forced to compile would land in that one directory at once. A suite that submits no build job — every test compiling in its own directory — passes it to the simulation jobs instead, and a retried job carries whatever its first attempt held. `--rebuild` says nothing about sharing: it neither implies nor suppresses `--share-build`.

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

`jobs` controls the single local-parallel pool. `max-jobs-per-array` controls each Slurm array. See [YAML formats](../reference/yaml.md#root_configyaml) for defaults and validation.

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

| | Options |
| --- | --- |
| cpu counts | `-c` / `--cpus-per-task`, `--cpus-per-gpu`, `--threads-per-core`, `-B` / `--extra-node-info` |
| task and node counts that multiply them | `-n` / `--ntasks`, `--ntasks-per-node`, `--ntasks-per-core`, `--ntasks-per-socket`, `--ntasks-per-gpu`, `-N` / `--nodes` |

All spellings are recognised (`--ntasks=4`, `--ntasks 4`, `-n 4`, `-n4`), and the **last** occurrence is the one reported, because that is the one sbatch obeys. `--exclusive` and `--overcommit` are deliberately not included: they change what is *allocated* rather than what is requested, so `ReqCPUS` still describes the reservation.

Where such an argument is present rtl_buddy records no request for that run's rows or its build job, so the analysis falls back to `ReqCPUS`; a DEBUG line (`rightsize request_from_scheduler`) names the argument responsible.

The `edit_hint` follows. An override masks every cpus field the layering could name, so applying a hint that named one would leave the next job's reservation exactly where it was and the finding would return — the same non-retiring advice this whole rule exists to stop. While an override is in force, a `cpus` finding's `edit_hint.path` is `cfg-dispatch.sbatch-args` (with `file` pointing at `root_config.yaml`) and its `note` says which field was superseded, for example:

```
sbatch-args `--ntasks=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.
```

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
