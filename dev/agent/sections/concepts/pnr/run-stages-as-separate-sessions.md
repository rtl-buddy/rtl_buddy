## Run stages as separate sessions

`stages: true` runs each stage as its own OpenROAD session reading the previous stage's checkpoint, so you can stop early or retry a later stage. It writes every checkpoint, keeps each session's script and log there, and concatenates them into `pnr.tcl` and `pnr.log`. Results match a one-session run at the same thread count, with the exceptions in [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#a-staged-pr-run-can-differ-from-a-one-session-run).

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run. It writes that checkpoint and a stage report and passes with `routed: false`, with no routed outputs, GDS or abstract; `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from the checkpoint of the stage before `<stage>`. `<run>` is `latest`, a run id or a checkpoint directory. The old run is unchanged, so detailed routing can be retried from one global route.
- The resumed run copies the inherited checkpoints' reports (`grt_antennas.rpt`, `route.drc.rpt`, `route.maze.log`, `drt_antennas.rpt`) into `artefacts/<run>/`.

A resume is refused (`fail_stage: resume`) when an earlier stage's generated script or input files, or the OpenROAD version, changed, when the OpenROAD version does not start with a release number and so names no build, or when it resumes a probe into a closure run. [`--accept-change`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#accept-a-change-on-resume) accepts a named script or input change.

- A `constraints:` path and a map of that path to every corner count as different.
- Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change.
- Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC are not checked unless listed under [`inputs:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks). Environment variables are not checked.

### Accept a change on resume

A resumed run equals a fresh run of the current configuration unless it records accepted changes. `--accept-change <change>`, repeatable, lets a resume proceed although an earlier stage's script or input differs, such as a `session` hook edited to affect only stages from `--from` on:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from global_route \
  --accept-change pnr/alu/hooks/session.tcl
```

| Value | Accepts |
| --- | --- |
| `script:<stage>` | That stage's script. |
| `script` | Every differing stage script, such as all those `placement.rotated-macro-footprint` changes. |
| A path | That input file. Relative to where rb runs, or absolute; rb compares resolved paths. |

- The refusal lists every script and input difference, ending each acceptable one with its value. It prints paths relative to where rb runs when under it, else absolute.
- Only differences a value names are accepted. A value that names no difference, or only unacceptable ones, refuses the resume, saying which and listing what differs.
- The OpenROAD version, manifest, probe and stage-before-`--from` checks come first, and each refuses alone.
- Only an input a stage from `--from` on reads can be accepted; otherwise the refusal names the first stage that reads it.
  - The floorplan session alone reads the netlist, SDC, pin constraints, planner files, `pdn-config`, `tracks-tcl` and `tapcell-tcl`.
  - A hook is read only by its stages, so a changed `after-cts` hook cannot be accepted from `--from global_route`.
  - `session` and `stage-end` hooks, Liberty, LEF, other PDK Tcl and `inputs:` count as read by every stage.
- No value accepts another OpenROAD version, a probe resumed into a closure run, a missing or changed checkpoint file, an unfinished stage before `--from`, an unreadable `manifest.json`, a missing or unreadable input, or an input configured or dropped since (a file added to or removed from a list, or an `inputs:` entry at another path).

The run records each accepted change under `accepted_changes` in `manifest.json` and its result, and warns `pnr.resume_accepted_change`. A script record names the stage and first differing line; an input record names the role and the old and new path and digest. A hardened block's `abstract.manifest.json` records them too, and they do not make it stale, since staleness compares the files and configuration the run read.

Resuming the resumed run needs no value while the script or file is as accepted; the new run records the change with `inherited_from` naming the run that accepted it. Otherwise the refusal names it again and the earlier record is dropped.
