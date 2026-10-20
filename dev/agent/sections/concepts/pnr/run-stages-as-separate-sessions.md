## Run stages as separate sessions

Set `stages: true` on a run to run each stage in its own OpenROAD session, one after another:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    stages: true
```

The stages are `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair` with [`post-route-repair`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-detailed-route), and `finish` (fill, parasitic extraction, the `before-reports` hooks, the final reports and the outputs). Each session but `finish` ends by writing its stage's [checkpoint](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints), which the next session reads, so a staged run writes every checkpoint. `stages: true` with a `checkpoints:` list that leaves out a stage other than `post_route_repair` is a config error.

A staged run's checkpoint directory also holds each session's script and log, `<NN>_<stage>.tcl` and `<NN>_<stage>.log`, and with each checkpoint a `<NN>_<stage>.state.tcl` of the flow variables a later stage reads (the `floorplan.macros` cell halos). The run keeps one `progress.jsonl` and one `manifest.json`. `artefacts/<run>/pnr.log` is the sessions' logs in order, written as each session runs, and `pnr.tcl` is their scripts in order.

Each session after `floorplan` starts with a prelude rb generates from the same flow template, so a staged run and a one-session run of the same configuration run the same flow commands. Their results are identical at equal thread counts unless OpenROAD carries in-memory state over a stage boundary in the one-session run; see [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#a-staged-pr-run-can-differ-from-a-one-session-run). The prelude is:

1. The header variables, `threads:`, `platform-tcl`, the Liberty of every corner and every LEF, including the views of `blocks:`.
2. `read_db` and `read_sdc` of the previous stage's checkpoint, and its saved flow variables.
3. `max-fanout`, `layer-rc-tcl`, the don't-use cells and the `session` hooks.
4. The flow's design variables (`$block`, `$macros`) and, after `place`, the detailed-placement options a rotated macro needs.
5. `set_propagated_clock [all_clocks]` after `cts`, the routing layers and layer adjustment after `global_route`, for `detailed_route` the `global_route` checkpoint's segments (`read_global_route_segments`), and for `finish` a fanout check, without which OpenROAD crashes in the final reports' max-fanout count.

The detailed router reads the route guides the database keeps, so a staged run routes exactly as a one-session run does; `read_guides` would rewrite them without their via layers. The `finish` session also takes the global route from those guides, as `rb power` does from a routed database. A routed database's pin access points can put a pin in another global-routing cell than the route reached, so OpenROAD would refuse the saved segments there. On a PDK with `rcx-rules` the final reports are timed on the extracted SPEF.

A session has what the previous stage's checkpoint holds and what its prelude sets up again, nothing more:

- The database keeps the design with its global-connect rules, placement blockages, routing layer range and layer adjustments, `dont_touch` marks, track grids and route guides.
- The checkpoint SDC, written at the end of the stage after its hooks, keeps the timing constraints, including `set_max_fanout` and propagated clocks.
- The state file keeps the flow variables named above.

Anything else an earlier session set is gone: Tcl variables, procedures and `rename` wrappers, `set_wire_rc`, `set_layer_rc` and `set_dont_use` that a hook ran, STA settings outside the SDC, and the resizer's in-memory state. In every session but `finish`, rb compares the global Tcl commands and procedures before and after each hook but a `session` one. A hook that renamed, deleted or redefined a command that existed before it logs the warning `pnr.hook_commands_not_kept` naming them, since later sessions run the original. A hook that only defined new commands logs them at info as `pnr.hook_commands_added`: a later session that calls one fails with an unknown command. See [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#session-state-does-not-cross-a-pr-stage-boundary).

A session that fails, prints an OpenROAD `[ERROR ...]` line or exits before it ends its stage fails the run with its stage in the description, and no later stage runs. Its stage is recorded as not finished cleanly, so no resume can start after it. A checkpoint that cannot be written fails the run with `fail_stage: checkpoint`, and a session that exits with code 0 before its end-of-stage line fails it with `fail_stage: incomplete`; `xfail:` excuses neither.

### Stop after a stage

`--stop-after <stage>` ends a run, staged or not, once that stage's checkpoint is written:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --stop-after global_route
```

Any stage of the run but `finish` can be named, and `post_route_repair` only with `post-route-repair`. A [probe](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-without-timing-repair) stops after `global_route` unless an earlier stage is named. The run writes the stage's checkpoint even without `checkpoints:`, prints a stage report and then `>>> STOPPED after <stage>`. The report gives the design area, setup and hold worst slack and TNS (per corner on a multi-corner platform), timed on no parasitics after `floorplan`, the placement estimate after `place` and `cts`, the global-route estimate after `global_route` and `detailed_route`, and the extracted parasitics after `post_route_repair`.

The result is a `PASS` with `routed: false` and `stopped_after: <stage>`, with `area_um2`, the slack and TNS fields from the report, `global_route_overflow` (the total overflow in the last global-route usage table) from `global_route` on, and `drc_count` after `detailed_route`. A stopped run writes no routed DEF, ODB, SPEF or GDS, publishes no abstract for `harden: true`, and skips the [routed-floorplan check](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#check-the-routed-floorplan) and the stream-out. A run that exits with code 0 before printing its stop line fails with `fail_stage: incomplete`, and one that cannot write the stage's checkpoint fails with `fail_stage: checkpoint`. A run's `xfail:` marker does not apply to a stopped run, which is no verdict on the routed design: its `PASS` stays `PASS`, never `XPASS`, and a `FAIL` stays `FAIL`.

### Resume a staged run

`--resume <run> --from <stage>` starts a new staged run at `<stage>` from a staged run's checkpoint of the stage before it:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --stop-after global_route
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --resume latest --from detailed_route
```

`<run>` is `latest` or a run id under the run's `checkpoints/`, both looked up there first, or else a checkpoint run directory, which a relative path names from the directory `rb` was invoked in. The resumed run writes its own run directory and leaves the old one unchanged, so detailed routing can be retried several times from one global route. Its manifest records `resumed_from` (run id and stage) and lists the inherited stages' scripts, logs and checkpoints with the run that wrote them (`rb pnr-export --checkpoint <run-id>/<stage>` on the resumed run exports an inherited checkpoint from the run that wrote it, wherever that run is), and its `pnr.log` starts with the inherited stages' logs. A run resumed from `finish` puts the inherited route's `route.drc.rpt` and `route.maze.log` (the last post-route repair round's, when the run has that stage) back in the artefact directory, so its `drc_count` and DRC report are that route's. A resumed run is staged whatever its `stages:` setting, and can itself be resumed. Both options need a single named run, and `--stop-after` may be added.

The resume is refused, with `fail_stage: resume` and a description that names what differs, unless:

- the run named was a staged run, and its stage before `--from` finished cleanly with its checkpoint written and unchanged since;
- the script of every stage before `--from`, generated from today's configuration, is the one that run used, as compared below;
- every input file those stages read has the same content;
- OpenROAD reports the same version; and
- the run named was not a [probe](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-without-timing-repair), unless this run is one too.

The input files are those the [checkpoint manifest](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) fingerprints: the netlist, the SDC, the Liberty of every corner, the LEF, `pin-constraints`, the planner files, the `pdn-config`, the PDK's `rcx-rules` (read first by post-route repair, else by `finish`), the PDK Tcl hooks (`platform-tcl`, `layer-rc-tcl`, `tracks-tcl`, `tapcell-tcl`) and the run's `hooks:` files. A file that one of them sources or reads in turn, such as a file a hook, `platform-tcl`, a `pdn-config` or the SDC sources, is not checked, and neither are environment variables.

A stage script is compared up to its end-of-stage line, without:

- whole-line comments;
- the two lines that name run directories (`RB_RUN_DIR` and `RB_PRED_DIR`);
- the thread count (`set_thread_count`); and
- each `set NAME value` line of the header at the top of the script whose variable the rest of that stage's script does not read, as `$NAME`, `${NAME}`, `$::NAME` or `${::NAME}`, itself or through another header line it keeps.

Everything else is compared, including rb's own Tcl procedures, so an rtl_buddy version that generates a different script for an earlier stage refuses the resume. The scan sees only the stage script: a hook file, `platform-tcl` or other file that the stage sources and that reads a header variable does not make that variable part of the comparison.

So a setting that only `--from` and later stages read may change between the runs: `detailed-route-verbose:` or a `before-detailed-route` hook, the routing layers (first read by `global_route`), or the fill cells (read by `finish`). The thread count may differ between stages, so a resume can run on another CPU allocation than the run it resumes; a staged run equals a one-session run of the same configuration only at equal thread counts.

A resume is checked before anything is cleared or written. A refused resume, or one that fails setup before it is checked, leaves `checkpoints/latest` and the previous run's outputs as they were, so the same `--resume latest` can be retried once the configuration is put back.

### Dispatch the stages to Slurm

`--dispatch slurm` runs each stage of a staged run as its own Slurm job:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --dispatch slurm
```

rb writes every stage's script into the run directory first, so the jobs run the configuration as it was when the run started. It then submits one job per stage, each held until the one before it succeeds (`--dependency=afterok`), and waits until the last one has left the queue. Each job runs its stage as a local staged run does and adds to the same `pnr.log`, `progress.jsonl` and checkpoints, so the result, manifest and outputs are those of the same run staged locally. `--dispatch slurm` makes a run staged whatever its `stages:` setting. With `--stop-after` or `--resume` it submits only the stages the run runs. The `post_route_repair` job runs every [post-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-on-the-detailed-route) round. `--dispatch local`, the default, runs the stages where `rb pnr` runs; `cfg-dispatch.backend` does not apply to `rb pnr`.

Each job's reservation is resolved field by field, most specific first, from the run's `resources.stages.<stage>`, the run's `resources:`, `cfg-dispatch.resources`, then the built-in defaults:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: auto
    resources:
      cpus: 4
      mem: 8G
      time: "02:00:00"
      stages:
        detailed_route: {cpus: 16, mem: 32G, time: "12:00:00"}
```

`threads: auto` takes each job's CPUs, so here detailed routing runs on 16 threads and every other stage on 4. A fixed `threads:` count is the same in every stage, clamped to the CPUs of a smaller job. Each job counts the CPUs it received when it starts, as a run inside an allocation does (see [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads)), so a `--cpus-per-task` in `cfg-dispatch.sbatch-args` or a site that grants a different count sets the thread count too, for every session of the `post_route_repair` job as well. A run that is not dispatched ignores `resources:`. A dispatched run without a `resources:` block warns (`pnr.dispatch_resources_default`) with the CPUs, memory and time every job then gets.

Every job asks for one task (`--ntasks=1`); `cfg-dispatch.sbatch-args` come after the reservation and apply to every job. The jobs are named `rb:pnr:<run>:<stage>`, tagged by `RTL_BUDDY_JOB_TAG` (see [Tag job names for one caller](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#tag-job-names-for-one-caller)). The console shows each job id as Slurm accepts it, then the stage `rb pnr` waits on and its state.

`rb pnr` itself stays on the submitting host. It writes the scripts, submits the jobs and waits for them, then runs there every check a local run makes on the routed result. The stream-out for `--gds`, `--png` or `harden: true` also runs there: KLayout reads the whole routed layout, so give the submitting host, or the allocation `rb pnr` runs in, memory to match.

A stage whose job fails, is cancelled, runs out of time or is killed for memory fails the run, and Slurm removes the jobs after it. The description names the stage and how the job ended, for example `stage 05_detailed_route: OpenROAD exited with code -9 (Slurm job 1234 ended OUT_OF_MEMORY)`. A job killed inside its stage gets the stage's `stage_end` from Slurm's accounting, and the stage's log is cut from `pnr.log` when the job had not written it. A stage succeeds only when its job wrote a successful result and Slurm's accounting records the job `COMPLETED`, so a job that Slurm ended after its stage succeeded, for example when its `time` ran out as it exited, fails the run at that stage. When accounting gives no end state for a job, its result is taken as written, with the warning `pnr.stage_jobs_unaccounted`.

`cfg-dispatch.max-wait` bounds how long each job waits in the queue before it starts: the first from its submission, each later one from when the job before it ends. How long a job runs is bounded by its own `time`. A job that waits longer, or one that cannot be submitted, fails the run and cancels the jobs; `sbatch` gets 10 minutes to answer a submission before it is stopped and the submission fails. The wait counts while `squeue` does not answer, and each `squeue` of the wait gets at most 30 seconds, so one that hangs counts as not answering. A job past `max-wait` then is given up only when Slurm's accounting (`sacct`) records it as still pending; when accounting has no answer either, only once it has been queued longer than `max-wait` plus its own `time`, so a job that started while the queue could not be read is never cancelled as unstarted. A job that `squeue` shows pending on a dependency that can never be met is cancelled by the wait, and that `scancel` gets at most 30 seconds too; one that hangs leaves the job counted as queued, and a later poll cancels it again.

A failed stage carries its own `fail_stage` only when its job ran the stage and ended itself; it then fails as it would in a local staged run. Every other failure is the scheduler's and carries `fail_stage: dispatch`, which `xfail:` never excuses, as for an `rb test` job the scheduler lost:

| How the stage's job ended | `fail_stage` |
| --- | --- |
| Ran its stage, which failed, and ended `COMPLETED` or `FAILED` | The stage's own: none, `incomplete` or `checkpoint` |
| Ran its stage, which succeeded, and ended in any state but `COMPLETED` | `dispatch` |
| Ran its stage and ended in any other state: `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED`, `NODE_FAIL`, `PREEMPTED` | `dispatch` |
| Refused its stage, never started it, or left no result | `dispatch` |
| Could not be submitted, or waited in the queue longer than `max-wait` | `dispatch` |
| Refused because an earlier dispatched run's jobs are still queued (below) | `dispatch` |

Ctrl-C, SIGTERM or SIGHUP cancels the run's own jobs, and no others, including a job whose submission is still waiting on `sbatch`. `rb pnr` then waits up to 30 seconds for them to leave the queue, closes the stage a cancelled job was running as for a killed job, finishes the manifest and exits. A second Ctrl-C ends that wait early, in every run under `-j`; one that arrives while Slurm is asked to cancel takes effect once the request is done, and each `squeue` and `scancel` of the cancel gets at most 10 seconds. Under `-j`, every run being dispatched stops the same way and no further run starts. Jobs still queued after the wait are named with the `scancel` command that cancels them. Any other error before the chain has left the queue, such as a full disk, cancels the jobs and finishes the manifest the same way before rb reports it. Either way, `rb pnr` marks the run given up (`dispatch/given_up`) before it cancels the jobs, and clears the run's outputs after the wait. A job that starts after the mark refuses its stage, and one already running its stage removes the run's outputs again when the stage ends and fails it, so a job that outlives the cancel leaves no routed result that looks valid. A signal that arrives after the chain has left the queue takes effect once rb has read how the chain ended; the run then fails as interrupted.

An `rb pnr` killed outright (SIGKILL, or the end of the allocation it runs in) leaves its jobs to run the rest of the chain unattended: the checks it makes after the last job never run and its manifest is never finished. While a job of an earlier dispatched run of the same configuration is queued or running, every `rb pnr` of it, dispatched or not, refuses to start before it clears anything, naming the jobs and the `scancel` command. It looks up only your own jobs, and refuses too while `squeue` does not answer, since it cannot tell. On a host without `squeue`, a run that is not dispatched refuses while the earlier run's `dispatch/` has changed within the total `time` its jobs asked for; after that it warns (`pnr.stage_jobs_unchecked`) and runs. That age is no proof that the jobs have ended, since a job can wait in the queue for any time before it starts. A submission that `sbatch` has not answered yet, such as one that was running when its `rb pnr` was killed, has no job id to look up, so it refuses every `rb pnr` of the configuration, naming its `dispatch/<NN>_<stage>.sbatch.tmp` (or `.sbatch.cancel`), until it is 11 minutes old: `sbatch` is stopped after 10 minutes, so a submission with no answer for longer has failed, and `rb pnr` warns (`pnr.stage_submission_presumed_failed`) and runs. Once the jobs have ended, or the submission is known to have died, removing the earlier run's `checkpoints/<run id>/dispatch/` directory lets `rb pnr` run without looking them up; a refusal that could not confirm the jobs names the directory. A job refuses its stage when its run has already finished or been given up, or when it runs another rtl_buddy version than the `rb pnr` that started the run.

The run directory's `dispatch/` holds `jobs.json`, with each stage's job id, dependency and reservation and whether all have left the queue, `given_up` once `rb pnr` has given the run up, and for each stage Slurm's answer to the submission (`<NN>_<stage>.sbatch`, `<NN>_<stage>.sbatch.tmp` until `sbatch` answers), the job's Slurm output (`<NN>_<stage>.slurm.log`), its rtl_buddy log (`<NN>_<stage>.rtl_buddy.log`) and how it ended (`<NN>_<stage>.result.json`). A job whose CPUs give another thread count than the head wrote runs a copy of the stage script with that count, `<NN>_<stage>.tcl` there; the stage script in the run directory stays as the manifest records it. The manifest's `dispatch` entry records the backend and, for each stage, its reservation, `threads:` and the thread count that reservation gives. Each stage's `stage_begin` and `stage_end` record the count its job set (`thread_plan`), and the run's `openroad_threads` result is the last stage job's.

### Stage resources

Each stage's wall time, CPU time (user plus system) and peak resident memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest, with `measured_by` naming how:

- In a staged run rb measures each session as a process (`measured_by: process`) and also records its thread count (`threads`, the count OpenROAD reported, else the one `threads:` set). A [dispatched](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm) stage is measured the same way inside its job, and its `stage_begin` and `stage_end` name the job (`slurm_job`) and `host`. A job killed inside its stage gets a `stage_end` with Slurm's accounting (`measured_by: scheduler`, and the job's `slurm_state`).
- In a one-session run with `checkpoints:` or `--stop-after`, the flow measures each stage from inside OpenROAD (`measured_by: session`) at the stage boundaries of the [checkpoint table](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints): wall time from the Tcl clock, CPU time from `/proc/self/stat`, and peak memory from `VmHWM`, reset at each stage boundary through `/proc/self/clear_refs`. The reset starts from the memory the process already holds, so a stage's peak includes what earlier stages left resident (the `finish` stage after detailed routing, for example). Without `/proc` the CPU time and peak memory are `null`, and the peak memory is `null` after the first stage when the reset is refused.
