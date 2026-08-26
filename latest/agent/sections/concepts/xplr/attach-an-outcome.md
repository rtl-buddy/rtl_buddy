## Attach an outcome

Declare terminal status, metrics, directions, units, and artefact paths:

```json
{
  "status": "success",
  "metrics": {
    "routed": true,
    "wns_ns": -0.21,
    "lut_pct": 71.4,
    "wall_clock_s": 900
  },
  "metric_meta": {
    "wns_ns": {"direction": "max", "unit": "ns"},
    "lut_pct": {"direction": "min", "unit": "%"}
  },
  "artifacts": ["post_route.dcp", "timing_summary.rpt"]
}
```

Metrics are numbers or booleans. Only numeric metrics with a `min` or `max` direction participate in Pareto dominance. Undirected measurements remain available for reporting and comparison.
