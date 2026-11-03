## Run stages as separate sessions

`stages: true` runs each stage (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`) as its own OpenROAD session that reads the previous stage's checkpoint. It writes every checkpoint, keeps each session's script and log in the checkpoint directory, and concatenates them into `pnr.tcl` and `pnr.log`. A staged run gives the same result as a one-session run at the same thread count, with the exceptions in [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#a-staged-pr-run-can-differ-from-a-one-session-run).

Stop early or resume:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run, writes that checkpoint and a stage report, and passes with `routed: false`. It writes no routed outputs, GDS or abstract, and `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from a staged run's checkpoint of the stage before. `<run>` is `latest`, a run id or a checkpoint directory. The old run is left unchanged, so detailed routing can be retried from one global route. The reports the inherited checkpoints keep (`grt_antennas.rpt`, `route.drc.rpt`, `route.maze.log`, `drt_antennas.rpt`) are copied into `artefacts/<run>/`.
- The resume is refused (`fail_stage: resume`) when the earlier stages' generated scripts, their input files or the OpenROAD version changed, or when resuming a probe into a closure run. `--accept-change` accepts a named script or input file change; see [Accept a change on resume](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#accept-a-change-on-resume). A `constraints:` path and a map of that path to every corner count as different. Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change. Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC are not checked unless listed under [`inputs:`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#run-tcl-hooks), and environment variables are not checked.

### Accept a change on resume

A resumed run equals a fresh run of the current configuration unless it records accepted changes. `--accept-change <change>`, repeatable, lets a resume go ahead although an earlier stage's script or input file differs, for example a `session` hook edited to change only the stages from `--from` on:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from global_route \
  --accept-change pnr/alu/hooks/session.tcl
```

- The refusal lists every script and input difference and ends each one a value accepts with that value. Only differences a value names are accepted. The checks of the OpenROAD version, the manifest, a probe and the stage before `--from` come first, and each refuses alone.
- `script:<stage>` accepts that stage's script, and `script` every stage script that differs, such as all those a `pnr.yaml` setting like `placement.rotated-macro-footprint` changes.
- An input file is named by its path, relative to the directory rb runs in or absolute; rb compares resolved paths. The refusal prints the path relative to that directory when the file is under it, else absolute.
- Only an input a stage from `--from` on reads can be accepted. The floorplan inputs (netlist, SDC, pin constraints, planner files, `pdn-config`, `tracks-tcl`, `tapcell-tcl`) are read by the floorplan session alone, and a hook point only by the stages it runs in: a changed `after-cts` hook cannot be accepted from `--from global_route`. The refusal says to resume from the first stage that reads the file instead. `session`, `stage-end`, Liberty, LEF, the other PDK Tcl files and the files under `inputs:` count as read by every stage.
- No value accepts another OpenROAD version, a probe resumed into a closure run, a missing or changed checkpoint file, a stage before `--from` that did not finish, an unreadable `manifest.json`, a missing or unreadable input file, or an input configured or dropped since: a file added to or removed from a list, or an `inputs:` entry at another path.
- A value that names no difference, or only one no value accepts, refuses the resume and says which, listing what does differ.
- The run records each accepted change under `accepted_changes` in its `manifest.json` and its result, and warns `pnr.resume_accepted_change` for each. A script record names the stage and the first line that differs; an input record names the role and the file's old and new path and digest. A hardened block's `abstract.manifest.json` records them too. They do not make the abstract stale: its staleness check compares the files and configuration the run read.
- Resuming the resumed run needs no value again while the script or file is as accepted; the new run records the change with `inherited_from` naming the run that accepted it. Otherwise the refusal names it again, and the earlier record is dropped.
