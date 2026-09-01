## Apply reservation advice

After a Slurm run, rtl_buddy compares reservations with `sacct` usage and prints a Reservation Advice table. Machine output returns findings in `payload.reservation_advice`. It never edits configuration.

Advice is calculated per test using the peak across runs in this invocation:

- utilization below `over-threshold` suggests a reduction;
- utilization above `near-limit`, `TIMEOUT`, or `OUT_OF_MEMORY` suggests an increase;
- suggestions use peak times `margin`, with floors of 5 minutes and 128 MiB;
- time advice is limited to Verilator because VCS license wait distorts elapsed time;
- memory advice is suppressed when the longest run is shorter than the accounting sample interval, except that an out-of-memory state still suggests an increase;
- `phase` is `sim`, `compile+sim`, or `compile` and `edit_hint` identifies the configuration field that actually controlled the allocation;
- CPU efficiency is measured against the cpus the job **requested**, not the cpus the scheduler handed out (`AllocCPUS`).

The suite's build job gets one row of its own, named `(build job)` with `phase: compile`. It suggests `time` in both directions and `cpus` only downwards, because low CPU efficiency there means build slots idled rather than that more were needed. Its `cpus` suggestion is per build, and it appears only for a job that ran one build at a time: with `compile.parallel` above 1 the job's CPU efficiency also carries idle slots in the tail, which no accounting field separates from a compile that under-used its own CPUs, so the row is withheld with reason `parallel-utilization-ambiguous` rather than advising a reduction that could starve the longest compile. Size `compile.parallel` against the suite's distinct compile keys first, then read the `cpus` row from a `parallel: 1` run. `time` advice is unaffected — concurrent builds take the wall clock of the longest, not of their sum. A `reduce` is withheld when nothing actually compiled (every build reused its stamp), when the head has no per-build records to judge by — `no-build-records`, which covers both a job that left no envelope at all and one whose envelope carries no `builds` list (an older build job, or one whose telemetry could not be serialised) — or when it finished inside one accounting interval; `rightsize.build_advice_withheld` records which, along with how many records it saw and how many seconds of them were real compiling. Without that guard a re-run of an unchanged suite would advise a limit the next real RTL change times out against, cancelling the fan-out behind it.

### Requested cpus versus allocated cpus

A partition with `SelectTypeParameters=NONE` on nodes with `ThreadsPerCore=2` allocates whole cores, so a job submitted with `--cpus-per-task=1` is charged two: `sacct` reports `ReqCPUS=1` and `AllocCPUS=2`. Measured against the allocation a single-threaded simulation can never exceed 0.5 efficiency, and the default `over-threshold: 0.5` would fire on every such test, advising a reduction to the `cpus: 1` the `tests.yaml` already holds — advice no edit can retire.

Efficiency is therefore taken against the request, which is what a `resources.cpus` or `compile.cpus` edit actually moves, and a `reduce` is emitted only when the suggestion is strictly below it. The denominator is the reservation rtl_buddy itself resolved and submitted as `--cpus-per-task` — the request by construction, so it holds on a site whose Slurm also normalises `ReqCPUS` to the rounded figure. Where that is unavailable the fallbacks are `ReqCPUS`, then `AllocCPUS`.

One case withdraws the first of those. `cfg-dispatch.sbatch-args` is appended **after** the generated reservation flags and therefore overrides them, so an argument there — not the resolved reservation — decides what the jobs request. `ReqCPUS` is *tasks × cpus-per-task*, so two families qualify:

| | Options | |
| --- | --- | --- |
| the cpu count | `-c` / `--cpus-per-task` | states the request |
| task and node counts | `-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes` | raise it |

`--ntasks-per-node` is in the second family because sbatch documents it as a *request* when `--ntasks` is absent ("request that ntasks be invoked on each node … meant to be used with the `--nodes` option"), so `--nodes=2 --ntasks-per-node=4` asks for eight tasks. It degrades to a per-node maximum when `--ntasks` is also given — and that option is in the set too, so the pair is caught either way.

The same applies to the **environment**. `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE` and `SBATCH_NODES` are sbatch's documented input variables for those options, and they reach sbatch because the dispatched submit inherits the head's environment — `SBATCH_NTASKS=4` beside the generated `--cpus-per-task=2` requests eight cpus. rtl_buddy reads them at submit time and treats them exactly like the equivalent `sbatch-args` entry. It does **not** sanitize the environment: a site that exports these means them. Command line beats environment, which is sbatch's own precedence, so a variable whose option is already written in `sbatch-args` is not reported — the job did not run with it. An unset or blank variable is not an override at all.

`SBATCH_CPUS_PER_TASK` is the one that looks like it should count and does not, for the same reason as `--cpus-per-gpu`: every submit path states `--cpus-per-task` on the command line, which beats the variable, so it can never take effect.

The `sbatch-args` half is read from the **backend**, not from the suite's `cfg-dispatch`. The backend is instantiated once, from the orchestration `root_config.yaml`, before the suite loop; `root_cfg` is then rebuilt for any suite that walks up to a different one. In a regression spanning project roots the two lists therefore differ, and only the backend's is what `sbatch` receives — so reading the suite's would miss an override the backend really appends, or invent one it does not. The generated reservation flags stay suite-derived, since they come from that suite's own resolved `resources:`; it is the verbatim passthrough that belongs to the backend.

The environment is read **once per suite, before that suite submits anything**, and the result is carried through to its analysis. A regression submits every suite before collecting any, and a sweep hook is `exec()`d in the head process, so a later suite's hook can set or unset `SBATCH_*` in between; re-reading it at analysis would judge an earlier suite's jobs by a later suite's environment. Both the per-test rows and the `(build job)` row use that one snapshot, so the two halves of a suite's advice always describe the same submission.

A **retry** is a fresh `sbatch` from whatever environment the process holds by then, and it is the retry's telemetry the analysis reads — so each resubmission re-reads the overrides for the rows it resubmits. The new values are applied only once that round has been accepted and waited on: a refused `sbatch` or a failed wait leaves the head holding the previous attempt's results and telemetry, which must not be paired with the reservation of an attempt that never ran. Where a round is abandoned the rows keep describing the attempt that produced their numbers.

Retries are also per run, so a test whose seeds did not all retry can end up with runs submitted under different requests. Efficiency is the peak across every run, and one `Reserved` and one `Field` cannot describe two reservations, so the `cpus` row for such a test is **withheld** and `rightsize.cpus_advice_withheld` records it with reason `mixed-cpu-requests` — the same answer `parallel-utilization-ambiguous` gives the build job. `mem` and `time` advice is unaffected, since no cpu argument moves those reservations.

One **combination** counts even though neither half does alone. sbatch documents a second mode for `--ntasks-per-gpu`: "specify the GPUs wanted (e.g. via `--gpus` or `--gres`) without specifying `--ntasks`, and the total task count will be automatically determined". So a GPU count (`--gpus` / `-G`, `--gpus-per-node`, `--gpus-per-socket`, or a `--gres` that asks for gpus) together with `--ntasks-per-gpu`, and no `--ntasks` anywhere, derives *gpus × ntasks-per-gpu* tasks — a task-count override exactly like `--ntasks`. Both halves may come from `sbatch-args` or from `SBATCH_*`, and the advice names the pair, since neither argument alone caused it. With `--ntasks` present the derivation runs the other way (it sets the GPU count instead) and `--ntasks` is already an override, so the pair is not reported.

All spellings are recognised (`--ntasks=4`, `--ntasks 4`, `-n 4`, `-n4`). Within **one** option the last occurrence is the one reported, because that is the one sbatch obeys, and the short and long spellings are the same option — `[-c 4, --cpus-per-task=8]` is one argument written twice, not two. **Across** options there is no winner at all: each distinct option is reported, because they combine rather than supersede one another.

The set is deliberately narrow, because a false positive is not free — it discards a request rtl_buddy knows, retargets the edit hint away from the field that really governs, and disables the compile floor. Four near misses are excluded:

- `--exclusive` and `--overcommit` change what is *allocated*, not what is requested, so `ReqCPUS` still describes the reservation.
- `--threads-per-core` and `-B` / `--extra-node-info` are **node-selection constraints**: they restrict which nodes and hardware threads may be used, while the generated `--cpus-per-task` still states the request. rtl_buddy therefore still knows it, and keeps using it.
- `--ntasks-per-core` and `--ntasks-per-socket` are **placement maxima** ("request the maximum ntasks be invoked on each core/socket … meant to be used with the `--ntasks` option"): they cap where the tasks `--ntasks` asked for may land, and a lone one requests nothing. The `--ntasks` they accompany is in the set, so a real task-count change is still caught.
- `--cpus-per-gpu` is documented as mutually exclusive with `--cpus-per-task`, which every dispatched job carries, so sbatch rejects the pair. A job submitted that way never runs, and there is nothing to right-size. `--ntasks-per-gpu` is left out of the table on its own, since alone it caps placement without requesting anything — but it is not ignored: see below.

Where such an argument or variable is present rtl_buddy records no request for that run's rows or its build job, so the analysis falls back to `ReqCPUS`; a DEBUG line (`rightsize request_from_scheduler`) names what was responsible.

The `edit_hint` follows. An override masks every cpus field the layering could name, so applying a hint that named one would leave the next job's reservation exactly where it was and the finding would return — the same non-retiring advice this whole rule exists to stop. While an override is in force, a `cpus` finding's `edit_hint.path` is `cfg-dispatch.sbatch-args` (with `file` pointing at `root_config.yaml`) and its `note` says which field was superseded, for example:

```
sbatch-args `--cpus-per-task=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.
```

`suggested` is always the whole-job cpu count, but only one shape of override can be handed it: **exactly one `--cpus-per-task`**, as above. The other two shapes cannot, and the note says so rather than giving advice that would not apply.

A lone task or node count is not a cpu count — writing 3 into `--ntasks` asks for three tasks, not three cpus — and it does not supersede the per-task field either: the generated `--cpus-per-task` is still in force, so both are levers and the note names both:

```
`--ntasks=4` multiplies this job's cpu request: the generated --cpus-per-task
from tests[name=wr_single].resources.cpus still applies, so the request is 8
per task x 4 tasks. Suggested value is the whole-job cpu count — lower
tests[name=wr_single].resources.cpus, the task count in sbatch-args, or both;
no single one of them takes it.
```

The `8 per task x 4 tasks` clause is an observation — the scheduler's own request over the flag the head submitted — and is omitted when that division is not exact.

Where several arguments are present they combine by sbatch's own precedence, which the note does **not** attempt to reproduce — with `--ntasks=8 --nodes=2 --ntasks-per-node=4 --cpus-per-task=2` the request is 16, not the product of all four, because `--ntasks` wins and `--ntasks-per-node` degrades to a maximum. The note names them and leaves the arithmetic to the reader, who is the only party that knows which one should shrink:

```
sbatch-args supersedes tests[name=wr_single].resources.cpus: `--ntasks=4` and
`--cpus-per-task=2` set this job's cpu request together. Suggested value is
the whole-job cpu count — decompose it across them per sbatch's own
precedence; no single one of them takes it.
```

An override that came only from the environment names no file at all: its `edit_hint.path` is `env` and there is no `file` key, because a variable lives in nothing an agent can edit. Where an `sbatch-args` entry is also in play the hint keeps pointing there — the command line is what can defeat the variable — and the note names both.

A **direct** cpu override also disables the **compile cpus floor**. That floor exists because a job compiling inside itself is allocated `max(sim, compile)`, so no reduction can take it below the compile side — but a `--cpus-per-task` in `sbatch-args` replaces that generated flag, and sbatch never sees the max. Left in place it clamps every suggestion up to the floor and then discards it for not being below the request, so a genuinely over-reserved run reports nothing at all.

A task or node count does **not** disable it. `--ntasks=2` leaves `--cpus-per-task=8` exactly where it was and asks for two tasks of it, so the floor still holds and the advice may not suggest below it: even one task costs 8 cpus, so a whole-job suggestion of 3 could never be reached and the finding would recur every run. The floor is kept *unscaled* rather than multiplied by the tasks observed — the task count is one of the two levers the advice offers, so 8 really is reachable, by dropping to a single task, and flooring at 8 × 2 would suppress every reduction there is.

The `mem` and `time` floors are untouched throughout, since no cpu argument supersedes them.

Only `cpus` is retargeted: none of these arguments supersedes a `mem` or `time` field, so those findings keep naming the reservation that governs them.

`mem` and `time` advice never had this exposure: both are already measured against `ReqMem` and `TimelimitRaw`, which `sacct` reports from the allocation the override actually produced, so no override can put a stale number in their denominators.

`Reserved` in the table and `reserved` in `payload.reservation_advice` are that requested figure. Where the scheduler gave out more, the allocated number rides along: the table shows `4 (8 allocated)` and the finding carries an `allocated` field, so `squeue` and `sacct` still reconcile. `allocated` is present on **every** finding for a stable key set, and is null except on a `cpus` row whose allocation differs from its request.

Wherever a row's `edit_hint` names the compile reservation, it names whichever file holds the value that won. A field the suite's own `compile` block set is reported as `compile.<field>` in that suite's `tests.yaml`; every other compile field is reported as `cfg-dispatch.compile.<field>` in `root_config.yaml`. Both forms can appear in one run — a suite that overrides only `mem` gets suite-level advice for `mem` and root-level advice for `time`.

This applies to the `(build job)` row and to any `compile+sim` row alike. A builder that compiles inside its own simulation job produces no build job at all, so the compile reservation only ever appears there, inside the field-wise maximum; a `raise` after `OUT_OF_MEMORY` on a field the suite set points at the suite, because raising `cfg-dispatch.compile.mem` would leave the suite's value in force and the same advice would return on the next run. Fields the test's own `resources` governs are unaffected and still name `tests[name=…].resources.<field>`.

Advice records the run count and regression level; do not use a smoke run to shrink a nightly reservation. Apply the provided `edit_hint`, rerun, and confirm the finding clears. Disable reports with `rightsize: {report: false}`. Without `sacct` accounting, dispatch completes but emits no advice.

To inspect accounting manually, query step rows without `sacct -X`:

```bash
sacct -j <jobid> --format=JobID,Elapsed,MaxRSS
```
