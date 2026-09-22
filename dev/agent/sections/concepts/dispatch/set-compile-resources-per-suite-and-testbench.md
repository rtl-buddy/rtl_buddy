## Set compile resources per suite and testbench

`cfg-dispatch.compile` is one reservation for every suite's build job, so a repo with one large top-level testbench and many leaf-cell benches sizes them all for the largest. A suite that differs states its own reservation at the **top level of its `tests.yaml`**, in the same `{cpus, mem, time}` shape:

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G          # this suite's verilation only; cpus and time inherited
  parallel: 1       # ...and how many of its builds run at once

testbenches:
  - name: soc_tb
    ...
```

The compile reservation resolves field by field in this order: testbench `compile`, suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults. A field an outer layer omits inherits, so the example above keeps the cluster-wide `cpus: 8` and `time: "02:00:00"` and moves only memory and concurrency. The block sizes the suite's build job, and — for a builder that cannot share a build — the compile half of the field-wise maximum that sizes each simulation job.

Where [verilation is split off](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#split-verilation-from-the-c-build) the block sizes two jobs. A `compile.verilate` sub-block of `{cpus, mem, time}` sizes the verilate job and layers over the same three layers field by field; `compile` itself then sizes the C++ build job alone. `compile.verilate.cpus` defaults to 2 because verilation is single-threaded, and its `mem` and `time` inherit the resolved `compile` values, so a suite that states nothing reserves the verilate job as the single build job was reserved, at two cpus. `compile.split-verilate` belongs to `cfg-dispatch.compile` or the suite block, like `parallel`, and is rejected in a testbench block.

A testbench states its own `compile:` when the suite's entries verilate at different scales — the same top level at two geometries, say, where the small one takes four minutes and 6 GB and the product one takes two hours and 130 GB:

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

A testbench block takes the same `{cpus, mem, time}` shape and wins over the suite block field by field, in both directions — it may lower a field as well as raise one. Writing `parallel` there is an error: it is how many builds the one build job runs at once, so it belongs to the suite block or to `cfg-dispatch`.

**The two blocks mean different things, and the build job's reservation is where that shows.** A suite-level `compile:` describes the **whole job** — the allocation you watch in `squeue` — and nothing below it may take the reservation under that figure. A testbench `compile:` describes **one build**, so the suite's build job aggregates them over the testbenches the plan actually selected tests from, then floors the result at the whole-job value:

| Field | Over the planned builds' blocks | Then |
|---|---|---|
| `cpus` | the largest single block's | floored at the suite-resolved value, then × `compile.parallel` as always |
| `mem` | the sum of the largest `min(parallel, n)` per-build figures — the builds that can be in flight together each hold their own peak | floored at the suite-resolved value |
| `time` | the makespan of the build job's own work queue: each build goes to whichever of the `parallel` workers frees up first, in plan order, and the job ends when the last worker does | floored at the suite-resolved value |

Each phase aggregates its own fields by those rules over the same set of distinct builds: the verilate job over the `compile.verilate` blocks, the build job over the `compile` blocks.

At `parallel: 1` that makespan is the serial total; with a worker per build it is the longest build. In between it is a real schedule, not `ceil(sum / parallel)` — 30, 30 and 20 minutes over two workers finish in **50**, not 40, and a reservation sized from the lower figure times the job out mid-compile.

The unit of the aggregation is one **build**, not one testbench: the build job groups on the compile directory a config resolves to after `preproc`, so two selected tests on one testbench that differ in `plusdefines`, `builder`, `model` or `assertions` compile separately and each hold their own peak. rtl_buddy counts one reservation per distinct `(testbench, plusdefines, builder, model, assertions)` among the planned tests — `assertions: true` puts Verilator's `--assert` flags into the compile command, so it splits the key like the rest. Two kinds of test are counted individually instead: one whose builder cannot share a build, which compiles into its own artefact directory, and one that declares a `preproc:` hook, whose plusdefines the hook may still change after this count is taken — a test with a preprocessing hook is assumed to produce its own compile. It cannot see the real compile key from the submit host, so where two such configs happen to resolve to the same key the job is reserved for a build it does not run — an over-count, which is the safe direction.

A config whose builder **cannot share a build** is counted per test, because it compiles into its own artefact directory: the build job runs `preproc` and compile for the whole plan, self-compiling configs included, and two of those are two builds however alike they are. Each field of a testbench `compile:` block must be greater than zero — a negative `mem` would be subtracted from the sum, and both are rejected when the suite loads.

A testbench with no `compile:` of its own enters no `cpus` or `time` sum; it is covered by the whole-job value, so a suite of five plain benches under `time: "00:30:00"` still reserves thirty minutes. `mem` is the exception, and only once some build *has* stated its own: memory is the one field where two builds genuinely need their peaks at the same moment, so every other planned build then contributes the figure the whole-job value implies for one build. A 256G testbench sharing a slot with an unannotated build under `compile: {mem: 8G, parallel: 2}` reserves 264G, not 256G. `time` differs because an unannotated build's wall clock is unknown and the whole-job figure already describes the queue they run in. A suite that states no per-testbench `mem` at all is untouched: there `compile.mem` keeps the meaning this page has always asked for — the whole job at `parallel` concurrent builds. Only planned tests count either: a run that selects nothing from `tb_chip_t1` reserves the suite's `8G`, which is what stops a pull request fencing off memory for a build it never runs.

Reservation advice names the entry whose block supplied the winning field, as `testbenches[name=tb_chip_t1].compile.mem`. Two shapes have no such entry to name, and both withhold the `reduce` row rather than aim it somewhere inapplicable, recording the reason and the paths in `rightsize.build_advice_withheld`:

- the value is a **sum of several builds** (`compile-aggregate`) — the suggestion is a whole-job figure, and writing it into one contributor's key would leave the total where it was;
- two sources produce it **independently** (`compile-origin-tied`) — two builds at the same figure, one that merely reaches the whole-job floor, or two of the `parallel` workers finishing at the same makespan, where lowering either alone moves nothing.

`raise` advice is unaffected by both: moving any one source up moves a maximum, and a sum with it. Where the field is a sum, though, the `suggested` value is translated into the named contributor's **own** new value rather than the whole-job figure — writing a 135-minute target into the 60-minute half of a 30 + 60 reservation would re-aggregate to 165, so the advice says `01:45:00` instead, and the machine event carries the whole-job figure as `suggested_total` beside the `aggregate_delta` that was added. For `time` the proposed value is then **scheduled** rather than predicted: one key can be several builds, and raising it can push a later copy behind a neighbour it used to run beside, so the queue absorbs part of the raise. rtl_buddy re-runs the same `parallel`-worker schedule on the proposed edit and closes the remaining gap until the makespan clears the target — advice that re-timed-out after being applied would be worse than none. A simulation job that compiles for itself is sized from its own testbench's block alone, never from the aggregate.

`parallel` layers the same way, over `cfg-dispatch.compile.parallel`, and must be at least 1. The build job is per suite, so a suite that compiles one key writes `parallel: 1` and its build job reserves `cpus` instead of `cpus` × the cluster-wide value — on a busy partition that is the difference between starting and queueing. A suite that says nothing keeps inheriting the cluster-wide value. Sizing it against the partition's widest node is the writer's job at either level, since only `cpus` is scaled for you. The build job's `Compiling N distinct build(s)` line names whichever key governed it, so the log says which file to edit; where the planned-config cap lowered the value it quotes what the file holds and reports the cap separately, rather than attributing the capped number to the key. `parallel` is still meaningless in a per-test or per-testbench `resources:` block, where unknown keys are dropped silently; in a testbench `compile:` block, and in any [`modes:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#size-a-reservation-per-builder-mode) block, it is rejected at load instead, because there the key reads as if it meant something.

The block is a scheduling fact only. It is not part of the compile fingerprint, so adding or changing it never invalidates a shared build stamp.

Size the **suite-level** `compile.time` for the longest batch the build job runs, not for one build (a testbench block states one build and is queued for you). With `compile.parallel: N` the suite's unique compile keys are compiled N at a time, so the job's wall clock is the makespan of a work queue N workers deep: each worker takes the next unbuilt key as it frees up, and the job ends when the last one finishes. `ceil(distinct builds / N)` times the slowest build is a safe upper bound to size against, and it is close to the real figure only when the builds take similar times; a mix of one long build and several short ones finishes nearer the long one alone. At the default `parallel: 1` it is the serial total of every key.

Size the **suite-level** `compile.mem` for `parallel` concurrent builds while no testbench states its own: the head scales only the `cpus` reservation, and N elaborations need roughly N times the memory. Once any testbench block does state one, the figure switches meaning — it is then read as one build's peak, for each build that declared none, and the head adds them up; write it per build from that point on. Size it from elaboration, not simulation. Large generated structures can make elaboration the memory peak; Slurm reports `OUT_OF_MEMORY`, while local runs may show `Killed`, SIGKILL, or exit 137. Raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`.

Where the split applies, the two phases want opposite sizes and `compile.verilate` is where the elaboration figures go. Verilation holds the peak RSS of the whole compile, so an `OUT_OF_MEMORY` on a large design is a `compile.verilate.mem` edit; its `cpus` can stay at the default. The build job is the other way round: its memory is one compiler process at a time, so a `compile.mem` sized for elaboration can come down once `compile.verilate.mem` carries that peak, and its `cpus` is the field that matters — size it at the Verilator `-j` or `--build-jobs` in `builder-opts.<mode>.compile-time` times `compile.parallel`.

VCS license wait under `-licqueue` counts against the Slurm time limit. Give `compile.time` queue headroom; `compile.license_queued` records only completed builds that waited. N concurrent elaborations hold up to N licenses at once, so raising `parallel` multiplies license pressure and can convert compute time into queue time; keep it at or below what the site's license pool can serve.

Dispatch requests `--acctg-freq=task=1` unless `sbatch-args` already supplies it. Keep fine-grained accounting if you want useful memory advice for short jobs.

<a id="retrying-a-license-queue-kill"></a>
