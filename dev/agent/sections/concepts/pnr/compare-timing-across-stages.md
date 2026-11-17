## Compare timing across stages

`stage_timing` events record each corner's slack at every stage end, so you can compare iterations stage by stage without your own reports. A run writing `progress.jsonl` (`checkpoints:` set, even to `[]`, `stages: true`, `--stop-after`, or post-route repair) appends one per stage end there and lists them under `timing` in `manifest.json`.

```json
{"event": "stage_timing", "stage": "global_route", "index": "04", "point": "stage_end", "parasitics": "global_route", "corners": {"tt": {"wns_setup_ps": -224.675, "wns_hold_ps": 343.534, "tns_ps": -322.268, "tns_hold_ps": 0.0}}}
```

| Field | Meaning |
| --- | --- |
| `stage`, `index` | The stage and its file index. |
| `point` | `stage_end`, `before_repair` or `round`. |
| `round` | The post-route round (0 is the detailed route); on the stage's `stage_end`, the kept round. |
| `parasitics` | `none` (no wire RC), `placement`, `global_route` (estimates) or `extracted` (SPEF). |
| `corners` | Per corner, in config order: `wns_setup_ps`, `wns_hold_ps`, `tns_ps`, `tns_hold_ps`, as in the result. A single-corner platform has one entry, named by `corner:`. |

| Stage | Events | `parasitics` |
| --- | --- | --- |
| `floorplan` | `stage_end` | `none` |
| `place`, `cts` | `stage_end` | `placement` |
| `global_route` | `before_repair` when [global-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing) runs, then `stage_end` | `global_route` |
| `detailed_route` | `stage_end` | `global_route`, from the route guides |
| `post_route_repair` | One `round` per round, then `stage_end` copied from the kept round | `extracted` |
| `finish` | `stage_end` | `extracted` with `rcx-rules`, else `global_route` |

- The numbers match the stage's own report on the same parasitics (the `--stop-after` report; the final reports for `finish`), with more digits.
- A side with no constrained path has null WNS and TNS.
- A corner rb could not time has nulls and an `error`. This never fails the run; no result field or verdict reads the events.
