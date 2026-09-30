## Keep stage checkpoints

A routing run killed at a scheduler wall limit leaves only its log, because the DEF and ODB are written after detailed routing. Set `checkpoints:` on the run to keep a database at each stage boundary:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    checkpoints: true            # or a stage, or a list: [cts, global_route]
```

| Stage | Written | Holds |
| --- | --- | --- |
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN |
| `place` | before `clock_tree_synthesis` | legalized global placement |
| `cts` | before `global_route` | clock tree, hold repair, legalization |
| `global_route` | after a successful `global_route` | the global route |

`true` keeps all four stages; a name or list keeps some; `[]` keeps only the progress file. Unset or `false` disables the feature. Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc` into `artefacts/<run>/checkpoints/<timestamp>-<pid>/`, and `checkpoints/latest` points at the newest run.

- `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow. After a kill, the last `step_begin` with no `step_end` is the step the run was in.
- A checkpoint is never final. `rb power` and a plain `rb pnr-export` never read it.
- Old run directories are not pruned. Delete the ones you no longer need.

Resume from a checkpoint is not supported. To look at one, open its ODB in OpenROAD or stream it out with [`rb pnr-export --checkpoint`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result).
