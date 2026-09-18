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

The pane also reads the physical model. Ticking `heat` fetches `GET /phy.json` — on the first tick, not on load — and fills module nodes with the shared heat ramp: cells and area from the synthesis half by module name, counted once for the module definition, and the power of every leaf row inside every instantiation of the module, summed by instance path. The inspector prints how many instantiations and how many rows each figure covers. Its metric switcher and its run dropdown are the `/phy` pane's, over the same `?dir=` coordinate, and `/gph?dir=<phys dir>` opens the pane on one run; a run the route refuses keeps the model on screen and reports the refusal, and a bad `?dir=` before anything has loaded falls back to the newest run. With no `phys-manifest.json` under the project the control is muted and names `rb synth` / `rb power`; the probe runs when the document is rendered, so a model produced after the tab opened needs the tab reloaded. Coverage and heat share the node fill, so ticking either releases the other. An inbound `phys_focus` turns the overlay on — it names a coordinate in the model and carries the metric to foreground, so a pane that has not read the model yet loads it and applies the focus when it lands — and highlights the node its target belongs to, a module by name and an instance path by the module whose body holds the leaf, without adding anything to what the pane emits.

The on-disk graph is never modified by the browser join. See [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/) for graph semantics.
