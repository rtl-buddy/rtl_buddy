---
name: rtl-buddy-dispatch
description: Run and diagnose rtl_buddy Slurm or local-parallel dispatch; use for resources, OOMs, retries, and shared-build dependencies.
---

# rtl_buddy dispatch

Report `rb --version` at the top of every run summary.

Use `rb --machine`. For the full scheduler contract and YAML fields, run `rb --machine docs show concepts/dispatch`.

## Choose the backend

- `--dispatch local` runs in-process and sequentially.
- `--dispatch local-parallel -j N` runs local subprocesses. It has no `resources:` enforcement, accounting, or right-sizing advice.
- `--dispatch slurm` submits shared build jobs and dependent simulation jobs. Gate it with `rb --machine tool-check --explain slurm`.

Dispatch implies shared builds. A simulation starts only after its compile-key build succeeds, so one failed or undersized build can block a whole group.

Dispatched `test`, `randtest`, `regression` exit 0 with no real failure, 1 when a job fails or its result envelope is missing, stale, or invalid, 2 on a fatal orchestration or configuration error.

## Know the build job

- One build job per suite compiles its distinct builds, `compile.parallel` at a time (default 1; a suite's `compile:` overrides `cfg-dispatch`).
- Above 1, every config's `preproc` runs before any builder starts, so no hook may mutate another config's inputs.
- Under Slurm, a Verilator suite splits the job in two. `rb-verilate-<hash>` (reserved from `compile.verilate`) emits C++ only, then `rb-build-<hash>` (reserved from `compile`) builds it on `afterok`. `compile.split-verilate: false` runs one job.
- `compile.build_phase_fallback` (WARNING): the build job verilated a key itself (marker missing or stale, or no `--no-verilate`). Correct but unsplit.
- A group larger than the cluster's array limit is split into arrays with logs under `slice-N/`. `max-jobs-per-array` throttles each slice, so peak concurrency is the cap times the slice count.
- If the submit host cannot run `scontrol`, or Slurm answers `Invalid job array specification`, set `cfg-dispatch.max-array-size`, and `max-array-tasks` where tasks per array are capped lower.

## Size resources from evidence

In machine mode, read `payload.reservation_advice` and apply each `edit_hint.file` and `edit_hint.path` exactly. The governing field is a test or testbench `resources:` entry, a `compile:` block on a testbench or atop the suite's `tests.yaml`, or `cfg-dispatch.compile` in `root_config.yaml`. Each layer overrides the previous field by field; `parallel` and `split-verilate` go only at suite level or above.

- Slurm `OUT_OF_MEMORY`, or a local Verilator or compiler SIGKILL or `Killed`: raise the governing `mem` (for elaboration, `compile.verilate.mem`), never `sim_timeout`.
- Scheduler `TIMEOUT`: raise the governing job `time`. `Sim hit timeout` in a completed job points at the test's `sim_timeout`.
- A `modes.<mode>` block on any of those layers sizes that builder mode alone and beats every base field. A `-M cov` OOM is a `modes.cov.mem` edit.
- Apply `raise` before `reduce`; under-reservation fails work.
- Size the suite `compile.mem` and `time` for the whole job at `compile.parallel: N`: N concurrent elaborations, with N at or below the site's VCS license pool. Only per-testbench blocks are aggregated for you, counted once per planned compile.
- A `(build job)` row (`phase: compile`) is the C++ build job. A `(verilate job)` row (`phase: verilate`) hints at `compile.verilate.<field>`. Their `cpus` suggestion is per build while `reserved` is the scaled product; read `edit_hint.note`.
- The build-job `cpus` row is withheld as `parallel-utilization-ambiguous` above `compile.parallel: 1`. Size `parallel` against the suite's distinct compile keys, then read `cpus` from a `parallel: 1` run.
- A build-job `reduce` is withheld (`rightsize.build_advice_withheld`) when nothing compiled or the job left no record. Right-size from a run that rebuilt.
- A misspelt key in a reservation block or in `cfg-dispatch` (and its `coverage`, `retry`, `rightsize`) is ignored with WARNING `config.unknown_key`, naming the nearest key, so the job got the inherited value. A later major makes it fatal; `modes:` already rejects unknown keys.
- The head reserves one build per test with a `preproc` hook, since it may set plusdefines. If the hook only writes stimulus, set `preproc-sets-plusdefines: false` on the test or suite so identical tests share one build. A hook that moves the key anyway logs `build_job.preproc_changed_compile_key` (`changed=[...]`); the reservation may be short.
- Right-size from representative levels and seeds; rerun until the advice retires. rtl_buddy never edits YAML.

## Size the coverage tail

Under Slurm, the coverage tail of `test`, `regression` and `randtest` (merge, `coverage-model.json`, LCOV exports, manifest) runs as one `rb:coverage` job after the fleet, logging to `.dispatch/coverage/`. Without Slurm, or for `randtest -r`, it runs in the head. A still-running tail job from an earlier run is waited for first; `--orphans cancel` cancels it.

- Reserve it with `cfg-dispatch.coverage` (inherits `resources`; `modes.cov` is the usual place). A merge OOM is an edit there.
- `Coverage tail FAILED` (`tail_failed`) keeps every test result, writes no model or manifest, and exits 1.
- `cfg-coverage` `merge-timeout` (seconds; unset is no limit) stops a hung merge, reported as a failed merge.

## Read cpu advice under sbatch-args

- `cpus` advice is judged against the cpus requested, not allocated; the table then reads `Reserved 4 (8 allocated)`. Edit the figure named in `Field`.
- A cpu override in `cfg-dispatch.sbatch-args` supersedes the resolved reservation: `-c` / `--cpus-per-task`, or a count that raises `ReqCPUS` (`-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes`). The exported `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` count the same way; `SBATCH_CPUS_PER_TASK` does not.
- The `cpus` `edit_hint` then points at `cfg-dispatch.sbatch-args` (`env` with no `file` for an environment-only override) and names the field it masks. Edit the argument, not the field.
- `suggested` is the whole-job cpu count; only a single `--cpus-per-task` takes it directly.
- A direct `--cpus-per-task` disables the compile `cpus` floor. A task or node count does not.

## Debug shared builds

Any tracked compile input change invalidates a shared build, compared by content (a byte-identical regeneration does not). An added or removed file in a `+incdir+` or `-y` directory rebuilds. An edited header rebuilds on VCS and Icarus, and on Verilator only if the build read it. Runtime-only plusargs, seeds, and sim timeout do not. Batch edits before a large build.

Same-key configs in a build job adopt the first Verilator build when its consumed inputs are unchanged and no new file can alter resolution (a `-y` file or shadowing header). A consumed input that differs is `build_job.group_input_drift`.

- An edit that seems ignored, or a suspicious PASS after one: read `compile.build_reused` (run log; console once per build directory) and the test's `compile.log` breadcrumb. Both name the reused directory and the stamp's age.
- `--rebuild` forces a fresh compile, once per build directory per invocation. Prefer it to deleting `artefacts/.shared-builds/`. Dropping `--share-build` does not stop reuse.
- `compile.build_lock_wait` is another process compiling the same directory, not a hang.
- `dispatch.build_job_deduped`: an earlier run's build job for this suite and job tag (`rb-build-<hash>`) is still queued or running, so this one waits on it (`--dependency=singleton`), then revalidates the build. Unchanged inputs reuse it; an edit or `--rebuild` recompiles. Expected after an interrupt. If it stays PENDING, inspect it with `squeue -j <ids> -O JobID,State,Reason`; `scancel` only a held or stale one.

For missing envelopes, retries, license queues, accounting gaps, and builders that compile inside simulation jobs, read `concepts/dispatch` and `known-issues`.
