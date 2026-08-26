## Cone-of-influence coverage

After every primary proof, `rb fpv` runs a structural yosys pass that:

1. Reads the same design + constraints + properties sby just used.
2. Counts total cells per module.
3. Selects every `$assert` cell and walks its cone of influence backwards (`t:$assert %ci*` — yosys's transitive input-cone operator).
4. Reports the fraction of design cells reached by at least one assertion.

Logic *outside* every property's COI is provably unverified by the property set — a direct, actionable "what's still uncovered" signal that simulation coverage doesn't give you. When the COI pass produces data, the results table grows a **COI** column:

```text
FPV Run     Result   Description                ...   COI
counter_inv PASS     property proved (bmc, depth 32)        73% (38/52)
```

- Per-module rollup lives in `FpvResults.results["coi"]["per_module"]` so machine consumers can find under-verified blocks.
- The COI pass is enabled by default (`coi: true`) and adds a few seconds to the run; disable per verification with `coi: false` in `fpv.yaml`.
- The pass uses `yosys` on `PATH`. If yosys is missing or errors the pass is logged as a warning and the COI column shows `-` — the primary proof verdict is unaffected.

Artefacts:

- `coi.ys` — generated yosys script.
- `coi.log` — full yosys log (parsed for `stat` output).
