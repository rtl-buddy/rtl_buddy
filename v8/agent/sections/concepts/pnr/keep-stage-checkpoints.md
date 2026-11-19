## Keep stage checkpoints

A run killed at a wall-time limit leaves only its log. `checkpoints: true`, a stage or a list saves `<NN>_<stage>.odb`, `.def` and `.sdc` at stage boundaries in `checkpoints/<run-id>/`, with `checkpoints/latest` pointing at the newest, so you can inspect or resume:

| Stage | Holds |
| --- | --- |
| `floorplan` | Floorplan, macros, power grid, pins. |
| `place` | Legalized global placement that passed `check_placement`. |
| `cts` | Clock tree and hold repair. |
| `global_route` | The route after global-route and antenna repair, with guides, segments and `grt_antennas.rpt`. With `global-route.grid-origin`, before the GCell grid is rebuilt. |
| `detailed_route` | The route after antenna repair, before fill, with its DRC report and `drt_antennas.rpt`. |
| `post_route_repair` | The kept round, with its DRC report and `drt_antennas.rpt` (only with `post-route-repair`). |

- On a multi-corner platform the `.sdc` holds every mode, and `<NN>_<stage>.<corner>.sdc` one corner's.
- Open a checkpoint in OpenROAD or with `rb pnr-export --checkpoint`. It is never a final result. Old run directories are not pruned.

A run that fails in global route keeps that database as the `global_route` checkpoint even without `checkpoints`, getting a run directory if needed.

- It is marked `failed`, with copies of the congestion reports (`04_global_route.congestion.rpt`, `04_global_route.congestion-<i>.rpt`).
- The run still fails, and `failed_global_route` names `<run-id>/global_route`. `rb pnr-export --checkpoint` opens it, no `--resume` starts from it, and `checkpoint_stages` omits it.
- A failure a hook catches is dropped when a later global route passes or the stage ends.
- A database OpenROAD did not finish writing, or rb could not file, stays in `global_route_failed/` until the next run, named by `pnr.failed_global_route_not_kept`.
