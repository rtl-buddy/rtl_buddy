---
name: rtl-buddy-dispatch
description: Run and diagnose rtl_buddy Slurm or local-parallel dispatch; use for resources, OOMs, retries, and shared-build dependencies.
---

# rtl_buddy dispatch

Report `rb --version` at the top of every run summary. Use `rb --machine`; read `rb --machine docs show concepts/dispatch` for scheduler procedures and `reference/yaml#parallel-dispatch` for fields.

## Choose the backend

- `--dispatch local` runs sequentially in-process.
- `--dispatch local-parallel -j N` runs subprocesses without reservation enforcement, accounting, or advice.
- `--dispatch slurm` submits builds and dependent simulations. Check prerequisites with `rb --machine tool-check --explain slurm`.

Dispatch implies shared builds. A failed build fails its dependent tests. Exit 0 means no real failure; 1 means failed jobs or missing, stale, or invalid results; 2 means fatal configuration or orchestration error.

## Size builds and arrays

- Each suite compiles distinct builds `compile.parallel` at a time (default 1). Suite `compile:` overrides root defaults. Above 1, all preprocessing runs before compilation; hooks must not change another config's inputs.
- Slurm splits Verilator work into verilation (`compile.verilate`) and C++ compilation (`compile`). Set `compile.split-verilate: false` for one job. `compile.build_phase_fallback` means correct but unsplit compilation because emitted sources are unavailable or unusable.
- `parallel` and `split-verilate` are valid only in root or suite `compile:`, never testbench or mode blocks.
- Size suite `compile.mem` for concurrent builds and `time` for the whole batch. Only per-testbench blocks are aggregated automatically. Keep `parallel` within the distinct build count and VCS license pool.
- Each test with a `preproc` hook counts as a build unless `preproc-sets-plusdefines: false` declares otherwise. Use that declaration for stimulus-only hooks. `build_job.preproc_changed_compile_key` means the hook violated it and reservations may be short.
- Large resource groups split into arrays; `max-jobs-per-array` throttles each slice, not the fleet. If probing fails or sbatch refuses the size, set `cfg-dispatch.max-array-size` and, where needed, `max-array-tasks`.

## Apply reservation advice

Read `payload.reservation_advice` and apply each explicit `edit_hint`. Its `file` is absolute; `path: env` has no file. Never infer the target from the test: inheritance, modes, and sbatch overrides can point elsewhere. For optional fields and logging, read `concepts/dispatch#apply-reservation-advice`.

- Apply `raise` before `reduce`; size from representative levels and seeds, then rerun to confirm. rtl_buddy never edits YAML.
- Slurm `OUT_OF_MEMORY` or compiler `Killed`/SIGKILL needs more governing `mem`, usually `compile.verilate.mem` for elaboration. Raising `sim_timeout` cannot help.
- Scheduler `TIMEOUT` needs more job `time`; `Sim hit timeout` in a completed job concerns the test's `sim_timeout`.
- Mode overrides beat base fields. A coverage-only OOM usually needs `modes.cov.mem` in the controlling block.
- `(build job)` and `(verilate job)` rows size different phases. Read `edit_hint.note`: suggested `cpus` is per build; `reserved` can include the parallel multiplier. CPU advice is withheld above `parallel: 1`; size concurrency first, then measure a serial run.
- Build reductions can be withheld when builds were reused or accounting/records are insufficient. Measure a representative rebuild.
- ERROR `config.unknown_key_rejected` (exit 2) names misspelt or misplaced reservation/dispatch keys. Correct every reported key before rerunning.

## Size the coverage tail

Slurm submits one `rb:coverage` job after simulations, logging to `.dispatch/coverage/`. Reserve it with `cfg-dispatch.coverage`, usually `modes.cov`, which inherits root `resources`. Without Slurm, including `randtest -r`, the tail runs in the head.

- `tail_failed` exits 1, preserving test results but writing no model or manifest.
- Set `cfg-coverage.merge-timeout` to bound a hung raw merge; unset means no limit.
- A discovered orphan tail must finish before this run writes coverage; `--orphans cancel` cancels it. Records are per artefact tree, while `cov_dir/` is shared across tags. See `concepts/dispatch#interrupted-runs-warn-cancel-adopt`.

## Interpret CPU overrides

CPU advice uses requested CPUs, not allocated CPUs. An override in `sbatch-args` or the environment can mask the YAML reservation; edit the target in `edit_hint`.

- `suggested` is the whole-job count. Only a single `--cpus-per-task` takes it directly; task/node multipliers require decomposition.
- A direct `--cpus-per-task` disables the compile CPU floor; task/node counts do not.
- `SBATCH_CPUS_PER_TASK` is not an override. Read `reference/yaml#sbatch-args-behavior` for recognized arguments and variables.

## Debug shared builds

Read `concepts/tests#sharing-compiled-builds-across-tests` for invalidation rules. Batch compile-input edits before an expensive build; runtime plusargs, seeds, and timeouts do not rebuild.

- Suspicious reuse: inspect `compile.build_reused` and the test's `compile.log` for the directory and stamp age. Use `--rebuild`, never delete a build during a run. Dropping `--share-build` under dispatch does not disable reuse.
- `build_job.group_input_drift`: same-key configs consumed different inputs. Fix preprocessing or give them distinct builds.
- `compile.build_lock_wait`: another process is compiling into that directory.
- `dispatch.build_job_deduped`: this suite and job tag wait on an earlier build through `singleton`, then revalidate it. Inspect `squeue -j <ids> -O JobID,State,Reason`; cancel only a confirmed held, unschedulable, or abandoned job.

For missing results, license retries, and unusable prebuilt stamps, read `concepts/dispatch#troubleshoot-builds` and `known-issues`.
