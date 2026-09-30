## AXI-perf overlay and notebook spawning

Start with a per-test `axi-perf.json` to add AXI performance data to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

The file must exist at startup and keep its canonical location under the test's artefacts, so the schematic can identify the test and launch its marimo notebook. Schematic selections and the notebook stay synchronized. To produce the JSON and transaction Parquet files, see [AXI Interconnect Profiling](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/axi-profile/#hub-integration).
