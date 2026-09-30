## Hub integration

Add the aggregate result to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

Use the per-test path above so the hub can infer the test and suite. The schematic then shows performance badges and can launch the matching marimo notebook. A hub-launched notebook joins the local event broker, so bundle selections in the schematic update the notebook.

The overlay is a hub view-builder option, not an `rb hier` option. See [Hub](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/hub/#axi-perf-overlay-and-notebook-spawning).
