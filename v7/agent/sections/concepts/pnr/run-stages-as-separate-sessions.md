## Run stages as separate sessions

`stages: true` runs each stage (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`) as its own OpenROAD session that reads the previous stage's checkpoint. It writes every checkpoint, keeps each session's script and log in the checkpoint directory, and concatenates them into `pnr.tcl` and `pnr.log`. A staged run gives the same result as a one-session run at the same thread count, with the exceptions in [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v7/known-issues/#a-staged-pr-run-can-differ-from-a-one-session-run).

Stop early or resume:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run, writes that checkpoint and a stage report, and passes with `routed: false`. It writes no routed outputs, GDS or abstract, and `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from a staged run's checkpoint of the stage before. `<run>` is `latest`, a run id or a checkpoint directory. The old run is left unchanged, so detailed routing can be retried from one global route.
- The resume is refused (`fail_stage: resume`) when the earlier stages' generated scripts, their input files or the OpenROAD version changed, or when resuming a probe into a closure run. A `constraints:` path and a map of that path to every corner count as different. Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change. Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC are not checked unless listed under [`inputs:`](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#run-tcl-hooks), and environment variables are not checked.
