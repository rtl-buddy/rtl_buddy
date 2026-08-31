---
description: Current rtl_buddy limitations, surprising behavior, and required workarounds by workflow.
---

# Quirks & Known Issues

Use this page when behavior differs from the normal workflow. Each section states the effect and the action to take; resolved bugs belong in release notes or migrations, not here.

## XPM CDC macros require rtl-buddy-cdc 0.4 or later

rtl-buddy-cdc 0.3.x treats `xpm_cdc_*` instances as dual-clock blackboxes, reports `CDC-BBX`, and drops their crossings from the report and domain map. Upgrade with `uv tool install -U rtl-buddy-cdc`. Waivers can hide the 0.3.x finding but cannot recover crossings beyond the macro.

## Coverage uses the platform builder

Coverage collection and labels use the platform-selected builder, even when a suite or test selects another `builder:`. A mismatch can mislabel or misparse coverage. Use `--builder <name>` for the run or make that builder the platform default. See [YAML Formats](reference/yaml.md).

## Verilator randomized runs may not reproduce

Verilator can produce different behavior for the same random seed. Use VCS with `-xlrm hier_inst_seed` when reproducibility is required, and give instances stable explicit names.

With hierarchical seeding, VCS writes `HierInstanceSeed.txt` in the simulation directory. If it is missing, rtl_buddy logs `sim.hier_seed_missing` and cannot record the seed, but does not change the test verdict.

## Tool-path fallback can select another installation

Configured tool directories take precedence only when they contain the requested executable. Otherwise rtl_buddy may use a matching executable on `PATH` and logs a fallback warning.

Warnings for fallback paths and unresolved variables are emitted once per process. Restart long-running `rb hub` or `rb mcp` processes after changing `root_config.yaml`, `.rtl-buddy/.env`, or the environment if you need the warning to be evaluated again.

## Hook scripts are not normal standalone scripts

`sweep` and `preproc` execute with the invocation directory as CWD and with `__name__ == "__rtl_buddy_hook__"`. Use injected `suite_dir`, `artifact_dir`, and `run_artifact_dir` paths; do not put required hook logic behind `if __name__ == "__main__":`.

rtl_buddy captures Python-level `print()` output as `hook.stdout` events. The capture has no `.buffer` or file descriptor, and child-process output bypasses it. Capture child output explicitly and print the text you want logged. For a generator that can write only relative to CWD, temporarily change to `suite_dir` and restore the previous directory. See [Plugins](concepts/plugins.md).

## Compilation-unit bind requires the slang frontend

Yosys's native `verilog` frontend does not resolve a top-level `bind`, so no formal cells elaborate. rtl_buddy fails a property-based proof that would otherwise pass vacuously. Set `frontend: slang` and configure the yosys-slang plugin. Inline assertions do not need this guard. See [Formal Property Verification](concepts/fpv.md).

## Verify that `anyconst` elaborates

Some yosys-slang builds drop `(* anyconst *)` without producing a `$anyconst` cell. The signal then varies freely each cycle and can invalidate symbolic-index proofs. Check the elaborated design before relying on it:

```bash
yosys -p 'read_slang ...; prep -top dut; select -assert-min 1 t:$anyconst'
```

Use a behavioral reference model when portable data-integrity checking matters.

## Narrow VCS access flags suppress cocotb defaults

For cocotb, rtl_buddy adds VPI access unless any configured compile option already starts with `-debug_access` or `+acc`. A narrower configured flag therefore suppresses the full default and may prevent signal writes. Remove the narrow flag or configure sufficient access, such as `-debug_access+all` and `+acc+rw`.

## VCS license waits pause the simulation timeout

When VCS prints its license-queue banner, rtl_buddy pauses `sim_timeout` until simulator output resumes, for up to one hour. A queued run can therefore outlive its nominal timeout. If a newer VCS banner is not recognized, the clock may resume too early; a timeout beside license messages in `test.err` indicates this case. Use the builder's `extra-sim-timeout` as a backstop.

## AXI profiling converts VCS VPD traces

`rb axi-profile run` converts `vcdplus.vpd` with `vpd2vcd`, trying `-full64` first and the legacy form second. Conversion details go to `artefacts/axi/<test>/vpd-convert.log`.

If `vcd2fst` is installed, the VCD becomes a cached `vcdplus.fst`; otherwise rtl_buddy keeps and ingests the larger VCD with a warning. Cached files live beside the original VPD in the test artifact directory.

## A timeout kill can leave `test.log` with an unflushed tail

When `sim_timeout` expires, the simulator may be terminated before flushing output. `test.log` can end mid-line or at a power-of-two byte count, so its final bytes are not an exact stop location. Follow the [timeout triage order](concepts/tests.md#triaging-sim-hit-timeout) before raising the limit.

## pywellen must remain below 0.25

`rb wave` annotations and `rb saif` require pywellen's removed random-access API, so the supported range is `>=0.20,<0.25`. A forced newer version fails at launch with `pywellen.api_missing`; restore the supported dependency range.

## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail immediately on same-host contention. The file remains after release; kernel lock state, not file presence, determines whether the tree is locked.

The lock is intentionally coarse across command families and is not assumed to coordinate different NFS hosts. Dispatched worker jobs skip it because they write planned subdirectories, so do not start another command against a tree with a dispatch run in flight.

## Dispatch changes build behavior

`--dispatch` implies `--share-build` and rejects `--early-stop`. `cfg-dispatch.backend` defaults `regression` and `randtest`, but `rb test` remains local unless `--dispatch` is explicit. A one-seed `randtest` replay also stays local.

Shareable builders compile once per compile key. Builders that cannot share compile in their jobs; fanned-out tests still use a build job to serialize access to their compile directory. See [Parallel Dispatch](concepts/dispatch.md).

## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time, array-throttle, and right-sizing settings. `-j` or `cfg-dispatch.jobs` is the only limit, so size concurrency for the heaviest test's memory use.

`cfg-dispatch.compile.parallel` is the exception: it is not a reservation but concurrency the build job itself honours, and that job occupies one pool slot while fanning out inside it. The real ceiling on the host is therefore `jobs` multiplied by `compile.parallel`, and nothing clamps it. Size the two together.

Normal interruption terminates the worker process groups. `SIGKILL` of the head process cannot run cleanup and can orphan `rb _test-job` children; inspect and stop them after a hard CI timeout or `kill -9`.

## Quote dispatch time values

YAML 1.1 parses an unquoted value such as `time: 4:00:00` as an integer. rtl_buddy rejects it rather than submit a 10-day Slurm reservation. Use `time: "4:00:00"` or a quoted minute count everywhere `resources:` appears.

## Dispatch build jobs cover the whole suite

One suite build job compiles every unique compile key, and all simulation jobs wait for the complete build. `cfg-dispatch.compile.parallel` compiles that many distinct builds at once inside that job, so `compile.time` must cover the longest batch rather than the serial total, and `compile.mem` must cover that many concurrent elaborations because only `cpus` is scaled for you. Count `compile.start` events to estimate the work.

The build job's reservation is `compile.cpus` multiplied by `min(parallel, planned tests)`. The cap is planned tests, not distinct compile keys: the head cannot know the keys without writing filelists on the submit host, which is the build job's own work. A suite whose tests share compile keys therefore reserves CPUs for build slots that never run — twenty tests over three keys with `parallel: 8` reserves eight builds' worth of CPUs for three. Set `parallel` to the expected distinct-build count, not to the test count, and confirm it against the `(build job)` row of the reservation advice.

Under dispatch, `sweep` runs once on the head, while `preproc` runs in the build job and again in every simulation job. Make `preproc` idempotent. Write shared generated files atomically to `artifact_dir`; write run-dependent files to `run_artifact_dir`.

`cfg-dispatch.compile.parallel` above 1 adds a second requirement: no config's `preproc` may mutate an input another config compiles. The build job runs every hook before any builder starts, because a config's compile key is only knowable after its own hook ran, so a hook that regenerates a suite-level file overwrites it for configs that have already been fingerprinted. At the default `parallel: 1` the job still runs `preproc` and compile per config in turn, so a generator hook that owns one shared file per config is safe there. Simulation jobs make the same demand at any setting, each re-running `preproc` on its own node.

Simulation jobs rely on the build stamp to skip recompilation. The stamp records a content hash of every tracked input under the project root, so content decides: a preprocessor that regenerates a filelist source byte-for-byte no longer invalidates anything, while one that changes a source's content invalidates every stamp and can trigger concurrent compiles into one directory. `compile.prebuilt_stamp_invalid` identifies this case. Avoid changing shared inputs from a per-test preprocessor.

A design compile error is reported as `CompileFail`, not `DispatchFail`. Infrastructure failures remain `DispatchFail`. A build the build job recorded as failed with a builder exit code, on the same inputs the simulation job would compile, is not recompiled by its simulation jobs — the summary row carries the build job's error and logs; fix the design or the build reservation, not the simulation one. A simulation job recompiles only when the stamp is invalid without that evidence (stamp drift, a build-side setup failure, or inputs that changed since the failed build), at simulation size with its transcript in the run's own `compile.retry.log` (under `run-NNNN/` for a fanned-out test), so size the simulation reservation for compilation only when relying on that recovery path.

## Slurm retry reuses artifact paths

A retry overwrites the first attempt's simulation capture and per-job rtl_buddy log; only `slurm-<tag>-retry<N>.log` remains attempt-specific. Use scheduler logs and the head's `dispatch.result_missing` event when diagnosing retries.

`max-wait` applies to each attempt, not the whole run, and includes the requested backoff. A later `--begin` in `sbatch-args` overrides rtl_buddy's retry delay because Slurm uses the last duplicate option. Remove custom `--begin` when using retry backoff.

## Slurm memory advice depends on accounting samples

rtl_buddy requests one-second task accounting unless `sbatch-args` already sets `--acctg-freq`. Memory advice is suppressed when the longest run ends within the active sampling interval because `MaxRSS` is unreliable; time and CPU advice remain available.

Right-sizing suggestions also have fixed five-minute and 128 MB floors and require at least 25% savings. Very small reservations can therefore produce no reduction advice even when utilization is low.

## `rb nvim-install` requires git and network access

The default install clones a pinned `rtl-buddy-nvim` revision. For an air-gapped system, provide a local checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>
```

The plugin pin must speak the hub protocol shipped by rtl_buddy; maintainers update both together.

## Generated `run.f` files are checkout-specific

rtl_buddy writes explicit source entries as absolute paths so Verilator cannot resolve a relative source through an include or library directory in another checkout. `+incdir+` and `-y` search directories are written absolute for the same reason. A relative search directory is resolved by the builder rather than by rtl_buddy: `-f` entries are read relative to the builder's working directory, and even a builder started in `run.f`'s own directory disagrees whenever a symlink sits between that directory and the design, because a relative path collapses `..` textually while a process walks it physically. Do not commit or copy `run.f` between checkouts. Use one symlink spelling of a checkout consistently, because path spelling affects compile keys.

Two path spellings cannot be pinned. An include directory whose path contains `+` keeps its relative spelling, because filelist parsers read `+incdir+a+b` as two directories and quoting does not change that; `filelist.incdir_unrepresentable` names those entries, and they stay dependent on the builder's working directory until the `+` is out of the path. A path containing whitespace is quoted instead, which Verilator's `-f` parser understands; Icarus's does not, and VCS is unverified, so keep whitespace out of checkout paths for those simulators.

On a cluster with different mount paths per node, a stamp from one node may not validate on another. This causes a safe recompile, not compilation of the wrong source.

Because the compile key hashes the text of each `run.f` entry, the first run after upgrading to a release that changed how an entry is spelled recompiles each shared build once. An absolute spelling also makes the key independent of where in the tree the consuming suite sits, so two suites at different depths that share a model now share one build where a relative spelling gave them different keys.

## Shared-build dependency tracking varies by simulator

Verilator reports consumed headers, library files, its standard includes, and its binary, so changes invalidate the shared-build stamp. Tracked inputs under the project root are compared by content hash, including a filelist source that lives there as a symlink into a tree outside it. Inputs the root does not cover are compared by size and mtime instead of being hashed on every validation: the toolchain's own includes and binary, any single input above 64 MB (`compile.hash_skipped_large` names those), and a reported dependency that resolves outside the root, which is how a header symlinked in from a shared tree is recorded. What remains untracked is what a builder never reports: VCS and Icarus emit no dependency file, so a header reached only through `+incdir+` or `-y` is absent from the stamp, and hashing cannot cover an input nothing recorded. Editing such a header can still reuse a stale build.

Ambient environment variables and undeclared tool inputs are not tracked either. Force a compile with `--rebuild`; dropping `--share-build` does not force one under `--dispatch`, which implies it. `compile.build_dep_changed` explains detected invalidation.

Concurrent processes populating one shared directory are serialised by an advisory `flock` on `<shared directory>/.rb-build.lock`; a process that has to wait logs `compile.build_lock_wait` and repeats it every few minutes. Two bounds apply. On an NFS mount with `nolock`, `local_lock=flock`, or `local_lock=all`, `flock` is process-local and *succeeds*, so there is no warning and the cross-node guarantee silently does not hold. And the lock file lives inside the directory it guards, so delete a shared build tree between runs, never during one — an `rm -rf` that races a live run leaves the next process locking a fresh inode while the old holder still writes. Where the filesystem cannot lock at all, `compile.build_lock_unavailable` is logged once per directory and the compile proceeds unserialised.

## The first run after upgrading recompiles every shared build

Build stamps hash content rather than comparing size and mtime, because a stat-only comparison can be answered from an NFS client's stale attribute cache: edit a source on the submit host, revalidate on a compute node inside that node's attribute-cache window, and `stat` still describes the pre-edit file, so the stamp validates and the run reports a PASS for a design it never compiled. Reading content closes that, since close-to-open consistency revalidates on `open()`. Stamps written by an earlier rtl_buddy carry no hashes and cannot validate, so expect one rebuild per build directory after upgrading and none afterwards. That settles only once every host runs the new version: on a partially upgraded cluster — submit host upgraded, compute nodes not — an old node rewrites a hashless stamp that a new one then rejects, so builds keep recompiling until the upgrade reaches every node.

Content decides from then on: regenerating a file byte-for-byte does not rebuild, and any real edit does. Reuse is also visible — `compile.build_reused` names the reused directory and its stamp's age on the console, and the test's `compile.log` repeats it with the command a rebuild would run, so an absent `compile.log` is no longer the only hint. Use `--rebuild` to compile regardless rather than deleting `artefacts/.shared-builds/` by hand; if you do delete a tree, delete it between runs and not during one, for the locking reason above.

## A declared `toplevel` shifts the shared-build key once

`toplevel:` now reaches the SystemVerilog builders as Verilator `--top-module`, VCS `-top`, or Icarus `-s`, and the flag is part of the compile fingerprint because it decides which modules are elaborated and what the model is called. A testbench that already declared `toplevel:` therefore gets a new shared-build directory on the first run after upgrading, and compiles once more. Testbenches without a `toplevel:` keep their existing key: the top is not inferred from the testbench `name`, so those builds are byte-identical to before and still elect a top from filelist order. A top pinned in the builder's `compile-time` opts continues to win; when it names a different module than `toplevel:`, the run logs `compile.toplevel_conflict` and the configured top is used. Simulator families other than Verilator, VCS, and Icarus get no top flag at all — `compile.toplevel_family_unsupported` records that at debug level.

## Yosys-backed flows do not support whitespace in paths

Yosys script parsing is not shell parsing: whitespace splits tokens, `#` starts a comment, and single quotes from `shlex.quote` do not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;`, and `#`.

String-valued parameter overrides require SystemVerilog quotes inside the YAML scalar; ordinary numeric values must not be quoted as strings.

## Unknown synthesis overrides are ignored after a warning

`synth.yaml` `tool_overrides` uses snake_case keys such as `plugin_path` and `single_unit`, unlike the kebab-case names under `cfg-synth-tools.opts`. An unknown key logs `synth_tool_config.unknown_override` and the run uses the default. A non-mapping override block or non-boolean `single_unit` is fatal. See [Synthesis](concepts/synthesis.md).

## FPV COI analysis is best-effort

A cone-of-influence Yosys failure logs `fpv coi_yosys_failed`, omits COI data, and does not fail a successful proof. If COI numbers disappear, inspect `artefacts/<name>/coi.log` and verify `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`.

## FPGA bitstream generation relaxes two I/O DRCs

Before `write_bitstream`, rtl_buddy downgrades Vivado NSTD-1 and UCIO-1 so bring-up designs without a complete pinout can produce a bitstream. The earlier DRC report and machine result retain their original severity. Treat either violation as blocking for real hardware and add the missing `IOSTANDARD` and `LOC` constraints.

## FPGA timing is optional unless gated

A completed routed run reports PASS even with negative slack. Read `timing_met`, `wns_ns`, and `failing_paths` for closure work. Set `require-timing-met: true` in `fpga.yaml` to make a reported miss fail the run; an unsupported `timing_met: null` cannot trigger the gate.

## Graph coverage source changes attribution

A merged LCOV `.info` attributes coverage by file, so every module declared in one file receives the same totals. Use the default `--coverage auto` or model artifacts for per-module attribution. Unresolved and re-anchored LCOV paths are reported in the summary.

`--coverage` requires `auto`, `model`, `none`, or a merged `.info` path. `--no-coverage` disables the join.

## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable and imports `rtl_buddy_view`:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the canonical tool key `rtl-buddy-view`.

## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and uses its exit code for clean versus findings. A pipeline that reads only stdout sees nothing; capture stderr or use `rb lint`, which scans both streams.
