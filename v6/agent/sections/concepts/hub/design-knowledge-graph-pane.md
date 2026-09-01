## Design knowledge graph pane

Build the graph, start the browser layer, and open `/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

`GET /graph.json` reads `graph.json` and `results-overlay.json`, joins results and coverage in memory, and adds presentation categories. It returns 404 with a command hint if no graph exists. Reload the page after rebuilding or refreshing results.

Clicking graph nodes can:

- send `selection_changed` for an instance, or the shallowest instance of a module;
- send `open_source` for nodes with file locations;
- translate a selected node into coverage focus.

A model node is placed through its `maps_to` edge, so it lands on the module the model actually roots at even when `models.yaml` sets `top:`. A model that opted out with `graph: false` has no such edge and no design coordinate: its send buttons stay dark and say so, rather than focusing a module id the graph does not contain.

The on-disk graph is never modified by the browser join. See [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/) for graph semantics.
