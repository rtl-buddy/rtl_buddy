---
description: Fan regression tests out in parallel — as Slurm jobs on a cluster or as capped subprocesses on one machine — after a single shared build, with per-test resource reservations and right-sizing advice.
---

# Parallel dispatch

By default `rb regression` runs every test in-process, one at a time.
**Dispatch** instead fans the tests out in parallel after a single shared
build, with one of two backends:

```bash
rb regression --dispatch slurm             # a cluster
rb regression --dispatch local-parallel    # this machine, no scheduler
rb randtest my_test 500 --dispatch slurm   # seed fan-out
```

`--dispatch local` (the default) is the unchanged in-process path.

| | `local` | `local-parallel` | `slurm` |
|---|---|---|---|
| Runs where | this process | this host, N subprocesses | cluster nodes |
| Needs | nothing | nothing | Slurm client + shared FS |
| Concurrency | 1 | `--jobs` / `cfg-dispatch.jobs` | `max-jobs-per-array` × arrays |
| `resources:` reservations | n/a | ignored (advisory) | enforced by the scheduler |
| Right-sizing advice | n/a | none (no accounting) | from `sacct` |

Both dispatch backends sit behind one interface and share everything that
is not the transport: the head expands sweeps **once** into a plan
manifest, submits one build job per suite, gates the sims on that build,
and collects a `result.json` per job. The sections below describe the
Slurm path first; [On one machine](#on-one-machine-dispatch-local-parallel)
covers what differs on a laptop.

## How it works

**Nothing heavy runs on the submit host** — it is usually an interactive
login node where a big Verilation is against policy. The head process only
plans and submits; the compile and the sims both run as scheduler jobs.

1. **Build job.** The head submits one `sbatch` job per suite that runs
   `rb _build-job` on a compute node: it compiles one shared `simv` per
   unique compile key (`--dispatch` implies
   [`--share-build`](tests.md#sharing-compiled-builds-across-tests)) and
   writes the shared build to the shared filesystem. Those compiles run
   **serially inside that one job**, so a suite with several compile keys
   needs a `compile.time` covering their total — see [Sizing the
   reservations](#sizing-the-reservations). If no test in the
   suite uses a share-build-capable builder there is nothing for the sim
   jobs to read, so the build job is skipped entirely rather than burning
   a compile — see [Builders that compile inside the
   job](#builders-that-compile-inside-the-job).
2. **Fan-out gated on the build.** One job per `(test, run_id)` is
   submitted, grouped by identical resolved resources into a Slurm
   **array** (`cfg-dispatch.max-jobs-per-array` maps to the array's `%N`
   throttle), each with `--dependency=afterok:<build-job>`. Slurm holds
   the sim elements until the build succeeds; each then re-invokes
   `rb _test-job`, whose own compile short-circuits on the shared-build
   stamp, so it runs simulation + post only.
3. **Collect.** The head waits for the queue to drain (once, across all
   suites), then loads each job's result and feeds the normal summary and
   exit code. A job that produced no result — a scheduler kill, a crash,
   or a build-job failure that made `afterok` cancel it — counts as a
   fail, never silently dropped.

The compile carries its own reservation (`cfg-dispatch.compile`,
defaulting to `resources`) since a large Verilation or VCS elaboration is
often heavier than the sims it precedes. Normally that reservation belongs
to the build job; when the builder cannot share a build the compile runs
inside each sim job instead and the block is folded into *that* job's
reservation. If the reservation is too small the build is
killed, `afterok` cancels the sims, and they surface as dispatch failures
pointing at the build log.

!!! note "Dependents are reaped, not left queued"
    Slurm reports a sim whose build failed as `PENDING` with reason
    `DependencyNeverSatisfied`, and by default leaves it queued **forever** —
    it is only reaped if the site sets `kill_invalid_depend` in
    `SchedulerParameters`. Two things prevent that stray:

    - every dependent submit carries **`--kill-on-invalid-dep=yes`**, so
      Slurm itself removes the job the moment the dependency fails. This is
      the one that matters, because it holds even when the head process is
      `SIGKILL`ed (a CI abort or timeout) and never gets to clean up;
    - failing that, collection notices such jobs, cancels them, and logs
      `dispatch.dependency_never_satisfied` instead of polling jobs that can
      never run.

    Pass `--kill-on-invalid-dep=no` in `sbatch-args` to opt out — user
    `sbatch-args` are appended last and win.

A single test's compile failure does **not**
fail the build job — the other sims still run, and the failing test
recompiles (and fails) in its own sim job. `--dispatch` cannot be combined
with `--early-stop`, and dispatched jobs deliberately skip the per-tree
lock (see [Known Issues](../known-issues.md#the-artefact-tree-lock-is-per-tree-and-its-lock-file-stays-behind)).

## How arrays interact with the shared build

Dispatch buckets tests by **two independent keys**, and they need not line
up:

- **Share-build groups by *compile key*** — a fingerprint of the compile
  inputs (filelist + compile flags + plusdefines). Tests whose inputs
  hash identically reuse one `simv` under
  `artefacts/.shared-builds/obj_dir_<key>/simv`.
- **Arrays group by *resolved resources*** — the `cpus`/`mem`/`time`
  reservation. Tests resolving to the same reservation share one `sbatch`
  array.

These are orthogonal because reservations are about *sim-time* needs while
compile keys are about *compile inputs*. A smoke test and a soak test of
the same DUT+testbench reuse one compiled `simv` (same compile key) but
reserve very different memory/time, so they land in **different arrays**.
Conversely, two unrelated blocks that happen to reserve the same slot
share one array but each build their own `simv`.

The sharing happens in the **build job**: it Verilates each unique compile
key exactly once (later configs with the same key short-circuit on the
stamp), and the sim arrays are gated on it by `afterok`, so no sim starts
until its shared build exists. So the counts are independent — distinct
`simv`s built = distinct compile keys; arrays submitted = distinct
resource tuples — and any combination is possible (one `simv` shared
across a 12-element array; three arrays all pointing at one `simv`; a
single array whose members each have their own `simv`).

Every array element re-runs `compile()`, finds the shared stamp on the
shared filesystem, short-circuits, and reads the same on-disk `simv`
**read-only** — which is why elements across different arrays (or suites)
that share a compile key all point at the same build, and why concurrent
elements are safe (`_test-job` is a cooperative reader that skips the
per-tree lock).

## Builders that compile inside the job

Share-build works for the builders whose compile output rtl_buddy can
redirect wholesale into the shared dir: **Verilator** (`--Mdir`), **VCS**
(`-o` for the executable plus `-Mdir` for its `csrc` tree, so the build is
self-contained), and **Icarus** (`-o` for the `.vvp` snapshot). All three
build once per compile key and short-circuit on the stamp.

Any other builder — and a builder configured with an *absolute*
`builder-simv:`, which pins the executable somewhere a per-compile-key dir
cannot honour — cannot put its build where other tests would find it, so
**the build stays inside the test's own `artefacts/<test>/`**. Under
`--share-build` it is still stamped there, which means it can be *reused*
by the next process to compile that same test even though it can never be
*shared* with a different one. That is what lets a build job compile it once
and the sim jobs skip their own compile. Two further consequences dispatch
handles for you:

- **No build job is submitted** when nothing in the suite can share a
  build *and* no test is fanned out over several runs. The build pass would
  compile on a compute node and produce something no sim job needs, so it
  is skipped and the elements run ungated (logged as
  `dispatch.build_job_skipped`). That is safe because each test owns its own
  `artefacts/<test>/`, so there is exactly one writer per directory.
- **A fanned-out test gets one anyway**, because there the one-writer
  property does not hold: `artefacts/<test>/` is keyed on the *test*, not on
  the run, so `randtest <test> N --dispatch` would otherwise run N full
  compiles into one directory at once and the losers would report
  `Compile failed` with nothing wrong
  ([#369](https://github.com/rtl-buddy/rtl_buddy/issues/369)). The build job
  is the single writer: it compiles once, and every element waits for it and
  short-circuits on the stamp it leaves. So whenever a build job exists,
  **every** group is gated on it with `afterok` — including the
  self-compiling ones, since the build job runs PRE+COMPILE for the whole
  plan and therefore writes into their directories too. The gate orders the
  elements but does not exclude them, so the stamp is what actually keeps
  them from recompiling; an element that compiles anyway logs
  `compile.prebuilt_stamp_invalid` — see
  [Known Issues](../known-issues.md#a-build-job-orders-the-fan-out-only-the-stamp-keeps-it-from-recompiling).
- **The job's reservation covers both phases.** A compile is frequently
  hungrier than the sim it precedes — a VCS elaboration usually is — so a
  sim-sized reservation would be killed during it. One allocation cannot
  carry two reservations, so the element-wise maximum of
  `cfg-dispatch.resources` (as resolved for that test) and
  `cfg-dispatch.compile` is used, field by field, and logged as
  `dispatch.compile_in_job`. Tests whose builder *can* share a build are
  unaffected: they keep the sim-sized reservation, and the compile
  reservation stays with the build job where it belongs. The combined size
  is kept even where a build job exists and the element expects to skip its
  compile — a stamp that fails to validate for any reason puts the compile
  back inside the job, and being over-reserved is cheaper than being killed.

!!! note "A VCS compile can wait for a license"
    `vcs` elaboration honours `-licqueue` exactly as `simv` does, so part
    of a compile's wall-clock can be time spent queuing for a seat rather
    than compiling — and Slurm's `--time` clock keeps running through it.
    rtl_buddy cannot pause the scheduler's clock, but it detects the queue
    banner and logs `compile.license_queued` with the compile transcript,
    so a build job killed at its time limit is diagnosable as a busy
    license server rather than an undersized reservation. Give a VCS
    `compile.time` headroom for it. Sharing the build helps here too: the
    build job compiles each key once, serially, taking one seat at a time
    instead of one per concurrent array element.

## On one machine: `--dispatch local-parallel`

Slurm needs a cluster, and it has no native macOS build — so on a laptop
the only options used to be "one test at a time" or "stand up a scheduler".
`local-parallel` closes that gap: the same plan → build job → gated
fan-out, with every job a plain subprocess on this host, throttled by one
pool of slots.

```bash
rb regression --dispatch local-parallel          # min(4, cpu count) jobs
rb regression --dispatch local-parallel -j 8     # eight at a time
rb randtest my_test 20 --dispatch local-parallel -j 4
```

Nothing to install: no scheduler client, no shared filesystem (the local
one is trivially "visible at the same paths"), no accounting database.
Concurrency comes from `-j/--jobs`, or `cfg-dispatch.jobs`, defaulting to
`min(4, cpu count)`. It is **one global pool** — across every suite and
every resource group, not per array — so `-j 4` means at most four
`rb` jobs alive at once, full stop. Build jobs jump the queue ahead of
waiting sims, since a build unblocks a whole suite and a sim unblocks
nothing.

What carries over unchanged: the sweep hook runs once on the head; each
suite's shared build compiles once and the sims short-circuit on its
stamp; a sim only starts once its build **exited 0** (this backend's
version of `afterok`), and if the build fails its sims never start and are
reported as producing no result, pointing at the build log.

Two things are deliberately **not** supported, and they are the reason to
still prefer Slurm where you have it:

- **Reservations are not enforced.** `resources:` cpus/mem/time are
  ignored rather than half-honoured — one host has no portable per-process
  cap (`ulimit`/`nice`/`taskset` are coarse and platform-specific). Any
  reservation that resolves to something non-default — from `cfg-dispatch`
  *or* from a per-testbench / per-test `resources:` — **warns** once
  (`dispatch.reservations_ignored`) so it cannot read as enforced. `-j` is
  the only backpressure, so size it for the *memory* your heaviest tests
  need, not just for cores.
- **No usage telemetry, so no right-sizing advice.** There is no `sacct`
  to ask, so `payload.reservation_advice` comes back empty instead of
  guessing. Right-size against a real cluster run.

!!! note "Ctrl-C cleans up; `kill -9` does not"
    Jobs run in their own process session, so an interrupt goes to the
    head, which takes the fleet down itself — the same shape as `scancel`,
    and it lets a simulator flush on a graceful signal. Teardown signals
    **every** job before waiting on any, so the grace period is one 5 s
    window for the whole fleet rather than 5 s per job, and an impatient
    second `Ctrl-C` can at worst skip the escalation to `SIGKILL` — never
    leave a job unsignalled. The trade-off: a `SIGKILL`ed head runs no
    cleanup at all and, unlike Slurm, there is no scheduler to reap the
    orphans — its children finish their runs. Prefer `Ctrl-C` over
    `kill -9` on a dispatched run.

## Requirements (Slurm)

- A Slurm client on the submit host (`sbatch`/`squeue`/`sacct`/`scancel`)
  — see [Installation](../install.md). `rb tool-check --explain slurm`
  reports readiness.
- A **shared filesystem** visible at the same absolute paths on the submit
  host and every compute node: the project checkout, the `artefacts/`
  tree, and the Python environment.
- The project's `rb` runnable on the compute nodes (the job runs
  `sys.executable -m rtl_buddy _test-job`).
- A share-build-capable builder (Verilator, VCS, or Icarus) to compile
  once per compile key; other builders still work but recompile inside
  each job — see [Builders that compile inside the
  job](#builders-that-compile-inside-the-job).

## Configuration: `cfg-dispatch`

All optional, in `root_config.yaml`:

```yaml
cfg-dispatch:
  backend: slurm            # local (in-process, default) | local-parallel | slurm
  jobs: 4                   # local-parallel only: concurrent subprocesses
  resources:                # cluster-wide per-SIM-job defaults
    cpus: 2
    mem: 4G
    time: "01:00:00"        # QUOTE time values (see below)
  compile:                  # reservation for the COMPILE (defaults to resources)
    cpus: 8
    mem: 16G
    time: "02:00:00"
  sbatch-args:              # passed to sbatch verbatim
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200   # concurrency throttle, PER submitted array
  poll-interval: 10         # seconds between queue polls (> 0)
  rightsize:                # reservation right-sizing (see below)
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`jobs` and `max-jobs-per-array` belong to different backends and do not
interact: `jobs` is `local-parallel`'s single global pool, while
`max-jobs-per-array` is a Slurm `%N` throttle (and is ignored by
`local-parallel`, which has no arrays).

`max-jobs-per-array` throttles each *submitted array*
(`--array=1-N%max-jobs-per-array`), not the run as a whole — a regression with several reservation shapes across
several suites submits several arrays, so peak concurrency is roughly
`max-jobs-per-array × arrays`. Size it per array, not per cluster.

!!! warning "Quote `time` values"
    YAML 1.1 reads an unquoted `time: 4:00:00` as the **integer 14400**
    (sexagesimal), which Slurm would take as 14400 *minutes* — 10 days.
    rtl_buddy rejects the unquoted form loudly, so this costs you a config
    error rather than a ten-day reservation; always write
    `time: "4:00:00"` (bare minutes work as a string too, `time: "240"`).

    The trap is easy to miss because it is inconsistent: a leading-zero form
    like `01:00:00` happens to survive as a string, so a file full of
    unquoted times can load fine until someone writes a single-digit hour.
    Quote every one, in `cfg-dispatch.resources`, `cfg-dispatch.compile`,
    and per-testbench / per-test `resources:` alike. See
    [Known Issues](../known-issues.md#an-unquoted-time-in-cfg-dispatchresources-is-yaml-sexagesimal).

## Per-test reservations

Not every test deserves the same slot. Override the reservation per
testbench or per test in `tests.yaml`, using the same fields; the
effective reservation is layered **test → testbench →
`cfg-dispatch.resources` → built-in defaults**, field by field:

```yaml
testbenches:
  - name: axi_tb
    resources: { cpus: 2, mem: 8G, time: "00:30:00" }
tests:
  - name: axi_smoke
    # inherits the testbench reservation
  - name: axi_soak
    resources: { mem: 24G, time: "04:00:00" }   # cpus inherited
```

Tests that resolve to the same reservation share one Slurm array;
differing reservations split into separate arrays.

## Sizing the reservations

[Right-sizing](#reservation-right-sizing) tunes these numbers from real
usage, but it can only
report on jobs that survived long enough to be measured. Four things decide
the first sizing, and each has bitten someone:

**`compile.time` covers the whole suite, not one compile.** The build job
compiles each of the suite's unique compile keys **serially**, in one job,
under one `--time`. A suite with six testbenches over one DUT has six keys,
so its build job needs roughly six compiles' worth of time — and the whole
array waits behind it, since every sim job is gated on that one job with
`afterok`. Sizing `compile.time` from a single observed compile is the usual
way to get a build job killed at its limit and a suite of dispatch failures
pointing at a build log that just stops. Two ways to shrink the number
rather than raise it: collapse compile keys (tests differing only in
`plusdefines:` each cost a key — see
[How arrays interact with the shared build](#how-arrays-interact-with-the-shared-build)),
or split the suite. Full consequences in
[Known Issues](../known-issues.md#a-suites-build-job-compiles-every-compile-key-serially-in-one-reservation).

**A job that compiles inside itself must be reserved for the compile.**
Where the builder cannot share a build, the compile happens under the
*sim* job's reservation. rtl_buddy folds `cfg-dispatch.compile` into it
field by field (see [Builders that compile inside the
job](#builders-that-compile-inside-the-job)), so the thing to get right is
`cfg-dispatch.compile` itself — a sim-sized `mem` there is what turns a
whole array into `OUT_OF_MEMORY` kills. Elaboration is usually the memory
peak of the entire flow, so size `compile.mem` from a real elaboration, not
from a simulation.

**Give a VCS build job headroom for the license queue.** `-licqueue` waits
count against `--time`, so a `compile.time` sized for compute alone will
eventually land on a busy license server — see [A VCS compile can wait for a
license](#builders-that-compile-inside-the-job). `compile.license_queued` is
logged when a compile *completes* after queueing, so a build job killed at
its limit produces no such event of its own: the evidence comes from the
keys that finished before it, or from a previous run.

**Ask for accounting fine enough to advise from.** Dispatch requests
`--acctg-freq=task=1` for you unless your `sbatch-args` already set
`--acctg-freq`; leave it alone unless the site requires otherwise, because
at the stock 30 s interval every sim job shorter than half a minute reports
a memory peak far below the truth. See [Reservation
right-sizing](#reservation-right-sizing).

## Reservation right-sizing

After a dispatched run, rtl_buddy compares what each test *reserved*
against what it *used* (from `sacct` accounting) and reports advice —
which resource is over- or under-reserved and what to set it to. It
**reports and suggests; it never edits `tests.yaml`** — you (or an agent)
apply the change as a reviewable diff.

In human mode this is a "Reservation Advice" table after the summary. In
`--machine` mode, `payload.reservation_advice` carries one event per
finding:

```json
{
  "event": "reservation-advice",
  "suite": "verif/demo_axi/tests.yaml",
  "test": "axi_soak",
  "resource": "mem",
  "reserved": "24G", "peak": "3G", "utilization": 0.13,
  "direction": "reduce", "suggested": "5G",
  "runs": 4, "reg_level": 1000, "states": ["COMPLETED"],
  "phase": "sim",
  "edit_hint": {"file": "verif/demo_axi/tests.yaml",
                "path": "tests[name=axi_soak].resources.mem"}
}
```

Semantics:

- Utilization is judged **per test**, using the peak across the test's
  runs/seeds this invocation, so a suggestion covers the worst run.
- Below `over-threshold` → `reduce`; above `near-limit`, or a scheduler
  `TIMEOUT`/`OUT_OF_MEMORY` kill → `raise` (the kill wins even without
  usage numbers). Suggested = peak × `margin`, floored at 5 min / 128M.
- **Time advice is Verilator-only.** A VCS `-licqueue` wait would
  masquerade as compute time, so time advice is suppressed off Verilator
  (see [#329](https://github.com/rtl-buddy/rtl_buddy/issues/329)); memory
  and CPU-efficiency advice are unaffected.
- **Memory advice needs a peak that was sampled.** `MaxRSS` is a high-water
  mark over accounting samples, so a test whose longest run finished inside
  one sampling interval was measured at most once and reports near-nothing.
  Dispatch therefore asks for per-second task accounting
  (`--acctg-freq=task=1`) on every job unless your `sbatch-args` already set
  `--acctg-freq`, and *still* suppresses utilization-based memory advice for
  any test that ran shorter than the interval actually in force — logging
  `rightsize.mem_advice_unsampled` with the test names rather than leaving
  the gap silent. An `OUT_OF_MEMORY` kill still raises, being a fact about
  the reservation rather than a measurement of it. Background:
  [#365](https://github.com/rtl-buddy/rtl_buddy/issues/365).
- Advice is labelled with `runs` and `reg_level`, so a `-l 0` smoke run
  is never used to shrink a nightly test's reservation.
- **`phase` says what the numbers cover.** `"sim"` is the usual case. A
  job that also compiled (its builder [compiles inside the
  job](#builders-that-compile-inside-the-job)) is labelled
  `"compile+sim"`: `MaxRSS` and `Elapsed` are high-water marks over the
  whole job, so they legitimately span both phases — do not read them as
  sim-only.
- **The hint names the field that actually governs.** A `compile+sim`
  job's allocation is the maximum of the sim and compile reservations, so
  where the compile side won, editing `tests[...].resources` would change
  nothing. Those findings point at `cfg-dispatch.compile.<field>` in
  `root_config.yaml` instead. Always apply the `edit_hint`'s `file` and
  `path` rather than inferring the field from `test`.
- Requires `sacct` (slurmdbd accounting). Without it, dispatch still works
  and right-sizing degrades gracefully to no advice. Turn it off with
  `rightsize: { report: false }`.

!!! note "`MaxRSS` populates on step rows only"
    Checking the advice by hand with `sacct -X` shows the field **blank** —
    `-X` returns allocation rows, and usage is recorded on the steps
    (`.batch`, `.extern`, …) and folded up. rtl_buddy queries without `-X`
    and gets this right; a human reproducing it may conclude the field is
    simply empty. Use `sacct -j <jobid> --format=JobID,Elapsed,MaxRSS` with
    no `-X`.

## Agent loop

An agent driving `--dispatch slurm --machine` closes the loop: run,
read `reservation_advice`, apply each `edit_hint` (raise under-reservations
first — those cost failed runs — then trim over-reservations), rerun to
confirm the advice retires. See the [bundled skill](../agents.md).
