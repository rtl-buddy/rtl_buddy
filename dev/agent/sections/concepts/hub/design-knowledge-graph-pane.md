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

The on-disk graph is never modified by the browser join. See [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/) for graph semantics.
