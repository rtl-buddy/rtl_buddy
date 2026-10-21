## Compare timing across stages

A run that writes `progress.jsonl` (`checkpoints:` set, even to `[]`, `stages: true`, `--stop-after`, or post-route repair) appends one `stage_timing` event to it when each stage ends, and lists the events per stage under `timing` in `manifest.json`. They compare iterations of a block stage by stage without reports of your own:

```json
{"event": "stage_timing", "stage": "global_route", "index": "04", "point": "stage_end", "parasitics": "global_route", "corners": {"tt": {"wns_setup_ps": -224.675, "wns_hold_ps": 343.534, "tns_ps": -322.268, "tns_hold_ps": 0.0}}}
```

| Field | Meaning |
| --- | --- |
| `stage`, `index` | The stage and its file index. |
| `point` | `stage_end`, `before_repair` or `round`. |
| `round` | The post-route repair round (0 is the detailed route); on a `stage_end` of `post_route_repair`, the kept round. |
| `parasitics` | What the slacks were timed on: `none` (no wire RC), `placement` (placement estimate), `global_route` (global-route estimate) or `extracted` (OpenRCX SPEF). |
| `corners` | Per corner, in config order, `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps`, in picoseconds as in the result row. A single-corner platform has one entry, named by its `corner:`. |

| Stage | Events | `parasitics` |
| --- | --- | --- |
| `floorplan` | `stage_end` | `none` |
| `place`, `cts` | `stage_end` | `placement` |
| `global_route` | `before_repair` when [global-route repair](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#tune-placement-clock-tree-and-repair) runs, then `stage_end` after it | `global_route` |
| `detailed_route` | `stage_end` | `global_route`, the estimate from the route guides |
| `post_route_repair` | one `round` per round, then a `stage_end` copied from the kept round | `extracted` |
| `finish` | `stage_end` | `extracted` with `rcx-rules`, else `global_route` |

A stage's numbers are what its own report prints on the same parasitics: the `--stop-after` stage report, and the final reports for `finish`. They carry more digits than the report. A side with no constrained path has null WNS and TNS. A corner rb could not time has nulls and an `error`; this never fails the run, and no result field or verdict reads the events.
