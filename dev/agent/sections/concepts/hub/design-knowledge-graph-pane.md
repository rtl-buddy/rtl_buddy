## Design knowledge graph pane

Build the graph, start the hub, and open `/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

`GET /graph.json` combines the graph, results overlay and coverage in memory without modifying the files on disk. It returns 404 with a command hint when no graph exists. Reload the page after rebuilding the graph or refreshing results. Graph semantics are in [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/).

Clicking a node selects the instance (or the shallowest instance of a module) in the schematic, opens its source when it has a file location, and sets the coverage focus. A model with `graph: false` has no design coordinate, so its send buttons stay dark and say so.

Ticking `heat` colors module nodes by a synthesis or power metric from the [`/phy` pane](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/hub/#synthpower-pane); `/gph?dir=<phys dir>` opens the overlay on one run. With no `phys-manifest.json` under the project, the control is muted and names `rb synth` and `rb power`; reload after producing one. Coverage and heat share the node fill, so enabling one releases the other.
