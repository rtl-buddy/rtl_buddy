## Looking at the Graph

Serve the pane through the hub and open `http://127.0.0.1:<http_port>/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

The pane rereads the graph and overlay on reload. It can tint design nodes with joined coverage, or module nodes with physical heat. Clicking a node focuses the schematic or opens the source in a connected editor. Focus a node from a script with:

```bash
rb hub send graph-focus module:dma_engine
```

See [Hub](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/hub/#design-knowledge-graph-pane) for pane behavior.
