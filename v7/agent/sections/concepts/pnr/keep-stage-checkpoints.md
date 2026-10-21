## Keep stage checkpoints

A run killed at a wall-time limit leaves only its log. `checkpoints: true` (or one stage, or a list) writes `<NN>_<stage>.odb`, `.def` and `.sdc` at each stage boundary into `checkpoints/<run-id>/`, with `checkpoints/latest` pointing at the newest:

| Stage | Holds |
| --- | --- |
| `floorplan` | Floorplan, macros, power grid, pins |
| `place` | Legalized global placement |
| `cts` | Clock tree and hold repair |
| `global_route` | The global route after global-route repair, with route guides and segments; with `global-route.grid-origin`, before the GCell grid is rebuilt from the guides |
| `detailed_route` | The detailed route, before fill, with its DRC report |
| `post_route_repair` | The kept repair round (only with `post-route-repair`) |

On a multi-corner platform the `.sdc` holds every corner's SDC mode, and `<NN>_<stage>.<corner>.sdc` each corner's alone.

Look at a checkpoint in OpenROAD or with `rb pnr-export --checkpoint`. It is never a final result. Old run directories are not pruned.

A run that fails in global route keeps that route's database as the `global_route` checkpoint, marked `failed`, with a copy of each congestion report (`04_global_route.congestion.rpt`, `04_global_route.congestion-<i>.rpt`), whether or not the run sets `checkpoints`; a run without them gets a run directory for it. The run still fails, and its `failed_global_route` field names the checkpoint as `<run-id>/global_route`. `rb pnr-export --checkpoint` opens it; no `--resume` starts from it, and `checkpoint_stages` does not list it. A failure a hook catches is dropped when a later global route passes or the `global_route` stage ends. A database OpenROAD did not finish writing, or that rb could not file, stays in `global_route_failed/` until the next run, and the `pnr.failed_global_route_not_kept` warning names it.
