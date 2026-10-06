---
description: Current rtl_buddy limitations, surprising behavior, and required workarounds, grouped by workflow.
---

# Quirks & Known Issues

Use this page when behavior differs from what you expect. Each section states the effect and the action to take. Sections run in workflow order, from CDC and simulation through dispatch, synthesis, formal, FPGA and tools.

## XPM CDC macros require rtl-buddy-cdc 0.4 or later

rtl-buddy-cdc 0.3.x treats `xpm_cdc_*` instances as dual-clock blackboxes. It reports `CDC-BBX` and drops their crossings from the report and domain map. Waivers hide the finding but do not recover the crossings. Upgrade with `uv tool install -U rtl-buddy-cdc`.

## rtl-buddy-cdc cannot take a filelist `+incdir+`

rtl-buddy-cdc has no include-path option, so `rb cdc` and the hub's domain-map build cannot pass it `+incdir+` entries. The run warns `cdc.filelist_incdirs_unsupported` and names the directories. A header that resolves only through one of them fails with `Cannot find include file`. Write the `` `include `` path relative to the including file, or use the `vivado` cdc tool.

## Coverage totals are per elaboration by default

Verilator scores each coverage point once per module elaboration. If a suite builds the same RTL under different defines or parameters, a block can look short on coverage when it is fully covered once the copies are collapsed.

- `rb cov summary`, `--coverage-dir-summary` and merged totals report the per-elaboration figure.
- For the collapsed figure, read `source_totals`: the `run (source)` row of `rb cov summary`, `rb cov summary --by-source`, `--coverage-source-summary` on `test` and `regression`, or the `/cov` pane's `figures` picker.

See [Coverage](concepts/coverage.md#per-elaboration-vs-source-point-figures).

## Coverage uses the platform builder

Coverage collection and labels use the platform-selected builder, even when a suite or test selects another `builder:`. A mismatch can mislabel or misparse coverage. Pass `--builder <name>` for the run, or make that builder the platform default. See [YAML Formats](reference/yaml.md).

## Coverage merging runs in the submitting process without a Slurm backend

Under `--dispatch slurm` the coverage tail (merge, model build, LCOV exports and manifest) runs as one job sized by `cfg-dispatch.coverage` (see [Run the coverage tail as a job](concepts/dispatch.md#run-the-coverage-tail-as-a-job)). Without dispatch, and under `--dispatch local-parallel`, it runs in the process that invoked `rb`.

- A few hundred inputs can peak in the gigabytes. A memory cap on that host can kill the merge; coverage then reports `FAIL` and the command exits 1 (see [Read a failed merge](concepts/coverage.md#read-a-failed-merge)).
- The merge has no time limit unless `cfg-coverage` sets `merge-timeout`, so a hung merge hangs the run.

Without Slurm, run the coverage command on a host with the memory it needs.

## Verilator randomized runs may not reproduce

Verilator can behave differently for the same random seed. When reproducibility matters, use VCS with `-xlrm hier_inst_seed` and give instances stable explicit names. If VCS does not write `HierInstanceSeed.txt` in the simulation directory, rtl_buddy warns `sim.hier_seed_missing` and cannot record the seed. The verdict is unchanged.

## Tool-path fallback can select another installation

A configured tool directory wins only when it contains the requested executable. Otherwise rtl_buddy may use a matching executable on `PATH` and warns once per process. After changing `root_config.yaml`, `.rtl-buddy/.env` or the environment, restart long-running `rb hub` and `rb mcp` processes.

## Hook scripts are not normal standalone scripts

`sweep` and `preproc` hooks run with the invocation directory as CWD and `__name__ == "__rtl_buddy_hook__"`. Use the injected `suite_dir`, `artifact_dir` and `run_artifact_dir` paths, and do not put required logic behind `if __name__ == "__main__":`.

Python `print()` output is captured as `hook.stdout` events, but child-process output bypasses the capture, so capture it and print it. See [Plugins](concepts/plugins.md).

## `toplevel:` must name the testbench in a plain SystemVerilog test

For a plain SystemVerilog testbench, `toplevel:` becomes Verilator `--top-module`, VCS `-top` or Icarus `-s`. It must name the testbench, not the DUT. cocotb and SystemC testbenches need no review.

- A `toplevel:` naming the DUT compiles, then the simulation exits at once and the test ends `NA` with `no PASS/FAIL markers found in .../test.log; result is NA`. Nothing in that output names `toplevel:`. A DUT with unconnected interface ports fails with `%Error-UNSUPPORTED: Interfaced port on top level module` instead.
- Without `toplevel:`, the top is chosen from filelist order, not the testbench `name`.
- A top pinned in the builder's `compile-time` opts overrides `toplevel:`. When they differ, the run warns `compile.toplevel_conflict` and uses the pinned top.

## Narrow VCS access flags suppress cocotb defaults

For cocotb, rtl_buddy adds VPI access unless a configured compile option already starts with `-debug_access` or `+acc`. A narrower flag suppresses the full default and can block signal writes. Remove it, or configure sufficient access such as `-debug_access+all` and `+acc+rw`.

## VCS license waits pause the simulation timeout

While VCS prints its license-queue banner, rtl_buddy pauses `sim_timeout` for up to one hour, so a queued run can outlive its timeout. An unrecognized newer banner resumes the clock too early; a timeout next to license messages in `test.err` indicates this. Set the builder's `extra-sim-timeout` as a backstop.

## A timeout kill can leave `test.log` with an unflushed tail

When `sim_timeout` expires, the simulator can be killed before it flushes output, so `test.log` can end mid-line and its last bytes do not locate the stop. Follow the [timeout triage order](concepts/tests.md#triaging-sim-hit-timeout) before raising the limit.

## Deeply nested expressions can exhaust the elaboration stack on macOS

`rb elab` analyses on threads with the platform default stack: 512 KiB on macOS, 8 MiB on Linux. About 400 nested conditional expressions on macOS kill the worker with no diagnostic, and the run reports `elaboration worker did not produce a result`. `max_parse_depth` does not help. Elaborate such generated RTL on Linux, or reduce the nesting. See [Model Elaboration](concepts/elaboration.md).

## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail at once if another process on the same host holds it.

- A lock left by a dead process on this host is reclaimed by the next run with the warning `artifact_lock.reclaimed`. A lock from another host is never reclaimed; clear it by hand once that machine is idle.
- The lock does not coordinate different NFS hosts. Dispatched worker jobs skip it, so do not start another command against a tree with a dispatch run in flight.
- A filesystem that cannot `flock` fails with `cannot lock this artefact tree`.

Give concurrent runs of one suite a [`--run-tag`](concepts/execution-context.md#namespace-concurrent-runs) so each locks its own tree.

## `--run-tag` covers the test flows, not every reader

`--run-tag` is accepted by `rb test`, `rb randtest`, `rb regression`, the dispatch job commands and `rb graph results`. Other commands do not see tagged runs:

- `rb wave`, `rb cov`, `rb phys` and other flow commands read the flat `artefacts/<test>/`. Open a tagged trace by path: `artefacts/.runs/<tag>/<test>/dump.fst`.
- The hub, the MCP server and `rb graph query` read the untagged `artefacts/graph/results-overlay.json`. Publish a tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.
- A relative path in `compile-time` builder opts resolves one directory deeper under a tag. Write `${RTL_BUDDY_PROJECT_ROOT}/...`.
- Merged coverage is not namespaced: `--coverage-merge*` writes `<command_root>/cov_dir/` whatever the tag, so merge in only one concurrent run.

## Tool flows delete their previous outputs before running

`rb cdc`, `rb synth`, `rb fpga`, `rb pnr` and `rb power` remove the outputs they are about to write before invoking the tool, and `rb hub` clears the `view.json` and domain map it caches under `.rtl-buddy/cache/`. A failed rerun therefore leaves no output instead of the previous run's. Copy out anything you want to compare first.

- A failed `rb synth` leaves no netlist, so `rb pnr` and `rb power` tell you to run `rb synth` first. `rb fpga` without `--bitstream` removes a previously built `<top>.bit`.
- A rerun that fails early, on a filelist or config error, also leaves nothing. With the backend tool missing, only `rb synth` and `rb pnr` still clear.
- A previous output that cannot be deleted (permissions, or a directory where the file belongs) fails the run with a fatal error. Fix the permissions or remove it by hand.
- A `pnr` run failed by `gds-mode: strict` removes the GDS, PNG and report but keeps the routed DEF, netlist, SDC and ODB.

Runs of different commands with the same name share one artifact directory. Give an FPGA run and a power run different names; both write `power.rpt`.

## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time and array settings. `-j` or `cfg-dispatch.jobs` is the only limit, so size it for the heaviest test's memory. `SIGKILL` of the head can orphan `rb _test-job` children; find and stop them. See [Run on one host](concepts/dispatch.md#run-on-one-host).

## Oversized resource groups are split into several arrays

A resource group larger than the cluster's array limit is submitted as several arrays, and `max-jobs-per-array` throttles each one, so peak concurrency is the throttle times the number of slices. With several clusters selected, or no `scontrol` on the submit host, the limit is unknown and sbatch refuses an oversized group; set `cfg-dispatch.max-array-size`. See [Split large groups into several arrays](concepts/dispatch.md#split-large-groups-into-several-arrays).

## Quote dispatch time values

YAML 1.1 reads an unquoted `time: 4:00:00` as an integer, and rtl_buddy rejects it. Quote every `time` value in `resources:`, `compile:` and `modes:` blocks.

## An unknown key in a reservation block is ignored after a warning

An unknown key in a `resources:` or `compile:` block, in `cfg-dispatch`, or in its `coverage:`, `retry:` or `rightsize:` block is ignored. Each one logs the warning `config.unknown_key`, naming the file, the block and the nearest known key, so a typo such as `memory:` reserves nothing but is reported. A later major release will make an unknown key fatal. A `modes:` block already rejects unknown keys, but a release without `modes:` drops the whole block silently, so confirm a new mode's reservation once. See [YAML formats](reference/yaml.md#parallel-dispatch).

## Dispatch build jobs cover the whole suite

One build job compiles every build of a suite. `compile.parallel` runs that many builds at once, but only `cpus` is scaled for you: size `compile.mem` for the concurrent builds and `compile.time` for the longest batch. Each build job reserves `cpus` times `min(parallel, planned tests)`, so set `parallel` to the expected number of distinct builds. See [Compile several builds at once](concepts/dispatch.md#compile-several-builds-at-once) and [Split verilation from the C++ build](concepts/dispatch.md#split-verilation-from-the-c-build).

## Preprocessors run in every dispatched job

Under dispatch, `sweep` runs once on the head. `preproc` runs in each build job and again in every simulation job. Make `preproc` idempotent, write shared generated files atomically to `artifact_dir`, and write run-dependent files to `run_artifact_dir`.

With `compile.parallel` above 1, no config's `preproc` may modify an input that another config compiles. Configs with the same compile key share one build, and a hook that rewrites an input per config on one key makes every config after the first fail with `build_job.group_input_drift`.

## Simulation jobs reuse the build stamp

A simulation job whose build stamp does not validate, against a build the build job recorded as built, fails with `compile.build_stamp_rejected` instead of recompiling. The usual cause is a `preproc` that writes different bytes on the simulation node. `build_job.group_leader_unstamped` means the first config of a build wrote no stamp, so the others compiled again; fix the directory as for `compile.stamp_write_failed`. There is no per-test opt-out of the shared directory under `--dispatch`. See [Recover when a gated job cannot use the build](concepts/dispatch.md#recover-when-a-gated-job-cannot-use-the-build).

## Design compile errors under dispatch are CompileFail

A design compile error is `CompileFail`; infrastructure failures stay `DispatchFail`. Simulation jobs do not recompile a config the build job recorded as failed, and the row carries the build job's error and logs. Fix the design or the build reservation, not the simulation one.

## Slurm retry reuses artifact paths

A retry overwrites the first attempt's simulation capture and per-job rtl_buddy log. Only `slurm-<tag>-retry<N>.log` stays per attempt, so diagnose retries from the scheduler logs. `max-wait` applies to each attempt. A `--begin` in `sbatch-args` overrides the retry delay, so remove it when you use retry backoff.

## Slurm memory advice depends on accounting samples

rtl_buddy requests one-second task accounting unless `sbatch-args` sets `--acctg-freq`. When the longest run ends within the sampling interval, `MaxRSS` is unreliable and memory advice is suppressed. Reduction advice needs at least 25% savings and floors of five minutes and 128 MB, so a small reservation gets none.

## Generated `run.f` files are checkout-specific

rtl_buddy writes sources, `+incdir+` and `-y` directories as absolute paths. Do not commit or copy `run.f` between checkouts, and spell a checkout's path the same way every time, because path spelling affects compile keys.

- An include directory whose path contains `+` stays relative, because filelist parsers read `+incdir+a+b` as two directories. rtl_buddy warns `filelist.incdir_unrepresentable`. Remove the `+` from the path.
- A path containing whitespace is quoted, which Icarus's `-f` parser does not understand. Keep whitespace out of checkout paths.

## Mount paths must match across cluster nodes

On a cluster with different mount paths per node, a build stamp from one node may not validate on another. Outside `--dispatch` the result is a recompile. Under `--dispatch` the simulation job fails with `compile.build_stamp_rejected` naming the path that differed (see [Simulation jobs reuse the build stamp](#simulation-jobs-reuse-the-build-stamp)). Spell the project root the same way on every node. `shared-build-root` removes the dependence on the root's spelling, but paths outside it, such as the toolchain, must still match.

## Shared-build stamps track dependencies differently per simulator

A build stamp decides whether a shared build can be reused.

- **Verilator** tracks the headers, libraries, standard includes and binary it reports consuming. Inputs under the project root are compared by content hash; inputs outside it, and any single input above 64 MB, by size and mtime.
- **VCS and Icarus** report no dependencies, so their stamps list every file in each `+incdir+` and `-y` directory and compare content. Adding, removing or editing a file there rebuilds, even one nothing includes. Verilator also compares that listing by file name.

The listing skips dot-directories, `artefacts/`, `obj_dir*` and rtl_buddy's own outputs, so a project directory with one of those names under an include path is not tracked. An `+incdir+` on a large tree slows every reuse check.

Do not point `+incdir+` at a directory a simulator or tool writes into: its scratch files change the listing and every run recompiles.

Environment variables, undeclared tool inputs and symlinked subdirectories are not tracked. For VCS and Icarus, an include resolved relative to the including file is also untracked. Force a compile with `--rebuild` after editing any of these.

## Shared-build locking

An advisory `flock` on `<shared directory>/.rb-build.lock` serialises concurrent processes populating one shared build. A waiting process logs `compile.build_lock_wait` every few minutes.

- On an NFS mount with `nolock`, `local_lock=flock` or `local_lock=all`, the lock is process-local and succeeds without warning, so it does not protect across nodes.
- Delete a shared build tree between runs, never during one; the lock file lives inside it.
- Where the filesystem cannot lock, the run warns `compile.build_lock_unavailable` and compiles unserialised.

Unshared builds have no lock, so do not run such a suite twice at once.

## Slurm serialises build jobs of the same suite

Build jobs of one suite run one at a time per user, [job tag](concepts/dispatch.md#tag-job-names-for-one-caller) and cluster, so an unrelated run of the same suite under the same tag, or both untagged, waits for an earlier one (`dispatch.build_job_deduped`). `--run-tag` does not separate them. Two users, or two runs under different job tags, sharing a tree rely on the [flock](#shared-build-locking). A federation with `DependencyParameters=disable_remote_singleton` does not serialise across clusters; pin one with `-M`. See [Troubleshoot builds](concepts/dispatch.md#troubleshoot-builds) for a build job that stays `PENDING`.

## A different rtl_buddy version does not reuse shared builds

Build stamps written by another rtl_buddy version do not validate, so each build directory recompiles once, and repeatedly while hosts of one cluster run different versions. Use `--rebuild` to compile regardless, not a manual delete of `artefacts/.shared-builds/`.

## Yosys-backed flows do not support whitespace in paths

Yosys scripts split on whitespace and treat `#` as a comment, and quoting does not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;` and `#`. String-valued parameter overrides need SystemVerilog quotes inside the YAML scalar.

## Static-lifetime functions corrupt the netlist under the slang frontend

A `function` or `task` outside a class declared without `automatic` has one shared storage location per formal, and yosys-slang models this literally. Calls can alias their arguments or leave a net with conflicting drivers, which folds to `x` and can drop a register and everything downstream. Simulation is unaffected, so the defect can go unnoticed.

`rb synth` scans the filelist's sources and included headers before Yosys runs:

- With `frontend: slang`, `static-functions: error` (the default) fails the run. The `verilog` frontend only warns.
- Add `automatic` to the declaration, or set `static-functions: warn` to stage a migration.
- Yosys `multiple conflicting drivers` warnings fail the run unless `conflicting-drivers: allow` is set. Tristate buses are not counted.

The scan reports declarations, so a subroutine with one call site can still fail, and it misses declarations produced by macros or in `-y` directories. A synth.yaml `defines:` that overrides a filelist `+define+` warns `synth.filelist_defines_overridden`. See [Synthesis](concepts/synthesis.md#gate-static-lifetime-subroutines).

## read_verilog drops an interface instance's own port connections

Yosys's `read_verilog` cannot bind an interface instance to a child module's interface port. It warns ``Could not find interface instance for `<inst>' in `<module>'`` and exits 0, and the instance's own port connections are lost: `bus_if b (.clk(clk));` leaves `\b.clk` undriven, so every flop clocked from it loses its clock, with plausible area and timing numbers.

- `unresolved-interfaces` gates the warning. `warn` (the default) logs `synth.unresolved_interface` per instance, `error` fails the run, and `allow` skips the scan.
- `frontend: slang` binds the instance properly, but an interface port on the synthesis top itself needs a flat-port wrapper.

See [Synthesis](concepts/synthesis.md#gate-unbound-interface-instances).

## Unknown synthesis overrides are ignored after a warning

`synth.yaml` `tool_overrides` uses snake_case keys such as `plugin_path` and `single_unit`, unlike the kebab-case names under `cfg-synth-tools.opts`. An unknown key logs the warning `synth_tool_config.unknown_override` and the default is used. A non-mapping block, or a non-boolean `single_unit` or `best_effort_hierarchy`, is fatal. See [Synthesis](concepts/synthesis.md).

## Wide adders map as ripple chains unless `synth-args` has `-noabc`

Yosys `synth` runs its generic `abc` pass before the mapped-run ABC step, and that pass's script includes `dc2`, which rebuilds log-depth adders, negates and incrementers as ripple chains. The mapped-run default script omits `dc2`, but it cannot restore depth the earlier pass removed. Add `-noabc` to the effort's `synth-args` for timing-critical datapaths. See [Synthesis](concepts/synthesis.md#choose-the-mapped-run-abc-script).

## Prefix adders off the critical path ripple under `abc-script: default`

With the `default` mapped-run script, `&dch -f` choices and `&nf` area recovery under the module's one global required time rebuild Kogge-Stone and other `+/choices/` adders off the critical path as ripple chains. A run whose `synth-args` request a `+/choices/` map uses the `delay` preset unless `abc-script` is set. See [Synthesis](concepts/synthesis.md#keep-prefix-adders-log-depth-with-the-delay-preset).

## `rb phys module` reports no power for an RTL module

The physical model's synthesis half holds RTL module names and its power half holds the Liberty cell of each leaf instance, so `rb phys module u_cpu` reports cell count and area with no instances and no power. Use `rb phys instance u_cpu`, which sums the leaf rows under the instance path. See [Physical Metrics](concepts/phys.md#what-the-module-join-can-answer).

## Phys pane and schematic selections cross only within one hierarchy

Selections between the `/phy` pane and the `/sch` schematic are paths rooted at the physical model's top. A `/sch` showing a testbench around the DUT, or another design, selects nothing, with no message. Open the schematic on the synthesis `top:`. See [Physical Metrics](concepts/phys.md#browse-the-model-in-the-hub).

## Graph-pane heat attributes a leaf to the nearest instance the graph knows

The `/gph` heat overlay rolls per-instance power up to the enclosing RTL module using the graph's design tier, so it is only as fine as that tier is complete.

- If the run's `top:` is a wrapper the graph was not built for, or the graph was narrowed with `rb graph build --model`, no leaf power can be attributed. The pane paints cells and area only.
- A row whose path runs through a level the graph lacks goes to the deepest level it has, which over-attributes that module.
- A module's power is summed over every instantiation, while its cells and area are counted once.

See [Design Knowledge Graph](concepts/graph.md#physical-heat-on-the-graph).

## Graph coverage source changes attribution

A merged LCOV `.info` attributes coverage by file, so every module declared in one file gets the same totals. For per-module attribution use the default `--coverage auto`, or `--coverage model`.

## A cocotb test over an opted-out model keeps a dangling DUT edge

`graph: false` in `models.yaml` withdraws every config-tier edge into the model's hierarchy, but the cocotb edge to `module:<toplevel>` still names the DUT and appears in the `merge.dangling` list of `graph-meta.json`. Opt out only models that no cocotb test runs against, or give the model a `top:`.

## Compilation-unit bind requires the slang frontend

Yosys's native `verilog` frontend does not resolve a top-level `bind`, so no formal cells elaborate. rtl_buddy fails a property-based proof that would otherwise pass vacuously. Set `frontend: slang` and configure the yosys-slang plugin. Inline assertions do not need this. See [Formal Property Verification](concepts/fpv.md).

## Verify that `anyconst` elaborates

Some yosys-slang builds drop `(* anyconst *)` without producing a `$anyconst` cell. The signal then varies freely each cycle and can invalidate symbolic-index proofs. Check the elaborated design before relying on it:

```bash
yosys -p 'read_slang ...; prep -top dut; select -assert-min 1 t:$anyconst'
```

Use a behavioral reference model when portable data-integrity checking matters.

## FPV COI analysis is best-effort

A cone-of-influence Yosys failure logs `fpv coi_yosys_failed`, omits COI data, and does not fail a successful proof. If COI numbers disappear, read `artefacts/<name>/coi.log` and check `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`.

## A simulation-only mutation campaign ignores `top`

Only the FPV oracle elaborates a top; the simulation oracle runs the suite's own testbenches. A `mut.yaml` that declares `top:` without `verify.fpv_config` warns `mut_config.top_override_unused` and scores every mutant unchanged. Remove the field, or add the FPV oracle it is meant to root. See [Mutation Testing](concepts/mut.md).

## FPGA bitstream generation relaxes two I/O DRCs

Before `write_bitstream`, rtl_buddy downgrades Vivado DRCs NSTD-1 and UCIO-1 so designs without a complete pinout can produce a bitstream. For real hardware, treat either violation as blocking and add the missing `IOSTANDARD` and `LOC` constraints.

## FPGA timing is optional unless gated

A completed routed run reports PASS even with negative slack. Read `timing_met`, `wns_ns` and `failing_paths` for closure work, or set `require-timing-met: true` in `fpga.yaml` to fail the run on a miss. A `timing_met: null` cannot trigger the gate.

## pywellen must stay within 0.25.x

`rb wave` annotations and `rb saif` depend on pywellen's API, which changes on each pre-1.0 minor release. The supported range is `>=0.25.6,<0.26`. Outside it, the command fails with `pywellen.api_missing`, naming the installed version and the range. Install a version in the range.

## `rb nvim-install` requires git and network access

The default install clones a pinned `rtl-buddy-nvim` revision. On an air-gapped system, pass a local checkout with `rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>`. The pinned plugin must speak the hub protocol shipped by rtl_buddy.

## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the tool key `rtl-buddy-view`. `rb graph build` needs viewer 0.4.0; other viewer-backed commands need 0.3.0, and `rb tool-check` marks only `rb graph` as `outdated` below 0.4.0.

## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and signals findings through its exit code. A pipeline that reads only stdout sees nothing. Capture stderr, or use `rb lint`, which scans both streams.

## `rb release` keeps every name a clear file uses

Verible's obfuscator renames a spelling everywhere it appears, so a name used by any file that ships unobfuscated, the release testbench included, is kept in every obfuscated file too. A testbench local called `count` keeps every design signal called `count`. `rb release` refuses a testbench that names a design module, package or interface unless `testbench.allow-design-refs` lists it, and reports the rest in the internal manifest's `forced_clear_units`. Keep the release testbench to the preserved interface and a package published for it. See [Customer releases](concepts/release.md#which-names-are-kept).

## `rb release` cannot obfuscate token-pasted macro names

Verible renames the pieces of a token-pasted name (`` `define NXT(a) a``_nxt ``) separately, so the pasted result no longer matches its declaration, and a macro string quote is renamed while the string is not. `rb release` refuses both in any file it obfuscates. Rewrite the macro, exclude the file with `obfuscate: false` and a reason, or set `obfuscation.token-paste: preserve` to keep every name the paste can form.
