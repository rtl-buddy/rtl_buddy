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

## Size resources from evidence

In machine mode, inspect `payload.reservation_advice`. Apply its `edit_hint.file`
and `edit_hint.path` exactly: the governing field may be a test/testbench
`resources:` entry or `cfg-dispatch.compile` in `root_config.yaml`.

- Slurm `OUT_OF_MEMORY`, or a local Verilator/compiler SIGKILL/`Killed`, means
  raise the governing `mem`; increasing `sim_timeout` cannot fix it.
- Scheduler `TIMEOUT` means raise the governing job `time`; `Sim hit timeout`
  inside a completed job instead points at the test's `sim_timeout`.
- Under-reservation costs failed work, so apply `raise` advice before `reduce`.
- Right-size from representative regression levels and seeds, then rerun until
  the advice retires. rtl_buddy suggests edits; it never changes YAML itself.

## Shared-build gotchas

Any tracked compile input change invalidates a shared build. Runtime-only
plusargs, seeds, and simulation timeout do not. Batch compile-input changes before
launching a large build; use independent cheap suites while it compiles.

For missing envelopes, retries, license queues, accounting gaps, and builders
that compile inside simulation jobs, read `concepts/dispatch` and `known-issues`.
