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
- `--dispatch local-parallel -j N` uses local subprocesses. It cannot enforce
  `resources:` or produce scheduler accounting/right-sizing advice.
- `--dispatch slurm` submits shared build jobs and dependent simulation jobs.
  Gate it with `rb --machine tool-check --explain slurm`.

Dispatch implies shared builds. A simulation starts only after its compile-key
build succeeds; one failed or undersized build can therefore block a whole group.
One build job per suite compiles its distinct builds, `cfg-dispatch.compile.parallel`
of them at a time (default 1). Above 1 the job runs every config's `preproc` before
any builder starts, so no hook may mutate another config's inputs; at the default
it still runs `preproc` and compile per config in turn.
Dispatched `test`, `randtest`, and `regression` preserve their normal aggregate
exit codes: 0 with no real failure, 1 when a job fails or its result envelope is
missing, stale, or invalid, and 2 for a fatal orchestration/configuration error.

## Size resources from evidence

In machine mode, inspect `payload.reservation_advice`. Apply its `edit_hint.file`
and `edit_hint.path` exactly: the governing field may be a test/testbench
`resources:` entry, a suite-level `compile:` block at the top of that suite's
`tests.yaml`, or `cfg-dispatch.compile` in `root_config.yaml`. The suite block
overrides `cfg-dispatch.compile` field by field, so one big suite can carry its
own `compile: {mem: ...}` instead of every suite's build job inheriting a raise
of the global reservation.

- Slurm `OUT_OF_MEMORY`, or a local Verilator/compiler SIGKILL/`Killed`, means
  raise the governing `mem`; increasing `sim_timeout` cannot fix it.
- Scheduler `TIMEOUT` means raise the governing job `time`; `Sim hit timeout`
  inside a completed job instead points at the test's `sim_timeout`.
- Under-reservation costs failed work, so apply `raise` advice before `reduce`.
- Size `compile.time` for the longest build batch, not the suite's serial total:
  with `compile.parallel: N` the distinct builds run N at a time. Size
  `compile.mem` for N concurrent elaborations — only `cpus` is scaled for you —
  and keep N at or below the site's license pool for VCS.
- A `(build job)` row with `phase: compile` is the suite's build job. Its `cpus`
  suggestion is per build while `reserved` is the scaled product submitted; read
  `edit_hint.note`. The `cpus` row appears only for a job that ran one build at a
  time — with `compile.parallel` above 1 the job's efficiency also counts idle
  slots, so it is withheld as `parallel-utilization-ambiguous`; size `parallel`
  against the suite's distinct compile keys and read `cpus` from a `parallel: 1`
  run. A `reduce` is withheld (`rightsize.build_advice_withheld`) when no build
  actually compiled, or when the job left no record of what it built, so
  right-size the build job from a run that rebuilt.
- Right-size from representative regression levels and seeds, then rerun until
  the advice retires. rtl_buddy suggests edits; it never changes YAML itself.

## Shared-build gotchas

Any tracked compile input change invalidates a shared build; stamps compare
content, so a byte-for-byte regeneration does not. Runtime-only plusargs, seeds,
and simulation timeout do not either. Batch compile-input changes before
launching a large build; use independent cheap suites while it compiles.

- An edit that seems not to take effect, or a suspicious PASS right after one:
  read the `compile.build_reused` line (the run's own log; console once per
  build directory) and the test's `compile.log` breadcrumb, which name the reused build directory and its stamp's age.
- `--rebuild` forces a fresh compile — once per build directory per invocation,
  carried by the build job. Prefer it to deleting `artefacts/.shared-builds/`;
  dropping `--share-build` does not stop reuse.
- A wait on `compile.build_lock_wait` is another process compiling into the same
  directory, not a hang.

For missing envelopes, retries, license queues, accounting gaps, and builders
that compile inside simulation jobs, read `concepts/dispatch` and `known-issues`.
