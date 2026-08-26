## Looking at the Graph

Serve the interactive graph pane through the hub:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

Open `http://127.0.0.1:<http_port>/gph`. The pane reads the graph and overlay on reload, groups nodes by specification, design, and verification flow, and can tint design nodes with joined coverage.

Node clicks can focus the schematic or open source in a connected editor. Drive the pane from a script with:

```bash
rb hub send graph-focus module:dma_engine
```

The hub caches the focus so it is delivered when the pane connects. See [Hub](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/hub/#design-knowledge-graph-pane) for browser behavior.
