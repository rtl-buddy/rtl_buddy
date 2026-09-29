---
description: Current rtl_buddy limitations, surprising behavior, and required workarounds, grouped by workflow.
---

# Quirks & Known Issues

Use this page when behavior differs from what you expect. Each section states the effect and the action to take. Sections run in workflow order: CDC, coverage, simulation, artifacts, dispatch, shared builds, synthesis, physical and graph views, formal and mutation, FPGA, then tools.

## XPM CDC macros require rtl-buddy-cdc 0.4 or later

rtl-buddy-cdc 0.3.x treats `xpm_cdc_*` instances as dual-clock blackboxes. It reports `CDC-BBX` and drops their crossings from the report and domain map. Upgrade with `uv tool install -U rtl-buddy-cdc`. Waivers can hide the finding but cannot recover the lost crossings.

## rtl-buddy-cdc cannot take a filelist `+incdir+`

rtl-buddy-cdc takes plain source paths and has no include-path option, so `rb cdc` and the hub's domain-map build cannot pass it a filelist `+incdir+`. Every other non-simulation flow forwards them (Yosys `-I`, Vivado `-include_dirs`).

The run logs `cdc.filelist_incdirs_unsupported` naming the directories. A header that resolves only through one of them fails in the analyzer with `Cannot find include file`. Write the `` `include `` path relative to the including file, or run the `vivado` cdc tool.

## Coverage totals are per elaboration by default

Verilator keys every coverage point by the module it elaborated. When a suite's compile keys build the same RTL with different defines or parameters, each source point is scored once per elaboration, and a key that exercises none of a block leaves that block's copy uncovered. A block reported short on branch coverage can be at 100% once the copies are collapsed.

- `rb cov summary`, `--coverage-dir-summary` and the merged totals report the per-elaboration figure.
- To see what the suite covered, read `source_totals`: the `run (source)` row of `rb cov summary`, `rb cov summary --by-source`, or `--coverage-source-summary` on `test` and `regression`.
- `--coverage-dir-summary` has no collapsed form. It is parsed from LCOV, which has already folded the elaborations.

See [Coverage](concepts/coverage.md#per-elaboration-vs-source-point-figures).

## Coverage uses the platform builder

Coverage collection and labels use the platform-selected builder, even when a suite or test selects another `builder:`. A mismatch can mislabel or misparse coverage. Use `--builder <name>` for the run, or make that builder the platform default. See [YAML Formats](reference/yaml.md).

## Coverage merging runs in the submitting process, with no timeout

`verilator_coverage --write`, the coverage model build and the LCOV exports all run in the process that invoked `rb`, including under `--dispatch slurm` where every simulation ran on a compute node. LCOV is exported once per test with `use-lcov` or `--coverage-html`, otherwise once for the merged database.

- A few hundred inputs can peak in the gigabytes, so a per-user memory cap on a shared submit host can kill the merge.
- A killed merge reports `FAIL` for toggle, expression and functional coverage, records `merge_failed` in the manifest and the machine envelope, and exits 1. See [Read a failed merge](concepts/coverage.md#read-a-failed-merge).
- The merge has no timeout, so a merge that hangs hangs the run.

Run the coverage-producing command itself on a compute node, for example by submitting `rb regression --coverage-merge` as one job.

## Verilator randomized runs may not reproduce

Verilator can produce different behavior for the same random seed. When reproducibility matters, use VCS with `-xlrm hier_inst_seed` and give instances stable explicit names.

With hierarchical seeding, VCS writes `HierInstanceSeed.txt` in the simulation directory. If it is missing, rtl_buddy logs `sim.hier_seed_missing` and cannot record the seed. The test verdict is unchanged.

## Tool-path fallback can select another installation

A configured tool directory takes precedence only when it contains the requested executable. Otherwise rtl_buddy may use a matching executable on `PATH` and logs a fallback warning.

Fallback and unresolved-variable warnings are emitted once per process. After changing `root_config.yaml`, `.rtl-buddy/.env` or the environment, restart long-running `rb hub` and `rb mcp` processes to see them again.

## Hook scripts are not normal standalone scripts

`sweep` and `preproc` hooks run with the invocation directory as CWD and with `__name__ == "__rtl_buddy_hook__"`. Use the injected `suite_dir`, `artifact_dir` and `run_artifact_dir` paths. Do not put required hook logic behind `if __name__ == "__main__":`.

rtl_buddy captures Python-level `print()` output as `hook.stdout` events. The capture object has no `.buffer` or file descriptor, and child-process output bypasses it. Capture child output explicitly and print the text you want logged. If a generator can write only relative to CWD, change to `suite_dir` temporarily and restore the previous directory. See [Plugins](concepts/plugins.md).

## `toplevel:` must name the testbench in a plain SystemVerilog test

For a plain SystemVerilog testbench, `toplevel:` reaches the builder as Verilator `--top-module`, VCS `-top` or Icarus `-s`, so it decides what the simulator elaborates. It must name the testbench, not the DUT the testbench instantiates. cocotb and SystemC testbenches already use `toplevel:` as the elaboration root and need no review.

- A `toplevel:` that names the DUT compiles, but the simulation exits at once because the elaborated root has no `initial` block. The test ends `NA` with `no PASS/FAIL markers found in .../test.log; result is NA` and `test result unknown`. Nothing in that output names `toplevel:`.
- A DUT with unconnected interface ports fails with `%Error-UNSUPPORTED: Interfaced port on top level module` instead.
- `toplevel:` is part of the compile fingerprint, so adding or changing it selects a new shared-build directory and compiles once. Without it, the top is not inferred from the testbench `name`; it is elected from filelist order.

A top pinned in the builder's `compile-time` opts wins over `toplevel:`, in every spelling the family accepts, including Icarus's glued `-stb`. When the two differ, the run logs `compile.toplevel_conflict` once, naming both, and uses the configured top. This holds for SystemC and cocotb too, which generate a top flag only when the builder pins none. Other simulator families get no top flag; `compile.toplevel_family_unsupported` records that at debug level.

## Narrow VCS access flags suppress cocotb defaults

For cocotb, rtl_buddy adds VPI access unless a configured compile option already starts with `-debug_access` or `+acc`. A narrower configured flag therefore suppresses the full default and can prevent signal writes. Remove the narrow flag, or configure sufficient access such as `-debug_access+all` and `+acc+rw`.

## VCS license waits pause the simulation timeout

When VCS prints its license-queue banner, rtl_buddy pauses `sim_timeout` until simulator output resumes, for up to one hour. A queued run can therefore outlive its nominal timeout.

If a newer VCS banner is not recognized, the clock can resume too early. A timeout next to license messages in `test.err` indicates this case. Set the builder's `extra-sim-timeout` as a backstop.

## A timeout kill can leave `test.log` with an unflushed tail

When `sim_timeout` expires, the simulator can be terminated before it flushes output. `test.log` can end mid-line or at a power-of-two byte count, so its last bytes do not locate the stop. Follow the [timeout triage order](concepts/tests.md#triaging-sim-hit-timeout) before raising the limit.

## Deeply nested expressions can exhaust the elaboration stack on macOS

`rb elab` runs its analysis pass on a slang thread pool whose threads take the platform default stack: 512 KiB on macOS, 8 MiB on Linux. `max_parse_depth` lifts only the parser's nesting limit, not the recursion in later passes.

On macOS, about 400 nested conditional expressions kill the worker with no diagnostic, and the run reports `elaboration worker did not produce a result`. The same source elaborates on Linux. Elaborate such generated RTL on Linux, or reduce the nesting. Nesting that costs less stack per level, such as parentheses, is unaffected. See [Model Elaboration](concepts/elaboration.md).

## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail immediately on same-host contention. The file stays after release; kernel lock state, not the file, says whether the tree is locked.

- If the lock record names this host and a process that no longer exists, or a pid whose start time does not match the live process, the next run reclaims the tree and logs `artifact_lock.reclaimed`.
- A record from another host, or with no host, is never reclaimed by a pid check. Clear it by hand once the other machine is idle.
- The lock is coarse across command families and is not assumed to coordinate different NFS hosts.
- Dispatched worker jobs skip it because they write planned subdirectories. Do not start another command against a tree with a dispatch run in flight.
- A filesystem that cannot `flock` (`ENOLCK` on some NFS mounts, a read-only tree) fails with `cannot lock this artefact tree`. Unlike the shared-build lock, this lock does not degrade to unlocked.

Give concurrent runs of one suite a [`--run-tag`](concepts/execution-context.md#namespace-concurrent-runs) so each locks its own tree. Two runs with the same tag still contend.

## `--run-tag` covers the test flows, not every reader

`--run-tag` is accepted by `rb test`, `rb randtest`, `rb regression`, the dispatch job commands and `rb graph results`. Plan for these consequences:

- `rb wave`, `rb cov`, `rb phys` and the other flow commands read the flat `artefacts/<test>/`. Open a tagged trace by path: `artefacts/.runs/<tag>/<test>/dump.fst`.
- The hub, the MCP server and `rb graph query` read the untagged `artefacts/graph/results-overlay.json`. Publish one tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.
- Two tagged Slurm runs of one suite serialise their build jobs, because the `--dependency=singleton` rendezvous is keyed on the suite directory. The simulation fan-outs still overlap.
- A relative path in `builder-opts.<mode>.compile-time` resolves against the compile's working directory, which is one level deeper under a tag. Write project files as `${RTL_BUDDY_PROJECT_ROOT}/...` so they resolve the same way either way.
- Merged coverage is not namespaced. `--coverage-merge*` writes `<command_root>/cov_dir/` whatever the tag, so two concurrent tagged runs that both merge write one directory. Merge in only one run, or merge afterwards from each run's per-test `coverage.dat`.

## Tool flows delete their previous outputs before running

`rb cdc`, `rb synth`, `rb fpga`, `rb pnr` and `rb power` remove the outputs they are about to write from the run's artifact directory before invoking the tool: reports, domain maps, synthesis netlists, DEF/ODB, GDS/PNG and bitstreams. `rb hub` does the same for the `view.json` and domain map it caches under `.rtl-buddy/cache/`.

An exit code cannot tell "nothing to report" from "crashed before writing" (rtl-buddy-cdc exits 1 when it finds violations). Clearing first means an absent report stays absent, so the flow says so and names its log instead of reporting a stale result.

- A failed rerun leaves no output rather than the previous run's. A failed `rb synth` leaves no netlist, so `rb pnr` and `rb power` tell you to run `rb synth` first.
- `rb fpga` without `--bitstream` removes a previously built `<top>.bit`.
- Copy out any artifact you want to compare against before rerunning.
- Logs are exempt: each flow truncates its own log, so a crashed run still has one to read.
- An artifact that cannot be deleted, such as a permissions problem or a directory where a file belongs, fails the run with a fatal error.

Clearing happens early, so a rerun that fails on a filelist error or unresolvable config also leaves nothing behind. The outputs a later command consumes, meaning the synthesis netlists and pnr's DEF and ODB, are cleared before anything else, even the tool-availability check. Outputs read back only within the same run are cleared just after that check. See [Which outputs a tool flow deletes](#which-outputs-a-tool-flow-deletes).

## Which outputs a tool flow deletes

A run that cannot find its backend tool deletes nothing for `rb cdc` (both backends), `rb fpga` and `rb power`, because a machine without the tool never produced those files. `rb synth` and `rb pnr` clear regardless, because a later command resolves their netlists and DEF/ODB by path and must not use an old one.

A configuration error, such as an unknown `platform:` or a part a backend cannot build, is never a skip. It is reported and clears the outputs.

- Outputs named after the top, such as `<top>.bit` and `<design>.routed.odb`, are matched by suffix, so changing `model:` or `top:` does not strand the previous top's files.
- Another command's durable outputs are never matched: `result.json`, `rb-compile-stamp.json`, the dispatch envelopes, `cdc.json`, CDC domain maps, `power.rpt` and the synthesis netlists all survive. The scan does not recurse into a workdir a tool owns.
- A tool that writes an output and then fails is a failure like any other, and the flow removes what it wrote. A failed run publishes nothing.
- The exception is a `pnr` run failed by `gds-mode: strict`. The layout and its report go, but the routed DEF, netlist, SDC and ODB stay, because OpenROAD finished cleanly.
- `rb pnr-export` clears only what an export publishes: the GDS, PNG, stream-out report, input manifest and `export.provenance.json`. It never clears the routed outputs it reads, whether the export succeeds or fails.

An artifact directory is keyed on the run's name, and names need not be unique across commands, so an `rb fpga` run, a CDC analysis and a simulation test with the same name share a directory. Avoid that. An FPGA run and a power run in one suite must not share a name, because both own `artefacts/<name>/power.rpt` and the second to run overwrites the first.

## Dispatch changes build behavior

`--dispatch` implies `--share-build` and rejects `--early-stop`. `cfg-dispatch.backend` applies to `regression` and `randtest` by default, but `rb test` stays local unless you pass `--dispatch`. A one-seed `randtest` replay also stays local.

Shareable builders compile once per compile key. Builders that cannot share compile inside their jobs, and fanned-out tests still use a build job to serialise access to their compile directory. See [Parallel Dispatch](concepts/dispatch.md).

## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time, array-throttle, array-size and right-sizing settings. `max-jobs-per-array` and `max-array-size` describe Slurm job arrays, which this backend never submits. `-j` or `cfg-dispatch.jobs` is the only limit, so size concurrency for the heaviest test's memory use.

The resolved `compile.parallel` is the exception. It is the suite's own `compile:` block where it sets one, otherwise `cfg-dispatch.compile.parallel`. The build job honours it internally while occupying one pool slot, so the host's real ceiling is `jobs` multiplied by `compile.parallel`, and nothing clamps it. Size the two together.

Normal interruption terminates the worker process groups. `SIGKILL` of the head process cannot run cleanup and can orphan `rb _test-job` children. After a hard CI timeout or `kill -9`, find and stop them.

## Oversized resource groups are split into several arrays

Slurm refuses an array larger than `MaxArraySize` or than `SchedulerParameters=max_array_tasks`. rtl_buddy reads both from `scontrol show config` once per cluster per run, before the first array submit, and splits a larger group into arrays of at most `min(max_array_tasks, MaxArraySize - 1)` elements.

- Each slice gets its own manifest and element logs under `slice-N/` in the run's dispatch directory. A group that fits in one array keeps the flat layout.
- `max-jobs-per-array` throttles each slice, so peak concurrency is the throttle times the number of slices.
- `cfg-dispatch.max-array-size` and `cfg-dispatch.max-array-tasks` override the probed values independently. An unset field still comes from `scontrol`, and either one alone is enough to split a group.
- When `sbatch-args` (`-M`) or `SBATCH_CLUSTERS` selects another cluster, the probe targets that cluster, and `sbatch-args` wins.
- With several clusters selected (`--clusters=a,b` or `all`), the limit is unknown because Slurm picks the cluster at submit. Set one of the two fields yourself.
- If the submit host cannot run `scontrol`, nothing is split and sbatch refuses an oversized group with `Invalid job array specification`. The run logs `dispatch.max_array_size_unknown`, and the submit failure repeats the hint. Set `cfg-dispatch.max-array-size` to the cluster's value, and `max-array-tasks` too where the cluster caps tasks per array lower.

## Quote dispatch time values

YAML 1.1 parses an unquoted value such as `time: 4:00:00` as an integer. rtl_buddy rejects it rather than submit a 10-day Slurm reservation. Write `time: "4:00:00"` or a quoted minute count everywhere `resources:` appears, `modes:` blocks included.

## An unknown key in a `resources:` block is dropped silently

A `resources:` block discards any key it does not define, with no warning, so a misspelled or unsupported field reserves nothing while reading as if it did. `parallel` and `split-verilate` are documented cases, since both are job-wide and meaningless there, but a typo such as `memory:` behaves the same way.

Check a new reservation against [YAML formats](reference/yaml.md#parallel-dispatch). Confirm it took effect in the `Reserved` column of the run's reservation advice, or in the job's own `--mem` and `--time`.

Two places are strict instead. A testbench `compile:` block rejects `parallel` and `split-verilate`, and a `modes:` block rejects every key it does not define. An rtl_buddy release that predates `modes:` drops the whole block in the silent way described above, so after adding one, confirm the mode's reservation once.

## Dispatch build jobs cover the whole suite

One suite build job compiles every unique compile key. Under `--dispatch slurm`, each simulation job is released as soon as its own key is built. `compile.parallel` compiles that many distinct builds at once inside the job, so `compile.time` must cover the longest batch rather than the serial total. `compile.mem` must cover that many concurrent builds, because only `cpus` is scaled for you. Count `compile.start` events to estimate the work.

- Under `--dispatch slurm`, a Verilator suite splits the job into a verilate job and a C++ build job. `compile.verilate.time` and `compile.verilate.mem` cover the verilations; `compile.time` and `compile.mem` cover the builds. `compile.parallel` applies to each phase separately.
- Elaboration is the memory peak and belongs to the verilate phase, so `OUT_OF_MEMORY` there is a `compile.verilate.mem` edit. `compile.split-verilate: false` returns to one job.
- Each build-phase job reserves that phase's `cpus` times `min(parallel, planned tests)`. The cap is planned tests, not distinct compile keys, because the head cannot know the keys without writing filelists on the submit host. Twenty tests over three keys with `parallel: 8` reserves eight builds' worth of CPUs for three.
- Set `parallel` to the expected count of distinct builds, and check it against the `(build job)` and `(verilate job)` rows of the reservation advice.

## Preprocessors run in every dispatched job

Under dispatch, `sweep` runs once on the head. `preproc` runs in each build-phase job and again in every simulation job. Make `preproc` idempotent, write shared generated files atomically to `artifact_dir`, and write run-dependent files to `run_artifact_dir`.

- With a resolved `compile.parallel` above 1, no config's `preproc` may mutate an input another config compiles. The build-phase job runs every hook before any builder starts, because a compile key is known only after its own hook has run. A hook that regenerates a suite-level file overwrites it for configs already fingerprinted.
- At `parallel: 1`, the job runs `preproc` and the compile per config in turn. A hook that owns one shared file per config is safe there between configs with distinct compile keys.
- Configs that share a compile key are compiled once and adopt that build. A hook that rewrites a consumed input per config on one key fails every config after the first with `build_job.group_input_drift`.
- Simulation jobs need the same discipline at any setting. Each reruns `preproc` on its own node, and under `--dispatch slurm` it can start while the build job is still compiling other keys.

Keep `preproc` idempotent and per-config at every setting.

## Simulation jobs reuse the build stamp

Simulation jobs skip recompilation by validating the build stamp, which records a content hash of every tracked input under the project root. A preprocessor that regenerates a filelist source byte-for-byte invalidates nothing. One that changes a source a compile consumes invalidates every stamp. A preprocessor that writes files no compile reads, such as its own program directory under a `+incdir+`, does not. `compile.prebuilt_stamp_invalid` identifies a job that recompiled and names what drifted.

A gated simulation job whose stamp fails against a build the build job recorded as built does not recompile. It fails with the stamp check's reason and `compile.build_stamp_rejected`, and leaves the shared build directory and its stamp as the build job left them, so same-key siblings still reuse the binary.

- A missing stamp counts as a stale one.
- A compile the build job could not stamp shows `compile.stamp_write_failed` in the build job's log and `stamp_written: false` beside the config in the build result. Give the shared build directory's filesystem room and permissions, then rerun.
- To fix drift, find what changes the inputs, usually a `preproc` that writes different bytes on the simulation node than on the build node. Otherwise give the affected tests a compile key of their own.
- There is no per-test opt-out of the shared directory under `--dispatch`. A suite that cannot share compiles locally without `--dispatch`.

Configs that share a compile key adopt the first config's build after checking the dependencies it reported consuming, so a per-test hook that writes beside the sources costs no second compile. A config whose consumed input differs from the leader's fails with `build_job.group_input_drift` rather than recompiling, because one key is one directory. `build_job.group_adoption_declined` (with a reason) and `build_job.group_leader_unstamped` mean the key was compiled more than once in this job. That count is what `cfg-dispatch.compile.parallel` is sized against.

## Design compile errors under dispatch are CompileFail

A design compile error is reported as `CompileFail`; infrastructure failures stay `DispatchFail`. When the build job recorded a config as failed with a builder exit code, on the same inputs the simulation job would compile, the simulation jobs do not recompile it. The summary row carries the build job's error and logs. Fix the design or the build reservation, not the simulation one.

A simulation job recompiles only when nothing built the config and nothing proved it cannot be built:

- The build job crashed or was cancelled before reaching the config.
- A build-side setup failure left no builder exit status.
- The inputs changed since the failed build.
- No readable envelope exists.

That compile runs at simulation size, with its transcript in the run's own `compile.retry.log` (under `run-NNNN/` for a fanned-out test). Size the simulation reservation for compilation only if you rely on this path.

## Slurm retry reuses artifact paths

A retry overwrites the first attempt's simulation capture and per-job rtl_buddy log. Only `slurm-<tag>-retry<N>.log` stays attempt-specific. When diagnosing retries, use the scheduler logs and the head's `dispatch.result_missing` event.

`max-wait` applies to each attempt, not the whole run, and includes the requested backoff. Slurm obeys the last of duplicate options, so a later `--begin` in `sbatch-args` overrides rtl_buddy's retry delay. Remove a custom `--begin` when you use retry backoff.

## Slurm memory advice depends on accounting samples

rtl_buddy requests one-second task accounting unless `sbatch-args` already sets `--acctg-freq`. When the longest run ends within the sampling interval, `MaxRSS` is unreliable and memory advice is suppressed. Time and CPU advice remain.

Right-sizing suggestions also have fixed floors of five minutes and 128 MB and need at least 25% savings. A very small reservation can therefore get no reduction advice even when utilization is low.

## Generated `run.f` files are checkout-specific

rtl_buddy writes explicit source entries, `+incdir+` directories and `-y` directories as absolute paths, so Verilator cannot resolve a relative source through an include or library directory in another checkout. Do not commit or copy `run.f` between checkouts. Use one symlink spelling of a checkout consistently, because path spelling affects compile keys.

A relative search directory is resolved by the builder, not by rtl_buddy. `-f` entries are read relative to the builder's working directory. Even a builder started in `run.f`'s own directory can disagree when a symlink sits between that directory and the design, because a relative path collapses `..` textually while a process walks it physically.

Two spellings cannot be made absolute:

- An include directory whose path contains `+` keeps its relative spelling, because filelist parsers read `+incdir+a+b` as two directories. `filelist.incdir_unrepresentable` names those entries. They depend on the builder's working directory until the `+` is out of the path.
- A path containing whitespace is quoted. Verilator's `-f` parser understands the quoting, Icarus's does not, and VCS is unverified. Keep whitespace out of checkout paths for those simulators.

## Mount paths must match across cluster nodes

On a cluster with different mount paths per node, a build stamp from one node may not validate on another.

- Outside `--dispatch`, the result is a safe recompile, never compilation of the wrong source.
- Under `--dispatch`, the simulation job does not recompile a build its build job recorded as built. It fails with `compile.build_stamp_rejected` naming the path that differed. See [Simulation jobs reuse the build stamp](#simulation-jobs-reuse-the-build-stamp).

Spell the project root the same way on every node, with one mount point and one symlink, or run the build and simulation jobs on nodes that share the spelling.

A configured `shared-build-root` relaxes this for the project. Compile keys are spelled relative to the project root and include each input's content hash, and the stamp's tracked inputs are re-anchored against the reading checkout's root. A stamp one workspace wrote therefore validates in another. Mount-path differences still matter for anything outside the project root, such as the toolchain's includes and binary.

In that mode, an input the hashing cap excludes is keyed by size and modification time. That keeps two checkouts apart, at the price of not sharing that suite's build between them. Switching the cache on or off recompiles each shared build once.

## Shared-build stamps track dependencies differently per simulator

A build stamp decides whether a shared build can be reused. What it tracks depends on the simulator.

- **Verilator** reports the headers, library files, standard includes and binary it consumed, so changes to them invalidate the stamp.
  - Tracked inputs under the project root are compared by content hash. That includes a filelist source that is a symlink into a tree outside the root.
  - Inputs outside the root are compared by size and mtime: the toolchain's includes and binary, any single input above 64 MB (`compile.hash_skipped_large` names those), and a reported dependency whose declared path lies outside the root.
  - A dependency is recorded by the path the build used, not its resolved target. A header symlinked in from a shared tree under a project-relative name is hashed like any other input, and retargeting the link invalidates the stamp.
- **VCS and Icarus** emit no dependency file, and their stamps record `deps: null`. The stamp instead lists every file in each `+incdir+` and `-y` directory the filelist names, compared content-first.
  - Editing, adding or removing a file there rebuilds.
  - The listing over-approximates on purpose: editing a header nothing includes still rebuilds. Over-invalidating costs a recompile, while under-invalidating reports a stale binary as green.

For Verilator the same directory listing is also kept, but it is compared by file name only. The dependency file already decides the content of every input the build opened. The names catch what it cannot see: a file that appears or vanishes, which for `-y` changes the next elaboration's module resolution. See [Shared-build include-directory listings](#shared-build-include-directory-listings).

## Shared-build include-directory listings

For VCS and Icarus, and as a name-only check for Verilator, the build stamp lists the files in every `+incdir+` and `-y` directory. The listing is rebuilt on every stamp validation.

- A `+incdir+` directory is walked recursively, because `` `include "nested/deep.svh" `` resolves beneath it.
- A `-y` directory is listed flat, because library resolution maps a module name to a file in the directory itself.
- Neither is filtered by suffix, since `+libext+` can be set in `builder-opts.compile-time` and never reach `run.f`.
- The walk takes a fraction of a second for a few thousand files, but an `+incdir+` pointed at a large tree makes every reuse check walk it.

Some names are left out:

- **Directories:** dot-directories (`.git`, `.svn`), `__pycache__`, and rtl_buddy's own trees: the suite's `artefacts/`, `.shared-builds/` and any `obj_dir*`. A project directory genuinely named `artefacts` or `obj_dir*` under an include path is not tracked.
- **Files:** editor and VCS bookkeeping (`.DS_Store`, `.gitignore`, `*.swp`, `*~` and similar) and rtl_buddy's own outputs (`run.f`, `compile.log`, `test.log`, `test.err`, `test.randseed`, `coverage.dat`, `simv`, `rb-compile-stamp.json`, `result.json`, and a dispatched job's `result-*.json` and `rtl_buddy-*.log`).
- **Temp files of those outputs:** `<output>.tmp` and `<output>.<pid>.<random>.tmp`. The patterns are anchored to those names, so an input called `defs.tmp` is still tracked.
- **The suite's `rtl_buddy.log`,** excluded by path. An input with that name elsewhere is tracked.

Every other file is listed, dot-prefixed ones included, because `` `include ".config.svh" `` is legal. Generated headers under `artefacts/` are tracked: an edit to one rebuilds, and only rtl_buddy's own outputs beside them are skipped. This applies when the include directory is an artefact directory itself, for example a `preproc` hook writing headers into `artifact_dir` and the filelist carrying `+incdir+artefacts/<test>`.

The exclusions exist because rtl_buddy writes into artefact directories after the fingerprint is taken. A listing that contained those files could never validate, and every run would recompile. Pruning `artefacts/` covers an `+incdir+` that is an ancestor of the artefact tree, such as `+incdir+.` in a `tests.yaml`. A simulator's own scratch output is not excluded, so pointing an `+incdir+` at a directory a builder writes into still makes every run recompile.

## What shared-build tracking does not cover

A shared build is reused when its stamp validates. These inputs are not part of the stamp:

- A directory that cannot be read is recorded as untracked.
- A symlinked subdirectory under an `+incdir+` is not descended, which bounds the walk against link loops.
- A header reached by a path no `+incdir+` names is tracked only where a builder reports it. This includes an include resolved relative to the including file's directory, which VCS and Verilator try before the search list. For builders with no dependency file it stays untracked.
- Ambient environment variables and undeclared tool inputs are not tracked.

Force a compile with `--rebuild`. Under `--dispatch`, `--share-build` is implied, so dropping the flag changes nothing. `compile.build_dep_changed` names a reported dependency that moved, and `compile.build_source_changed` names a filelist entry that changed, or the file inside the directory where one is listed.

## Shared-build locking

Concurrent processes populating one shared directory are serialised by an advisory `flock` on `<shared directory>/.rb-build.lock`. A process that has to wait logs `compile.build_lock_wait` and repeats it every few minutes.

- On an NFS mount with `nolock`, `local_lock=flock` or `local_lock=all`, `flock` is process-local and succeeds. There is no warning, and the cross-node guarantee does not hold.
- The lock file lives inside the directory it guards. Delete a shared build tree between runs, never during one. An `rm -rf` that races a live run leaves the next process locking a fresh inode while the old holder still writes.
- Where the filesystem cannot lock at all, `compile.build_lock_unavailable` is logged once per directory and the compile proceeds unserialised.

Unshared builds have no lock and no build job. When no planned test can share a build, the run logs `dispatch.build_job_skipped`, and each simulation job compiles into its own per-test artefact directory. Within one run each directory has one writer. Two concurrent runs of such a suite write the same directories with nothing between them, so do not run one twice at once.

## Slurm serialises build jobs of the same suite

Under `--dispatch slurm`, a build job writes the suite's shared-build tree. It is named after the suite and submitted with `--dependency=singleton`, so a second run of the same suite waits until every earlier job of that name and owner has terminated. It then revalidates the shared build and reuses it if unchanged.
A split suite's verilate job carries the same clause under its own `rb-verilate-<hash>` name. An interrupted run's orphaned build job is waited on rather than raced.

`dispatch.build_job_deduped` names the job being waited on when the head's `squeue` probe can see it. The probe only supplies the message. A failed probe is not retried for the rest of the run.

The guarantee has bounds:

- The name covers the suite directory alone, not the planned tests, builder mode or compile keys. An unrelated run of the same suite makes the second job wait for a build it may then redo, which costs queue latency.
- `singleton` is per user, so two users building into one shared tree still meet at the advisory `flock` described in [Shared-build locking](#shared-build-locking).
- A `--dependency` of your own that uses the any-of separator `?` cannot be composed with, because Slurm allows one separator per expression. The dedup stands down and records `dispatch.build_dedup_unavailable` at DEBUG, leaving your gate unchanged.
- A gate exported as `SBATCH_DEPENDENCY` counts as yours. It is folded into the same composition when `sbatch-args` names no dependency.
- Where a site sets `DependencyParameters=disable_remote_singleton`, two invocations routed to different clusters of one federation that share this filesystem are not serialised. Pin a cluster with `-M`, or rely on the `flock`. A multi-cluster `sbatch-args` selection records this caveat once per run at DEBUG.
- The build job's `--job-name` and `--dependency` are emitted after your `sbatch-args`. You cannot rename a build job (simulation jobs are unaffected) or replace its dependency, only add to it.

## A pending build job is usually waiting as intended

A build job that stays `PENDING` after `dispatch.build_job_deduped` is normally waiting on an earlier job of the same suite. Inspect that job before cancelling anything:

- `squeue -j <ids> -O JobID,State,Reason` on the ids the warning names.
- `squeue --name=<job name>` to find them; `dispatch.build_submitted` records the name.
- `scontrol show job <id>` for the full record.

If the earlier job is `RUNNING`, or `PENDING` with an ordinary capacity reason (`Resources`, `Priority`), the wait ends when that build does. Cancelling it would discard the build this run is about to reuse and kill the simulation jobs gated on it.

Use `scancel` only on a predecessor that will not finish: held (`JobHeldUser`, `JobHeldAdmin`), unschedulable (`PartitionConfig`, `BadConstraints`), or an abandoned run you no longer want. Nothing times such a job out by default.

A re-run of an interrupted run finds its orphaned jobs from the run manifest and reports, cancels or adopts them according to `--orphans`. See [interrupted runs](concepts/dispatch.md#interrupted-runs-warn-cancel-adopt).

## The first run after upgrading recompiles every shared build

Build stamps hash content instead of comparing size and mtime, because a stat-only comparison can be answered from an NFS client's stale attribute cache and validate a build of a design that was never compiled. Stamps written by an older rtl_buddy carry no hashes or include-directory listings and cannot validate, so expect one rebuild per build directory after upgrading, and none afterwards.

- On a partially upgraded cluster, an old node rewrites a hashless stamp that a new node then rejects. Builds keep recompiling until every host runs the new version.
- During such a partial upgrade, a gated simulation job can judge a failed compile's inputs to have moved and retry it once, though it would fail the same way.
- Content decides from then on. Regenerating a file byte-for-byte does not rebuild, and any real edit does.
- `compile.build_reused` prints the reused directory and its stamp's age on the console, and the test's `compile.log` repeats it with the command a rebuild would run.
- Use `--rebuild` to compile regardless, rather than deleting `artefacts/.shared-builds/` by hand. If you delete a tree, do it between runs, never during one (see [Shared-build locking](#shared-build-locking)).

## Yosys-backed flows do not support whitespace in paths

Yosys script parsing is not shell parsing: whitespace splits tokens, `#` starts a comment, and the single quotes from `shlex.quote` do not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;` and `#`.

String-valued parameter overrides need SystemVerilog quotes inside the YAML scalar. Do not quote ordinary numeric values as strings.

## Static-lifetime functions corrupt the netlist under the slang frontend

A `function` or `task` declared without `automatic` outside a class has static lifetime, so all its call sites share one storage location per formal. yosys-slang models this literally. Two calls in one combinational process alias their arguments. Calls split across a combinational and a clocked process leave the shared net with conflicting drivers, which folds to `x` and can drop a register and everything downstream. Simulation is unaffected, so the defect can go unnoticed.

`rb synth` scans the filelist's sources, and the headers they `` `include ``, before Yosys runs:

- With `frontend: slang`, `static-functions: error` (the default) fails the run. The legacy `verilog` frontend inlines per call site, so it only warns.
- Add `automatic` to the declaration.
- Set `static-functions: warn` to stage a migration. `static_function_findings` then appears in the machine output of each affected run.
- Yosys `multiple conflicting drivers` warnings fail the run unless `conflicting-drivers: allow` is set. A legitimate tristate bus produces the same warning and is not counted.
- Both gates cover the Yosys elaboration stage, so they apply to the `yosys` and `openroad` backends alike.

A slang run can fail this way even when its subroutines have a single call site and its netlist is correct, because the scan reports the declaration, not the aliasing. The `error` default is deliberate: the failure it guards is a silently corrupted netlist with plausible area and timing. See [Synthesis](concepts/synthesis.md#gate-static-lifetime-subroutines).

## The static-lifetime scan is approximate

The scan is a tokenizer with a preprocessor that tracks only whether macros are defined. It can miss declarations and report spurious ones.

- It evaluates `` `ifdef `` against exactly the macros Yosys receives: the generated filelist's `+define+` entries, then the synth.yaml entry's `defines:` (which win on conflict), plus what the selected frontend predefines. `read_verilog` predefines `SYNTHESIS` and `YOSYS`; `read_slang` predefines `SYNTHESIS` and slang's built-ins, but not `YOSYS`.
- A bare `+define+X` is passed valueless and takes the frontend's meaning: an empty body under `read_verilog`, `1` under slang. Verilator and Icarus split the same way. Write `+define+X=1` when you mean a value.
- When `defines:` overrides a filelist entry, the run logs one `synth.filelist_defines_overridden` warning naming both values. Simulation then elaborates with the filelist's value and synthesis with the synth.yaml one.
- `` `undefineall `` follows the frontend in use. slang re-applies the command-line macros after clearing and `read_verilog` does not, so the same source can leave a guarded region compiled under one frontend and not the other.
- It misses declarations produced by macros (macro bodies are skipped at their `` `define ``), the contents of `-y` library directories, and headers whose `` `include `` cannot be resolved (logged at DEBUG).
- It can report spuriously because `` `if `` expressions are not evaluated and scope nesting is tracked by keyword pairing rather than parsed.

Add Verible's `explicit-function-lifetime` rule through `rb lint` and `cfg-verible` to cover testbench and non-synthesisable sources.

## read_verilog drops an interface instance's own port connections

Yosys's `read_verilog` cannot bind a SystemVerilog interface instance to the interface port of a child module. It derives a per-child `<child>$interfaces$<interface>` module whose ports are the interface's members, wires them in the parent through implicitly declared `<instance>.<member>` wires, warns ``Could not find interface instance for `<inst>' in `<module>'`` and exits 0.

The members survive, but the interface instance's own port connections do not. `bus_if b (.clk(clk));` leaves `\b.clk` undriven, and every flop clocked from it in the subtree loses its clock, with plausible area and timing numbers.

The same elaboration also emits `Identifier `\<inst>.<member>' is implicitly declared` for each member, and ``Range select [n:m] out of bounds on signal `\<inst>.<member>'`` where a child slices a member. Both come from a throwaway elaboration before the interface ports are derived. They are noise, not the defect, and are not gated.

- `unresolved-interfaces` gates the binding warning. `warn` (the default) logs one `synth.unresolved_interface` per instance, `error` fails the run and drops the netlist, and `allow` skips the scan.
- The default is not `error` because the fallback gives a correct netlist whenever the interface has no ports of its own, or none the subtree reads.
- `frontend: slang` binds the instance properly and emits neither the warning nor the disconnect. It cannot represent an interface port on the synthesis top itself, so a top with one still needs a flat-port wrapper.

See [Synthesis](concepts/synthesis.md#gate-unbound-interface-instances).

## Unknown synthesis overrides are ignored after a warning

`synth.yaml` `tool_overrides` uses snake_case keys such as `plugin_path` and `single_unit`, unlike the kebab-case names under `cfg-synth-tools.opts`. An unknown key logs `synth_tool_config.unknown_override` and the run uses the default. A non-mapping override block, or a non-boolean `single_unit` or `best_effort_hierarchy`, is fatal. See [Synthesis](concepts/synthesis.md).

## `rb phys module` reports no power for an RTL module

The two halves of the physical model use different names for `module`. The synthesis half holds RTL module names as Yosys' `stat` saw them. The power half holds the Liberty cell each leaf instance is an instance of, because a mapped netlist's leaves are cells.

`rb phys module u_cpu` therefore reports the RTL module's cell count and area with an empty instance list and no power, and flattening the design does not change that. The payload's `instance_join` states the reason and the console prints it.

To ask what a block burns, use its instance path: `rb phys instance u_cpu` sums the leaf rows under it. A name that exists in both namespaces reports both, marked by `namespaces` and a collision note, and the two are never added together. See [Physical Metrics](concepts/phys.md#what-the-module-join-can-answer).

## Phys pane and schematic selections cross only within one hierarchy

Clicking an instance in the `/phy` pane broadcasts a path rooted at the physical model's own top, and an inbound selection is resolved against the pane's own rows. Neither surface knows which design the other displays.

A `/sch` showing a testbench around the DUT, or a different design, receives a path that names no instance there and selects nothing. A selection broadcast from such a view fails to land in the pane the same way. Nothing reports it, because an unmatched path looks the same as a click on a row the other surface does not hold.

Open the schematic on the design the model was built from, meaning the synthesis `top:` and not a testbench that wraps it. The two surfaces then follow each other. See [Physical Metrics](concepts/phys.md#browse-the-model-in-the-hub).

## Graph-pane heat attributes a leaf to the nearest instance the graph knows

The `/gph` heat overlay rolls per-instance power up to the enclosing RTL module by resolving each model row's rootless path to the deepest instance node that contains it. The attribution is only as fine as the graph's design tier is complete.

- If the run's `top:` is a wrapper the graph was not built for, or the graph was narrowed with `rb graph build --model`, no elaboration is rooted at the model's top. No leaf power is attributable, and the pane says so in its status line and paints cells and area only. Borrowing a testbench export's paths would file the DUT's power under whatever wraps it.
- A row whose path runs through a level the graph does not carry is attributed to the deepest level it does, up to the design top. That over-attributes that module rather than dropping the row.
- A module's cells and area are its definition's, counted once, and its area already includes its submodules'. Its power is summed over every instantiation. The pane prints how many instantiations and leaf rows each figure covers.

`rb phys instance <path>` answers the same question against the hierarchy the model recorded. See [Design Knowledge Graph](concepts/graph.md#physical-heat-on-the-graph).

## Graph coverage source changes attribution

A merged LCOV `.info` attributes coverage by file, so every module declared in one file receives the same totals. For per-module attribution use the default `--coverage auto`, or model artifacts. Unresolved and re-anchored LCOV paths are reported in the summary.

`--coverage` accepts `auto`, `model`, `none` or a merged `.info` path. `--no-coverage` disables the join.

## A cocotb test over an opted-out model keeps a dangling DUT edge

`models.yaml` `graph: false` withdraws every config-tier edge into the model's hierarchy. The binding tier's cocotb hop `python_module --binds_to--> module:<toplevel>` is derived from the merged graph and still names the DUT. That id stays in the `merge.dangling` list of `graph-meta.json`, as it does under `--no-design`. Opt out only models that no cocotb test runs against, or give the model a `top:` instead.

## Compilation-unit bind requires the slang frontend

Yosys's native `verilog` frontend does not resolve a top-level `bind`, so no formal cells elaborate. rtl_buddy fails a property-based proof that would otherwise pass vacuously. Set `frontend: slang` and configure the yosys-slang plugin. Inline assertions do not need this guard. See [Formal Property Verification](concepts/fpv.md).

## Verify that `anyconst` elaborates

Some yosys-slang builds drop `(* anyconst *)` without producing a `$anyconst` cell. The signal then varies freely each cycle and can invalidate symbolic-index proofs. Check the elaborated design before relying on it:

```bash
yosys -p 'read_slang ...; prep -top dut; select -assert-min 1 t:$anyconst'
```

Use a behavioral reference model when portable data-integrity checking matters.

## FPV COI analysis is best-effort

A cone-of-influence Yosys failure logs `fpv coi_yosys_failed`, omits COI data, and does not fail a successful proof. If COI numbers disappear, read `artefacts/<name>/coi.log` and check `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`.

## A simulation-only mutation campaign ignores `top`

Only the FPV oracle elaborates a top. The simulation oracle runs the test suite's own testbenches. A `mut.yaml` that declares `top:` but sets no `verify.fpv_config` logs `mut_config.top_override_unused` at load and scores every mutant unchanged. Remove the field, or add the FPV oracle the top is meant to root. A campaign with an FPV oracle applies `top` to the baseline proof and every mutant proof. See [Mutation Testing](concepts/mut.md).

## FPGA bitstream generation relaxes two I/O DRCs

Before `write_bitstream`, rtl_buddy downgrades Vivado DRCs NSTD-1 and UCIO-1 so bring-up designs without a complete pinout can produce a bitstream. The earlier DRC report and the machine result keep the original severity. For real hardware, treat either violation as blocking and add the missing `IOSTANDARD` and `LOC` constraints.

## FPGA timing is optional unless gated

A completed routed run reports PASS even with negative slack. Read `timing_met`, `wns_ns` and `failing_paths` for closure work. Set `require-timing-met: true` in `fpga.yaml` to fail the run on a reported miss. An unsupported `timing_met: null` cannot trigger the gate.

## pywellen must stay within 0.25.x

`rb wave` annotations and `rb saif` read traces through pywellen's random-access Waveform API, which changes on every pre-1.0 minor release. The supported range is two-sided: `>=0.25.6,<0.26`. A forced out-of-range version fails at launch with `pywellen.api_missing`, naming the installed version and the supported range. Restore the supported dependency range.

## AXI profiling converts VCS VPD traces

`rb axi-profile run` converts `vcdplus.vpd` with `vpd2vcd`, trying `-full64` first and the legacy form second. Conversion details go to `artefacts/axi/<test>/vpd-convert.log`.

If `vcd2fst` is installed, the VCD becomes a cached `vcdplus.fst`. Otherwise rtl_buddy keeps and ingests the larger VCD with a warning. Cached files live beside the original VPD in the test artifact directory.

## `rb nvim-install` requires git and network access

The default install clones a pinned `rtl-buddy-nvim` revision. On an air-gapped system, provide a local checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>
```

The plugin pin must speak the hub protocol shipped by rtl_buddy. Maintainers update both together.

## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution. rtl_buddy invokes its `rtl-buddy-view` executable and imports `rtl_buddy_view`:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the canonical tool key `rtl-buddy-view`.

`rb graph build` needs viewer 0.4.0, while the other viewer-backed commands need 0.3.0. `rb tool-check` reports the viewer as `ok` from 0.3.0 and marks only `rb graph` as `outdated` below 0.4.0. `rb tool-check --required-for graph` exits non-zero in that state.

## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and uses its exit code for clean versus findings. A pipeline that reads only stdout sees nothing. Capture stderr, or use `rb lint`, which scans both streams.
