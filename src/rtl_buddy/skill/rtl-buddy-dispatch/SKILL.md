---
name: rtl-buddy-dispatch
description: Run and diagnose rtl_buddy Slurm or local-parallel dispatch; use for resources, OOMs, retries, and shared-build dependencies.
---

# rtl_buddy dispatch

Report `rb --version` at the top of every run summary.

Use `rb --machine` and read `rb --machine docs show concepts/dispatch` for the
full scheduler contract and YAML fields.

## Choose the backend

- `--dispatch local` runs in-process and sequentially.
- `--dispatch local-parallel -j N` uses local subprocesses; no `resources:`
  enforcement, accounting, or right-sizing advice.
- `--dispatch slurm` submits shared build jobs and dependent simulation jobs.
  Gate it with `rb --machine tool-check --explain slurm`.

Dispatch implies shared builds. A simulation starts only after its compile-key
build succeeds, so one failed or undersized build can block a whole group.
One build job per suite compiles its distinct builds, `compile.parallel` of them
at a time (default 1; a suite's `compile:` overrides cfg-dispatch's). Above 1 the
job runs every config's `preproc` before any builder starts, so no hook may
mutate another's inputs (at 1 it is `preproc` then compile, per config).
Under slurm a Verilator suite splits that job in two: `rb-verilate-<hash>`
(reserved from `compile.verilate`, emits C++ only), then `rb-build-<hash>` on
`afterok` (reserved from `compile`, builds it with `--no-verilate`).
`compile.parallel` applies per phase; keys and stamps are unchanged.
`compile.build_phase_fallback` (WARNING) means the build job verilated a key
itself — marker missing or stale, or no `--no-verilate` in that Verilator —
correct but unsplit. `compile.split-verilate: false` runs one job.
An oversized group is split across arrays, each with its own manifest and logs
under `slice-N/`, from the `MaxArraySize` in `scontrol show config`. Where the
submit host cannot run it, set `cfg-dispatch.max-array-size` (and
`max-array-tasks` where the cluster caps tasks-per-array lower) or Slurm
refuses the array (`Invalid job array specification`).
`max-jobs-per-array` throttles each slice, so peak concurrency is that cap x
the slice count.
Dispatched `test`, `randtest`, and `regression` keep their aggregate exit
codes: 0 with no real failure, 1 when a job fails or its result envelope is
missing, stale, or invalid, 2 for a fatal orchestration/configuration error.

## Size resources from evidence

In machine mode, inspect `payload.reservation_advice`. Apply its `edit_hint.file`
and `edit_hint.path` exactly: the governing field may be a test/testbench
`resources:` entry, a `compile:` block on a testbench or atop that suite's
`tests.yaml`, or `cfg-dispatch.compile` in `root_config.yaml`. Each overrides
the previous field by field (`parallel` and `split-verilate` only to suite
level), so a big suite or testbench carries its own `compile: {mem: ...}`,
counted once per PLANNED
compile: the job sums the overlapping ones, schedules their time over
`parallel`, and floors at the suite value.

- Slurm `OUT_OF_MEMORY`, or a local Verilator/compiler SIGKILL/`Killed`, means
  raise the governing `mem` (elaboration: `compile.verilate.mem`), never
  `sim_timeout`.
- A `modes.<mode>` block on any of those layers sizes that mode alone and beats
  every base field: a `-M cov` OOM is a `modes.cov.mem` edit.
- Scheduler `TIMEOUT` means raise the governing job `time`; `Sim hit timeout`
  in a completed job points at the test's `sim_timeout`.
- Under-reservation costs failed work: apply `raise` advice before `reduce`.
- Size the suite `compile.mem`/`time` for the WHOLE job at `compile.parallel: N`
  — N concurrent elaborations, N at or below the site's VCS license pool. Only
  per-testbench blocks are aggregated for you.
- A `(build job)` row with `phase: compile` is the C++ build job; a
  `(verilate job)` row with `phase: verilate` is the verilate job, hinting at
  `compile.verilate.<field>`. Their `cpus` suggestion is per build while
  `reserved` is the scaled product submitted; read `edit_hint.note`. A `cpus`
  row appears only for a job that ran one build at a time — above
  `compile.parallel: 1` the efficiency also counts idle slots, so it is
  withheld as `parallel-utilization-ambiguous`; size `parallel` against the
  suite's distinct compile keys and read `cpus` from a `parallel: 1` run. A
  `reduce` is withheld (`rightsize.build_advice_withheld`) when no build
  actually compiled, or when the job left no record, so right-size from a run
  that rebuilt.
- `cpus` advice is judged against the cpus the job **requested**, not the cpus
  Slurm allocated: a site that hands out whole cores charges a `cpus: 1` job
  two and no reservation edit can change that. Where the two differ the table
  reads `Reserved 4 (8 allocated)`. Edit the figure named in `Field`.
- ...unless `cfg-dispatch.sbatch-args` carries an argument that sets the job's
  cpu request: `-c`/`--cpus-per-task`, or a task/node count that raises
  `ReqCPUS` (`-n`/`--ntasks`, `--ntasks-per-node`, `-N`/`--nodes`).
  Node-selection constraints (`--threads-per-core`, `-B`), placement maxima
  (`--ntasks-per-core`/`-socket`/`-gpu`) and `--exclusive` do not — they change
  what is selected, capped or allocated, not what is requested.
- The environment counts too: `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE` and
  `SBATCH_NODES` are inherited by the submit and count like the matching
  `sbatch-args` entry (command line wins; the environment is never sanitized).
  `SBATCH_CPUS_PER_TASK` is NOT one — every submit states `--cpus-per-task`,
  which beats it. The advice then falls back to `ReqCPUS` and the `cpus`
  `edit_hint` points at `cfg-dispatch.sbatch-args` (`env` and no `file` for an
  env-only override), naming the field it masks — edit the argument, not the
  field. `mem`/`time` rows are unaffected.
- `suggested` is always the whole-job cpu count; only a single
  `-c`/`--cpus-per-task` can take it straight. A task or node count is not a
  cpu count, and several combine by sbatch's precedence — decompose it yourself.
- A DIRECT `-c`/`--cpus-per-task` disables the compile `cpus` floor — it
  replaced the generated flag, so that floor never reached sbatch. A task or
  node count does not: `--cpus-per-task` is still in force, so the floor stays.
- Right-size from representative regression levels and seeds; rerun until the
  advice retires. rtl_buddy suggests edits; it never edits YAML.

## Shared-build gotchas

Any tracked compile input change invalidates a shared build; stamps compare
content, so a byte-for-byte regeneration does not. Stamps list each
`+incdir+`/`-y` dir: an added or removed file rebuilds; an edited header does
on VCS/Icarus, on Verilator only if the build read it. Runtime-only plusargs,
seeds, and sim timeout do not. Batch edits before a large build. In a build
job, same-key configs adopt the first Verilator build if its consumed inputs
are unchanged and no new file can alter resolution (`-y` file, shadowing
header); a consumed input that differs is `build_job.group_input_drift`.

- An edit that seems not to take effect, or a suspicious PASS right after one:
  read `compile.build_reused` (run log; console once per build dir) and the
  test's `compile.log` breadcrumb — both name the reused build dir and its
  stamp's age.
- `--rebuild` forces a fresh compile, once per build dir per invocation. Prefer
  it to deleting `artefacts/.shared-builds/`; dropping `--share-build` does
  not stop reuse.
- `compile.build_lock_wait` is another process compiling into the same
  dir, not a hang.
- `dispatch.build_job_deduped`: an earlier run's build job for this suite
  (`rb-build-<hash>`, one per suite dir) is still queued or running, so this
  one waits on it (`--dependency=singleton`), then revalidates the shared
  build: unchanged inputs reuse it, an edit or `--rebuild` recompiles. Expected
  after an interrupt. If it stays PENDING, inspect the job ahead
  (`squeue -j <ids> -O JobID,State,Reason`); `scancel`
  only a held or stale one — a healthy build gates the sims.

For missing envelopes, retries, license queues, accounting gaps, and builders
that compile inside sim jobs, read `concepts/dispatch` and `known-issues`.
