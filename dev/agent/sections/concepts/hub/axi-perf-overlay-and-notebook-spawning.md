## AXI-perf overlay and notebook spawning

Start with a canonical per-test `axi-perf.json` to add AXI performance data to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

The file must exist at startup. Keeping the canonical layout lets the schematic identify the source test and launch its marimo notebook. The hub starts notebooks through `/api/axi-profile/notebook` and injects the local event-broker URL so schematic selections and the notebook remain synchronized.

See [AXI Interconnect Profiling](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/axi-profile/#hub-integration) for how to produce the JSON and transaction Parquet files.
