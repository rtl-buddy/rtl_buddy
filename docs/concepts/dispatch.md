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

1. **Head-node build pass.** The head process compiles one shared `simv`
   per unique compile key — `--dispatch` implies
   [`--share-build`](tests.md#sharing-compiled-builds-across-tests) — and
   stops at compile.
2. **Fan-out.** One job per `(test, run_id)` is submitted via `sbatch`.
   Jobs with identical resolved resources are grouped into a single Slurm
   **array**; `cfg-dispatch.max-jobs` maps to the array's `%N` concurrency
   throttle. Each job re-invokes `rb _test-job`, whose own compile
   short-circuits on the shared-build stamp, so it runs simulation + post
   only.
3. **Collect.** The head waits for the queue to drain (once, across all
   suites), then loads each job's result and feeds the normal summary and
   exit code. A job that produced no result (scheduler kill, crash) counts
   as a fail — never silently dropped.

Because the head builds once and stops at compile, `--dispatch` cannot be
combined with `--early-stop`, and dispatched jobs deliberately skip the
per-tree lock (see [Known Issues](../known-issues.md#the-artefact-tree-lock-is-per-tree-and-its-lock-file-stays-behind)).

## Requirements

- A Slurm client on the submit host (`sbatch`/`squeue`/`sacct`/`scancel`)
  — see [Installation](../install.md). `rb tool-check --explain slurm`
  reports readiness.
- A **shared filesystem** visible at the same absolute paths on the submit
  host and every compute node: the project checkout, the `artefacts/`
  tree, and the Python environment.
- The project's `rb` runnable on the compute nodes (the job runs
  `sys.executable -m rtl_buddy _test-job`).
- Verilator is the first-class builder (share-build is Verilator-only);
  other builders still work but recompile inside each job.

## Configuration: `cfg-dispatch`

All optional, in `root_config.yaml`:

```yaml
cfg-dispatch:
  backend: slurm            # default: local (in-process)
  resources:                # cluster-wide per-job defaults
    cpus: 2
    mem: 4G
    time: "01:00:00"        # QUOTE time values (see below)
  sbatch-args:              # passed to sbatch verbatim
    - --partition=verif
    - --account=chip
  max-jobs: 200             # concurrency throttle, PER submitted array
  poll-interval: 10         # seconds between queue polls (> 0)
  rightsize:                # reservation right-sizing (see below)
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`max-jobs` throttles each *submitted array* (`--array=1-N%max-jobs`), not
the run as a whole — a regression with several reservation shapes across
several suites submits several arrays, so peak concurrency is roughly
`max-jobs × arrays`. Size it per array, not per cluster.

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
- Requires `sacct` (slurmdbd accounting). Without it, dispatch still works
  and right-sizing degrades gracefully to no advice. Turn it off with
  `rightsize: { report: false }`.

## Agent loop

An agent driving `--dispatch slurm --machine` closes the loop: run,
read `reservation_advice`, apply each `edit_hint` (raise under-reservations
first — those cost failed runs — then trim over-reservations), rerun to
confirm the advice retires. See the [bundled skill](../agents.md).
