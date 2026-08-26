## Dead-assume detection

The same yosys pass that computes COI coverage also rolls up `$assume` cells, splitting them into those whose logic intersects with the assertion COI versus those that don't. The latter are *structurally dead* — they constrain signals no assertion ever observes, usually a sign the environment spec drifted from the property set.

When the design has any `$assume` cells, the results table grows an **Assumes** column:

```text
FPV Run     Result   Description                ...   Assumes
counter_inv PASS     property proved (bmc, depth 32)        3 used, 2 dead
```

- `N used` (all assumes are inside the assertion COI) — silent, just a sanity confirmation.
- `M used, K dead` — `K` assumes are not reachable from any assertion. Either remove them or extend the assertion set to cover the signal they constrain.

The detection is structural and conservative: it does not prove an assumption is *semantically* dead, just that yosys's elaborated graph shows no path from the assume to any assertion. In particular, assumes inside dead-code regions or untouched submodules will surface here. The detection rides on the same `coi.ys`/`coi.log` artefacts.
