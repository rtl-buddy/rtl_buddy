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

## Tool flows delete their previous outputs before running

`rb cdc`, `rb synth`, `rb fpga`, `rb pnr`, and `rb power` remove the outputs they are about to write — reports, domain maps, synthesis netlists, DEF/ODB, GDS/PNG, bitstream — from the run's artifact directory before invoking the tool. `rb hub` does the same for the `view.json` and domain map it caches under `.rtl-buddy/cache/`, which outlive the build that filled them. An exit code cannot distinguish "produced nothing to report" from "crashed before writing" (rtl-buddy-cdc's exit 1 means rule violations were found), so a report left by an earlier run would otherwise be parsed and its counts reported as the current result. Clearing first makes an absent report absent, and the flow then says so and names its log.

A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced. That covers `rb cdc` (both backends — the open analyzer is not an rtl_buddy dependency, so a project can legitimately run without it), `rb fpga` and `rb power`. `rb synth` and `rb pnr` clear regardless, tool present or not, because their netlists and their DEF/ODB are resolved by path by a *later* command — a missing tool still means there is no fresh netlist, and `rb pnr` or `rb power` must not silently use the old one. A configuration error is never a skip: an unknown `platform:`, or a part a backend cannot build, is reported whether or not the tool is present, and clears the outputs on its way out.

The clearing happens early, not just before the tool starts: a rerun that fails on a filelist error, an unresolvable config, or any other check still leaves nothing behind. Outputs a later command consumes — the synthesis netlists `rb pnr` and `rb power` resolve, and pnr's DEF and ODB — are cleared as the very first thing the run does, ahead of even the tool-availability check, because a missing tool still means there is no fresh netlist for the downstream command to use. Outputs only read back within the same run are cleared just after that check instead.

Outputs named after the design's top — `<top>.bit`, `<design>.routed.odb` — are matched by suffix rather than by name, so editing a run's `model:` or `top:` does not strand the previous top's files in the same directory. Another command's durable outputs are never matched this way either: a run's `result.json`, its build cache `rb-compile-stamp.json`, the dispatch envelopes, and the sibling commands' reports — `cdc.json`, the CDC domain maps, `power.rpt`, the synthesis netlists — all survive. This matters because an artifact directory is keyed on a run's *name* and names need not be unique across commands: an `rb fpga` run, a CDC analysis and a simulation test called the same thing all share one directory.

Sharing a name is still worth avoiding, because clearing is not the only way two commands collide. An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names. An artifact directory belongs to one run, so everything carrying a suffix that flow writes is that flow's own output; nothing else in the directory is touched, and the scan never recurses into a workdir a tool owns.

A tool that writes an output and *then* fails counts as a failure like any other, and every flow removes what it wrote on the way out. Yosys writes its netlist before the trailing `stat` that can error; the CDC analyzers write their report before they finish; Vivado's FPGA flow writes all five reports before it reaches `write_bitstream`; OpenROAD writes `power.rpt` and the routed database before its script ends; KLayout can leave a zero-length GDS or a half-rendered PNG; and the hierarchy renderer can write a `view.json` before exiting non-zero or emitting a schema this rtl_buddy rejects. Those outputs are removed again, so a run that reports a failure has published nothing. A configuration error — an unknown `platform:`, a part a backend cannot build — clears them too; it is a failed run, not a skip.

Consequently a failed rerun leaves no output at all rather than the previous run's. This propagates: a failed `rb synth` leaves no netlist, so `rb pnr` and `rb power` report that you need to run `rb synth` first instead of consuming the previous netlist. An `rb fpga` run without `--bitstream` likewise removes a previously built `<top>.bit`. Copy an artifact you want to compare against out of `artefacts/<name>/` before rerunning. Logs are exempt: each flow truncates its own log, so a crashed run still has one to read.

An artifact that cannot be deleted — a permissions problem, or a directory where a file belongs — fails the run with a fatal error rather than letting it proceed, since continuing would risk reporting the previous run's numbers.

## Dispatch changes build behavior

`--dispatch` implies `--share-build` and rejects `--early-stop`. `cfg-dispatch.backend` defaults `regression` and `randtest`, but `rb test` remains local unless `--dispatch` is explicit. A one-seed `randtest` replay also stays local.

Shareable builders compile once per compile key. Builders that cannot share compile in their jobs; fanned-out tests still use a build job to serialize access to their compile directory. See [Parallel Dispatch](concepts/dispatch.md).

## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time, array-throttle, array-size, and right-sizing settings. `max-jobs-per-array` and `max-array-size` describe Slurm job arrays, of which this backend submits none, so neither throttles nor splits anything here. `-j` or `cfg-dispatch.jobs` is the only limit, so size concurrency for the heaviest test's memory use.

`cfg-dispatch.compile.parallel` is the exception: it is not a reservation but concurrency the build job itself honours, and that job occupies one pool slot while fanning out inside it. The real ceiling on the host is therefore `jobs` multiplied by `compile.parallel`, and nothing clamps it. Size the two together.

Normal interruption terminates the worker process groups. `SIGKILL` of the head process cannot run cleanup and can orphan `rb _test-job` children; inspect and stop them after a hard CI timeout or `kill -9`.

## Oversized resource groups are split into several arrays

A resource group larger than the cluster's Slurm `MaxArraySize` cannot be one job array. rtl_buddy submits it as several, each holding at most `MaxArraySize - 1` elements — or fewer where the cluster sets `SchedulerParameters=max_array_tasks`, an inclusive cap on the tasks in one array that `scontrol` reports separately and that the slice size takes the minimum with. Two consequences are worth planning for: `max-jobs-per-array` throttles each slice, so the group's peak concurrency is that throttle multiplied by the number of slices; and a slice's manifest and element logs live under `slice-N/` in the run's dispatch directory instead of directly in it. A group that fits in a single array keeps the flat layout.

The limit is read from `scontrol show config` once per cluster per run, before the run's first array submit, with the `-M` of any cross-cluster `sbatch-args` so the answer describes the cluster the arrays are submitted to. `SBATCH_CLUSTERS` selects a cluster the same way, with `sbatch-args` winning. A selection naming several clusters — `--clusters=a,b`, or the reserved `all` — leaves the limit unknown, since Slurm chooses between them at submit; pin `cfg-dispatch.max-array-size` for those. Where the submit host cannot run `scontrol`, nothing is split and sbatch refuses an oversized group with `Invalid job array specification`; set `cfg-dispatch.max-array-size` to the cluster's value, and `cfg-dispatch.max-array-tasks` too where the cluster caps tasks-per-array below it — each ceiling is configured, and layered over the probe, on its own, and either one alone is enough to split a group. `dispatch.max_array_size_unknown` in the run log names that case, and the submit failure itself repeats the hint. A resolved limit is recorded at debug level as `dispatch.max_array_size`, whose `source` field reads `config` or `scontrol`.

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

Verilator reports consumed headers, library files, its standard includes, and its binary, so changes invalidate the shared-build stamp. Tracked inputs under the project root are compared by content hash, including a filelist source that lives there as a symlink into a tree outside it. Inputs the root does not cover are compared by size and mtime instead of being hashed on every validation: the toolchain's own includes and binary, any single input above 64 MB (`compile.hash_skipped_large` names those), and a reported dependency that resolves outside the root, which is how a header symlinked in from a shared tree is recorded.

VCS and Icarus emit no dependency file, and their stamps still record `deps: null`. Include directories are covered for them — and for Verilator — by the stamp's own listing of every `+incdir+` and `-y` directory the filelist names: one entry per regular file, compared the same content-first way. Editing, adding, or removing a file in one of those directories therefore rebuilds on every builder. The listing is also the only thing that notices a file *appearing* in a `-y` library directory, which no dependency file can report: `-y` resolves by module name on demand, so a file that changes the next elaboration was opened by nobody during the last one. It over-approximates on purpose — editing a header nothing includes rebuilds a build that did not strictly need it — because over-invalidating costs a recompile while under-invalidating reports a stale binary as green.

An `+incdir+` is walked **recursively**, because `` `include "nested/deep.svh" `` resolves beneath the directory; a `-y` directory is listed **flat**, because library resolution maps a module name to a file in the directory itself. Neither is filtered by suffix: `+libext+` can be set in `builder-opts.compile-time` and never reach `run.f`, so a filter derived from `run.f` would silently miss the library file that appears with any other suffix. The cost of the walk is a fraction of a second for a few thousand files, but it is paid on every stamp validation, so pointing an `+incdir+` at a large tree makes every reuse check walk it.

Two kinds of name are left out of the walk. **Directories**: dot-directories (`.git`, `.svn`) and RTL Buddy's own managed trees — the suite's `artefacts/`, `.shared-builds/`, and any `obj_dir*`. A project directory genuinely named `artefacts` or `obj_dir*` under an include path is therefore not tracked. **Files**: a denylist of editor and VCS bookkeeping (`.DS_Store`, `.gitignore`, `.gitattributes`, `.gitkeep`, `*.swp`, `*.swo`, `*~`, `.#*`, `#*#`) plus RTL Buddy's own per-test outputs (`run.f`, `compile.log`, `compile.retry.log`, `test.log`, `test.err`, `test.randseed`, `coverage.dat`, `simv`, `simv.vvp`, `rb-compile-stamp.json`, `result.json`, and a dispatched job's `result-*.json` / `rtl_buddy-*.log`). Every other file is listed, dot-prefixed ones included, because `` `include ".config.svh" `` is legal and resolves.

Both exclusions exist for one failure. Everything RTL Buddy writes into an artefact directory is written *after* the fingerprint that would list it, so a listing containing any of it could never validate again: every later run saw a different listing and recompiled, and under `--dispatch` that is every gated simulation job. Pruning the directory covers an `+incdir+` that is an *ancestor* of the artefact tree (`+incdir+.` in a `tests.yaml`, `+incdir+..` from a design directory holding verif suites). Excluding the output names covers an include root that **is** an artefact directory: a `preproc` hook may generate headers into its `artifact_dir`, and the filelist then carries `+incdir+artefacts/<test>` with no `artefacts` component left for the walk to see. **Generated headers under `artefacts/` are tracked** — they are real compile inputs and an edit to one rebuilds; only RTL Buddy's own outputs beside them are skipped.

A simulator's own scratch output in the same directory is not excluded, because its spelling belongs to the tool rather than to RTL Buddy. Pointing an `+incdir+` at a directory a builder writes into is therefore still a way to make every run recompile.

What the listing does not cover: a directory that cannot be read is recorded as untracked, as it was before; a symlinked subdirectory under an `+incdir+` is not descended, which bounds the walk against a link loop; and a header reached by any path no `+incdir+` names is tracked only where a builder reports it — including the common case of an include resolved relative to the including file's own directory, which VCS and Verilator try before the search list, and which therefore stays untracked for the builders with no dependency file.

Ambient environment variables and undeclared tool inputs are not tracked either. Force a compile with `--rebuild`; dropping `--share-build` does not force one under `--dispatch`, which implies it. `compile.build_dep_changed` names a reported dependency that moved and `compile.build_source_changed` a filelist entry, naming the file inside the directory where one is listed.

Concurrent processes populating one shared directory are serialised by an advisory `flock` on `<shared directory>/.rb-build.lock`; a process that has to wait logs `compile.build_lock_wait` and repeats it every few minutes. Two bounds apply. On an NFS mount with `nolock`, `local_lock=flock`, or `local_lock=all`, `flock` is process-local and *succeeds*, so there is no warning and the cross-node guarantee silently does not hold. And the lock file lives inside the directory it guards, so delete a shared build tree between runs, never during one — an `rm -rf` that races a live run leaves the next process locking a fresh inode while the old holder still writes. Where the filesystem cannot lock at all, `compile.build_lock_unavailable` is logged once per directory and the compile proceeds unserialised.

Under `--dispatch slurm` a second run of the same suite does not create that contention in the first place: each build job is named after the suite whose shared-build tree it writes and is submitted with `--dependency=singleton`, so Slurm itself defers it until every earlier job of that name and owner has terminated. An interrupted run's orphaned build job is therefore waited on rather than raced; the waiting job then revalidates the shared build and reuses it if the inputs are unchanged. `dispatch.build_job_deduped` names the job being waited on when the head's `squeue` probe can see it — that probe only supplies the message, the guarantee does not need it, and a probe that fails is not retried for the rest of the run. Four bounds. The name covers the suite directory alone — not the planned tests, the builder mode, or the compile keys, all of which can differ between two runs that still write one `obj_dir_<key>` — so an unrelated run of the same suite makes the second job wait for a build it then rebuilds, costing queue latency. `singleton` is per user, so two *users* building into one shared tree still meet at the lock. And a `--dependency` of your own that uses the any-of separator `?` cannot be composed with (Slurm permits one separator per expression), so the dedup stands down there and records `dispatch.build_dedup_unavailable` at DEBUG — your gate is left exactly as it was. `singleton` is also per cluster where a site sets `DependencyParameters=disable_remote_singleton`: a federation normally resolves it across its clusters, but with that option two invocations routed to different clusters of one federation that shares this filesystem are not serialised against each other — pin a cluster with `-M`, or rely on the `flock` above. A multi-cluster `sbatch-args` selection records that caveat once per run at DEBUG. A gate exported as `SBATCH_DEPENDENCY` counts as yours too: it is folded into the same composition when `sbatch-args` names no dependency of its own, so the build job's `--dependency` never silently replaces it. Two `sbatch-args` flags do not reach the build job for this reason: its `--job-name` and `--dependency` are emitted after your arguments, so you cannot rename a build job (simulation jobs are unaffected) and cannot replace its dependency, only add to it.

A build job that stays `PENDING` after `dispatch.build_job_deduped` is usually waiting exactly as intended, so inspect the job ahead of it before cancelling anything: `squeue -j <ids> -O JobID,State,Reason` on the ids that warning names, or `squeue --name=<job name>` to find them (`dispatch.build_submitted` records the name), or `scontrol show job <id>`. `RUNNING`, or `PENDING` with an ordinary capacity reason (`Resources`, `Priority`), means the wait ends when that build does — cancelling it would discard the build this run is about to reuse and kill the simulation jobs gated on it. Only a predecessor that will not finish deserves `scancel`: held (`JobHeldUser`, `JobHeldAdmin`), unschedulable (`PartitionConfig`, `BadConstraints`), or an abandoned run you no longer want. Nothing times such a job out by default.

Unshared builds are covered by neither mechanism. A suite where no planned test can share a build gets no build job at all (`dispatch.build_job_skipped`) and compiles inside each simulation job, into per-test artefact directories that take no build lock. Within one run those have a single writer, but two concurrent runs of such a suite write the same directories with nothing between them: do not run one twice at once. Interrupting a run also still leaves its jobs on the cluster and a re-run does not attach to them — the dedup makes the re-run's build job wait for the orphaned build, but the orphan's simulation jobs run to completion on their own.

## The first run after upgrading recompiles every shared build

Build stamps hash content rather than comparing size and mtime, because a stat-only comparison can be answered from an NFS client's stale attribute cache: edit a source on the submit host, revalidate on a compute node inside that node's attribute-cache window, and `stat` still describes the pre-edit file, so the stamp validates and the run reports a PASS for a design it never compiled. Reading content closes that, since close-to-open consistency revalidates on `open()`. Stamps written by an earlier rtl_buddy carry no hashes, and ones written before include directories were listed are silent where a listing is now expected; neither can validate, so expect one rebuild per build directory after upgrading and none afterwards. A directory listing also changes the input digest a dispatched build job records beside a failed compile, so during a partial upgrade a gated simulation job can judge that compile's inputs to have moved and retry it once, even though it would fail the same way. That settles only once every host runs the new version: on a partially upgraded cluster — submit host upgraded, compute nodes not — an old node rewrites a hashless stamp that a new one then rejects, so builds keep recompiling until the upgrade reaches every node.

Content decides from then on: regenerating a file byte-for-byte does not rebuild, and any real edit does. Reuse is also visible — `compile.build_reused` names the reused directory and its stamp's age on the console, and the test's `compile.log` repeats it with the command a rebuild would run, so an absent `compile.log` is no longer the only hint. Use `--rebuild` to compile regardless rather than deleting `artefacts/.shared-builds/` by hand; if you do delete a tree, delete it between runs and not during one, for the locking reason above.

## Check every `toplevel:` before upgrading: it now roots the compile

`toplevel:` used to be inert for a plain SystemVerilog testbench — only the SystemC path, the cocotb path, and `rb graph` / `rb hier` read it, and the schema described it as the "top-level DUT module name". It now reaches the builder as Verilator `--top-module`, VCS `-top`, or Icarus `-s`, so it decides what the simulator elaborates.

A project that took the old wording literally and pointed `toplevel:` at the **DUT** rather than at the testbench therefore compiles a different design after upgrading, and it does so quietly. The observable symptom is a test that used to pass turning into `NA`: the compile succeeds, the simulation runs and exits immediately because the elaborated root contains no `initial` block, and the run ends with `no PASS/FAIL markers found in .../test.log; result is NA` and `test result unknown` in the summary. Nothing in that output names `toplevel:`, so it is worth checking up front. Before upgrading, review every `toplevel:` on a plain SystemVerilog testbench and confirm it names the module the simulator should elaborate — for an SV bench that is the bench itself, not the DUT it instantiates. A DUT with unconnected interface ports fails loudly instead (`%Error-UNSUPPORTED: Interfaced port on top level module`), which is the same mistake with a better error. cocotb and SystemC testbenches need no review: for those, `toplevel:` was already the elaboration root.

Declaring `toplevel:` also shifts the shared-build key once. The flag is part of the compile fingerprint, because it decides which modules are elaborated and what the model is called, so a testbench that gains a top flag gets a new shared-build directory on the first run after upgrading and compiles once more. That is every plain SystemVerilog testbench with a `toplevel:`, plus cocotb on Verilator and Icarus. SystemC and cocotb-on-VCS keys are unchanged: those paths already passed their own top flag, so the plumbing recognises it and adds nothing. Testbenches without a `toplevel:` are unchanged too — the top is not inferred from the testbench `name`, so those builds still elect a top from filelist order and hash exactly as before.

A top pinned in the builder's `compile-time` opts continues to win, in every spelling the family accepts: Verilator takes `--top-module`, `-top-module`, `--top`, and `-top`, and Icarus accepts the module glued to the flag (`-stb`). When the configured top names a different module than `toplevel:`, the run logs `compile.toplevel_conflict` once — naming both tops — and the configured top is used. That holds for SystemC and cocotb testbenches too: those backends generate their own top flag only when the builder pins none, so a configured top is never silently overridden by the generated one. Simulator families other than Verilator, VCS, and Icarus get no top flag at all; `compile.toplevel_family_unsupported` records that at debug level.

## Yosys-backed flows do not support whitespace in paths

Yosys script parsing is not shell parsing: whitespace splits tokens, `#` starts a comment, and single quotes from `shlex.quote` do not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;`, and `#`.

String-valued parameter overrides require SystemVerilog quotes inside the YAML scalar; ordinary numeric values must not be quoted as strings.

## Static-lifetime functions corrupt the netlist under the slang frontend

A `function` or `task` declared without `automatic` outside a class has static lifetime, so all of its call sites share one storage location per formal. yosys-slang models this literally: two calls in one combinational process alias their arguments, and calls split across a combinational and a clocked process leave the shared net with conflicting drivers, which folds to `x` and can drop a register and everything downstream. Simulation is unaffected, so the design can carry the defect indefinitely.

`rb synth` scans the filelist's sources, and the headers they `` `include ``, before Yosys runs, and fails with `frontend: slang` (`static-functions: error`) or warns with the legacy `verilog` frontend, which inlines per call site. Add `automatic` to the declaration. Yosys `multiple conflicting drivers` warnings fail the run unless `conflicting-drivers: allow` is set; a legitimate tristate bus produces the same warning and is not counted. Both gates cover the Yosys elaboration stage, so they apply to the `yosys` and `openroad` backends alike.

**A slang run that passed before can now fail**, including one whose subroutines have a single call site and whose netlist happens to be correct: the scan reports the declaration, not the aliasing. The `error` default is deliberate — the failure it guards is a silently corrupted netlist with plausible area and timing. Set `static-functions: warn` to stage the migration; `static_function_findings` then appears in the machine output of each affected run.

The scan is a tokenizer with a definedness-only preprocessor, and it is imperfect in both directions. It misses declarations produced by macros (macro bodies are skipped at their `` `define ``), the contents of `-y` library directories, and headers whose `` `include `` cannot be resolved (logged at DEBUG). It can also report spuriously, because `` `if `` expressions are not evaluated and scope nesting is tracked by keyword pairing rather than parsed. Add Verible's `explicit-function-lifetime` rule through `rb lint` and `cfg-verible` to cover testbench and non-synthesisable sources. See [Synthesis](concepts/synthesis.md#gate-static-lifetime-subroutines).

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

## A cocotb test over an opted-out model keeps a dangling DUT edge

`models.yaml` `graph: false` withdraws every config-tier edge into the model's hierarchy, but the binding tier's cocotb hop `python_module --binds_to--> module:<toplevel>` is derived from the merged graph and still names the DUT. That id stays in `graph-meta.json`'s `merge.dangling` list, exactly as it does under `--no-design`. Opt out only models that no cocotb test runs against, or give the model a `top:` instead.

## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable and imports `rtl_buddy_view`:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the canonical tool key `rtl-buddy-view`.

## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and uses its exit code for clean versus findings. A pipeline that reads only stdout sees nothing; capture stderr or use `rb lint`, which scans both streams.
