---
description: Fan regression tests out as parallel Slurm jobs after the shared build, with per-test resource reservations and reservation right-sizing advice.
---

# Parallel dispatch (Slurm)

By default `rb regression` runs every test in-process, one at a time. On
a cluster you can instead **dispatch** the tests as parallel
[Slurm](https://slurm.schedmd.com/) jobs after a single shared build:

```bash
rb regression --dispatch slurm
rb randtest my_test 500 --dispatch slurm   # seed fan-out
```

`--dispatch local` (the default) is the unchanged in-process path.

## How it works

**Nothing heavy runs on the submit host** — it is usually an interactive
login node where a big Verilation is against policy. The head process only
plans and submits; the compile and the sims both run as scheduler jobs.

1. **Build job.** The head submits one `sbatch` job per suite that runs
   `rb _build-job` on a compute node: it compiles one shared `simv` per
   unique compile key (`--dispatch` implies
   [`--share-build`](tests.md#sharing-compiled-builds-across-tests)) and
   writes the shared build to the shared filesystem. If no test in the
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
cannot honour — has no shared stamp, so **each array element recompiles
inside its own job**. That is correct, just unshared, and it has two
consequences dispatch handles for you:

- **No build job is submitted** when nothing in the suite can share a
  build. The build pass would compile on a compute node and produce
  something no sim job can read, so it is skipped and the elements run
  ungated (logged as `dispatch.build_job_skipped`). A suite mixing
  builders still gets one, and only the groups that actually read it are
  gated on it with `afterok`.
- **The job's reservation covers both phases.** A compile is frequently
  hungrier than the sim it precedes — a VCS elaboration usually is — so a
  sim-sized reservation would be killed during it. One allocation cannot
  carry two reservations, so the element-wise maximum of
  `cfg-dispatch.resources` (as resolved for that test) and
  `cfg-dispatch.compile` is used, field by field, and logged as
  `dispatch.compile_in_job`. Tests whose builder *can* share a build are
  unaffected: they keep the sim-sized reservation, and the compile
  reservation stays with the build job where it belongs.

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

## Requirements

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
  backend: slurm            # default: local (in-process)
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

`max-jobs-per-array` throttles each *submitted array*
(`--array=1-N%max-jobs-per-array`), not the run as a whole — a regression with several reservation shapes across
several suites submits several arrays, so peak concurrency is roughly
`max-jobs-per-array × arrays`. Size it per array, not per cluster.

!!! warning "Quote `time` values"
    YAML 1.1 reads an unquoted `time: 4:00:00` as the **integer 14400**
    (sexagesimal), which Slurm would take as 14400 *minutes* — 10 days.
    rtl_buddy rejects the unquoted form loudly; always write
    `time: "4:00:00"`. See
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

## Agent loop

An agent driving `--dispatch slurm --machine` closes the loop: run,
read `reservation_advice`, apply each `edit_hint` (raise under-reservations
first — those cost failed runs — then trim over-reservations), rerun to
confirm the advice retires. See the [bundled skill](../agents.md).
