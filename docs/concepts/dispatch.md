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

| Backend | Execution | Concurrency | Resource enforcement | Usage advice |
|---|---|---|---|---|
| `local` | Current process | 1 | None | None |
| `local-parallel` | Subprocesses on this host | `--jobs` or `cfg-dispatch.jobs` | No | No |
| `slurm` | Cluster jobs | Per-array throttle | Yes | From `sacct` |

- `local` is the default. `rb test` dispatches only when `--dispatch` is given explicitly; it does not inherit `cfg-dispatch.backend`. Other `cfg-dispatch` settings apply once a backend is selected.
- `rb test` takes the same name list or regex filter locally and under dispatch, and creates one simulation job per selected test. See [Run tests](tests.md#run-tests).
- Dispatch cannot be combined with `--early-stop`.
- Dispatch implies [`--share-build`](tests.md#sharing-compiled-builds-across-tests), expands sweep hooks once on the head, and skips the per-tree lock in worker jobs.

## Run on one host

`local-parallel` uses one global subprocess pool across all suites and resource groups:

```bash
rb regression --dispatch local-parallel       # min(4, CPU count)
rb regression --dispatch local-parallel -j 8
rb randtest my_test 20 --dispatch local-parallel -j 4
```

- Build jobs run first. A simulation starts only after its build exits 0; a failed build makes its dependent simulations dispatch failures.
- CPU, memory, and time reservations are not enforced and produce no advice. A non-default reservation logs `dispatch.reservations_ignored`. Choose `--jobs` for the memory demand of the heaviest concurrent tests.
- `Ctrl-C` stops the head and its process groups. `SIGKILL` skips cleanup and may leave child processes running.
- `local-parallel` never splits verilation or releases simulations early.

## Meet the Slurm requirements

Before using `--dispatch slurm`, provide:

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host, plus `scontrol` for the array-size probe. Run `rb tool-check --explain slurm`.
- A shared filesystem exposing the project, artefacts, and Python environment at identical absolute paths on submit and compute hosts.
- The project's Python environment on compute hosts. Workers run `sys.executable -m rtl_buddy`.

The submit process only plans, submits, waits, and collects. Compilation and simulation run on compute nodes.

## Follow a Slurm run

For each suite, dispatch:

1. Writes a plan and, when needed, submits one build job. Where [verilation is split off](#split-verilation-from-the-c-build) this is two chained jobs.
2. Builds each unique compile key. A compile key fingerprints sources, flags, defines, and the resolved builder.
3. Groups simulations with identical resolved resources into Slurm arrays, gated with `afterok` on the build.
4. Collects each worker's `result.json` into the normal summary and exit status.

Arrays group by resource tuple, not compile key, so tests may share an executable but not an array, or an array but not a build. `max-jobs-per-array` is a `%N` throttle on each array; total concurrency can approach the throttle times the number of arrays.

A missing result from a scheduler kill, worker crash, or dependency failure is a failed row, not a dropped test. A compile failure for one compile key does not stop unrelated keys. Its tests report that compile's exit status and error lines, and their simulation jobs do not repeat it.

## Know which suites get a build job

Verilator, VCS, and Icarus can place outputs in a shared compile-key directory. Other builders, and builders with an absolute `builder-simv`, keep the build under the test artefact directory.

- With no shared-capable tests and no seed fan-out, no build job is submitted (`dispatch.build_job_skipped`). Each simulation job compiles in its own directory.
- A fanned-out test still gets a build job, which prevents concurrent compiles into the same test directory. Workers use the stamp that job leaves.
- A job that compiles for itself, because its builder cannot share a build, reserves the field-wise maximum of its simulation and compile reservations.
- A job gated on a build job keeps its simulation reservation. See [recovery when a gated job cannot use the build](#recover-when-a-gated-job-cannot-use-the-build).

## Compile several builds at once

The build job compiles one compile key at a time. `compile.parallel` raises that to N distinct keys compiled concurrently in the same job. Set it in `cfg-dispatch.compile`, or in a suite's own `compile:` block, which wins. Concurrency is over distinct keys, never over tests: configs sharing a key are compiled once, by whichever the job reaches first, and the rest adopt that build.

- Adoption requires the inputs the first build consumed to be unchanged and no new file to alter resolution (a `-y` file or shadowing header).
- A config whose consumed input differs fails with `build_job.group_input_drift`, because one compile key is one binary. Give it its own compile key, or fix the hook that rewrites the input per test.
- Builders that report no dependencies (VCS, Icarus) cannot narrow adoption this way and compare the full stamp.
- Configs whose resolved `builder-simv` output is one file are grouped the same way. That is an absolute pin, or a relative path whose `..` escapes the per-test workspace.
- A config whose compile fails is reported per test, and the job still exits 0 so its `afterok` dependents run.

Preprocessing hooks always run serially. Their position depends on `parallel`:

- At the default `parallel: 1`, the job runs `preproc` and then the compile for one config before the next. A hook that regenerates a shared input cannot overwrite what an earlier config is about to compile.
- Above 1, every config's `preproc` runs first, then the builders overlap. No config's `preproc` may mutate another config's inputs. Simulation jobs need the same property, because each `rb _test-job` re-runs its own `preproc` concurrently.

## Split large groups into several arrays

Slurm refuses an array larger than `MaxArraySize` or than `SchedulerParameters=max_array_tasks`. rtl_buddy reads both from `scontrol show config` once per run and splits a larger group into arrays of at most `min(max_array_tasks, MaxArraySize - 1)` elements.

- Each slice gets its own manifest and logs under `slice-N/` in the run's dispatch directory and a `/N` job-name suffix. All slices wait on the same build job, and the summary, cancellation, and reservation advice treat them as one group.
- `max-jobs-per-array` throttles each slice, so peak concurrency is the throttle times the number of slices.
- `cfg-dispatch.max-array-size` and `cfg-dispatch.max-array-tasks` override the probed values independently. An unset field still comes from `scontrol`, and the probe is skipped only when both are set.
- Either limit alone is enough to slice. A site that can state only its task cap gets arrays of `max-array-tasks` elements.
- With several clusters selected, no single limit applies. Set `max-array-size`, or `max-array-tasks` alone if that is the ceiling you know. See [several clusters](#use-a-non-default-or-several-slurm-clusters).
- If neither limit is known, the group is submitted whole (`dispatch.max_array_size_unknown` in the run log). If sbatch refuses it, the error names the field to set.
- If sbatch refuses an array that is within every known limit, the error names the slice size used and the field to lower: `cfg-dispatch.max-array-tasks` when the task cap was binding, otherwise `cfg-dispatch.max-array-size`. A cluster can enforce a ceiling it does not report.
- `dispatch.max_array_size` at debug level records the probed cluster, both ceilings, the `max_elements` that governed, and `source: config` or `source: scontrol` for the governing value. An absent `max_array_tasks` means no task cap is known.

A group that fits in one array is submitted whole, with no `slice-N/` level.

## Use a non-default or several Slurm clusters

When `sbatch-args` selects a cluster (`-M name`, `-Mname`, `--clusters=name`, or `--clusters name`; the last one wins), the array-size probe, polling, cancelling, and accounting all target that cluster. `SBATCH_CLUSTERS` in the environment selects a cluster the same way; `sbatch-args` wins over it.

- Every job records the cluster that accepted it, because a job ID only means something on its own cluster. Polling issues one `squeue -M <cluster>` per cluster, cancelling one `scancel -M <cluster>`, and accounting one `sacct -M <cluster>`.
- A `max-wait` failure prints a command Slurm accepts, such as `squeue -M alpha -j '77_[1-2]'`. `dispatch.max_wait_exceeded` carries `jobs`, `clusters`, and `queries` as separate fields.
- A single-cluster site sees plain commands without `-M`.
- A selection of several clusters (`--clusters=a,b` or `all`) leaves the array limit unknown, because Slurm picks the cluster at submit. Set `cfg-dispatch.max-array-size` or `max-array-tasks`.
- With several clusters, the build-job dedup probe stands down.
- Early release issues one `scontrol -M <cluster>` batch per cluster.

## Reuse shared builds and force a rebuild

A stamp validates by content: every tracked input under the project root is compared by hash. A `preproc` hook that regenerates a file byte-for-byte reuses the build, and any real edit invalidates it. See [Sharing compiled builds](tests.md#sharing-compiled-builds-across-tests) for what invalidates a stamp.

- **Reuse is reported.** `compile.build_reused` names the reused directory and the age of its stamp. It prints once per build directory per process on the console, and every reuse lands in the log file. The test's `compile.log` carries the same breadcrumb with the command a rebuild would run. Under dispatch, a gated job that reuses writes it at the top of the build job's `compile.log`.
- **`--rebuild` compiles even where the stamp validates.** It forces at most one rebuild per build directory per invocation, and `compile.rebuild_forced` reports it once. Prefer it to deleting `artefacts/.shared-builds/`.
- **Under dispatch, `--rebuild` rides the build job**, the single writer of the shared directory. Gated simulation jobs never carry it. A suite with no build job passes it to the simulation jobs. A retried job carries whatever its first attempt carried. `--rebuild` neither implies nor suppresses `--share-build`.
- **A build lock wait is not a hang.** `compile.build_lock_wait` means another process is compiling into the same directory.
- **The head audits builds at collect.** If runs of one build directory did not all launch the same executable, it warns `dispatch.binary_mismatch` with the number of distinct input digests. One run rebuilt the shared directory instead of reusing it, so its neighbours may have simulated a binary that was replaced under them. The runs are still scored as they ran.

## Recover when a gated job cannot use the build

When a gated simulation job's stamp check fails, the outcome depends on what the build job recorded for that test. The job keeps its simulation reservation.

- **The builder ran and failed on the same inputs.** The record has the builder's exit status and an input fingerprint matching the job's. Fingerprints follow the stamp's content rule, so a `touch` or re-checkout still matches. The job does not recompile. The row reports the build job's exit status and error lines, and `compile.log` stays as the build job wrote it.
- **The test is recorded as built, but the stamp disagrees or is missing.** The job does not recompile. The row reports what the stamp check found, and `compile.build_stamp_rejected` says the same in the job's log. It also says whether this job's inputs hash to the digest the build job recorded. A different digest usually means `preproc` generates different bytes on this node, or an edit landed mid-run.
- **The test is recorded as built with `stamp_written: false`.** The compile succeeded and only the stamp write failed, usually on a read-only or full filesystem; the build job logged `compile.stamp_write_failed`. The row names the write. Give that filesystem room and permissions, and re-run.
- **Nothing built the config and nothing proved it cannot be built.** This covers a crashed or cancelled build job, a failure with no builder exit status (a `preproc` or filelist error), changed inputs since the failure, and an unreadable envelope. The job recompiles at its simulation size and writes `compile.retry.log` in its run's artefact directory (`run-NNNN/` under the test directory, or the test directory for a single run). `compile.prebuilt_stamp_invalid` in the job's log shows the retry and what drifted.

A declining job leaves the shared directory and stamp as the build job left them, so sibling jobs still validate the same stamp and later runs still reuse the binary.

`cfg-dispatch.compile` does not size the recompile path. A suite that relies on it for heavy builds must size the simulation reservation for compilation. Better, fix whatever drifts the inputs so no gated job compiles.

## Serialise build jobs per suite

Each build job is named `rb-build-<hash>`, where the hash covers the suite directory. The verilate job of a split suite is `rb-verilate-<hash>` over the same hash. Both are submitted with `--dependency=singleton`, so at most one build job of a given identity runs at a time per cluster.

- **Interrupted runs.** A run that was Ctrl-C'd leaves its build job on the cluster. The re-run's build job waits for it while holding no allocation, revalidates the stamp under the build lock, and reuses the build if the inputs are unchanged. `--rebuild`, an edit, or a different builder recompiles.
- **Identity is the suite directory alone**, because that owns `artefacts/.shared-builds/`. Two runs of one suite share a name whether or not they compile the same thing. A false match costs queue latency, never a wrong build. A different suite never waits. Concurrent `--run-tag` runs of one suite still serialise their build jobs, while their simulation jobs overlap.
- **The head names the predecessors.** It asks `squeue` for its own jobs of that name in any state that still holds a record, and reports them as `dispatch.build_job_deduped` with their IDs. The probe is informational. If it is unavailable, the line is absent and the guarantee is unchanged (`dispatch.build_dedup_unavailable`, DEBUG).
- **Your own `--dependency` is composed**, for example `afterok:7,singleton`. It comes from the last `--dependency` or `-d` in `sbatch-args`, or from `SBATCH_DEPENDENCY` when `sbatch-args` names none. An expression using the any-of separator `?` cannot be composed, so the dedup stands down (`dispatch.build_dedup_unavailable`, DEBUG) and the build lock does the work.
- **A `--job-name` in `sbatch-args` does not rename the build job.** The generated name is emitted last because the singleton serialises on it. It still renames simulation jobs.
- **A federation may not serialise across clusters.** A site with `DependencyParameters=disable_remote_singleton` fulfils `singleton` on the submitting cluster only. Pin one cluster with `-M` in `sbatch-args`, or rely on the shared build directory's `flock` (see [known issues](../known-issues.md)). A multi-cluster selection logs this caveat once per run at DEBUG.

`local-parallel` needs none of this, because the build lock already serialises processes on one host.

## Diagnose a build job that stays pending

If a build job stays `PENDING` after `dispatch.build_job_deduped`, look at the job it waits for before acting:

```bash
squeue -j <ids> -O JobID,State,Reason     # ids from the warning
squeue --name=<job name>                  # to find them
scontrol show job <id>
```

- A predecessor that is `RUNNING`, or `PENDING` for a capacity reason such as `Resources` or `Priority`, is the serialisation working. The wait ends when that build does. Cancelling it throws away the build this run is about to reuse, and the simulation jobs gated on it.
- `scancel` a predecessor that will not finish: held (`JobHeldUser`, `JobHeldAdmin`), unschedulable (`PartitionConfig`, `BadConstraints`), or an abandoned run you no longer want. Nothing times such a job out by default.

## Handle dependents of a failed build job

When a build job exists, every dependent is submitted with `--kill-on-invalid-dep=yes`. A failed build removes jobs that could never satisfy `afterok`, and collection also cancels any `DependencyNeverSatisfied` remnants. A `--kill-on-invalid-dep=no` in `sbatch-args` overrides the default.

A job whose only gate is `singleton` carries no `--kill-on-invalid-dep`, because it can never become unsatisfiable. That is the verilate job, and the build job of an unsplit suite. A configured `afterok` composed into either can still become unsatisfiable.

A split suite's build job gates on its verilate job with `afterok`, so it carries `--kill-on-invalid-dep=yes`. For that job the flag is appended after `sbatch-args`, so `--kill-on-invalid-dep=no` in `sbatch-args` applies to simulation jobs only.

## Split verilation from the C++ build

Verilation is single-threaded; the C++ build that follows is not. One job sized for the build holds its cores idle for the whole elaboration, which on a large design is the longer half. Under `--dispatch slurm`, a suite whose builds use the Verilator family runs the two as chained jobs:

| Job | Reservation | Work |
|---|---|---|
| `rb-verilate-<hash>` | `compile.verilate` | Emits C++ sources and the Makefile under `--Mdir`, and builds nothing |
| `rb-build-<hash>` | `compile` | Compiles and links what the verilate job emitted |

The build job depends on the verilate job (`afterok`) and both carry the `singleton` dedup. Simulation arrays are gated on the build job. `dispatch.verilate_submitted` reports the submission. Compile keys, fingerprints, build directories, sharing, `--rebuild`, adoption, and early release are identical to an unsplit run. `compile.parallel` applies to each phase separately.

The verilate job writes `.rb-verilate.json` in each build directory: the compile fingerprint, a status of `ok` or `failed`, and the transcript path. It writes no build stamp, releases no simulation gate, and always exits 0, so a failed verilation still lets the build job report it. Per compile key the build job then:

- builds with `--no-verilate` when the marker's fingerprint matches and its status is `ok` (`compile.verilate_reused` marks a key whose marker already vouched for the inputs);
- records the key as failed against the verilate transcript when the status is `failed`, without compiling again (`compile.verilate_failed`);
- verilates and builds the key itself when the marker is missing or stale, or the Verilator lacks `--no-verilate`. It logs `compile.build_phase_fallback` at WARNING with the `test` and a `reason` of `marker-missing`, `marker-stale`, or `no-verilate-unsupported`. The result is correct but unsplit, and costs only the verilate job's reservation.

`compile.verilate_marker_write_failed` means the marker could not be written; that key falls back to a full build in the build job.

Set `compile.split-verilate: false` in `cfg-dispatch.compile` or a suite's `compile:` block to run one build job instead.

## Start simulations as each compile key finishes

Every simulation job of a suite is gated on that suite's one build job. So that a key compiled in the first minute does not wait for the slowest key, the build job releases each key as it lands. Once a key has compiled and its stamp is on disk, the build job clears the dependency of exactly that key's simulation jobs with `scontrol update JobId=<id> Dependency=`. It logs one `dispatch.key_released` per key, naming the tests and job IDs.

- Jobs are still submitted with `afterok` and `--kill-on-invalid-dep=yes`, so a build job that dies mid-compile reaps every job it had not released.
- A released job is outside that net. An element still pending when the build job later dies on another key runs anyway, because its own key is built and stamped. An interrupted or failed head still cancels it by job ID.
- A released array element honours the array's `%N` throttle. Retried jobs are never released.

These are not released:

- Jobs of a key whose compile failed. They keep the gate, start after the build job, read the build record, and decline the recompile.
- Jobs of a key that compiled but left no stamp (`stamp_written: false`).
- Jobs of a key whose build record could not be written (`dispatch.release_skipped`).
- Every job of a suite whose `cfg-dispatch.sbatch-args` carries its own `--dependency=...`. That expression is the job's effective gate, and clearing it would drop the site's own serialisation. The head logs `dispatch.gates_skipped` once. An exported `SBATCH_DEPENDENCY` does not disable release (`dispatch.env_dependency_overridden`); put the expression in `sbatch-args` if it must hold these jobs.
- Every job, when `scontrol` is not on the PATH of the compute node running the build job (`dispatch.release_unavailable`, logged once). Release is issued from that node, not the submit host.

## Diagnose early release

Early release writes files and calls `scontrol` from the build job. These console messages report its failures:

- `dispatch.gates_unavailable`: the build job polls up to two minutes for `artefacts/.dispatch/gates-<pid>-<token>.json`, which the head writes after the suite's last submission, and never saw it.
- `dispatch.release_failed` (warning): the scheduler refused a release. Those jobs start when the build job ends. A timeout or an unusable `scontrol` shares a 60-second budget per key and turns release off for the rest of the build job; the warning says how many jobs were not attempted. A per-ID refusal does not.
- `dispatch.build_result_partial`: after each key the build job rewrites `build-result-<pid>-<token>.json` marked `"partial": true`, and the final write drops the mark. A partial envelope left by a build job that did not end successfully means it did not finish. The head uses the records it has and treats an unnamed test like a missing envelope.
- `dispatch.build_result_final_write_lost`: the build job ended successfully but the final write was lost. Every test was compiled, every gate opens, and a missing simulation result is classified as an ordinary one.

In both envelope cases the build job's compile-reservation advice is dropped, because its records cover only part of what the reservation paid for.

## Interrupted runs: warn, cancel, adopt

A head that is killed leaves its fleet running: Ctrl-C too late to cancel, a dropped SSH session, or a login node reboot. Before its first submission, the head writes `artefacts/.dispatch/run-<pid>-<token>.json` with the run token, suite config, plan, and rows. Its `status` is `submitting` while jobs are accepted, `running` after the last submission, `collected` when results are collected, and `cancelled` when the head cancels its fleet. A manifest left at `running` or `submitting` is what an interrupted run leaves.

Every per-run file under `.dispatch/` (`plan-`, `build-result-`, `build-`, `gates-`, `array-…/`) carries the run token beside the PID, so a reused PID after a reboot cannot overwrite files that a still-queued fleet reads.

On start, every `--dispatch slurm` run scans the suite's `artefacts/.dispatch/` tree, including namespaced directories, for manifests still `running` or `submitting` that belong to this suite and another token. It asks `squeue` whether their jobs are still queued. A `squeue` that cannot answer counts the jobs as live. A manifest with nothing left is marked `stale` and never probed again. One with live jobs is an orphan, and `--orphans` (or `cfg-dispatch.orphans`) decides what happens:

| Policy | Effect |
|---|---|
| `warn` (default) | Logs `dispatch.orphans_found` with the manifest, the run token, and the live job IDs, then submits a fresh fleet. The orphan keeps running. |
| `cancel` | `scancel`s the orphan's jobs and re-probes until the queue no longer holds them, marks the manifest `cancelled` (`dispatch.orphans_cancelled`), then submits. If jobs remain after 30 seconds, nothing is submitted: the run fails with `dispatch.orphans_cancel_failed` naming the IDs to cancel by hand. |
| `adopt` | Submits nothing. Waits on the orphan's jobs, collects their result envelopes, and marks the manifest `collected` (`dispatch.orphans_adopted`). |

A `submitting` record counts as live for `warn` and `cancel`, but can never be adopted, because rows its head never submitted would score as missing results.

Orphans are identified from the manifest, never from scheduler job names, because two runs of one suite submit the same build-job name. `--orphans` is inert for `local` and `local-parallel`, which leave nothing behind; `--orphans adopt` there is a fatal error.

## Adopt an interrupted run

`--orphans adopt` requires exactly one orphan whose recorded run matches this invocation:

- the same test config, backend, and expanded tests and run IDs in the same order;
- the same plan, compared entry by entry: plusargs, plusdefines, resolved testbench, hook paths, per-test `resources:`, builder, and the resolved seed with its master seed;
- the same invocation options: `--builder-mode`, `--builder`, `--extra-sim-timeout`, the shared-build root, `--rebuild`, and each job's resolved reservation.

Any difference is a fatal error naming the test and the field, because adopting across it would report the orphan's results under this run's configuration. A `rb randtest` run that draws fresh seeds plans different seeds every time and can never be adopted. Two orphans with live jobs are also fatal, since choosing one would abandon the other's fleet.

Adopted envelopes are accepted by the orphan's run token, the same identity check a live head makes, so a stale envelope from an older run is rejected.

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
    parallel: 4          # builds compiled at once; reserves up to cpus x parallel,
                         # capped at the planned test count
    split-verilate: true # Verilator suites verilate in their own Slurm job
    verilate:            # that job's reservation; mem and time inherit compile
      cpus: 2
  sbatch-args:
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200
  max-array-size: 1001   # omit to read MaxArraySize from `scontrol show config`
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

`jobs` sizes the single local-parallel pool. `max-jobs-per-array` throttles each Slurm array, and `max-array-size` sets how large one array may be before the group is split. See [YAML formats](../reference/yaml.md#root_configyaml) for defaults and validation.

Always quote `time` values. YAML 1.1 can parse an unquoted `4:00:00` as the integer `14400`, and rtl_buddy rejects that form. Quote times in global, compile, testbench, and test reservations, and in every [`modes:`](#size-a-reservation-per-builder-mode) block.

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

Tests with identical resolved reservations share an array. Compilation normally uses `cfg-dispatch.compile`. When compilation happens inside a simulation job, that job receives the field-wise maximum of both reservations.

## Size a reservation per builder mode

A `modes:` sub-block sizes the same test for the builder mode it runs in. A `-M cov` build carries per-point counters through the design and a `-M debug` build dumps waves, so a simulation that fits in 1 GB under `-M reg` can need far more memory and about twice the wall clock.

Every reservation block takes one: `cfg-dispatch.resources`, `cfg-dispatch.compile`, a suite's top-level `compile:`, and a testbench's or test's `resources:` and `compile:`.

```yaml
resources:
  cpus: 1
  mem: 1G
  time: "00:15:00"
  modes:
    cov: {mem: 16G, time: "00:30:00"}
    debug: {mem: 4G}
```

- The base value resolves as usual, then the block for the run's mode is applied over it, least specific layer first. Any mode block beats every base field, so a suite-wide `modes.cov.mem` is not undone by one test's base `mem`.

  ```text
  test.modes[m] > testbench.modes[m] > cfg-dispatch.modes[m]
      > test > testbench > cfg-dispatch > built-in default
  ```

- A field a mode block omits keeps its base value. A mode with no block reserves the base value.
- The mode is the effective `--builder-mode`: `debug` for `rb test` and `reg` for `rb regression` when the flag is absent. Jobs carry the same value to the compute node.
- Mode names are your own `cfg-rtl-builder.builder-opts` keys. Nothing checks that the mode exists. Quote a name YAML 1.1 reads as a boolean (`on`, `no`, `yes`).
- A mode block takes `cpus`, `mem`, and `time`, plus `verilate` inside a `compile:` block. `parallel`, `split-verilate`, a nested `modes:`, and unknown keys are rejected at load. `modes:` is not accepted in `compile.verilate` (write `compile.modes.<mode>.verilate`) or on an elaboration profile.
- A compile mode block layers the same way. A coverage build is its own compile key, so its verilate peak is its own figure:

  ```yaml
  compile:
    mem: 8G
    modes:
      cov:
        mem: 32G
        verilate: {mem: 48G}
  ```

`modes:` is a scheduling fact only and is not part of the compile fingerprint, so changing one never invalidates a shared build stamp. Reservation advice names the mode key that governed, for example `tests[name=...].resources.modes.cov.mem` or `compile.modes.cov.mem`.

## Set compile resources per suite and testbench

`cfg-dispatch.compile` is one reservation for every suite's build job. A suite that differs states its own at the top level of its `tests.yaml`, in the same `{cpus, mem, time}` shape:

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G          # this suite's verilation; cpus and time inherited
  parallel: 1       # builds run at once in this suite's build job

testbenches:
  - name: soc_tb
    ...
```

The compile reservation resolves field by field: testbench `compile`, suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults. The example keeps the cluster-wide `cpus: 8` and `time: "02:00:00"` and changes memory and concurrency. The block sizes the suite's build job, and for a builder that cannot share a build, the compile half of each simulation job's field-wise maximum. It is not part of the compile fingerprint.

Where [verilation is split off](#split-verilation-from-the-c-build), the block sizes two jobs:

- A `compile.verilate` sub-block of `{cpus, mem, time}` sizes the verilate job and layers over the same three layers. `compile` then sizes the C++ build job alone.
- `compile.verilate.cpus` defaults to 2 because verilation is single-threaded. Its `mem` and `time` inherit the resolved `compile` values.
- `compile.split-verilate` belongs to `cfg-dispatch.compile` or the suite block, and is rejected in a testbench block.

A testbench states its own `compile:` when a suite's entries verilate at different scales, such as one top level at two geometries:

```yaml
compile:
  mem: 8G           # the small geometry, which most pull requests run

testbenches:
  - name: tb_chip_small
    filelist: [...]
  - name: tb_chip_t1
    filelist: [...]
    compile:
      mem: 256G
      time: "06:00:00"
```

A testbench block takes the same `{cpus, mem, time}` shape and wins over the suite block field by field, in both directions. Writing `parallel` there is an error, because it belongs to the suite block or `cfg-dispatch`.

## Aggregate testbench compile blocks in the build job

The two blocks mean different things. A suite-level `compile:` describes the whole build job, the allocation you see in `squeue`, and nothing below it can reduce that reservation. A testbench `compile:` describes one build. The build job aggregates them over the testbenches the plan selected tests from, then floors each result at the suite-level value:

| Field | Over the planned builds' blocks | Then |
|---|---|---|
| `cpus` | the largest single block's | floored at the suite-resolved value, then multiplied by `compile.parallel` |
| `mem` | the sum of the largest `min(parallel, n)` per-build figures, since builds in flight together each hold their own peak | floored at the suite-resolved value |
| `time` | the makespan of the job's work queue: each build goes to the first free of `parallel` workers, in plan order | floored at the suite-resolved value |

Each split phase aggregates its own fields over the same builds: the verilate job over `compile.verilate` blocks, the build job over `compile` blocks.

The makespan is a real schedule, not `ceil(sum / parallel)`. Builds of 30, 30, and 20 minutes over two workers finish in 50 minutes, not 40. At `parallel: 1` it is the serial total.

- **One build** is one distinct `(testbench, plusdefines, builder, model, assertions)` among the planned tests. Tests differing in any of these compile separately.
- **Counted individually:** a test whose builder cannot share a build, and a test that declares a `preproc:` hook. The submit host cannot see the real compile key, so this can over-count, which is the safe direction.
- **No `compile:` of its own:** a testbench adds nothing to the `cpus` or `time` aggregate. Once some build states its own `mem`, every other planned build contributes the per-build memory the suite value implies. A 256G testbench beside an unannotated build under `compile: {mem: 8G, parallel: 2}` reserves 264G.
- **No testbench states `mem`:** the suite `compile.mem` keeps its meaning of the whole job at `parallel` concurrent builds.
- **Only planned tests count.** A run that selects nothing from `tb_chip_t1` reserves the suite's `8G`.
- **Each testbench `compile:` field must be greater than zero**, or the suite fails to load.
- A simulation job that compiles for itself is sized from its own testbench's block alone.

## Read build advice for aggregated reservations

Reservation advice names the entry whose block supplied the winning field, such as `testbenches[name=tb_chip_t1].compile.mem`. Two shapes have no such entry, and the `reduce` row is withheld, with the reason and paths in `rightsize.build_advice_withheld`:

- `compile-aggregate`: the value is a sum of several builds. Writing a whole-job figure into one contributor would leave the total where it was.
- `compile-origin-tied`: two sources produce the value independently, such as two builds at the same figure or two workers finishing at the same makespan. Lowering either alone moves nothing.

`raise` advice is unaffected. Where the field is a sum, `suggested` is translated into the named contributor's own new value. For a 30 + 60 minute sum with a 135-minute target, the advice says `01:45:00` for the 60-minute entry. The machine event carries the whole-job figure as `suggested_total` beside the `aggregate_delta`. For `time`, rtl_buddy re-runs the `parallel`-worker schedule on the proposed edit, so applying the advice clears the target.

## Size compile parallelism, memory, and time

- **`parallel`** layers over `cfg-dispatch.compile.parallel` and must be at least 1. A suite that compiles one key writes `parallel: 1`, so its build job reserves `cpus` instead of `cpus` times the cluster-wide value. Only `cpus` is scaled for you; keep the total within the partition's widest node. The build job's `Compiling N distinct build(s)` line names the key that governed. When the planned-config cap lowered the value, the line quotes what the file holds and reports the cap separately.
- **`parallel` is silently dropped** in a per-test or per-testbench `resources:` block. It is rejected in a testbench `compile:` block and in any `modes:` block.
- **Suite `compile.time`** should cover the longest batch, not one build. `ceil(distinct builds / N)` times the slowest build is a safe upper bound, close to the real figure only when builds take similar times. At `parallel: 1` it is the serial total.
- **Suite `compile.mem`** is for `parallel` concurrent builds while no testbench states its own, because only `cpus` is scaled for you. Once any testbench states one, it is one build's peak for each build that has none.
- **Size memory from elaboration**, not simulation. Large generated structures can make elaboration the memory peak. Slurm reports `OUT_OF_MEMORY`; local runs may show `Killed`, SIGKILL, or exit 137. Raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`.
- **With the split**, verilation holds the compile's peak RSS. An `OUT_OF_MEMORY` on a large design is a `compile.verilate.mem` edit, and its `cpus` can stay at the default. The build job needs one compiler process's memory, so `compile.mem` can come down. Size its `cpus` at the Verilator `-j` or `--build-jobs` in `builder-opts.<mode>.compile-time` times `compile.parallel`.
- **VCS license waits** under `-licqueue` count against the Slurm time limit, so give `compile.time` queue headroom. `compile.license_queued` records only completed builds that waited. N concurrent elaborations hold up to N licenses, so keep `parallel` at or below the site's license pool.
- Dispatch requests `--acctg-freq=task=1` unless `sbatch-args` supplies it. Keep fine-grained accounting for useful memory advice on short jobs.

## Retry license-queue timeouts

Retry is disabled until `retry.attempts` is nonzero. It applies to simulation jobs only, and retries a missing result only when evidence identifies a VCS license wait:

- Slurm state is `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED`. `FAILED` and `CANCELLED` are not retried.
- Captured output ends in license-queue banner content after the last `-licqueue` marker.
- The suite build job succeeded, so the shared-build stamp is available.

For `local-parallel`, queue evidence alone is sufficient because there is no scheduler state. Build jobs are never retried.

- The delay for retry `n` is `min(backoff-max-sec, backoff-sec * 2^(n-1))`, multiplied by jitter. Slurm holds retries with `--begin`; local-parallel holds them outside the worker pool. A `--begin` in `sbatch-args` overrides the backoff.
- `max-wait` bounds each collection round, not the total run, and excludes the requested backoff. An exhausted retry remains a failure.
- A retry submission failure logs `dispatch.retry_abandoned` and keeps the already-scored run.
- Each retry gets a scheduler log named `slurm-<tag>-retry<N>.log`. Test capture files are reused and truncated by the next attempt. `dispatch.retry` and `dispatch.result_missing` in `rtl_buddy.log` are the durable reason trail.

Where available, prefer scheduler-side gating with Slurm `Licenses=` and `--licenses=<name>:1`: jobs wait without consuming an allocation.

## Monitor and stop a run

At normal verbosity dispatch prints:

- suite submission lines with build and simulation job IDs;
- progress when counts change and at `progress-interval` heartbeats;
- a line when each suite drains;
- a warning with outstanding IDs when `max-wait` expires.

Set `progress-interval: 0` to suppress console progress; events remain in the head log.

On timeout or interrupt, the head cancels the outstanding fleet and re-probes the queue before retiring its run record. If jobs survived `scancel`, the record stays `running` and `dispatch.orphans_cancel_failed` names them, so the next run can find them. Each re-probe carries the remaining grace period as its `squeue` timeout, and a query that runs out of time counts the jobs as still live.

The drain poll asks `squeue` for every state in which a job is still alive, including the held states and `STOPPED`. `PREEMPTED` and `REVOKED` are left out: Slurm keeps those records until purged, so waiting on them would delay collection and retry. A preempted job is where the collector expects it and can be retried.

- `dispatch.wait_states_narrowed` (DEBUG): an old `squeue` refused a state name, so rtl_buddy dropped that name and asked again.
- `dispatch.wait_states_unfiltered` (WARNING): an old `squeue` refused the filter without naming a state, so squeue's default of pending, running, and completing applies. A job held in another state can be reported finished early. Both events name the cluster, and the fallback is remembered per cluster.
- `dispatch.wait_poll_failed`: a controller timeout or unreachable cluster. The jobs stay outstanding and the next poll asks again, so an `squeue` that never answers ends in the `max-wait` failure. It is a WARNING the first time per cluster and DEBUG after.
- `Invalid job id specified` from `squeue` means completion: every ID has aged out of the queue.

## Find dispatch logs and result files

Logs are separated by process:

| Process | rtl_buddy log | Related files |
|---|---|---|
| Head | `<suite>/rtl_buddy.log` | Console output |
| Simulation | `artefacts/<test>/dispatch/rtl_buddy-<tag>.log` | `result-<tag>.json`, `slurm-<tag>.log` or `local-parallel-<tag>.log` |
| Verilate | `artefacts/.dispatch/verilate-rtl_buddy-<pid>.log` | `verilate-result-<pid>.json`, `verilate-<pid>.log` |
| Build | `artefacts/.dispatch/build-rtl_buddy-<pid>.log` | `build-result-<pid>.json`, `build-<pid>.log`, `gates-<pid>.json` |
| Run record | none | `artefacts/.dispatch/run-<pid>-<token>.json`; see [Interrupted runs](#interrupted-runs-warn-cancel-adopt) |

`<tag>` is the run ID or `single`; `<pid>` is the head process ID, followed by the run token in the `.dispatch` files. Failure descriptions point to the relevant worker and scheduler logs.

- **Co-located configs.** When a dispatched regression lists several test configs from one directory, each gets a namespace below `.dispatch`, such as `artefacts/.dispatch/tests-7a91c2d4e6f8/` (a config stem plus a hash of its resolved path). Its plan, build files, array manifests, scripts, and scheduler logs live there. Configs in separate directories keep the flat layout. Per-test outputs stay in `artefacts/<test>/`. If two co-located configs expand tests onto the same per-test directory, the regression exits before submitting. Rename one test or put the configs in separate directories.
- **`--run-tag <name>`** moves every path in the table into `artefacts/.runs/<name>/`, including the head log. The shared build directory is not namespaced, so tagged runs that compile the same thing reuse one build. See [Namespace concurrent runs](execution-context.md#namespace-concurrent-runs).
- **`build-result-<pid>.json`** lists the `built` and `failed` test names and a `builds` record per planned config: `test`, `builder`, `duration_sec`, `reused`, and `group`. Equal `group` values (the output path the compile writes) identify one single-writer output.
  - Successful records add `fingerprint_sha`, and `stamp_written: false` when the stamp write failed.
  - Failed records add `returncode`, `fingerprint_sha`, `error_tail` (the last lines of the transcript), and `transcript`.
  - The head adds the build job's `sacct` row under `telemetry`, and copies each test's record into its `result-<tag>.json`, where [`rb graph results`](graph.md#results-overlay) surfaces it.
- **`verilate-result-<pid>.json`** mirrors that envelope for the verilate job. The build job reads the per-directory marker, not this file.

## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns the findings in `payload.reservation_advice`. rtl_buddy never edits configuration. Disable reports with `rightsize: {report: false}`. Without `sacct` accounting, dispatch completes but emits no advice.

Advice is calculated per test from the peak across runs in this invocation:

- utilization below `over-threshold` suggests a reduction;
- utilization above `near-limit`, `TIMEOUT`, or `OUT_OF_MEMORY` suggests an increase;
- suggestions use peak times `margin`, with floors of 5 minutes and 128 MiB;
- time advice is limited to Verilator, because VCS license wait distorts elapsed time;
- memory advice is suppressed when the longest run is shorter than the accounting sample interval, except that an out-of-memory state still suggests an increase;
- `phase` is `sim`, `compile+sim`, `compile`, or `verilate`, and `edit_hint` identifies the field that controlled the allocation.

A test inside the `over-threshold` to `near-limit` band produces no finding. A lower `over-threshold` shortens the `reduce` list: `0.3` reports only tests that used under a third of what they asked for. Neither threshold suppresses a `raise` from a `TIMEOUT` or `OUT_OF_MEMORY` kill. Findings list `raise` first in the table, the `rightsize.advice` events, and the payload, because under-reservation costs failed work.

Advice records the run count and regression level. Do not use a smoke run to shrink a nightly reservation. Apply the `edit_hint`, rerun, and confirm the finding clears.

To inspect accounting manually, query step rows without `sacct -X`:

```bash
sacct -j <jobid> --format=JobID,Elapsed,MaxRSS
```

## Read advice for the build and verilate jobs

The suite's build job gets a row named `(build job)` with `phase: compile`. A split suite also gets `(verilate job)` with `phase: verilate`, and the `(build job)` row then describes the C++ build alone. Both suggest `time` in both directions. They suggest `cpus` only downward, because low CPU efficiency there means build slots idled.

- The `cpus` suggestion is per build. It appears only for a job that ran one build at a time. With `compile.parallel` above 1, idle slots in the tail also lower efficiency, so the row is withheld as `parallel-utilization-ambiguous`. Size `compile.parallel` against the suite's distinct compile keys first, then read `cpus` from a `parallel: 1` run.
- `time` advice is unaffected by `parallel`, because concurrent builds take the wall clock of the longest.
- A `reduce` is withheld when nothing compiled (every build reused its stamp), when the head has no per-build records (`no-build-records`), or when the job finished inside one accounting interval. `rightsize.build_advice_withheld` records which, with the record count and the seconds of real compiling. Without this guard, a re-run of an unchanged suite would advise a limit that the next real RTL change times out against.

Whichever file holds the winning compile value is the one named in `edit_hint`. A field the suite's own `compile` block set is `compile.<field>` in that suite's `tests.yaml`. Every other compile field is `cfg-dispatch.compile.<field>` in `root_config.yaml`. Verilate-phase paths add the sub-block: `cfg-dispatch.compile.verilate.<field>`, `compile.verilate.<field>`, or `testbenches[name=X].compile.verilate.<field>`.

The same holds for a `compile+sim` row. A builder that compiles inside its simulation job has no build job, so the compile reservation appears only inside the field-wise maximum. A `raise` after `OUT_OF_MEMORY` on a suite-set field points at the suite, because raising `cfg-dispatch.compile.mem` would leave the suite's value in force. Fields that the test's `resources` governs still name `tests[name=...].resources.<field>`.

## Judge cpu advice against requested cpus

Cpu efficiency is measured against the cpus the job requested, not the cpus the scheduler allocated. A partition with `SelectTypeParameters=NONE` on nodes with `ThreadsPerCore=2` allocates whole cores, so `--cpus-per-task=1` is charged two: `sacct` reports `ReqCPUS=1` and `AllocCPUS=2`. Against the allocation, a single-threaded simulation could never exceed 0.5 efficiency, and the default `over-threshold: 0.5` would advise a reduction to a `cpus: 1` that `tests.yaml` already holds.

- The request is the `--cpus-per-task` rtl_buddy resolved and submitted. If that is unavailable, the fallbacks are `ReqCPUS`, then `AllocCPUS`.
- A `reduce` is emitted only when the suggestion is strictly below the request.
- `Reserved` in the table and `reserved` in `payload.reservation_advice` are the requested figure. Where the scheduler gave more, the table shows `4 (8 allocated)`.
- `allocated` is present on every finding and is null except on a `cpus` row whose allocation differs from its request.

`mem` and `time` advice is measured against `ReqMem` and `TimelimitRaw`, which `sacct` reports from the actual allocation, so overrides never distort them.

## Handle cpu overrides in sbatch-args and SBATCH variables

`cfg-dispatch.sbatch-args` is appended after the generated reservation flags, so an argument there decides what the jobs request. Because `ReqCPUS` is tasks times cpus-per-task, two families of option count as a cpu override:

| | Options |
| --- | --- |
| the cpu count | `-c` / `--cpus-per-task` |
| task and node counts | `-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes` |

- A GPU count (`--gpus` / `-G`, `--gpus-per-node`, `--gpus-per-socket`, or a `--gres` asking for GPUs) together with `--ntasks-per-gpu` and no `--ntasks` derives the task count, so the pair counts. The advice names both.
- The environment counts too. `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` reach sbatch through the inherited environment and count like the matching option. A command-line option in `sbatch-args` beats its variable, and a blank variable is not an override. The environment is read once per suite, before that suite submits.
- `SBATCH_CPUS_PER_TASK` does not count, because every submit states `--cpus-per-task`, which beats it.
- Within one option, the last occurrence is the one reported, and short and long spellings are the same option. Across options there is no winner, and each distinct option is reported.
- These do not count: `--exclusive` and `--overcommit` (they change allocation, not the request), `--threads-per-core` and `-B` (node selection), `--ntasks-per-core` and `--ntasks-per-socket` (placement maxima), and `--cpus-per-gpu` (sbatch rejects it beside the generated `--cpus-per-task`).
- The list is read from the backend's `root_config.yaml`. In a regression spanning project roots, that is the orchestration config, not each suite's.

When an override is present, rtl_buddy records no request for that run and its analysis falls back to `ReqCPUS`. A DEBUG line (`rightsize request_from_scheduler`) names the cause. A test whose runs were retried under different cpu requests gets no `cpus` row, and `rightsize.cpus_advice_withheld` records `mixed-cpu-requests`.

## Edit the field a cpu override names

An override masks every cpus field the layering could name, so a hint aimed at one of them would leave the next job unchanged. While an override is in force, a `cpus` finding's `edit_hint.path` is `cfg-dispatch.sbatch-args`, with `file` pointing at the backend's `root_config.yaml`. Its `note` says which field was superseded. An override that came only from the environment has `path: env` and no `file`.

`suggested` is always the whole-job cpu count. Only a single `--cpus-per-task` can take it directly:

```
sbatch-args `--cpus-per-task=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.
```

A lone task or node count multiplies the per-task cpus and does not replace them. The note names both levers. The `8 per task x 4 tasks` clause appears only when that division is exact:

```
`--ntasks=4` multiplies this job's cpu request: the generated --cpus-per-task
from tests[name=wr_single].resources.cpus still applies, so the request is 8
per task x 4 tasks. Suggested value is the whole-job cpu count — lower
tests[name=wr_single].resources.cpus, the task count in sbatch-args, or both;
no single one of them takes it.
```

Several arguments combine by sbatch's own precedence. The note names them and leaves the arithmetic to you:

```
sbatch-args supersedes tests[name=wr_single].resources.cpus: `--ntasks=4` and
`--cpus-per-task=2` set this job's cpu request together. Suggested value is
the whole-job cpu count — decompose it across them per sbatch's own
precedence; no single one of them takes it.
```

A direct `--cpus-per-task` in `sbatch-args` disables the compile `cpus` floor, because it replaces the generated flag that carried it. A task or node count does not, since the generated `--cpus-per-task` still applies. The floor stays unscaled, because dropping to one task reaches it. Only `cpus` findings are retargeted; `mem` and `time` findings keep naming the reservation that governs them.
