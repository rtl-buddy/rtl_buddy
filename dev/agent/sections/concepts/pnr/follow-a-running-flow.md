## Follow a running flow

- `tail -f artefacts/<run>/pnr.log`. Each step starts with a `>>> ` line.
- Global route prints a per-layer usage table (`GRT-0096 Final congestion report`).
- Detailed route logs each iteration (`DRT-0195`) and its violation count (`DRT-0199`), so a converging route can be told from a stalled one. `detailed-route-verbose: 0` silences this; higher levels add detail.
- With [checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints), `checkpoints/latest/progress.jsonl` records each step, hook and stage. After a kill, the last `step_begin` without a `step_end` is where the run was. Its `stage_timing` events give each corner's slack at every stage end; see [Compare timing across stages](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#compare-timing-across-stages).
