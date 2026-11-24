## Read a tagged P&R run

P&R runs made with [`rb pnr --run-tag`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-several-pr-calls-at-once) are read by `rb power` under the same tag:

```bash
rb pnr blk_a_pnr -c pnr/pnr.yaml --run-tag a &
rb pnr blk_b_pnr -c pnr/pnr.yaml --run-tag b &
wait
rb power blk_a_power -c power/power.yaml --run-tag a
```

A tagged call writes each power run under `artefacts/.runs/<tag>/<run>/`, with its own lock and `rtl_buddy.log` in `artefacts/.runs/<tag>/`, so tagged power calls run side by side as tagged P&R calls do.

- A `pnr` source reads the P&R run from `artefacts/.runs/<tag>/<run>/` of its `pnr.yaml`, never the untagged one. A `pnr-path` that is a symlink resolves to its target's directory, where `rb pnr` wrote the run. A tag that has not run it fails the run at `setup`, naming the `rb pnr ... --run-tag <tag>` command that makes it. A copy that cannot be written, for example on a full disk, also fails at `setup` and withdraws the run's previous outputs; runs reading other P&R runs go on.
- The routed ODB, routed SDC, SPEF, `pnr.tcl` and `blocks.consumed.json` are copied into `artefacts/.runs/<tag>/.pnr/` of the power suite while holding the P&R tree's lock, so a same-tag `rb pnr` still running is waited for (`artifact_lock.wait`). Each copy is checked against its source, and only the copy is read. A copy whose files changed afterwards fails the run, naming the directory to remove.
- With `power.yaml` and `pnr.yaml` in one directory, a tag's P&R and power calls share one tree, `artefacts/.runs/<tag>/`. A power call started while the same-tag `rb pnr` runs then fails at once (`artifact_lock.contended`) instead of waiting, as any two calls on one tree do. Start it after the P&R call ends, or keep the two files in separate directories.
- Copies are keyed by content and never overwritten, so a P&R run re-made under the tag is copied again. They use disk until you remove the tag's tree (`rm -r artefacts/.runs/<tag>`).
- Whether the SPEF is newer than `pnr.tcl`, which decides whether it is read, is judged on the P&R run's own files at each call, not on the copy's times.
- `blocks:` resolve to the abstract copies the P&R run recorded in `blocks.consumed.json`, as [`rb pnr-export --run-tag`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-and-read-tagged-results) does.
- The P&R run's `session` hooks run in its own tagged directory.
- A `synth` source reads the untagged synthesis netlist through the run's own copy, as untagged. `phys-run` publishes into the synthesis run's untagged directory, so a run that sets it fails at `setup` under a tag.
