---
description: Run tests concurrently on one host or Slurm, configure resources and retries, and diagnose dispatched builds and jobs.
---

# Parallel dispatch

Dispatch runs `test`, `randtest`, or regression work in parallel. It plans the run, shares compilations where possible, and runs builds and simulations on one host or as Slurm jobs.

```bash
rb regression --dispatch local-parallel
rb regression --dispatch slurm
rb randtest my_test 500 --dispatch slurm
rb test smoke reset_error --dispatch slurm
rb test --filter '^smoke_' --dispatch slurm
```

## Choose a backend

| Backend | Execution | Concurrency | Resources enforced | Reservation advice |
|---|---|---|---|---|
| `local` | Current process | 1 | No | No |
| `local-parallel` | Subprocesses on this host | `--jobs` or `cfg-dispatch.jobs` | No | No |
| `slurm` | Cluster jobs | Per-array throttle | Yes | From `sacct` |

- `local` is the default. `rb test` dispatches only when `--dispatch` is given on the command line; it does not inherit `cfg-dispatch.backend`. Other `cfg-dispatch` settings apply once a backend is selected.
- `rb test` takes the same name list or regex filter locally and under dispatch, and runs one simulation job per selected test. See [Run tests](tests.md#run-tests).
- Dispatch cannot be combined with `--early-stop`.
- Dispatch implies [`--share-build`](tests.md#sharing-compiled-builds-across-tests).

## Run on one host

`local-parallel` runs one subprocess pool across all suites. The default pool size is `min(4, CPU count)`.

```bash
rb regression --dispatch local-parallel
rb regression --dispatch local-parallel -j 8
rb randtest my_test 20 --dispatch local-parallel -j 4
```

- Builds run first. A simulation starts only after its build succeeds; if the build fails, its simulations are reported as failed.
- CPU, memory, and time reservations are not enforced. Choose `--jobs` for the memory demand of your heaviest concurrent tests.
- `Ctrl-C` stops the run and its child processes. `SIGKILL` skips cleanup and can leave children running.
- Verilation is never split off and simulations are never released early.

## Meet the Slurm requirements

Before using `--dispatch slurm`, provide:

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host, plus `scontrol` for the array-size probe. Check with `rb tool-check --explain slurm`.
- A shared filesystem that exposes the project, artefacts, and Python environment at identical absolute paths on submit and compute hosts.
- The project's Python environment on compute hosts. Workers run `python -m rtl_buddy` from the interpreter that started the run.

The submit process only plans, submits, waits, and collects. Compilation and simulation run on compute nodes.

## Follow a Slurm run

For each suite, dispatch submits one build job that compiles each distinct build once, then groups simulations with identical resolved resources into Slurm arrays that start after the build succeeds. Where [verilation is split off](#split-verilation-from-the-c-build), the build is two chained jobs. Results come back into the normal summary and exit status.

A compile failure fails the tests that use that build, with the compiler's exit status and error lines; unrelated builds continue. A job that produces no result (scheduler kill, node failure, dependency failure) is reported as a failed test, never dropped.

`max-jobs-per-array` throttles each array, so total concurrency can approach the throttle times the number of arrays.

Verilator, VCS, and Icarus can share a build. Other builders, and builders with an absolute `builder-simv`, compile inside each simulation job, which then reserves the field-wise maximum of its simulation and compile reservations. If no planned test can share a build and none is fanned out over seeds, no build job is submitted (`dispatch.build_job_skipped`).

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
    parallel: 4          # builds compiled at once in the build job
    split-verilate: true # Verilator suites verilate in their own Slurm job
  sbatch-args:
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200
  max-array-size: 1001   # omit to read MaxArraySize from `scontrol show config`
  progress-interval: 60
  max-wait: 7200
  retry:
    attempts: 2
    backoff-sec: 60
  rightsize:
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`jobs` sizes the local-parallel pool. `max-jobs-per-array` throttles each Slurm array. See [YAML formats](../reference/yaml.md#root_configyaml) for defaults and validation.

Quote every `time` value. YAML 1.1 can read an unquoted `4:00:00` as the integer `14400`, which rtl_buddy rejects.

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

Tests with identical resolved reservations share an array.

## Size a reservation per builder mode

A `modes:` sub-block sizes the same test for the builder mode it runs in. A `-M cov` build carries coverage counters and a `-M debug` build dumps waves, so a test that fits in 1 GB under `-M reg` can need far more memory and about twice the wall clock. Every reservation block takes one: `cfg-dispatch.resources` and `.compile`, a suite's `compile:`, and a testbench's or test's `resources:` and `compile:`.

```yaml
resources:
  cpus: 1
  mem: 1G
  time: "00:15:00"
  modes:
    cov: {mem: 16G, time: "00:30:00"}
    debug: {mem: 4G}
```

- Any mode block beats every base field. Layers resolve most specific first: `test.modes[m] > testbench.modes[m] > cfg-dispatch.modes[m] > test > testbench > cfg-dispatch > built-in default`.
- A field a mode block omits keeps its base value.
- The mode is the effective `--builder-mode`: `debug` for `rb test` and `reg` for `rb regression` when the flag is absent.
- Mode names are your own `cfg-rtl-builder.builder-opts` keys and are not checked. Quote a name that YAML reads as a boolean (`on`, `no`).
- A mode block takes `cpus`, `mem`, and `time`, plus `verilate` inside `compile:` (`compile.modes.cov.verilate`, not `compile.verilate.modes`). Other keys are rejected at load.

## Set compile resources per suite and testbench

`cfg-dispatch.compile` is one reservation for every suite's build job. A suite that needs something different sets `compile:` at the top level of its `tests.yaml`:

```yaml
compile:
  mem: 48G          # cpus and time inherited
  parallel: 1       # builds run at once in this suite's build job
```

A testbench can set its own `compile:` when a suite's entries verilate at different scales:

```yaml
compile:
  mem: 8G           # the small geometry, which most runs use

testbenches:
  - name: tb_chip_t1
    compile:
      mem: 256G
      time: "06:00:00"
```

- Fields resolve in this order: testbench `compile`, suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults.
- A testbench block wins over the suite block field by field, in both directions. Each field must be greater than zero, and `parallel` is not allowed there.
- `compile.verilate` sizes the verilate job of a split suite and `compile` sizes the C++ build alone. See [Split verilation from the C++ build](#split-verilation-from-the-c-build). `compile.split-verilate` belongs to `cfg-dispatch.compile` or the suite block, not a testbench.

The suite block is the floor for the build job. The job aggregates the testbench blocks of the tests in the run:

- `cpus`: the largest block, times `compile.parallel`.
- `mem`: the sum of the largest `min(parallel, n)` builds, since concurrent builds each hold their own peak.
- `time`: the finish time of the job's work queue, where each build starts on the first free of `parallel` workers in plan order. Builds of 30, 30, and 20 minutes on two workers take 50 minutes.

Only testbenches with selected tests count, so a run that skips `tb_chip_t1` reserves the suite's `8G`. Builds with no `compile:` of their own contribute the per-build memory the suite value implies: a 256G testbench beside an unannotated build under `compile: {mem: 8G, parallel: 2}` reserves 264G.

## Compile several builds at once

The build job compiles one build at a time by default. `compile.parallel` raises that to N concurrent builds. Set it in `cfg-dispatch.compile` or a suite's `compile:` block, which wins. It must be at least 1 and is ignored in per-test `resources:` blocks.

- Only `cpus` is scaled by `parallel`. Keep the total within the widest node of the partition.
- `compile.time` should cover the longest batch, not one build. `ceil(distinct builds / N)` times the slowest build is a safe upper bound.
- A suite that compiles one build sets `parallel: 1` so it does not reserve `cpus` times the cluster-wide value.
- At `parallel: 1`, each config runs `preproc` and then compiles before the next starts. Above 1, every config's `preproc` runs first, then the builds overlap. Hooks must not modify another config's inputs. Simulation jobs need the same property because each re-runs its own `preproc` concurrently.
- Configs that resolve to the same build are compiled once. A config whose inputs differ from that build fails with `build_job.group_input_drift`.
- A config whose compile fails is reported per test, and the build job still exits 0 so its dependents run.

## Split verilation from the C++ build

Verilation is single-threaded and the C++ build is not, so one job sized for the build wastes its cores during elaboration. Under `--dispatch slurm`, a suite that uses the Verilator family runs the two phases as chained jobs:

| Job | Reservation | Work |
|---|---|---|
| `rb-verilate-<hash>` | `compile.verilate` | Emits C++ sources and the Makefile, builds nothing |
| `rb-build-<hash>` | `compile` | Compiles and links what the verilate job emitted |

Simulation arrays still wait on the build job. `compile.verilate.cpus` defaults to 2; its `mem` and `time` inherit the resolved `compile` values. `compile.parallel` applies to each phase separately.

- If verilation fails, the verilate job still exits 0 and the build job reports the failure per test.
- If the verilate output is missing or stale, or the Verilator lacks `--no-verilate`, the build job verilates that build itself and warns `compile.build_phase_fallback`. The result is correct but unsplit.
- Set `compile.split-verilate: false` in `cfg-dispatch.compile` or a suite's `compile:` block to run one build job instead.

An `OUT_OF_MEMORY` on a large design is normally a `compile.verilate.mem` edit; `compile.mem` then only needs one compiler process's memory. Size the build job's `cpus` at the Verilator `-j` or `--build-jobs` value in `builder-opts.<mode>.compile-time` times `compile.parallel`.

## Split large groups into several arrays

Slurm refuses an array larger than `MaxArraySize` or `SchedulerParameters=max_array_tasks`. rtl_buddy reads both from `scontrol show config` once per run and splits a larger group into arrays of at most `min(max_array_tasks, MaxArraySize - 1)` elements.

- Each slice logs under `slice-N/` in the run's dispatch directory and has a `/N` job-name suffix. All slices wait on the same build job, and summary, cancellation, and advice treat them as one group.
- `max-jobs-per-array` throttles each slice, so peak concurrency is the throttle times the number of slices.
- `cfg-dispatch.max-array-size` and `max-array-tasks` override the probed values independently; an unset one still comes from `scontrol`. Either alone is enough to slice.
- If neither limit is known, the group is submitted whole. If sbatch refuses it, or refuses an array within every known limit, the error names the field to set or lower. A cluster can enforce a ceiling it does not report.

## Use a non-default or several Slurm clusters

When `sbatch-args` selects a cluster (`-M name` or `--clusters=name`), or `SBATCH_CLUSTERS` does, the array-size probe, polling, cancelling, and accounting all target that cluster. `sbatch-args` wins over the environment. A `max-wait` failure prints commands you can run as is, such as `squeue -M alpha -j '77_[1-2]'`.

With several clusters selected (`--clusters=a,b` or `all`), Slurm picks the cluster at submit and no single array limit applies. Set `cfg-dispatch.max-array-size`, or `max-array-tasks` alone if that is the ceiling you know. Build-job deduplication is also skipped.

## Reuse shared builds and force a rebuild

A shared build is reused when no tracked input under the project root has changed. Inputs are compared by content, so a `preproc` hook that regenerates a file byte for byte still reuses the build. See [Sharing compiled builds](tests.md#sharing-compiled-builds-across-tests).

- `compile.build_reused` names the reused directory and its age. The test's `compile.log` records the same with the command a rebuild would run.
- `--rebuild` compiles even where the build would be reused, once per build directory per run. Prefer it to deleting `artefacts/.shared-builds/`. Under dispatch the build job performs the rebuild, or the simulation jobs do when the suite has no build job.
- `compile.build_lock_wait` means another process is compiling into the same directory. It is not a hang.
- Build jobs are named `rb-build-<hash>` from the suite directory and use `--dependency=singleton`, so one build job per suite runs at a time per cluster. A re-run after `Ctrl-C` waits for the interrupted build job, then reuses its build if the inputs are unchanged. Concurrent `--run-tag` runs of one suite serialise their builds while their simulations overlap.
- A `--dependency` in `sbatch-args` or `SBATCH_DEPENDENCY` is combined with the singleton (`afterok:7,singleton`). An expression using `?` cannot be combined, so the singleton is dropped. A `--job-name` in `sbatch-args` renames simulation jobs only.
- A site with `DependencyParameters=disable_remote_singleton` may not serialise across clusters. Pin one cluster with `-M` in `sbatch-args`, or see [known issues](../known-issues.md).
- When a build fails, Slurm removes the dependent jobs (`--kill-on-invalid-dep=yes`). Override with `--kill-on-invalid-dep=no` in `sbatch-args`.

## Start simulations as each build finishes

Simulation jobs wait on the suite's build job, but the build job releases each build's simulations as soon as that build is done, so a fast build does not wait for the slowest. A released array element still honours the array's throttle.

These jobs start only when the build job ends:

- Jobs whose build failed or left no usable build, and retried jobs.
- Every job of a suite whose `sbatch-args` has its own `--dependency=...`, because clearing it would drop your gate (`dispatch.gates_skipped`). An exported `SBATCH_DEPENDENCY` does not disable release (`dispatch.env_dependency_overridden`); put the expression in `sbatch-args` if it must hold these jobs.
- All jobs, when `scontrol` is not on the PATH of the compute node running the build job (`dispatch.release_unavailable`).

## Recover when a gated job cannot use the build

When a simulation job finds the build unusable, the outcome depends on what the build job recorded for that test. The job keeps its simulation reservation.

- **The build failed on the same inputs.** The job does not recompile. Its row reports the build job's exit status and error lines.
- **The build succeeded but is missing or does not match.** The job does not recompile. The row and `compile.build_stamp_rejected` say what was found, and whether this job's inputs match the build job's. A mismatch usually means `preproc` generates different bytes on this node, or an edit landed mid-run.
- **The build succeeded but could not be recorded.** The build job logged `compile.stamp_write_failed`, usually from a read-only or full filesystem. Fix it and re-run.
- **Nothing proves the build cannot work.** This covers a crashed or cancelled build job, a `preproc` or filelist error, and inputs changed since the failure. The job recompiles at its simulation size, writes `compile.retry.log` in its run directory, and logs `compile.prebuilt_stamp_invalid`.

`cfg-dispatch.compile` does not size that recompile. Fix whatever changes the inputs, or size the simulation reservation for compilation.

## Interrupted runs: warn, cancel, adopt

If the head process dies (late `Ctrl-C`, dropped SSH session, login node reboot), its Slurm jobs keep running. Every `--dispatch slurm` run checks the suite's `artefacts/.dispatch/` records at start-up for other runs whose jobs are still queued. `--orphans` (or `cfg-dispatch.orphans`) chooses what to do with them:

| Policy | Effect |
|---|---|
| `warn` (default) | Logs `dispatch.orphans_found` with the run and job IDs, then submits a fresh fleet. The old jobs keep running. |
| `cancel` | Cancels the old jobs and waits until the queue no longer holds them, then submits. If jobs remain after 30 seconds, nothing is submitted and the run fails with `dispatch.orphans_cancel_failed` naming the IDs to cancel by hand. |
| `adopt` | Submits nothing. Waits on the old jobs, collects their results, and reports them as this run's (`dispatch.orphans_adopted`). |

A run that died while still submitting counts as live for `warn` and `cancel` but can never be adopted, because rows it never submitted would score as missing. `--orphans` has no effect for `local` and `local-parallel`; `--orphans adopt` there is a fatal error.

`adopt` needs exactly one orphan whose recorded run matches this invocation: the same test config, backend, tests and run IDs in order, the same per-test plan (plusargs, plusdefines, testbench, hooks, `resources:`, builder, resolved seed), and the same `--builder-mode`, `--builder`, `--extra-sim-timeout`, `--rebuild`, and reservations. Any difference is a fatal error naming the test and field. A `rb randtest` run that draws fresh seeds can never be adopted, and two orphans with live jobs are also fatal.

## Retry license-queue timeouts

Retry is off until `retry.attempts` is nonzero. It applies to simulation jobs only and only when the evidence points at a VCS license wait: the Slurm state is `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED`, the captured output ends in license-queue banner text after the last `-licqueue` marker, and the suite's build job succeeded. For `local-parallel`, the queue text alone is enough.

The delay before retry `n` is `min(backoff-max-sec, backoff-sec * 2^(n-1))` with jitter. A `--begin` in `sbatch-args` overrides it. `max-wait` bounds each collection round, not the whole run. Each retry writes `slurm-<tag>-retry<N>.log`; `dispatch.retry` and `dispatch.result_missing` in `rtl_buddy.log` give the reason. An exhausted retry stays a failure.

Where available, prefer Slurm `Licenses=` with `--licenses=<name>:1`, so jobs wait without holding an allocation. VCS waits under `-licqueue` count against the time limit, so give `compile.time` headroom and keep `parallel` at or below the site's license pool.

## Monitor and stop a run

At normal verbosity the console shows submission lines with job IDs, progress at each `progress-interval`, a line when each suite drains, and a warning with outstanding IDs when `max-wait` expires. Set `progress-interval: 0` to silence progress; events stay in the log.

On timeout or `Ctrl-C`, the head cancels the outstanding jobs and re-checks the queue. If jobs survive `scancel`, `dispatch.orphans_cancel_failed` names them, and the next run finds them as orphans.

Logs are separated by process:

| Process | rtl_buddy log | Related files |
|---|---|---|
| Head | `<suite>/rtl_buddy.log` | Console output |
| Simulation | `artefacts/<test>/dispatch/rtl_buddy-<tag>.log` | `result-<tag>.json`, `slurm-<tag>.log` or `local-parallel-<tag>.log` |
| Verilate | `artefacts/.dispatch/verilate-rtl_buddy-<pid>.log` | `verilate-<pid>.log` |
| Build | `artefacts/.dispatch/build-rtl_buddy-<pid>.log` | `build-<pid>.log` |

`<tag>` is the run ID or `single`; `<pid>` is the head process ID. Failure messages point to the relevant logs.

- Regressions that list several test configs from one directory get a namespace directory under `.dispatch`, such as `artefacts/.dispatch/tests-7a91c2d4e6f8/`. If two such configs expand tests onto the same per-test directory, the regression exits before submitting. Rename one test or separate the configs.
- `--run-tag <name>` moves these paths into `artefacts/.runs/<name>/`. The shared build directory stays put, so tagged runs that compile the same thing reuse one build. See [Namespace concurrent runs](execution-context.md#namespace-concurrent-runs).
- Each test's `result-<tag>.json` includes its build record, which [`rb graph results`](graph.md#results-overlay) shows.

## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns the findings in `payload.reservation_advice`. rtl_buddy never edits configuration. Turn the report off with `rightsize: {report: false}`. Without `sacct` accounting there is no advice.

Advice is per test, from the peak across runs in this invocation:

- Utilization below `over-threshold` suggests a reduction. A lower value reports fewer tests: `0.3` lists only tests that used under a third of what they asked for.
- Utilization above `near-limit`, a `TIMEOUT`, or an `OUT_OF_MEMORY` suggests an increase. These are listed first because under-reservation costs failed work.
- Suggestions are peak times `margin`, with floors of 5 minutes and 128 MiB.
- Time advice is given for Verilator only, because VCS license waits distort elapsed time. Memory advice is skipped for runs shorter than the accounting sample interval, except after an out-of-memory kill.
- `phase` is `sim`, `compile+sim`, `compile`, or `verilate`, and `edit_hint` names the field that controlled the allocation.

Do not use a smoke run to shrink a nightly reservation. Apply the `edit_hint`, rerun, and confirm the finding clears. To inspect accounting yourself, query step rows without `sacct -X`.

## Read advice for the build and verilate jobs

The build job gets a row named `(build job)` with `phase: compile`, and a split suite also gets `(verilate job)` with `phase: verilate`. Both suggest `time` in either direction and `cpus` only downward.

- The `cpus` suggestion appears only when the job ran one build at a time. With `compile.parallel` above 1, size `parallel` against the suite's distinct builds first, then read `cpus` from a `parallel: 1` run.
- A `reduce` is withheld when every build was reused, when the head has no per-build records, or when the job ended within one accounting interval. `rightsize.build_advice_withheld` records why. `raise` advice is unaffected.
- When the value is a sum or maximum over several builds, no single entry can lower it, so `reduce` is withheld. A `raise` names the contributing testbench and translates the total into that entry's own new value.
- `edit_hint` names the file holding the winning value: `compile.<field>` in the suite's `tests.yaml` when the suite set it, otherwise `cfg-dispatch.compile.<field>` in `root_config.yaml`. Verilate paths add `.verilate`.

## Judge cpu advice against requested cpus

Cpu efficiency is measured against the cpus the job requested, not the cpus the scheduler allocated. A partition that allocates whole cores can charge two cpus for `--cpus-per-task=1`, which would make every single-threaded run look half used. The table shows `4 (8 allocated)` when the two differ, and a `reduce` is suggested only when it is strictly below the request. `mem` and `time` advice use the scheduler's own figures.

## Handle cpu overrides in sbatch-args

`cfg-dispatch.sbatch-args` is appended after the generated reservation flags, so these options change what jobs actually request:

- `-c` / `--cpus-per-task`
- `-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes`
- A GPU count with `--ntasks-per-gpu` and no `--ntasks`

`SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` count too; a command-line option beats its variable. `SBATCH_CPUS_PER_TASK`, `--exclusive`, `--overcommit`, and placement options such as `--threads-per-core` do not.

While an override is in force, `cpus` findings point at `cfg-dispatch.sbatch-args` (`path: env` when only the environment set it) and say which field it superseded. `suggested` is the whole-job cpu count. Only a single `--cpus-per-task` can take it directly:

```
sbatch-args `--cpus-per-task=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.
```

A lone task or node count multiplies the per-task cpus. The note names both levers:

```
`--ntasks=4` multiplies this job's cpu request: the generated --cpus-per-task
from tests[name=wr_single].resources.cpus still applies, so the request is 8
per task x 4 tasks. Suggested value is the whole-job cpu count — lower
tests[name=wr_single].resources.cpus, the task count in sbatch-args, or both;
no single one of them takes it.
```

Several arguments combine by sbatch's own precedence, and the note leaves the arithmetic to you:

```
sbatch-args supersedes tests[name=wr_single].resources.cpus: `--ntasks=4` and
`--cpus-per-task=2` set this job's cpu request together. Suggested value is
the whole-job cpu count — decompose it across them per sbatch's own
precedence; no single one of them takes it.
```

A test whose retries ran under different cpu requests gets no `cpus` row (`rightsize.cpus_advice_withheld`). A direct `--cpus-per-task` in `sbatch-args` also removes the compile `cpus` floor.

## Troubleshoot reservations, arrays, and waits

- **`dispatch.reservations_ignored`:** `local-parallel` does not enforce `cpus`, `mem`, or `time`. Lower `--jobs` if memory runs out.
- **`OUT_OF_MEMORY` (Slurm) or `Killed` / exit 137 (local):** raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`. Elaboration of large generated structures can be the memory peak.
- **sbatch refuses an array:** set `cfg-dispatch.max-array-size` or `max-array-tasks`. `dispatch.max_array_size_unknown` (INFO) means no limit could be read.
- **`dispatch.max_wait_exceeded`:** jobs were still outstanding at `max-wait`. Run the printed `squeue` command. The head cancels them.
- **`dispatch.wait_poll_failed`:** `squeue` timed out or the cluster is unreachable. Polling continues; a scheduler that never answers ends in the `max-wait` failure.
- **`dispatch.wait_states_unfiltered`:** an old `squeue` refused the state filter, so a job held in another state can be reported finished early.
- **`dispatch.result_missing`:** a job produced no result. It counts as a failure unless the message says it is being retried.
- **`dispatch.retry_abandoned`:** a retry could not be submitted. The earlier result stays scored.
- **`dispatch.orphans_found` / `dispatch.orphans_cancel_failed`:** see [Interrupted runs](#interrupted-runs-warn-cancel-adopt).

## Troubleshoot builds

- **`dispatch.build_job_deduped` and the build job stays `PENDING`:** inspect the job it waits for with `squeue -j <ids> -O JobID,State,Reason` and `scontrol show job <id>`. `RUNNING`, or `PENDING` for `Resources` or `Priority`, is normal; cancelling it discards a build this run will reuse. `scancel` it only if it is held (`JobHeldUser`, `JobHeldAdmin`), unschedulable (`PartitionConfig`, `BadConstraints`), or an abandoned run.
- **`build_job.group_input_drift`:** configs that share one build have different inputs. Give the config its own build, or fix the hook that rewrites the input per test.
- **`compile.build_stamp_rejected`, `compile.stamp_write_failed`, `compile.prebuilt_stamp_invalid`:** see [Recover when a gated job cannot use the build](#recover-when-a-gated-job-cannot-use-the-build).
- **`dispatch.binary_mismatch`:** runs of one build directory did not all use the same executable, because one run rebuilt it while others used it. Results are scored as they ran; re-run to confirm.
- **`compile.build_phase_fallback`** (`marker-missing`, `marker-stale`, `no-verilate-unsupported`): the build job verilated for itself. The result is correct but unsplit.
- **`compile.verilate_failed`:** verilation failed. The build job reports it per test without compiling again.
- **`compile.verilate_marker_write_failed`:** the verilate output could not be recorded, so the build job compiles that build in full.
- **`compile.license_queued`:** a VCS build waited for a license. Add `compile.time` headroom or lower `parallel`.
- **`build_job.compile_worker_error`:** the build job's compile raised an error. The test counts as a compile failure and its simulation job retries the compile.

## Troubleshoot early release

- **`dispatch.gates_unavailable`:** the build job never saw the release list the head writes after its last submission. Jobs start when the build job ends.
- **`dispatch.release_failed`:** the scheduler refused a release; those jobs start when the build job ends. A timeout or unusable `scontrol` turns release off for the rest of the build job.
- **`dispatch.release_skipped`:** a build's record could not be written, so its jobs are not released.
- **`dispatch.release_unavailable`:** `scontrol` is not on the compute node's PATH. Nothing is released early.
- **`dispatch.build_result_partial`:** the build job did not finish. The head uses the records it has and treats other tests as missing results.
- **`dispatch.build_result_final_write_lost`:** the build job succeeded but its final record was lost. Every gate opens, and a missing simulation result is an ordinary failure.
