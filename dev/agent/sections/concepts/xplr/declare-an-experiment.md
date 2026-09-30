## Declare an experiment

The manifest records the delta from its parent and the complete resolved state:

```json
{
  "parent": "exp-0002",
  "hypothesis": "Reducing FIFO depth should improve delay without increasing LUT use.",
  "knobs": [
    {
      "name": "fifo_depth",
      "from": 6,
      "to": 2,
      "layer": "flow",
      "rationale": "The previous unroll change exhausted the source-level delay improvement."
    }
  ],
  "config_snapshot": {
    "fifo_depth": 2,
    "unroll_factor": 1,
    "place.directive": "default"
  },
  "provenance": {
    "tools": [{"name": "vivado", "version": "2022.1"}],
    "agent": "timing-search"
  }
}
```

- `knobs` is the change from `parent`; `config_snapshot` is the absolute state needed to reproduce or branch from the experiment.
- Knob values are arbitrary JSON scalars. The optional `layer` is `source`, `flow` or `impl`.
- Write `hypothesis` as one falsifiable sentence: what should move, in which direction, and why.
- Give every changed knob a rationale tied to an earlier experiment, report or observation.
- Set `parent` to the experiment you actually started from.
- Keys are strict. An unknown field or schema violation exits 2 and names the invalid key and the allowed alternatives.
